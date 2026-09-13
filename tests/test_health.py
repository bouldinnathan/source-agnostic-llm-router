from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from llm_router.health import probe_endpoints
from llm_router.runtime import RuntimeRegistry
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, PolicyConfig, RouterConfig


def config_for(*endpoints: EndpointConfig, policy: PolicyConfig | None = None) -> RouterConfig:
    return RouterConfig(
        endpoints={endpoint.name: endpoint for endpoint in endpoints},
        models=tuple(
            ModelConfig(id=f"model-{endpoint.name}", endpoint=endpoint.name, upstream_model="qwen")
            for endpoint in endpoints
        ),
        policy=policy or PolicyConfig(),
    )


@pytest.mark.parametrize(
    ("adapter", "base_url", "expected_path", "payload"),
    [
        ("ollama-chat", "http://laptop.local:11434", "/api/tags", {"models": []}),
        ("openai-compatible", "http://desktop.local:1234/v1", "/v1/models", {"data": []}),
        ("openai-responses", "https://api.example/v1", "/v1/models", {"data": []}),
        ("anthropic", "https://api.example", "/v1/models", {"data": []}),
        ("anthropic-messages", "https://api.example/v1", "/v1/models", {"data": []}),
        ("gemini", "https://api.example/v1beta", "/v1beta/models", {"models": []}),
    ],
)
def test_builtin_probes_use_model_lists_without_inference(
    adapter: str, base_url: str, expected_path: str, payload: dict
) -> None:
    config = config_for(
        EndpointConfig(name="worker", adapter=adapter, base_url=base_url, auth=AuthConfig(scheme="none"))
    )
    runtime = RuntimeRegistry(config.policy)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == expected_path
        return httpx.Response(200, json=payload)

    assert asyncio.run(probe_endpoints(config, runtime, transport=httpx.MockTransport(handler))) == {
        "worker": True
    }
    assert len(calls) == 1
    assert runtime.snapshot(config.models)["endpoints"]["worker"]["reachable"] is True


def test_failed_probe_blocks_until_success_without_resetting_model_circuit() -> None:
    config = config_for(
        EndpointConfig(name="worker", adapter="ollama", base_url="http://laptop.local:11434"),
        policy=PolicyConfig(circuit_breaker_failures=1),
    )
    runtime = RuntimeRegistry(config.policy)
    assert runtime.endpoint_available("worker")  # Optimistic before the first probe.
    runtime.record_failure("model-worker", "inference failed")

    async def scenario() -> None:
        await probe_endpoints(
            config, runtime, transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        assert not runtime.endpoint_available("worker")
        runtime.state("model-worker").circuit_open_until = 0.0
        assert runtime.is_available("model-worker")
        assert not runtime.endpoint_available("worker")
        runtime.record_failure("model-worker", "inference still failed")
        await probe_endpoints(
            config,
            runtime,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []})),
        )

    asyncio.run(scenario())
    assert runtime.endpoint_available("worker")
    assert not runtime.is_available("model-worker")
    assert runtime.state("model-worker").failures == 2
    assert runtime.snapshot(config.models)["endpoints"]["worker"]["last_error"] is None


def test_unknown_adapter_remains_unprobed_until_health_path_is_configured() -> None:
    config = config_for(
        EndpointConfig(name="generic", adapter="generic-json", base_url="http://custom.local"),
        EndpointConfig(
            name="health",
            adapter="generic-json",
            base_url="http://custom.local/api",
            auth=AuthConfig(scheme="none"),
            health_path="/health",
        ),
    )
    runtime = RuntimeRegistry(config.policy)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/health"
        return httpx.Response(204)

    result = asyncio.run(probe_endpoints(config, runtime, transport=httpx.MockTransport(handler)))
    assert result == {"generic": None, "health": True}
    assert len(calls) == 1
    assert runtime.endpoint_available("generic")
    assert runtime.snapshot(config.models)["endpoints"]["generic"]["probed"] is False


def test_probes_discoverable_endpoints_without_enabled_models() -> None:
    config = config_for(
        EndpointConfig(name="discover", adapter="ollama", base_url="http://laptop.local", discover=True),
        EndpointConfig(name="disabled", adapter="ollama", base_url="http://offline.local"),
    )
    config = replace(config, models=tuple(replace(model, enabled=False) for model in config.models))
    runtime = RuntimeRegistry(config.policy)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []}))
    assert asyncio.run(probe_endpoints(config, runtime, transport=transport)) == {"discover": True}


