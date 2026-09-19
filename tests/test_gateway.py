from __future__ import annotations

import asyncio
import json

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
            "version": "0.3.4",
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
