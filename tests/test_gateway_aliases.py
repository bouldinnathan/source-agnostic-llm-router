from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from llm_router.adapters import AdapterRegistry
from llm_router.bootstrap import BootstrapResult
from llm_router.config import config_from_mapping
from llm_router.discovery import DiscoveryReport, DiscoverySettings, ProbeResult
from llm_router.errors import UpstreamError
from llm_router.gateway import RouterGateway, create_app
from llm_router.provisioning import ProvisioningSettings
from llm_router.router import LLMRouter
from llm_router.schema import UpstreamResult

from test_gateway import StaticGateway, request


class FleetAdapter:
    def __init__(self, offline=()):
        self.offline = set(offline)
        self.calls = []

    async def complete(self, endpoint, model, query):
        self.calls.append((endpoint.name, model.upstream_model, query))
        if endpoint.name in self.offline:
            raise UpstreamError("simulated outage")
        return UpstreamResult(text="answer", usage={})


def fleet(*, offline=(), primary_tools=True):
    config = config_from_mapping({
        "router": {"max_attempts": 4, "health_check_interval_seconds": 0.01},
        "endpoints": {
            "ollama-a": {"adapter": "ollama-chat", "base_url": "http://golemframe.test:11434", "machine_id": "golemframe"},
            "studio-b": {"adapter": "openai-compatible", "base_url": "http://pantheon.test:1234/v1", "machine_id": "pantheon", "auth": {"scheme": "none"}},
        },
        "models": [
            {"id": "qwen-a", "endpoint": "ollama-a", "upstream_model": "qwen3:14b", "replica_group": "qwen", "quality": 0.6, "estimated_latency_ms": 3000, "max_output_tokens": 4096, "capabilities": {"general": 1, "tool_use": int(primary_tools)}},
            {"id": "qwen-b", "endpoint": "studio-b", "upstream_model": "qwen3-14b", "replica_group": "qwen", "quality": 0.9, "estimated_latency_ms": 100, "max_output_tokens": 4096, "capabilities": {"general": 1, "tool_use": 1}},
            {"id": "other-b", "endpoint": "studio-b", "upstream_model": "different-model", "quality": 1, "estimated_latency_ms": 1, "max_output_tokens": 4096, "capabilities": {"general": 1, "tool_use": 1}},
        ],
    })
    adapter = FleetAdapter(offline)
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", adapter)
    adapters.register("openai-compatible", adapter)
    return LLMRouter(config, adapters=adapters), adapter


@pytest.mark.parametrize("path,key", [("/api/tags", "models"), ("/v1/models", "data")])
def test_both_model_lists_expose_ha_preferred_and_strict_aliases(path, key):
    router, _ = fleet()
    response = asyncio.run(request(create_app(gateway=StaticGateway(router)), "GET", path))
    names = {row.get("id", row.get("model")) for row in response.json()[key]}
    assert {"auto", "qwen-ha", "qwen-golemframe", "qwen-golemframe-nofailover", "qwen-pantheon", "qwen-pantheon-nofailover"} <= names


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_preferred_machine_retries_same_group_and_keeps_client_alias(path):
    router, adapter = fleet(offline={"ollama-a"})
    response = asyncio.run(request(create_app(gateway=StaticGateway(router)), "POST", path, json={
        "model": "qwen-golemframe", "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
    }))
    assert response.status_code == 200
    assert response.json()["model"] == "qwen-golemframe"
    assert response.json()["router"]["deployment"] == "qwen-b"
    assert [(call[0], call[1]) for call in adapter.calls] == [("ollama-a", "qwen3:14b"), ("studio-b", "qwen3-14b")]


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_nofailover_never_sends_prompt_to_another_machine(path):
    router, adapter = fleet(offline={"ollama-a"})
    response = asyncio.run(request(create_app(gateway=StaticGateway(router)), "POST", path, json={
        "model": "qwen-golemframe-nofailover", "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
    }))
    assert response.status_code == 503
    assert [call[0] for call in adapter.calls] == ["ollama-a"]


def test_health_failure_skips_preferred_machine_and_recovery_restores_preference():
    router, adapter = fleet()
    app = create_app(gateway=StaticGateway(router))
    body = {"model": "qwen-golemframe", "stream": False, "messages": [{"role": "user", "content": "hello"}]}
    router.runtime.record_endpoint_probe("ollama-a", False, "offline")
    first = asyncio.run(request(app, "POST", "/api/chat", json=body))
    router.runtime.record_endpoint_probe("ollama-a", True)
    second = asyncio.run(request(app, "POST", "/api/chat", json=body))
    assert first.json()["router"]["deployment"] == "qwen-b"
    assert second.json()["router"]["deployment"] == "qwen-a"
    assert [call[0] for call in adapter.calls] == ["studio-b", "ollama-a"]