def test_probes_preserve_authentication_headers_and_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_TEST_KEY", "hidden-key")
    monkeypatch.setenv("HEALTH_TEST_HEADER", "header-value")
    endpoint = EndpointConfig(
        name="private",
        adapter="anthropic",
        base_url="https://private.local",
        auth=AuthConfig(key_env="HEALTH_TEST_KEY"),
        headers={"X-Health-Context": "${HEALTH_TEST_HEADER}"},
        options={"api_version": "2023-06-01"},
        verify_tls=False,
    )
    config = config_for(endpoint)
    runtime = RuntimeRegistry(config.policy)
    clients = []
    original_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs["verify"] is False
        assert kwargs["follow_redirects"] is False
        clients.append(original_client(**kwargs))
        return clients[-1]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "hidden-key"
        assert request.headers["X-Health-Context"] == "header-value"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    for _ in range(2):
        assert asyncio.run(
            probe_endpoints(config, runtime, transport=httpx.MockTransport(handler))
        ) == {"private": True}
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert all(client.is_closed for client in clients)
    assert "hidden-key" not in str(runtime.snapshot(config.models))


def test_gemini_probe_uses_query_auth_without_logging_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_TEST_KEY", "hidden-query-key")
    config = config_for(
        EndpointConfig(
            name="gemini",
            adapter="gemini",
            base_url="https://api.example/v1beta",
            auth=AuthConfig(key_env="HEALTH_TEST_KEY"),
        )
    )
    runtime = RuntimeRegistry(config.policy)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "hidden-query-key"
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    assert asyncio.run(probe_endpoints(config, runtime, transport=httpx.MockTransport(handler))) == {
        "gemini": False
    }
    snapshot = runtime.snapshot(config.models)
    assert "hidden-query-key" not in str(snapshot)
    assert snapshot["endpoints"]["gemini"]["last_error"] == "Health probe HTTP failure: ConnectError"


@pytest.mark.parametrize("response", [httpx.Response(200, text="not JSON"), httpx.Response(200, json={"error": "wrong server"}), httpx.Response(302, headers={"Location": "http://other.local"})])
def test_invalid_model_lists_and_redirects_are_unavailable(response: httpx.Response) -> None:
    config = config_for(EndpointConfig(name="worker", adapter="ollama", base_url="http://worker.local"))
    runtime = RuntimeRegistry(config.policy)
    result = asyncio.run(
        probe_endpoints(config, runtime, transport=httpx.MockTransport(lambda request: response))
    )
    assert result == {"worker": False}
    assert not runtime.endpoint_available("worker")


def test_timeout_and_concurrency_are_bounded() -> None:
    config = config_for(
        *(EndpointConfig(name=f"worker-{index}", adapter="ollama", base_url=f"http://worker-{index}.local") for index in range(4)),
        policy=PolicyConfig(health_check_timeout_seconds=0.02),
    )
    runtime = RuntimeRegistry(config.policy)
    active = 0
    maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
        return httpx.Response(200, json={"models": []})

    result = asyncio.run(
        probe_endpoints(config, runtime, transport=httpx.MockTransport(handler), max_concurrency=2)
    )
    assert result == {f"worker-{index}": False for index in range(4)}
    assert maximum == 2
    assert active == 0
    assert all(
        entry["last_error"] == "Health probe timed out"
        for entry in runtime.snapshot(config.models)["endpoints"].values()
    )


def test_cancelling_probes_does_not_mark_worker_unavailable() -> None:
    config = config_for(EndpointConfig(name="worker", adapter="ollama", base_url="http://worker.local"))
    runtime = RuntimeRegistry(config.policy)

    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.sleep(10)
            return httpx.Response(200, json={"models": []})

        task = asyncio.create_task(
            probe_endpoints(config, runtime, transport=httpx.MockTransport(handler))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert runtime.endpoint_available("worker")
    assert runtime.snapshot(config.models)["endpoints"]["worker"]["probed"] is False


def test_unexpected_probe_failure_does_not_cancel_other_workers() -> None:
    config = config_for(
        EndpointConfig(name="bad", adapter="ollama", base_url="http://bad.local"),
        EndpointConfig(name="good", adapter="ollama", base_url="http://good.local"),
    )
    runtime = RuntimeRegistry(config.policy)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.local":
            raise RuntimeError("transport failed with secret payload")
        return httpx.Response(200, json={"models": []})

    result = asyncio.run(probe_endpoints(config, runtime, transport=httpx.MockTransport(handler)))
    assert result == {"bad": False, "good": True}
    assert runtime.snapshot(config.models)["endpoints"]["bad"]["last_error"] == "Health probe failed: RuntimeError"
