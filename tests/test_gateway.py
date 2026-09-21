from __future__ import annotations

import asyncio
import json
import time

import pytest

import httpx

from llm_router.adapters import AdapterRegistry
from llm_router.bootstrap import BootstrapResult
from llm_router.discovery import DiscoveryReport, DiscoverySettings
from llm_router.errors import ConfigError, UpstreamError
from llm_router.gateway import RouterGateway, create_app
from llm_router.provisioning import ProvisioningReport, ProvisioningSettings
from llm_router.router import LLMRouter
from llm_router.schema import UpstreamResult

from conftest import make_config


class ToolAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests = []

    async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        if self.fail:
            raise UpstreamError("simulated outage", status_code=503)
        return UpstreamResult(
            text="",
            usage={"input_tokens": 10, "output_tokens": 4},
            finish_reason="tool_calls",
            tool_calls=(
                {
                    "id": "call_light",
                    "type": "function",
                    "function": {
                        "name": "HassTurnOn",
                        "arguments": {"name": "Kitchen"},
                    },
                },
            ),
        )


class StaticGateway:
    def __init__(self, router: LLMRouter | None) -> None:
        self._router = router

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def router(self) -> LLMRouter:
        if self._router is None:
            from llm_router.gateway import GatewayUnavailable

            raise GatewayUnavailable("No models are currently available")
        return self._router

    async def refresh(self) -> bool:
        return self._router is not None

    def status(self):  # type: ignore[no-untyped-def]
        return {
            "status": "ready" if self._router else "unavailable",
            "version": "0.7.0",
        }


class ProvisioningGateway(StaticGateway):
    def __init__(self, router: LLMRouter) -> None:
        super().__init__(router)
        self.provision_calls = []

    async def provision(self, **kwargs):  # type: ignore[no-untyped-def]
        self.provision_calls.append(kwargs)
        return ProvisioningReport(
            "planned" if kwargs.get("dry_run") else "installed",
            "http://127.0.0.1:11434",
            "resource checks passed",
            selected_model=kwargs.get("requested_model") or "qwen3.5:4b",
        )


def make_gateway_router(*, fail: bool = False) -> tuple[LLMRouter, ToolAdapter]:
    config = make_config(
        models=[
            {
                "id": "tool-model",
                "endpoint": "source-a",
                "upstream_model": "tool-model",
                "quality": 0.95,
                "capabilities": {
                    "general": 1.0,
                    "tool_use": 1.0,
                    "structured_output": 1.0,
                },
            }
        ],
        router={"max_attempts": 1},
    )
    adapter = ToolAdapter(fail=fail)
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", adapter)
    return LLMRouter(config, adapters=adapters), adapter


async def request(app, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_home_assistant_ollama_surface_lists_only_virtual_models() -> None:
    router, _ = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))

    response = asyncio.run(request(app, "GET", "/api/tags"))

    assert response.status_code == 200
    names = [model["model"] for model in response.json()["models"]]
    assert names[0] == "auto"
    assert "auto:quality" in names
    assert "tool-model" not in names


def test_ollama_chat_preserves_home_assistant_tools_non_streaming() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    body = {
        "model": "auto",
        "stream": False,
        "messages": [{"role": "user", "content": "Turn on the kitchen"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "HassTurnOn",
                    "description": "Turn on an entity",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }

    response = asyncio.run(request(app, "POST", "/api/chat", json=body))

    assert response.status_code == 200
    payload = response.json()
    function = payload["message"]["tool_calls"][0]["function"]
    assert function == {"name": "HassTurnOn", "arguments": {"name": "Kitchen"}}
    assert payload["router"]["deployment"] == "tool-model"
    assert adapter.requests[0].required_capabilities == ("tool_use",)


def test_home_assistant_float_context_window_is_accepted_as_integer() -> None:
    # Home Assistant's number selector stores whole numbers as floats, so its
    # Ollama integration sends {"options": {"num_ctx": 8192.0}}.
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    body = {
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"num_ctx": 8192.0, "num_predict": 256.0},
    }

    response = asyncio.run(request(app, "POST", "/api/chat", json=body))

    assert response.status_code == 200, response.text
    query = adapter.requests[0]
    assert query.min_context_window == 8192 and type(query.min_context_window) is int
    assert query.max_tokens == 256 and type(query.max_tokens) is int

    for spelled in ("8192", "8192.0", " 8192 "):
        as_text = asyncio.run(request(app, "POST", "/api/chat", json={**body, "options": {"num_ctx": spelled}}))
        assert as_text.status_code == 200, as_text.text
        assert adapter.requests[-1].min_context_window == 8192 and type(adapter.requests[-1].min_context_window) is int


def test_unusable_client_tuning_values_are_ignored_not_fatal() -> None:
    # The router corrects what it can and drops what it cannot; a conversation
    # never fails over a tuning knob the client cannot fix.
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    base = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    for options in ({"num_ctx": 8192.5}, {"num_ctx": True}, {"num_ctx": "eight thousand"}, {"num_ctx": {"n": 1}}, {"num_ctx": 0}, {"num_ctx": -5}):
        response = asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": options}))
        assert response.status_code == 200, (options, response.text)
        assert adapter.requests[-1].min_context_window is None, options
    for options in ({"num_predict": "lots"}, {"num_predict": 0}, {"num_predict": 2.5}):
        response = asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": options}))
        assert response.status_code == 200, (options, response.text)
        assert adapter.requests[-1].max_tokens == 2048, options
    response = asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": {"num_predict": "64", "temperature": "0.5"}}))
    assert response.status_code == 200
    assert adapter.requests[-1].max_tokens == 64 and adapter.requests[-1].temperature == 0.5
    for temperature in ("warm", 5, -1, True, [0.5]):
        response = asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": {"temperature": temperature}}))
        assert response.status_code == 200, (temperature, response.text)
        assert adapter.requests[-1].temperature is None, temperature
    openai = asyncio.run(request(app, "POST", "/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}], "max_tokens": "many", "temperature": "hot",
    }))
    assert openai.status_code == 200, openai.text
    assert adapter.requests[-1].max_tokens == 2048 and adapter.requests[-1].temperature is None