def test_agent_history_and_tool_requirement_survive_machine_selection():
    router, adapter = fleet(primary_tools=False)
    app = create_app(gateway=StaticGateway(router))
    messages = [
        {"role": "user", "content": "Read the file"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
    ]
    body = {"model": "qwen-golemframe", "messages": messages, "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
    response = asyncio.run(request(app, "POST", "/v1/chat/completions", json=body))
    assert response.status_code == 200
    assert [call[0] for call in adapter.calls] == ["studio-b"]
    assert adapter.calls[0][2].messages == tuple(messages)
    assert adapter.calls[0][2].required_capabilities == ("tool_use",)
    strict = asyncio.run(request(app, "POST", "/v1/chat/completions", json={**body, "model": "qwen-golemframe-nofailover"}))
    assert strict.status_code == 503
    assert len(adapter.calls) == 1


def test_alias_show_advertises_actual_capabilities_and_pull_never_provisions():
    from test_gateway import ProvisioningGateway
    router, _ = fleet(primary_tools=False)
    gateway = ProvisioningGateway(router)
    app = create_app(gateway=gateway)
    shown = asyncio.run(request(app, "POST", "/api/show", json={"model": "qwen-golemframe-nofailover"}))
    assert shown.json()["capabilities"] == ["completion"]
    pulled = asyncio.run(request(app, "POST", "/api/pull", json={"model": "qwen-ha", "stream": False}))
    assert pulled.status_code == 200
    assert not gateway.provision_calls


def test_all_offline_is_unready_but_model_aliases_remain_listed():
    router, _ = fleet()
    gateway = RouterGateway(discovery=False)
    gateway._router = router
    for name in router.config.endpoints:
        router.runtime.record_endpoint_probe(name, False)
    app = create_app(gateway=gateway)
    health = asyncio.run(request(app, "GET", "/readyz"))
    listed = asyncio.run(request(app, "GET", "/api/tags"))
    assert health.status_code == 503
    assert listed.status_code == 200
    assert "qwen-ha" in {model["name"] for model in listed.json()["models"]}


def test_failed_discovery_does_not_make_healthy_retained_router_unready():
    router, _ = fleet()
    gateway = RouterGateway(discovery=False)
    gateway._router = router
    gateway._last_error = "temporary discovery failure"
    response = asyncio.run(request(create_app(gateway=gateway), "GET", "/readyz"))
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_failed_discovery_retains_machine_aliases_and_health_state():
    router, _ = fleet()
    router.config = replace(router.config, models=tuple(replace(model, tags=("discovered",)) for model in router.config.models))
    router.runtime.record_endpoint_probe("ollama-a", False)
    partial_config = replace(router.config, endpoints={"studio-b": router.config.endpoints["studio-b"]}, models=tuple(model for model in router.config.models if model.endpoint == "studio-b"))
    partial = LLMRouter(partial_config, runtime=router.runtime)
    report = DiscoveryReport(partial_config, (ProbeResult("ollama-a", "ollama", router.config.endpoints["ollama-a"].base_url, False),))
    retained = RouterGateway._retain_failed_sources(BootstrapResult(partial, report, None), router)
    from llm_router.aliases import build_aliases
    assert "qwen-golemframe" in build_aliases(retained.router.config)
    assert not retained.router.runtime.endpoint_available("ollama-a")


def test_gateway_health_runs_with_discovery_disabled_and_stops_cleanly(monkeypatch):
    router, _ = fleet()
    calls = []

    async def scenario():
        checked_twice = asyncio.Event()
        async def bootstrap(*args, **kwargs):
            return BootstrapResult(router, DiscoveryReport(router.config, ()), router.config)
        async def probe(config, runtime):
            calls.append(config)
            if len(calls) >= 2:
                checked_twice.set()
            return {}
        monkeypatch.setattr("llm_router.gateway.bootstrap_router", bootstrap)
        monkeypatch.setattr("llm_router.gateway.probe_endpoints", probe)
        gateway = RouterGateway(discovery=False, provisioning_settings=ProvisioningSettings(enabled=False))
        await gateway.start()
        try:
            await asyncio.wait_for(checked_twice.wait(), 1)
        finally:
            await gateway.stop()
        assert gateway._health_task is None
    asyncio.run(scenario())
    assert len(calls) >= 2


def test_recovered_known_endpoint_triggers_model_rediscovery(monkeypatch):
    router, _ = fleet()
    router.runtime.record_endpoint_probe("ollama-a", False)
    refreshed = []
    async def probe(config, runtime):
        runtime.record_endpoint_probe("ollama-a", True)
        return {"ollama-a": True}
    async def bootstrap(*args, **kwargs):
        refreshed.append(True)
        return BootstrapResult(router, DiscoveryReport(router.config, ()), router.config)
    monkeypatch.setattr("llm_router.gateway.probe_endpoints", probe)
    monkeypatch.setattr("llm_router.gateway.bootstrap_router", bootstrap)
    gateway = RouterGateway(settings=DiscoverySettings(include_loopback=False, include_cloud=False))
    gateway._router = router
    asyncio.run(gateway.check_health())
    assert refreshed == [True]


def test_explicit_health_path_is_not_overridden_by_failed_model_listing(monkeypatch):
    router, _ = fleet()
    endpoint = replace(router.config.endpoints["ollama-a"], health_path="/health")
    router.config = replace(router.config, endpoints={**router.config.endpoints, "ollama-a": endpoint})
    router.runtime.record_endpoint_probe("ollama-a", False)
    async def probe(config, runtime):
        runtime.record_endpoint_probe("ollama-a", True)
        return {"ollama-a": True}
    async def bootstrap(*args, **kwargs):
        report = DiscoveryReport(router.config, (ProbeResult("ollama-a", "ollama", endpoint.base_url, False, error="HTTP 404", endpoint="ollama-a"),))
        return BootstrapResult(router, report, router.config)
    monkeypatch.setattr("llm_router.gateway.probe_endpoints", probe)
    monkeypatch.setattr("llm_router.gateway.bootstrap_router", bootstrap)
    gateway = RouterGateway()
    gateway._router = router
    asyncio.run(gateway.check_health())
    assert gateway._router.runtime.endpoint_available("ollama-a")


def test_refresh_retains_discovered_models_when_stable_machine_moves_to_offline_address():
    router, _ = fleet()
    original = replace(router.config.models[0], tags=("discovered",))
    router.config = replace(router.config, models=(original,))
    moved = replace(router.config.endpoints["ollama-a"], base_url="http://new-address.test:11434", discover=True)
    config = replace(router.config, endpoints={"ollama-a": moved}, models=())
    discovered = LLMRouter(config, runtime=router.runtime)
    report = DiscoveryReport(config, (ProbeResult("ollama-a", "ollama", moved.base_url, False, endpoint="ollama-a"),))
    result = RouterGateway._retain_failed_sources(BootstrapResult(discovered, report, config), router)
    assert result.router.config.models == (original,)
    assert result.router.config.endpoints["ollama-a"].base_url == moved.base_url


def test_refresh_never_restores_old_deployment_over_explicit_disabled_override():
    router, _ = fleet()
    original = replace(router.config.models[0], tags=("discovered",))
    router.config = replace(router.config, models=(original,))
    disabled = replace(original, id="new-explicit-id", enabled=False, tags=())
    config = replace(router.config, models=(disabled,))
    discovered = LLMRouter(config, runtime=router.runtime)
    report = DiscoveryReport(config, (ProbeResult("ollama-a", "ollama", config.endpoints["ollama-a"].base_url, False, endpoint="ollama-a"),))
    result = RouterGateway._retain_failed_sources(BootstrapResult(discovered, report, config), router)
    assert result.router.config.models == (disabled,)


def test_refresh_never_resurrects_removed_identity_when_same_url_has_new_name():
    router, _ = fleet()
    original = replace(router.config.models[0], tags=("discovered",))
    router.config = replace(router.config, models=(original,))
    renamed = replace(router.config.endpoints["ollama-a"], name="new-name", machine_id="new-machine", discover=True)
    config = replace(router.config, endpoints={"new-name": renamed}, models=())
    discovered = LLMRouter(config, runtime=router.runtime)
    report = DiscoveryReport(config, (ProbeResult("new-name", "ollama", renamed.base_url, False, endpoint="new-name"),))
    result = RouterGateway._retain_failed_sources(BootstrapResult(discovered, report, config), router)
    assert not result.router.config.models
    assert set(result.router.config.endpoints) == {"new-name"}
