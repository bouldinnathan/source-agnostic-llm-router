from __future__ import annotations

import asyncio
import json

import pytest

from llm_router.adapters.base import BaseHTTPAdapter
from llm_router.adapters.anthropic import AnthropicMessagesAdapter
from llm_router.adapters.gemini import GeminiGenerateContentAdapter
from llm_router.adapters.generic import GenericJSONAdapter
from llm_router.adapters.ollama import OllamaChatAdapter
from llm_router.adapters.openai import OpenAIChatAdapter
from llm_router.errors import UpstreamError
import httpx

from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, QueryRequest


class CapturingOpenAI(OpenAIChatAdapter):
    def __init__(self) -> None:
        self.path = ""
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.path = path
        self.payload = dict(payload)
        return {
            "choices": [
                {"message": {"content": "hello"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class CapturingGeneric(GenericJSONAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {"data": {"outputs": [{"value": "generic answer"}]}}


class CapturingToolOpenAI(OpenAIChatAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "turn_on",
                                    "arguments": '{"entity":"light.kitchen"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }


class CapturingToolOllama(OllamaChatAdapter):
    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "turn_on", "arguments": {"room": "kitchen"}}}
                ],
            },
            "done_reason": "stop",
        }


class CapturingToolAnthropic(AnthropicMessagesAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "turn_on", "input": {"room": "kitchen"}}
            ],
            "stop_reason": "tool_use",
        }


class CapturingToolGemini(GeminiGenerateContentAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "turn_on", "args": {"room": "kitchen"}}}
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }


MODEL = ModelConfig(id="test", endpoint="source", upstream_model="upstream")


def test_openai_adapter_translates_and_parses() -> None:
    adapter = CapturingOpenAI()
    endpoint = EndpointConfig(name="source", adapter="openai-chat")
    request = QueryRequest.from_prompt("hello", max_tokens=99, temperature=0.2)

    result = asyncio.run(adapter.complete(endpoint, MODEL, request))

    assert adapter.path == "/chat/completions"
    assert adapter.payload["model"] == "upstream"
    assert adapter.payload["messages"][-1]["content"] == "hello"
    assert adapter.payload["max_tokens"] == 99
    assert result.text == "hello"
    assert result.usage["completion_tokens"] == 1


def test_generic_adapter_supports_nested_paths() -> None:
    adapter = CapturingGeneric()
    endpoint = EndpointConfig(
        name="source",
        adapter="generic-json",
        options={
            "request_mode": "prompt",
            "prompt_field": "input.prompt",
            "model_field": "input.model",
            "max_tokens_field": "generation.limit",
            "response_path": "data.outputs.0.value",
        },
    )

    result = asyncio.run(
        adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("nested"))
    )

    assert adapter.payload["input"] == {"prompt": "nested", "model": "upstream"}
    assert adapter.payload["generation"]["limit"] == 1024
    assert result.text == "generic answer"


def test_auth_uses_environment_without_exposing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_TEST_KEY", "super-secret")
    endpoint = EndpointConfig(
        name="source",
        adapter="openai-chat",
        auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
    )

    headers, params = BaseHTTPAdapter().connection_metadata(endpoint, {})

    assert headers["Authorization"] == "Bearer super-secret"
    assert params == {}


def test_missing_auth_variable_has_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_ROUTER_KEY", raising=False)
    endpoint = EndpointConfig(
        name="source",
        adapter="openai-chat",
        auth=AuthConfig(key_env="MISSING_ROUTER_KEY"),
    )

    with pytest.raises(UpstreamError, match="MISSING_ROUTER_KEY") as captured:
        BaseHTTPAdapter().connection_metadata(endpoint, {})

    assert "secret" not in str(captured.value).lower()


def test_openai_tool_calls_and_ollama_history_are_normalized() -> None:
    adapter = CapturingToolOpenAI()
    endpoint = EndpointConfig(name="source", adapter="openai-chat")
    request = QueryRequest(
        messages=(
            {"role": "user", "content": "turn it on"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "turn_on", "arguments": {"room": "kitchen"}}}
                ],
            },
            {"role": "tool", "tool_name": "turn_on", "content": "done"},
        )
    )

    result = asyncio.run(adapter.complete(endpoint, MODEL, request))

    assert result.text == ""
    assert result.tool_calls[0]["function"]["arguments"] == {
        "entity": "light.kitchen"
    }
    sent_call = adapter.payload["messages"][1]["tool_calls"][0]
    assert json.loads(sent_call["function"]["arguments"]) == {"room": "kitchen"}
    assert adapter.payload["messages"][2]["tool_call_id"] == sent_call["id"]