def test_openai_float_max_tokens_is_accepted_as_integer() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))

    response = asyncio.run(request(app, "POST", "/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64.0,
    }))

    assert response.status_code == 200, response.text
    assert adapter.requests[0].max_tokens == 64 and type(adapter.requests[0].max_tokens) is int


def test_ollama_chat_returns_valid_ndjson_stream() -> None:
    router, _ = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))

    response = asyncio.run(
        request(
            app,
            "POST",
            "/api/chat",
            json={
                "model": "auto:balanced",
                "stream": True,
                "messages": [{"role": "user", "content": "Turn on a light"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "HassTurnOn",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )
    )

    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert chunks[0]["done"] is False
    assert chunks[0]["message"]["tool_calls"]
    assert chunks[-1]["done"] is True


def test_openai_compatible_surface_returns_tool_calls() -> None:
    router, _ = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))

    response = asyncio.run(
        request(
            app,
            "POST",
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Turn on the kitchen"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "HassTurnOn",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )
    )

    assert response.status_code == 200
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "HassTurnOn"
    assert json.loads(call["function"]["arguments"]) == {"name": "Kitchen"}


def test_virtual_local_model_adds_local_preference_without_weakening_constraints() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))

    response = asyncio.run(
        request(
            app,
            "POST",
            "/api/chat",
            json={
                "model": "auto:local",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    )

    assert response.status_code == 200
    assert adapter.requests[0].preferred_tags == ("local",)


def test_provisioning_endpoint_supports_safe_dry_run() -> None:
    router, _ = make_gateway_router()
    gateway = ProvisioningGateway(router)
    app = create_app(gateway=gateway)

    response = asyncio.run(
        request(
            app,
            "POST",
            "/router/provision",
            json={"dry_run": True, "model": "qwen3.5:2b"},
        )
    )

    assert response.status_code == 200
    assert response.json()["status"] == "planned"
    assert gateway.provision_calls == [
        {
            "dry_run": True,
            "requested_model": "qwen3.5:2b",
            "allow_remote": None,
        }
    ]


def test_gateway_auth_and_failure_responses_are_protocol_compatible(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "gateway-secret")
    router, _ = make_gateway_router(fail=True)
    app = create_app(gateway=StaticGateway(router))

    unauthorized = asyncio.run(request(app, "GET", "/api/tags"))
    failed = asyncio.run(
        request(
            app,
            "POST",
            "/api/chat",
            headers={"Authorization": "Bearer gateway-secret"},
            json={
                "model": "auto",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    )

    assert unauthorized.status_code == 401
    assert "error" in unauthorized.json()
    assert failed.status_code == 503
    assert failed.json()["error"].startswith("All selected LLM deployments failed")


def test_failed_refresh_retains_last_working_router(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    router, _ = make_gateway_router()
    report = DiscoveryReport(router.config, ())
    calls = 0

    async def fake_bootstrap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return BootstrapResult(router=router, discovery=report, configured=router.config)
        raise ConfigError("temporary discovery failure")

    monkeypatch.setattr("llm_router.gateway.bootstrap_router", fake_bootstrap)
    gateway = RouterGateway(discovery=False)

    assert asyncio.run(gateway.refresh()) is True
    assert asyncio.run(gateway.refresh()) is False
    assert asyncio.run(gateway.router()) is router
    assert gateway.status()["status"] == "degraded"


def test_gateway_provisions_in_background_then_refreshes_router(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    router, _ = make_gateway_router()
    report = DiscoveryReport(router.config, ())
    bootstrap_calls = 0

    async def fake_bootstrap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        if bootstrap_calls == 1:
            raise ConfigError("no models yet")
        return BootstrapResult(router=router, discovery=report, configured=None)

    class FakeProvisioner:
        async def provision(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProvisioningReport(
                "installed",
                "http://127.0.0.1:11434",
                "model download completed",
                selected_model="qwen3.5:4b",
            )

    monkeypatch.setattr("llm_router.gateway.bootstrap_router", fake_bootstrap)
    gateway = RouterGateway(
        settings=DiscoverySettings(refresh_seconds=3600),
        provisioning_settings=ProvisioningSettings(enabled=True),
        provisioner=FakeProvisioner(),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await gateway.start()
        assert gateway._provision_task is not None
        await gateway._provision_task
        assert await gateway.router() is router
        await gateway.stop()

    asyncio.run(scenario())

    assert bootstrap_calls == 2
    assert gateway.status()["provisioning"]["status"] == "installed"


def test_unavailable_gateway_returns_503_instead_of_crashing() -> None:
    app = create_app(gateway=StaticGateway(None))

    response = asyncio.run(request(app, "GET", "/api/tags"))

    assert response.status_code == 503
    assert response.json() == {"error": "No models are currently available"}


def test_client_output_limit_is_only_forwarded_when_set() -> None:
    # Home Assistant sends no num_predict; Ollama's own default is unlimited and
    # a 2048-token cap can leave a thinking model with nothing to answer with.
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    base = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    assert asyncio.run(request(app, "POST", "/api/chat", json=base)).status_code == 200
    assert adapter.requests[-1].max_tokens == 2048 and adapter.requests[-1].max_tokens_specified is False
    assert asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": {"num_predict": 64}})).status_code == 200
    assert adapter.requests[-1].max_tokens == 64 and adapter.requests[-1].max_tokens_specified is True
    assert asyncio.run(request(app, "POST", "/api/chat", json={**base, "options": {"num_predict": "lots"}})).status_code == 200
    assert adapter.requests[-1].max_tokens_specified is False, "An unusable limit is treated as no limit"
    openai = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    assert asyncio.run(request(app, "POST", "/v1/chat/completions", json=openai)).status_code == 200
    assert adapter.requests[-1].max_tokens_specified is False
    assert asyncio.run(request(app, "POST", "/v1/chat/completions", json={**openai, "max_completion_tokens": 32})).status_code == 200
    assert adapter.requests[-1].max_tokens == 32 and adapter.requests[-1].max_tokens_specified is True


def test_keep_alive_and_think_pass_through_only_when_usable() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    base = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    for keep_alive, expected in ((-1, -1), ("300s", "300s"), (600.0, 600), ("5m", "5m"), ("bogus", None), (True, None), (-2, None), ("", None)):
        response = asyncio.run(request(app, "POST", "/api/chat", json={**base, "keep_alive": keep_alive}))
        assert response.status_code == 200, (keep_alive, response.text)
        assert adapter.requests[-1].keep_alive == expected, keep_alive
    assert asyncio.run(request(app, "POST", "/api/chat", json=base)).status_code == 200
    assert adapter.requests[-1].keep_alive is None and adapter.requests[-1].think is None
    assert asyncio.run(request(app, "POST", "/api/chat", json={**base, "think": False})).status_code == 200
    assert adapter.requests[-1].think is False and "reasoning" not in adapter.requests[-1].required_capabilities
    assert asyncio.run(request(app, "POST", "/api/chat", json={**base, "think": "yes"})).status_code == 200
    assert adapter.requests[-1].think is None
    openai = asyncio.run(request(app, "POST", "/v1/chat/completions", json={"model": "auto", "messages": base["messages"], "keep_alive": -1}))
    assert openai.status_code == 200 and adapter.requests[-1].keep_alive is None, "keep_alive is an Ollama concept"


class StreamingAdapter:
    """Answers in fragments; optionally slow to start or failing part-way."""

    def __init__(self, *, pieces=("Hel", "lo"), first_token_delay=0.0, piece_delay=0.0, fail_after=None, tool_calls=(), thinking="") -> None:  # type: ignore[no-untyped-def]
        self.pieces = pieces
        self.first_token_delay = first_token_delay
        self.piece_delay = piece_delay
        self.fail_after = fail_after
        self.tool_calls = tool_calls
        self.thinking = thinking
        self.closed = 0

    async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        from llm_router.adapters.base import final_result
        return await final_result(self.stream(endpoint, model, request))

    async def stream(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        from llm_router.adapters.base import StreamDelta

        try:
            await asyncio.sleep(self.first_token_delay)
            if self.fail_after == 0:
                raise UpstreamError("backend went away", kind="connection")
            if self.thinking:
                yield StreamDelta(thinking=self.thinking, first_token_ms=5.0)
            for index, piece in enumerate(self.pieces):
                yield StreamDelta(text=piece, first_token_ms=5.0 if index == 0 and not self.thinking else None)
                if self.fail_after == index + 1:
                    raise UpstreamError("backend went away", kind="connection")
                await asyncio.sleep(self.piece_delay)
            yield UpstreamResult(text="".join(self.pieces), usage={"input_tokens": 3, "output_tokens": 2}, finish_reason="stop", tool_calls=tuple(self.tool_calls))
        finally:
            self.closed += 1


class RecordingGateway(StaticGateway):
    def __init__(self, router):  # type: ignore[no-untyped-def]
        super().__init__(router)
        self.failures = []

    def record_request_failure(self, **fields):  # type: ignore[no-untyped-def]
        self.failures.append(fields)


def make_streaming_app(adapter, *, max_attempts: int = 1):  # type: ignore[no-untyped-def]
    config = make_config(
        models=[{"id": "stream-model", "endpoint": "source-a", "upstream_model": "stream-model", "quality": 0.9, "capabilities": {"general": 1.0, "tool_use": 1.0}}],
        router={"max_attempts": max_attempts},
    )
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", adapter)
    gateway = RecordingGateway(LLMRouter(config, adapters=adapters))
    return create_app(gateway=gateway), gateway


def _sse_events(text: str) -> tuple[list[str], list]:
    comments = [line for line in text.splitlines() if line.startswith(":")]
    events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return comments, [event if event == "[DONE]" else json.loads(event) for event in events]


def test_ollama_chat_relays_fragments_with_heartbeats_then_the_final_chunk(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.02)
    adapter = StreamingAdapter(pieces=("Hel", "lo"), first_token_delay=0.08, thinking="hmm", tool_calls=(
        {"id": "call_1", "type": "function", "function": {"name": "HassTurnOn", "arguments": {"name": "Kitchen"}}},
    ))
    app, _ = make_streaming_app(adapter)
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/x-ndjson")
    chunks = [json.loads(line) for line in response.text.splitlines()]
    contents = [chunk["message"].get("content") for chunk in chunks]
    heartbeats = [chunk for chunk in chunks if chunk["message"].get("content") == "" and not chunk["done"] and "thinking" not in chunk["message"] and "tool_calls" not in chunk["message"]]
    assert heartbeats, "A silent backend still produces empty keep-alive chunks"
    assert all(chunk["model"] == "auto" and "created_at" in chunk for chunk in chunks)
    thinking = next(chunk for chunk in chunks if "thinking" in chunk["message"])
    assert thinking["message"]["thinking"] == "hmm" and thinking["message"]["content"] == ""
    assert [content for content in contents if content] == ["Hel", "lo"], "Text is relayed as it is generated, never repeated at the end"
    tool_chunk = next(chunk for chunk in chunks if chunk["message"].get("tool_calls"))
    assert tool_chunk["message"]["tool_calls"][0]["function"] == {"name": "HassTurnOn", "arguments": {"name": "Kitchen"}}
    final = chunks[-1]
    assert final["done"] is True and final["done_reason"] == "stop"
    assert final["prompt_eval_count"] == 3 and final["eval_count"] == 2
    assert final["router"]["deployment"] == "stream-model"
    assert adapter.closed == 1


def test_openai_chat_relays_server_sent_events_with_usage_and_done(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.02)
    adapter = StreamingAdapter(pieces=("Hi", " there"), first_token_delay=0.08)
    app, _ = make_streaming_app(adapter)
    body = {"model": "auto", "stream": True, "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": "hi"}]}
    response = asyncio.run(request(app, "POST", "/v1/chat/completions", json=body))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    comments, events = _sse_events(response.text)
    assert comments and all(comment.startswith(": keepalive") for comment in comments), "SSE comments keep the connection alive"
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    assert all(chunk["object"] == "chat.completion.chunk" and chunk["model"] == "auto" for chunk in chunks)
    assert len({chunk["id"] for chunk in chunks}) == 1
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]]
    assert deltas[0] == {"role": "assistant", "content": "Hi"}
    assert deltas[1] == {"content": " there"}
    finish = [chunk for chunk in chunks if chunk["choices"] and chunk["choices"][0]["finish_reason"]]
    assert finish[-1]["choices"][0]["finish_reason"] == "stop" and finish[-1]["router"]["deployment"] == "stream-model"
    usage = [chunk for chunk in chunks if not chunk["choices"]]
    assert usage and usage[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    without_usage = asyncio.run(request(app, "POST", "/v1/chat/completions", json={**body, "stream_options": {}}))
    _, events = _sse_events(without_usage.text)
    assert all(chunk == "[DONE]" or chunk["choices"] for chunk in events), "Usage chunks only when the client asked"


def test_streaming_failures_after_the_first_token_are_reported_in_band() -> None:
    app, gateway = make_streaming_app(StreamingAdapter(pieces=("Hel", "lo"), fail_after=1))
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200, "The status was already sent with the first fragment"
    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert chunks[0]["message"]["content"] == "Hel"
    assert "error" in chunks[-1] and "backend went away" in chunks[-1]["error"] and "3 characters" in chunks[-1]["error"]
    assert gateway.failures[-1]["kind"] == "stream_interrupted" and gateway.failures[-1]["api"] == "ollama"

    app, gateway = make_streaming_app(StreamingAdapter(pieces=("Hel", "lo"), fail_after=1))
    response = asyncio.run(request(app, "POST", "/v1/chat/completions", json={"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}))
    _, events = _sse_events(response.text)
    assert events[0]["choices"][0]["delta"]["content"] == "Hel"
    assert events[-1]["error"]["type"] == "stream_interrupted" and "[DONE]" not in events
    assert gateway.failures[-1]["kind"] == "stream_interrupted" and gateway.failures[-1]["api"] == "openai"


def test_quick_failures_on_streaming_requests_keep_real_http_status_codes(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    app, gateway = make_streaming_app(StreamingAdapter(fail_after=0))
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 503 and "All selected LLM deployments failed" in response.json()["error"]
    assert gateway.failures[-1]["kind"] == "all_attempts_failed" and gateway.failures[-1]["status"] == 503
    response = asyncio.run(request(app, "POST", "/v1/chat/completions", json={"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 503 and response.json()["error"]["type"] == "router_unavailable"
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 400

    # A backend that stays silent past the first heartbeat and then fails can
    # only report the failure in the stream that has already begun.
    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.02)
    app, gateway = make_streaming_app(StreamingAdapter(first_token_delay=0.08, fail_after=0))
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200
    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert chunks[0]["message"]["content"] == "" and chunks[0]["done"] is False
    assert "All selected LLM deployments failed" in chunks[-1]["error"]
    assert gateway.failures[-1]["kind"] == "all_attempts_failed" and gateway.failures[-1]["status"] == 200


def test_non_streaming_clients_still_get_one_json_answer_after_a_mid_answer_failover() -> None:
    config = make_config(
        models=[
            {"id": "flaky", "endpoint": "source-a", "upstream_model": "m", "quality": 0.95, "capabilities": {"general": 1.0}},
            {"id": "steady", "endpoint": "source-b", "upstream_model": "m", "quality": 0.9, "capabilities": {"general": 1.0}},
        ],
        router={"max_attempts": 2},
    )

    class PerEndpoint(StreamingAdapter):
        async def stream(self, endpoint, model, request):  # type: ignore[no-untyped-def]
            self.fail_after = 1 if endpoint.name == "source-a" else None
            self.pieces = ("Hel", "lo") if endpoint.name == "source-a" else ("Bon", "jour")
            async for item in super().stream(endpoint, model, request):
                yield item

    adapters = AdapterRegistry()
    adapters.register("ollama-chat", PerEndpoint())
    app = create_app(gateway=StaticGateway(LLMRouter(config, adapters=adapters)))
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["content"] == "Bonjour" and payload["router"]["deployment"] == "steady"


async def _drive_until_hang_up(app, path: str, body: dict, *, spec_version: str, disconnect_after: float):  # type: ignore[no-untyped-def]
    """Run the ASGI app by hand so the client can hang up part-way through."""
    sent: list = []
    state = {"body_sent": False}

    async def receive():  # type: ignore[no-untyped-def]
        if not state["body_sent"]:
            state["body_sent"] = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await asyncio.sleep(disconnect_after)
        return {"type": "http.disconnect"}

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"router.test"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 4321), "server": ("router.test", 80),
    }
    started = time.perf_counter()
    await asyncio.wait_for(app(scope, receive, send), 3.0)
    return sent, time.perf_counter() - started


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_a_client_that_hangs_up_before_the_first_token_stops_the_backend(monkeypatch, spec_version, path) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.5)
    adapter = StreamingAdapter(first_token_delay=5.0)
    app, gateway = make_streaming_app(adapter)
    router = asyncio.run(gateway.router())
    body = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    sent, elapsed = asyncio.run(_drive_until_hang_up(app, path, body, spec_version=spec_version, disconnect_after=0.05))
    assert elapsed < 1.0, "The router notices the hang-up itself instead of waiting for the backend or the next heartbeat"
    assert adapter.closed == 1, "The backend stream is closed, which stops generation"
    assert router.runtime.state("stream-model").active_requests == 0
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] in ([], [499])
    assert router.metrics.snapshot()["traffic"]["totals"]["requests_ok"] == 0
    assert gateway.failures == [], "A client hanging up is not a router failure"


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_a_client_that_hangs_up_mid_answer_stops_the_backend(monkeypatch, spec_version) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.5)
    adapter = StreamingAdapter(pieces=("Hel", "lo", "!"), piece_delay=5.0)
    app, gateway = make_streaming_app(adapter)
    router = asyncio.run(gateway.router())
    body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    sent, elapsed = asyncio.run(_drive_until_hang_up(app, "/api/chat", body, spec_version=spec_version, disconnect_after=0.05))
    assert elapsed < 1.0
    bodies = [message for message in sent if message["type"] == "http.response.body" and message.get("body")]
    assert json.loads(bodies[0]["body"])["message"]["content"] == "Hel", "The first fragment was relayed before the hang-up"
    assert adapter.closed == 1 and router.runtime.state("stream-model").active_requests == 0


def test_session_keys_come_from_the_header_or_the_conversation_fingerprint() -> None:
    from starlette.datastructures import Headers
    from types import SimpleNamespace

    from llm_router.gateway import _session_key

    def request(**headers):  # type: ignore[no-untyped-def]
        return SimpleNamespace(headers=Headers(headers))

    system = {"role": "system", "content": "You are a house assistant. Entities: light.kitchen"}
    turn_one = [system, {"role": "user", "content": "turn on the kitchen light"}]
    turn_two = [*turn_one, {"role": "assistant", "content": "Done."}, {"role": "user", "content": "and the hall"}]
    key = _session_key(request(), "qwen-ha", turn_one)
    assert key is not None and key.startswith("conversation:") and len(key) == len("conversation:") + 32
    assert _session_key(request(), "qwen-ha", turn_two) == key, "Later turns of the same conversation share the key"
    assert _session_key(request(), "qwen-ha", [system, {"role": "user", "content": "what time is it"}]) != key
    assert _session_key(request(), "other-model", turn_one) != key
    parts = [system, {"role": "user", "content": [{"type": "text", "text": "turn on the kitchen light"}, {"type": "image_url", "image_url": {"url": "data:..."}}]}]
    assert _session_key(request(), "qwen-ha", parts) == key, "Text parts count; attachments do not"
    assert _session_key(request(**{"X-LLM-Router-Session": " agent-42 "}), "qwen-ha", turn_one) == "named:agent-42"
    assert _session_key(request(), "qwen-ha", [{"role": "assistant", "content": "hello"}]) is None
    assert "turn on" not in key, "Only a digest is kept, never prompt text"


def test_in_flight_answers_are_counted_until_they_finish(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    class CountingGateway(RecordingGateway):
        def __init__(self, router):  # type: ignore[no-untyped-def]
            super().__init__(router)
            self.in_flight = 0
            self.peak = 0

        def begin_request(self) -> None:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)

        def end_request(self) -> None:
            self.in_flight -= 1

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.5)
    config = make_config(models=[{"id": "stream-model", "endpoint": "source-a", "upstream_model": "m", "quality": 0.9, "capabilities": {"general": 1.0}}], router={"max_attempts": 1})
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", StreamingAdapter(pieces=("Hel", "lo")))
    gateway = CountingGateway(LLMRouter(config, adapters=adapters))
    app = create_app(gateway=gateway)
    for body in ({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}]}):
        response = asyncio.run(request(app, "POST", "/api/chat", json=body))
        assert response.status_code == 200
    assert gateway.peak == 1 and gateway.in_flight == 0, "Streamed and buffered answers both count while in progress and are released after"

    adapters.register("ollama-chat", StreamingAdapter(pieces=("Hel", "lo", "!"), piece_delay=5.0))
    asyncio.run(_drive_until_hang_up(app, "/api/chat", {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, spec_version="2.3", disconnect_after=0.05))
    assert gateway.in_flight == 0, "A hang-up releases the count too"
    response = asyncio.run(request(app, "POST", "/api/chat", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 503 or response.status_code == 200  # a failing/slow adapter either way releases
    assert gateway.in_flight == 0


def _anthropic_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
    return events


def test_anthropic_messages_translate_tool_rounds_and_answers_in_anthropic_shape() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    body = {
        "model": "auto", "max_tokens": 512, "temperature": 0.3,
        "system": [{"type": "text", "text": "You control a house."}],
        "metadata": {"user_id": "user_abc_session_123"},
        "tools": [{"name": "HassTurnOn", "description": "Turn on", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}],
        "messages": [
            {"role": "user", "content": "Turn on the kitchen light"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Sure."}, {"type": "tool_use", "id": "toolu_1", "name": "HassTurnOn", "input": {"name": "Kitchen"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "done"}]}, {"type": "text", "text": "thanks, and the hall"}]},
        ],
    }
    response = asyncio.run(request(app, "POST", "/v1/messages", json=body))
    assert response.status_code == 200, response.text
    query = adapter.requests[-1]
    roles = [message["role"] for message in query.messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert query.messages[0]["content"] == "You control a house."
    assert query.messages[2]["tool_calls"][0]["function"] == {"name": "HassTurnOn", "arguments": {"name": "Kitchen"}}
    assert query.messages[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "done"}
    assert query.tools[0]["function"]["parameters"]["properties"]["name"]["type"] == "string"
    assert query.max_tokens == 512 and query.max_tokens_specified and query.temperature == 0.3
    assert "tool_use" in query.required_capabilities
    assert query.session_key is not None and query.session_key.startswith("anthropic:") and "session_123" not in query.session_key
    payload = response.json()
    assert payload["type"] == "message" and payload["role"] == "assistant" and payload["model"] == "auto"
    assert payload["stop_reason"] == "tool_use"
    assert payload["content"][0] == {"type": "tool_use", "id": "call_light", "name": "HassTurnOn", "input": {"name": "Kitchen"}}
    assert payload["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert payload["router"]["deployment"] == "tool-model"

    missing_limit = asyncio.run(request(app, "POST", "/v1/messages", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    assert missing_limit.status_code == 400 and missing_limit.json()["error"]["type"] == "invalid_request_error"
    unknown = asyncio.run(request(app, "POST", "/v1/messages", json={"model": "claude-nope", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}))
    assert unknown.status_code == 404 and unknown.json()["error"]["type"] == "not_found_error"
    estimate = asyncio.run(request(app, "POST", "/v1/messages/count_tokens", json={"model": "auto", "messages": [{"role": "user", "content": "x" * 400}]}))
    assert estimate.status_code == 200 and 80 <= estimate.json()["input_tokens"] <= 130


def test_anthropic_streaming_uses_message_events_pings_and_tool_use_blocks(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.02)
    adapter = StreamingAdapter(pieces=("Hel", "lo"), first_token_delay=0.08, thinking="hmm", tool_calls=(
        {"id": "toolu_9", "type": "function", "function": {"name": "HassTurnOn", "arguments": {"name": "Kitchen"}}},
    ))
    app, gateway = make_streaming_app(adapter)
    body = {"model": "auto", "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    response = asyncio.run(request(app, "POST", "/v1/messages", json=body))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = _anthropic_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "message_start" and events[0][1]["message"]["role"] == "assistant"
    assert "ping" in names, "Silence before the first token is covered by ping events"
    assert names[-2:] == ["message_delta", "message_stop"]
    blocks = [(payload["index"], payload["content_block"]["type"]) for name, payload in events if name == "content_block_start"]
    assert blocks == [(0, "thinking"), (1, "text"), (2, "tool_use")]
    text = "".join(payload["delta"]["text"] for name, payload in events if name == "content_block_delta" and payload["delta"]["type"] == "text_delta")
    assert text == "Hello"
    thinking = [payload["delta"]["thinking"] for name, payload in events if name == "content_block_delta" and payload["delta"]["type"] == "thinking_delta"]
    assert thinking == ["hmm"]
    tool_json = "".join(payload["delta"]["partial_json"] for name, payload in events if name == "content_block_delta" and payload["delta"]["type"] == "input_json_delta")
    assert json.loads(tool_json) == {"name": "Kitchen"}
    stops = [payload["index"] for name, payload in events if name == "content_block_stop"]
    assert stops == [0, 1, 2], "Every block is closed"
    assert events[-2][1]["delta"]["stop_reason"] == "tool_use" and events[-2][1]["usage"]["output_tokens"] == 2

    app, gateway = make_streaming_app(StreamingAdapter(pieces=("Hel", "lo"), fail_after=1))
    response = asyncio.run(request(app, "POST", "/v1/messages", json=body))
    events = _anthropic_events(response.text)
    assert events[-1][0] == "error" and events[-1][1]["error"]["type"] == "api_error" and "backend went away" in events[-1][1]["error"]["message"]
    assert gateway.failures[-1]["api"] == "anthropic" and gateway.failures[-1]["kind"] == "stream_interrupted"


def test_anthropic_clients_authenticate_with_x_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key")
    router, _ = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    body = {"model": "auto", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}
    denied = asyncio.run(request(app, "POST", "/v1/messages", json=body, headers={"x-api-key": "wrong"}))
    assert denied.status_code == 401 and denied.json() == {"type": "error", "error": {"type": "authentication_error", "message": "Invalid or missing gateway API key"}}
    assert asyncio.run(request(app, "POST", "/v1/messages", json=body, headers={"x-api-key": "router-key"})).status_code == 200
    assert asyncio.run(request(app, "POST", "/v1/messages", json=body, headers={"authorization": "Bearer router-key"})).status_code == 200
    assert asyncio.run(request(app, "POST", "/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, headers={"x-api-key": "router-key"})).status_code == 401, "Only the Anthropic surface reads x-api-key"


def test_openai_responses_translate_items_and_flat_tools_and_answer_in_responses_shape() -> None:
    router, adapter = make_gateway_router()
    app = create_app(gateway=StaticGateway(router))
    body = {
        "model": "auto", "instructions": "You are Codex.", "max_output_tokens": 300, "store": False,
        "tools": [{"type": "function", "name": "shell", "description": "Run", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}, {"type": "web_search_preview"}],
        "text": {"format": {"type": "json_schema", "name": "plan", "schema": {"type": "object"}, "strict": True}},
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{\"cmd\": \"ls\"}"},
            {"type": "function_call", "call_id": "call_2", "name": "shell", "arguments": "{\"cmd\": \"pwd\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
            {"type": "function_call_output", "call_id": "call_2", "output": "/home"},
            {"role": "user", "content": "now turn on the light"},
        ],
    }
    response = asyncio.run(request(app, "POST", "/v1/responses", json=body))
    assert response.status_code == 200, response.text
    query = adapter.requests[-1]
    assert [message["role"] for message in query.messages] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert query.messages[0]["content"] == "You are Codex."
    assert [call["function"]["arguments"] for call in query.messages[2]["tool_calls"]] == [{"cmd": "ls"}, {"cmd": "pwd"}], "Parallel calls share one assistant turn"
    assert query.messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}
    assert [tool["function"]["name"] for tool in query.tools] == ["shell"], "Only function tools reach a backend"
    assert query.response_format == {"type": "json_schema", "json_schema": {"name": "plan", "schema": {"type": "object"}, "strict": True}}
    assert query.max_tokens == 300 and query.max_tokens_specified
    payload = response.json()
    assert payload["object"] == "response" and payload["status"] == "completed" and payload["model"] == "auto"
    call = payload["output"][-1]
    assert call["type"] == "function_call" and call["call_id"] == "call_light" and call["name"] == "HassTurnOn"
    assert json.loads(call["arguments"]) == {"name": "Kitchen"}
    assert payload["usage"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}

    chained = asyncio.run(request(app, "POST", "/v1/responses", json={"model": "auto", "input": "hi", "previous_response_id": "resp_old"}))
    assert chained.status_code == 400 and "previous_response_id" in chained.json()["error"]["message"]


def test_openai_responses_streaming_emits_the_event_sequence_codex_expects(monkeypatch) -> None:
    import llm_router.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAM_HEARTBEAT_SECONDS", 0.02)
    adapter = StreamingAdapter(pieces=("Hi", " there"), first_token_delay=0.08, thinking="hmm", tool_calls=(
        {"id": "call_7", "type": "function", "function": {"name": "shell", "arguments": {"cmd": "ls"}}},
    ))
    app, gateway = make_streaming_app(adapter)
    body = {"model": "auto", "stream": True, "input": "hi"}
    response = asyncio.run(request(app, "POST", "/v1/responses", json=body))
    assert response.status_code == 200
    comments, _ = _sse_events(response.text)
    assert comments, "Silence is covered by keep-alive comments"
    events = _anthropic_events(response.text)
    names = [name for name, _ in events]
    assert names[:2] == ["response.created", "response.in_progress"]
    assert names.index("response.output_item.added") < names.index("response.output_text.delta")
    deltas = "".join(payload["delta"] for name, payload in events if name == "response.output_text.delta")
    assert deltas == "Hi there"
    assert "hmm" not in response.text, "Reasoning is not relayed to Responses clients"
    done = next(payload for name, payload in events if name == "response.output_item.done" and payload["item"]["type"] == "message")
    assert done["item"]["content"][0]["text"] == "Hi there" and done["item"]["status"] == "completed"
    call_done = next(payload for name, payload in events if name == "response.output_item.done" and payload["item"]["type"] == "function_call")
    assert call_done["item"]["call_id"] == "call_7" and json.loads(call_done["item"]["arguments"]) == {"cmd": "ls"}
    assert "response.function_call_arguments.done" in names
    completed = events[-1]
    assert completed[0] == "response.completed" and completed[1]["response"]["status"] == "completed"
    assert completed[1]["response"]["usage"]["total_tokens"] == 5
    assert [item["type"] for item in completed[1]["response"]["output"]] == ["message", "function_call"]
    sequence = [payload["sequence_number"] for _, payload in events]
    assert sequence == sorted(sequence) and len(set(sequence)) == len(sequence)

    app, gateway = make_streaming_app(StreamingAdapter(pieces=("Hi", " there"), fail_after=1))
    response = asyncio.run(request(app, "POST", "/v1/responses", json=body))
    events = _anthropic_events(response.text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "stream_interrupted"
    assert gateway.failures[-1]["api"] == "responses"
