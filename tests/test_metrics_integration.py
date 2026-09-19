"""Passive metrics through real routing/API paths, with fake upstream inference."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from llm_router.adapters import AdapterRegistry
from llm_router.bootstrap import BootstrapResult
from llm_router.discovery import DiscoveryReport
from llm_router.errors import UpstreamError
from llm_router.gateway import RouterGateway, create_app
from llm_router.metrics import MetricsStore
from llm_router.router import LLMRouter
from llm_router.schema import QueryRequest, UpstreamResult
from llm_router import updater

from conftest import make_config


class MeasuredAdapter:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_first = fail_first

    async def complete(self, endpoint, model, request):
        self.calls.append(endpoint.name)
        if self.fail_first and endpoint.name == "source-a":
            raise UpstreamError("private upstream failure", status_code=503)
        return UpstreamResult(
            text="private generated answer",
            usage={"prompt_eval_count": 100, "eval_count": 20},
            raw={
                "message": {"content": "private generated answer"},
                "prompt_eval_count": 100, "prompt_eval_duration": 1_000_000_000,
                "eval_count": 20, "eval_duration": 500_000_000,
                "load_duration": 1_200_000_000,
            },
        )


def configured(tmp_path: Path, *, fail_first: bool = False):
    config = make_config(models=[
        {"id": "qwen-a", "endpoint": "source-a", "upstream_model": "qwen",
         "quality": .99, "capabilities": {"general": 1.0}},
        {"id": "qwen-b", "endpoint": "source-b", "upstream_model": "qwen",
         "quality": .8, "capabilities": {"general": 1.0}},
    ])
    config = replace(config, endpoints={
        name: replace(endpoint, machine_id=machine)
        for (name, endpoint), machine in zip(config.endpoints.items(), ("golemframe", "pantheon"))
    })
    store = MetricsStore(tmp_path / "observations" / "metrics.sqlite3")
    adapter = MeasuredAdapter(fail_first)
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", adapter)
    router = LLMRouter(config, adapters=adapters, metrics=store)
    gateway = RouterGateway(discovery=False, metrics_store=store)
    gateway._router = router
    return router, gateway, store, adapter


async def get(app, url, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
        return await client.get(url, **kwargs)


def test_completion_records_actual_server_not_ha_alias_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key")
    router, gateway, store, adapter = configured(tmp_path)
    app = create_app(gateway=gateway)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer router-key"}, json={
                "model": "qwen-ha", "messages": [{"role": "user", "content": "private user prompt"}],
                "max_tokens": 16,
            })
            assert response.status_code == 200, response.text
            return await client.get("/router/metrics", headers={"Authorization": "Bearer router-key"})

    response = asyncio.run(exercise())
    assert response.status_code == 200
    row = response.json()["deployments"][0]
    assert row["model"] == "qwen" and row["endpoint"] == "source-a"
    assert row["machine"] == "golemframe" and row["current"] is True
    assert row["successes"] == 1 and row["failures"] == 0
    assert row["metrics"]["input_tokens_per_second"]["latest"] == 100
    assert row["metrics"]["output_tokens_per_second"]["latest"] == 40
    assert row["metrics"]["load_duration_ms"]["latest"] == 1200
    assert row["slow_load_count"] == 1
    assert adapter.calls == ["source-a"]
    restarted = MetricsStore(tmp_path / "observations" / "metrics.sqlite3")
    assert restarted.snapshot()["deployments"][0]["successes"] == 1
    persisted = (tmp_path / "observations" / "metrics.sqlite3").read_bytes()
    for secret in (b"private user prompt", b"private generated answer", b"router-key"):
        assert secret not in persisted and secret.decode() not in response.text
    assert "no-store" in response.headers["cache-control"]


def test_failed_attempt_and_fallback_keep_separate_metrics(tmp_path):
    router, _, store, adapter = configured(tmp_path, fail_first=True)
    result = asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    assert result.endpoint == "source-b" and adapter.calls == ["source-a", "source-b"]
    rows = {row["endpoint"]: row for row in store.snapshot()["deployments"]}
    assert rows["source-a"]["failures"] == 1 and rows["source-a"]["successes"] == 0
    assert rows["source-a"]["metrics"]["input_tokens_per_second"] is None
    assert rows["source-a"]["metrics"]["load_duration_ms"] is None
    assert rows["source-b"]["successes"] == 1 and rows["source-b"]["failures"] == 0


def test_storage_failure_never_loses_answer_or_retries_inference(tmp_path, monkeypatch):
    router, _, store, adapter = configured(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("private path disk failure")

    monkeypatch.setattr(store, "record", broken)
    result = asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    assert result.text == "private generated answer"
    assert adapter.calls == ["source-a"]
    assert router.runtime.state("qwen-a").successes == 1


def test_metrics_disk_work_runs_outside_request_event_loop(tmp_path, monkeypatch):
    router, _, store, _ = configured(tmp_path)
    main_thread = threading.get_ident()
    observed_threads = []
    original = store.record

    def record(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "record", record)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    assert observed_threads and main_thread not in observed_threads


def test_invalid_optional_statistics_preserve_success_count(tmp_path, monkeypatch):
    router, _, store, adapter = configured(tmp_path)

    def broken(*args, **kwargs):
        raise ValueError("bad statistics")

    monkeypatch.setattr("llm_router.router.extract_observation", broken)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    row = store.snapshot()["deployments"][0]
    assert row["successes"] == 1 and row["metrics"]["output_tokens_per_second"] is None
    assert adapter.calls == ["source-a"]


@pytest.mark.parametrize("path", ["/status", "/healthz", "/readyz", "/status/data", "/router/metrics"])
def test_observation_reads_and_dry_routes_never_infer_or_create_database(tmp_path, monkeypatch, path):
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    router, gateway, store, adapter = configured(tmp_path)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Metrics reads cannot discover or invoke models")

    monkeypatch.setattr(gateway, "refresh", forbidden)
    monkeypatch.setattr(gateway, "provision", forbidden)
    router.route(QueryRequest.from_prompt("dry run"))
    response = asyncio.run(get(create_app(gateway=gateway), path))
    assert response.status_code == 200
    assert not (tmp_path / "observations").exists()
    assert adapter.calls == []


@pytest.mark.parametrize("path", ["/router/metrics", "/router/metrics?api_key=router-key", "/status/data"])
def test_private_metrics_need_bearer_before_any_store_read(tmp_path, monkeypatch, path):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key")
    _, gateway, store, _ = configured(tmp_path)

    def forbidden():
        raise AssertionError("Unauthorized access must not read history")

    monkeypatch.setattr(store, "snapshot", forbidden)
    response = asyncio.run(get(create_app(gateway=gateway), path))
    assert response.status_code == 401 and "source-a" not in response.text


def test_public_summary_never_contains_performance_or_reads_database(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key")
    router, gateway, store, _ = configured(tmp_path)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))

    def forbidden():
        raise AssertionError("Public health cannot read detailed metrics")

    monkeypatch.setattr(store, "snapshot", forbidden)
    response = asyncio.run(get(create_app(gateway=gateway), "/healthz"))
    assert response.status_code == 200
    assert "performance" not in response.json()
    assert "source-a" not in response.text and "golemframe" not in response.text


def test_metrics_api_and_detailed_status_include_historical_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    router, gateway, _, _ = configured(tmp_path)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    gateway._router = None
    app = create_app(gateway=gateway)
    api = asyncio.run(get(app, "/router/metrics")).json()
    page = asyncio.run(get(app, "/status/data")).json()
    assert api["deployments"][0]["current"] is False
    assert page["performance"] == api
    assert api["schema_version"] == 2 and api["collection"] == "passive"


def test_metrics_store_error_is_generic_and_does_not_break_dashboard(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    _, gateway, store, _ = configured(tmp_path)

    def broken():
        raise OSError("/private/path/provider-secret")

    monkeypatch.setattr(store, "snapshot", broken)
    app = create_app(gateway=gateway)
    response = asyncio.run(get(app, "/router/metrics"))
    assert response.status_code == 503
    assert response.json()["available"] is False and "provider-secret" not in response.text
    dashboard = asyncio.run(get(app, "/status/data"))
    assert dashboard.status_code == 200 and dashboard.json()["performance"]["available"] is False


def test_gateway_refresh_preserves_shared_metrics_store(tmp_path, monkeypatch):
    router, gateway, store, _ = configured(tmp_path)
    replacement = LLMRouter(router.config)

    async def bootstrap(*args, **kwargs):
        return BootstrapResult(replacement, DiscoveryReport(router.config, ()), None)

    monkeypatch.setattr("llm_router.gateway.bootstrap_router", bootstrap)
    assert asyncio.run(gateway.refresh()) is True
    assert gateway._router.metrics is store


def test_metrics_current_flags_use_one_router_snapshot_during_refresh(tmp_path):
    router, gateway, _, _ = configured(tmp_path)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))

    class RefreshDuringIteration:
        def __iter__(self):
            gateway._router = SimpleNamespace(config=SimpleNamespace(endpoints={}, models=()))
            return iter(router.config.models)

    gateway._router = SimpleNamespace(config=SimpleNamespace(
        models=RefreshDuringIteration(), endpoints=router.config.endpoints,
    ))
    snapshot = gateway.metrics_status()
    assert snapshot["deployments"][0]["current"] is True


@pytest.mark.parametrize("rollback", [False, True])
def test_runtime_update_and_rollback_never_wipe_metrics(tmp_path, monkeypatch, rollback):
    router, _, _, _ = configured(tmp_path)
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    db_path = tmp_path / "observations" / "metrics.sqlite3"
    before = db_path.read_bytes()
    install = tmp_path / "install"
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "python").touch()
    new_runtime = install / "releases" / "new" / "venv"
    (new_runtime / "bin").mkdir(parents=True)
    (new_runtime / "bin" / "python").touch()
    monkeypatch.setattr(updater, "_run", lambda *args, **kwargs: None)
    restarts = []

    def restart():
        restarts.append(True)
        if rollback and len(restarts) == 1:
            raise RuntimeError("simulated failed new runtime")

    monkeypatch.setattr(updater, "_restart_and_verify", restart)
    if rollback:
        with pytest.raises(RuntimeError, match="restored"):
            updater._activate(install, new_runtime, True)
    else:
        updater._activate(install, new_runtime, True)
    assert db_path.read_bytes() == before
    restored = MetricsStore(db_path)
    assert restored.snapshot()["deployments"][0]["successes"] == 1
    router.metrics = restored
    asyncio.run(router.complete(QueryRequest.from_prompt("Hello again")))
    assert restored.snapshot()["deployments"][0]["successes"] == 2


def test_traffic_counts_client_requests_across_failover_and_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key")
    router, gateway, store, adapter = configured(tmp_path, fail_first=True)
    app = create_app(gateway=gateway)

    async def exercise():
        headers = {"Authorization": "Bearer router-key"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", headers=headers, json={
                "model": "qwen-ha", "messages": [{"role": "user", "content": "private user prompt"}],
            })
            assert response.status_code == 200, response.text
            return (
                (await client.get("/router/metrics", headers=headers)).json(),
                (await client.get("/status/data", headers=headers)).json(),
                (await client.get("/healthz")).json(),
                (await client.get("/readyz")).json(),
            )

    api, page, health, ready = asyncio.run(exercise())
    assert adapter.calls == ["source-a", "source-b"]
    totals = api["traffic"]["totals"]
    assert totals["requests_ok"] == 1 and totals["requests_failed"] == 0
    assert totals["reroutes_ok"] == 1 and totals["failures"] == {"http_5xx": 1}
    assert totals["input_tokens"] == 100 and totals["output_tokens"] == 20
    assert api["traffic"]["windows"]["24h"] == totals
    assert page["performance"]["traffic"]["totals"] == totals
    for public in (health, ready):
        assert "traffic" not in public and "requests_ok" not in json.dumps(public)
    assert "private user prompt" not in json.dumps(api) + json.dumps(page)


def test_traffic_storage_failure_never_loses_answer_or_retries_inference(tmp_path, monkeypatch):
    router, _, store, adapter = configured(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("private path disk failure")

    monkeypatch.setattr(store, "record_request", broken)
    result = asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    assert result.text == "private generated answer"
    assert adapter.calls == ["source-a"]