def test_provider_tool_only_responses_are_valid() -> None:
    tools = (
        {
            "type": "function",
            "function": {
                "name": "turn_on",
                "description": "Turn on a room",
                "parameters": {"type": "object"},
            },
        },
    )
    request = QueryRequest.from_prompt("turn it on", tools=tools)

    ollama = asyncio.run(
        CapturingToolOllama().complete(
            EndpointConfig(name="source", adapter="ollama-chat"), MODEL, request
        )
    )
    anthropic_adapter = CapturingToolAnthropic()
    anthropic = asyncio.run(
        anthropic_adapter.complete(
            EndpointConfig(
                name="source",
                adapter="anthropic-messages",
                auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
            ),
            MODEL,
            request,
        )
    )
    gemini_adapter = CapturingToolGemini()
    gemini = asyncio.run(
        gemini_adapter.complete(
            EndpointConfig(
                name="source",
                adapter="gemini-generate",
                auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
            ),
            MODEL,
            request,
        )
    )

    assert ollama.tool_calls[0]["function"]["name"] == "turn_on"
    assert anthropic.tool_calls[0]["function"]["arguments"] == {"room": "kitchen"}
    assert anthropic_adapter.payload["tools"][0]["input_schema"] == {"type": "object"}
    assert gemini.tool_calls[0]["function"]["arguments"] == {"room": "kitchen"}
    assert gemini_adapter.payload["tools"][0]["functionDeclarations"][0]["name"] == "turn_on"


@pytest.mark.parametrize(
    "outcome,kind,retryable",
    [
        (httpx.ReadTimeout("slow"), "timeout", True),
        (httpx.ConnectError("refused"), "connection", True),
        (httpx.RemoteProtocolError("closed"), "connection", True),
        (httpx.Response(503, json={"error": "busy"}), "http_5xx", True),
        (httpx.Response(404, json={"error": "missing"}), "http_4xx", False),
        (httpx.Response(200, content=b"<html>not json</html>"), "invalid_response", False),
        (httpx.Response(200, json=["not", "an", "object"]), "invalid_response", False),
    ],
)
def test_http_failures_carry_a_traffic_failure_kind(monkeypatch, outcome, kind, retryable) -> None:
    from llm_router.adapters import base

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    real_client = httpx.AsyncClient

    def client(**kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", client)
    adapter = base.BaseHTTPAdapter()
    endpoint = EndpointConfig(name="source", adapter="openai-chat", base_url="http://source.invalid/v1", auth=AuthConfig(scheme="none"))
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(adapter.post_json(endpoint, "/chat/completions", {"model": "x"}))
    assert captured.value.kind == kind
    assert captured.value.retryable is retryable
    assert "private" not in str(captured.value)


def test_configuration_problems_are_configuration_failures(monkeypatch) -> None:
    from llm_router.adapters import base

    adapter = base.BaseHTTPAdapter()
    with pytest.raises(UpstreamError) as missing_url:
        asyncio.run(adapter.post_json(EndpointConfig(name="source", adapter="openai-chat"), "/x", {}))
    assert missing_url.value.kind == "configuration"
    monkeypatch.delenv("ROUTER_TEST_MISSING_KEY", raising=False)
    endpoint = EndpointConfig(
        name="source", adapter="openai-chat", base_url="http://source.invalid/v1",
        auth=AuthConfig(key_env="ROUTER_TEST_MISSING_KEY", scheme="bearer"),
    )
    with pytest.raises(UpstreamError) as missing_key:
        adapter.connection_metadata(endpoint, {})
    assert missing_key.value.kind == "configuration"
    assert UpstreamError("parse problem", retryable=False).kind == "invalid_response"
    assert UpstreamError("unknown", status_code=429).kind == "http_4xx"
    assert UpstreamError("unknown").kind == "other"
    assert UpstreamError("unknown", kind="not-a-kind").kind == "other"


class _PayloadOllama(OllamaChatAdapter):
    def __init__(self) -> None:
        self.payload: dict = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {"message": {"role": "assistant", "content": "OK"}, "done_reason": "stop"}


def test_ollama_adapter_forwards_requested_context_window() -> None:
    adapter = _PayloadOllama()
    endpoint = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid")
    asyncio.run(adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", min_context_window=8192.0, max_tokens=32)))
    assert adapter.payload["options"] == {"num_predict": 32, "num_ctx": 8192}
    assert type(adapter.payload["options"]["num_ctx"]) is int
    asyncio.run(adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=32)))
    assert "num_ctx" not in adapter.payload["options"], "No context request means Ollama keeps its own default"


def test_output_limit_is_forwarded_only_when_the_client_set_one() -> None:
    ollama = _PayloadOllama()
    endpoint = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid")
    asyncio.run(ollama.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=2048, max_tokens_specified=False)))
    assert "num_predict" not in ollama.payload["options"], "No client limit means Ollama's own (unlimited) default"
    asyncio.run(ollama.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=64)))
    assert ollama.payload["options"]["num_predict"] == 64
    openai = CapturingOpenAI()
    openai_endpoint = EndpointConfig(name="source", adapter="openai-chat")
    asyncio.run(openai.complete(openai_endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=2048, max_tokens_specified=False)))
    assert "max_tokens" not in openai.payload
    asyncio.run(openai.complete(openai_endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=64)))
    assert openai.payload["max_tokens"] == 64


class _ReasoningOnlyOpenAI(OpenAIChatAdapter):
    def __init__(self, response: dict) -> None:
        self.response = response

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        return self.response


@pytest.mark.parametrize("response", [
    {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "private thoughts"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"role": "assistant", "content": "", "reasoning": "private thoughts"}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}]},
])
def test_reasoning_only_openai_responses_are_diagnosed(response) -> None:
    adapter = _ReasoningOnlyOpenAI(response)
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(adapter.complete(EndpointConfig(name="source", adapter="openai-chat"), MODEL, QueryRequest.from_prompt("hi")))
    assert "output budget on reasoning" in str(captured.value)
    assert "private thoughts" not in str(captured.value)
    assert captured.value.kind == "invalid_response"


def test_reasoning_only_ollama_response_is_diagnosed() -> None:
    class ThinkingOllama(OllamaChatAdapter):
        async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
            return {"message": {"role": "assistant", "content": "", "thinking": "private thoughts"}, "done_reason": "length"}

    with pytest.raises(UpstreamError) as captured:
        asyncio.run(ThinkingOllama().complete(EndpointConfig(name="source", adapter="ollama-chat", base_url="http://s.invalid"), MODEL, QueryRequest.from_prompt("hi")))
    assert "output budget on reasoning" in str(captured.value) and "private thoughts" not in str(captured.value)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _streamed_backend(monkeypatch, *, body: bytes | None = None, content=None, status: int = 200, captured: list | None = None, content_type: str | None = None, responses: list | None = None):
    """Serve a canned body (or async byte stream) through the adapter's own HTTP client."""
    from llm_router.adapters import base

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        headers = {"content-type": content_type} if content_type else {}
        response = httpx.Response(status, content=content if content is not None else body, headers=headers)
        if responses is not None:
            responses.append(response)
        return response

    def client(**kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("verify", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", client)


OLLAMA_ENDPOINT = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid")
OPENAI_ENDPOINT = EndpointConfig(name="source", adapter="openai-chat", base_url="http://source.invalid/v1", auth=AuthConfig(scheme="none"))


def test_ollama_adapter_streams_and_folds_chunks(monkeypatch) -> None:
    chunks = [
        {"model": "m", "message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"model": "m", "message": {"role": "assistant", "content": "lo"}, "done": False},
        {"model": "m", "message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": False},
        {"model": "m", "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "turn_on", "arguments": {"room": "kitchen"}}}]}, "done": False},
        {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 7, "eval_count": 3, "total_duration": 1000, "load_duration": 10, "eval_duration": 500, "prompt_eval_duration": 200},
    ]
    captured: list = []
    _streamed_backend(monkeypatch, body="\n".join(json.dumps(chunk) for chunk in chunks).encode(), captured=captured)
    result = asyncio.run(OllamaChatAdapter().complete(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi", keep_alive=-1, think=False)))
    assert captured[0]["stream"] is True and captured[0]["keep_alive"] == -1 and captured[0]["think"] is False
    assert result.text == "Hello"
    assert result.tool_calls[0]["function"]["name"] == "turn_on"
    assert result.usage == {"prompt_eval_count": 7, "eval_count": 3, "total_duration": 1000}
    assert result.finish_reason == "stop"
    assert result.raw["eval_duration"] == 500 and result.raw["message"]["thinking"] == "hmm"
    assert result.first_token_ms is not None and result.first_token_ms >= 0


def test_openai_adapter_streams_sse_and_reassembles_tool_calls(monkeypatch) -> None:
    events = [
        {"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "delta": {"content": "Hi"}}]},
        {"choices": [{"index": 0, "delta": {"content": " there"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "turn_on", "arguments": "{\"ro"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "om\": \"kitchen\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + ": keepalive\n\ndata: [DONE]\n\n"
    captured: list = []
    _streamed_backend(monkeypatch, body=body.encode(), captured=captured)
    result = asyncio.run(OpenAIChatAdapter().complete(OPENAI_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert captured[0]["stream"] is True and captured[0]["stream_options"] == {"include_usage": True}
    assert result.text == "Hi there"
    assert result.tool_calls[0]["id"] == "call_a" and result.tool_calls[0]["function"]["name"] == "turn_on"
    assert result.tool_calls[0]["function"]["arguments"] == {"room": "kitchen"}, "Argument fragments are joined before parsing"
    assert result.usage == {"prompt_tokens": 9, "completion_tokens": 4}
    assert result.finish_reason == "tool_calls"
    assert result.raw["id"] == "c1" and result.first_token_ms is not None


def test_unstreamed_json_reply_is_accepted_when_backend_ignores_stream(monkeypatch) -> None:
    """A backend or proxy that ignores ``stream`` still yields a usable answer."""
    captured: list = []
    body = {"choices": [{"message": {"role": "assistant", "content": "whole"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    _streamed_backend(monkeypatch, body=json.dumps(body).encode(), captured=captured, content_type="application/json")
    result = asyncio.run(OpenAIChatAdapter().complete(OPENAI_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert captured[0]["stream"] is True, "The adapter still asks for a stream; only the reply shape differs"
    assert result.text == "whole" and result.usage == {"prompt_tokens": 3, "completion_tokens": 1}
    assert result.first_token_ms is not None

    body = {"message": {"role": "assistant", "content": "done"}, "done": True, "done_reason": "stop", "eval_count": 4}
    _streamed_backend(monkeypatch, body=json.dumps(body, indent=2).encode(), content_type="application/json")
    result = asyncio.run(OllamaChatAdapter().complete(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert result.text == "done" and result.usage == {"eval_count": 4}, "Pretty-printed JSON is not mistaken for NDJSON"


def test_streaming_can_be_disabled_per_endpoint(monkeypatch) -> None:
    captured: list = []
    _streamed_backend(monkeypatch, body=json.dumps({"message": {"role": "assistant", "content": "OK"}, "done": True}).encode(), captured=captured)
    endpoint = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid", options={"stream": False})
    result = asyncio.run(OllamaChatAdapter().complete(endpoint, MODEL, QueryRequest.from_prompt("hi")))
    assert captured[0]["stream"] is False and result.text == "OK" and result.first_token_ms is None


@pytest.mark.parametrize("body,fragment", [
    (b'{"error": "model requires more system memory (12 GiB)"}\n', "model requires more system memory"),
    (b'not json\n', "invalid JSON"),
])
def test_ollama_stream_errors_are_reported_safely(monkeypatch, body, fragment) -> None:
    _streamed_backend(monkeypatch, body=body)
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(OllamaChatAdapter().complete(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert fragment in str(captured.value)


def test_openai_stream_error_event_and_http_status(monkeypatch) -> None:
    _streamed_backend(monkeypatch, body=b'data: {"error": {"message": "context length exceeded", "type": "invalid"}}\n\n')
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(OpenAIChatAdapter().complete(OPENAI_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert "context length exceeded" in str(captured.value)
    _streamed_backend(monkeypatch, body=b'{"error": "busy"}', status=503)
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(OpenAIChatAdapter().complete(OPENAI_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    assert captured.value.status_code == 503 and captured.value.kind == "http_5xx" and captured.value.retryable


def test_stream_timeouts_distinguish_first_token_idle_and_cap(monkeypatch) -> None:
    from llm_router.adapters.base import STREAM_TIMEOUTS, StreamTimeouts

    async def silent():
        await asyncio.sleep(0.4)
        yield b'{"message":{"role":"assistant","content":"late"},"done":true}\n'

    async def stalls():
        yield b'{"message":{"role":"assistant","content":"Hi"},"done":false}\n'
        await asyncio.sleep(0.4)
        yield b'{"message":{"role":"assistant","content":"!"},"done":true}\n'

    async def slow_total():
        for _ in range(40):
            yield b'{"message":{"role":"assistant","content":"x"},"done":false}\n'
            await asyncio.sleep(0.02)
        yield b'{"message":{"role":"assistant","content":""},"done":true}\n'

    cases = [
        (silent, StreamTimeouts(first_token=0.05, idle=1.0, total=None), "no first token within 0.05 s"),
        (stalls, StreamTimeouts(first_token=1.0, idle=0.05, total=None), "no token for 0.05 s"),
        (slow_total, StreamTimeouts(first_token=1.0, idle=1.0, total=0.15), "request cap of 0.15 s"),
    ]
    for content, timeouts, fragment in cases:
        _streamed_backend(monkeypatch, content=content())
        token = STREAM_TIMEOUTS.set(timeouts)
        try:
            with pytest.raises(UpstreamError) as captured:
                asyncio.run(OllamaChatAdapter().complete(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
        finally:
            STREAM_TIMEOUTS.reset(token)
        assert captured.value.kind == "timeout", fragment
        assert fragment in str(captured.value), str(captured.value)
    _streamed_backend(monkeypatch, content=stalls())
    token = STREAM_TIMEOUTS.set(StreamTimeouts(first_token=1.0, idle=1.0, total=None))
    try:
        result = asyncio.run(OllamaChatAdapter().complete(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")))
    finally:
        STREAM_TIMEOUTS.reset(token)
    assert result.text == "Hi!", "A pause shorter than the idle limit is fine"


def test_ollama_adapter_stream_yields_fragments_then_the_result(monkeypatch) -> None:
    from llm_router.adapters.base import StreamDelta
    from llm_router.schema import UpstreamResult

    chunks = [
        {"message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": False},
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 2},
    ]
    _streamed_backend(monkeypatch, body="\n".join(json.dumps(chunk) for chunk in chunks).encode())

    async def collect():
        items = []
        async for item in OllamaChatAdapter().stream(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi")):
            items.append(item)
        return items

    items = asyncio.run(collect())
    deltas, result = items[:-1], items[-1]
    assert all(isinstance(delta, StreamDelta) for delta in deltas)
    assert [(delta.text, delta.thinking) for delta in deltas] == [("", "hmm"), ("Hel", ""), ("lo", "")]
    assert deltas[0].first_token_ms is not None and deltas[1].first_token_ms is None, "Only the first fragment is timed"
    assert isinstance(result, UpstreamResult) and result.text == "Hello" and result.usage == {"eval_count": 2}
    assert result.first_token_ms == deltas[0].first_token_ms


def test_closing_an_adapter_stream_early_closes_the_backend_connection(monkeypatch) -> None:
    from llm_router.adapters.base import StreamDelta

    responses: list = []

    async def content():
        yield json.dumps({"message": {"role": "assistant", "content": "Hel"}, "done": False}).encode() + b"\n"
        await asyncio.Event().wait()  # the backend would keep generating for a long time

    _streamed_backend(monkeypatch, content=content(), responses=responses)

    async def scenario():
        stream = OllamaChatAdapter().stream(OLLAMA_ENDPOINT, MODEL, QueryRequest.from_prompt("hi"))
        first = await stream.__anext__()
        assert isinstance(first, StreamDelta) and first.text == "Hel"
        assert responses[0].is_closed is False
        await asyncio.wait_for(stream.aclose(), 1.0)
        assert responses[0].is_closed is True, "Closing the adapter stream closes the upstream response, which stops generation"

    asyncio.run(scenario())
