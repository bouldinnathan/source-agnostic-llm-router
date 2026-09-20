"""Dashboard routing settings endpoint: authentication, persistence, live effect."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.routing_settings import RoutingSettings, RoutingSettingsStore

from conftest import make_config


AUTH = {"Authorization": "Bearer router-test-key"}
MUTATE = {**AUTH, "X-LLM-Router-Settings": "1"}


async def request(app, method="GET", path="/status/settings", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
        return await client.request(method, path, **kwargs)


def replica_router() -> LLMRouter:
    config = make_config(models=[
        {"id": "qwen-a", "endpoint": "source-a", "upstream_model": "qwen", "replica_group": "qwen"},
        {"id": "qwen-b", "endpoint": "source-b", "upstream_model": "qwen", "replica_group": "qwen"},
    ])
    config = replace(config, endpoints={
        name: replace(endpoint, machine_id=machine)
        for (name, endpoint), machine in zip(config.endpoints.items(), ("golemframe", "192.168.194.10"))
    })
    return LLMRouter(config)


@pytest.fixture(autouse=True)
def protect_models_and_network(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Routing settings must not use models, discovery, or real networking")

    for name in ("refresh", "check_health", "provision"):
        monkeypatch.setattr(RouterGateway, name, forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    monkeypatch.setattr(gateway_module, "probe_endpoints", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    tmp_path.chmod(0o700)
    store = RoutingSettingsStore(tmp_path / "routing-settings.json")
    gateway = RouterGateway(discovery=False, routing_settings_store=store)
    gateway._router = replica_router()
    gateway._router.settings = gateway.routing_settings
    return create_app(gateway=gateway), gateway, store


def model_names(app, path):  # type: ignore[no-untyped-def]
    payload = asyncio.run(request(app, "GET", path, headers=AUTH)).json()
    return [item["model"] for item in payload["models"]] if path == "/api/tags" else [item["id"] for item in payload["data"]]


def test_defaults_and_get_are_reported_without_side_effects(setup):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    response = asyncio.run(request(app, headers=AUTH))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "settings": RoutingSettings().to_dict(),
        "storage": {"available": True, "error": None},
        "races": [],
    }
    assert not store.path.exists(), "Reading settings never creates the file"
    page = asyncio.run(request(app, "GET", "/status/data", headers=AUTH)).json()
    assert page["routing"] == response.json()
    assert {row["name"]: row["advertised"] for row in page["aliases"]} == {
        "qwen-ha": True, "qwen-golemframe": True, "qwen-golemframe-nofailover": True,
        "qwen-192-168-194-10": True, "qwen-192-168-194-10-nofailover": True,
    }


def test_hiding_machine_aliases_trims_client_lists_but_keeps_names_resolvable(setup):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    assert "qwen-192-168-194-10" in model_names(app, "/api/tags")
    response = asyncio.run(request(app, "POST", headers=MUTATE, json={"advertise_machine_aliases": False}))
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["advertise_machine_aliases"] is False
    assert RoutingSettingsStore(store.path).load().advertise_machine_aliases is False, "Saved before applied"
    assert gateway._router.settings.advertise_machine_aliases is False, "Applied to the live router without restart"
    for path in ("/api/tags", "/v1/models"):
        names = model_names(app, path)
        assert "qwen-ha" in names and "auto" in names
        assert not any("192-168-194-10" in name or name.endswith("-nofailover") or name == "qwen-golemframe" for name in names), names
    show = asyncio.run(request(app, "POST", "/api/show", headers=AUTH, json={"model": "qwen-192-168-194-10-nofailover"}))
    assert show.status_code == 200, "A hidden name still resolves for clients that already use it"
    page = asyncio.run(request(app, "GET", "/status/data", headers=AUTH)).json()
    assert {row["name"] for row in page["aliases"] if not row["advertised"]} == {
        "qwen-golemframe", "qwen-golemframe-nofailover", "qwen-192-168-194-10", "qwen-192-168-194-10-nofailover",
    }
    assert page["counts"]["aliases"] == 5, "The dashboard still lists every generated name"


def test_settings_persist_across_gateway_restart_and_router_replacement(setup):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    asyncio.run(request(app, "POST", headers=MUTATE, json={"prefer_fastest_replica": True, "race_replicas": True, "race_every": 3}))
    restarted = RouterGateway(discovery=False, routing_settings_store=RoutingSettingsStore(store.path))
    assert restarted.routing_settings == RoutingSettings(prefer_fastest_replica=True, race_replicas=True, race_every=3)
    restarted._router = replica_router()
    restarted._router.settings = restarted.routing_settings
    assert restarted._router.ranker.prefer_fastest is True
    assert restarted.routing_status()["settings"]["race_every"] == 3


@pytest.mark.parametrize("body,fragment", [
    ({"race_every": 1}, "race_every"), ({"race_every": "twenty"}, "race_every"),
    ({"advertise_machine_aliases": "no"}, "true or false"), ({"unknown": True}, "Unexpected routing settings request fields"),
    (["list"], "Unexpected routing settings request fields"), ({}, None),
])
def test_invalid_bodies_change_nothing(setup, body, fragment):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    response = asyncio.run(request(app, "POST", headers=MUTATE, json=body))
    if fragment is None:
        assert response.status_code == 200 and response.json()["settings"] == RoutingSettings().to_dict()
        assert RoutingSettingsStore(store.path).load() == RoutingSettings()
        return
    assert response.status_code == 400
    assert fragment in response.json()["error"]
    assert not store.path.exists()
    assert gateway.routing_settings == RoutingSettings()


@pytest.mark.parametrize("key", [None, "", "  "])
def test_settings_require_a_configured_key(setup, monkeypatch, key):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    if key is None:
        monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", key)
    for method in ("GET", "POST"):
        response = asyncio.run(request(app, method, headers=MUTATE, json={"race_replicas": True}))
        assert response.status_code == 403
    assert not store.path.exists()


@pytest.mark.parametrize("headers,status", [
    ({}, 401), ({"Authorization": "Bearer wrong", "X-LLM-Router-Settings": "1"}, 401),
    (AUTH, 403), ({**MUTATE, "Origin": "http://evil.test"}, 403),
])
def test_mutations_need_bearer_key_custom_header_and_same_origin(setup, headers, status):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    response = asyncio.run(request(app, "POST", headers=headers, json={"race_replicas": True}))
    assert response.status_code == status
    assert "router-test-key" not in response.text and "routing-settings.json" not in response.text
    assert not store.path.exists()
    assert gateway.routing_settings == RoutingSettings()
    same_origin = asyncio.run(request(app, "POST", headers={**MUTATE, "Origin": "http://router.test"}, json={"race_replicas": True}))
    assert same_origin.status_code == 200
    queried = asyncio.run(request(app, "GET", "/status/settings?race_replicas=false", headers=AUTH))
    assert queried.status_code == 400


def test_storage_failure_keeps_live_settings_unchanged_and_is_generic(setup, monkeypatch):  # type: ignore[no-untyped-def]
    app, gateway, store = setup

    def broken(settings):  # type: ignore[no-untyped-def]
        raise RuntimeError("private /path/to/settings problem")

    monkeypatch.setattr(store, "save", broken)
    response = asyncio.run(request(app, "POST", headers=MUTATE, json={"advertise_machine_aliases": False}))
    assert response.status_code == 503
    assert "private" not in response.text and "/path/to" not in response.text
    assert gateway.routing_settings == RoutingSettings()
    assert gateway._router.settings.advertise_machine_aliases is True, "Nothing applies unless it was saved"


def test_unreadable_saved_file_falls_back_to_defaults_and_reports(tmp_path):  # type: ignore[no-untyped-def]
    tmp_path.chmod(0o700)
    path = tmp_path / "routing-settings.json"
    path.write_text("corrupt")
    path.chmod(0o600)
    gateway = RouterGateway(discovery=False, routing_settings_store=RoutingSettingsStore(path))
    assert gateway.routing_settings == RoutingSettings()
    status = gateway.routing_status()
    assert status["storage"]["available"] is False and "defaults" in status["storage"]["error"]
    assert str(path) not in status["storage"]["error"]
    assert path.read_text() == "corrupt"


def test_race_history_is_exposed_only_to_the_authenticated_dashboard(setup):  # type: ignore[no-untyped-def]
    app, gateway, store = setup
    gateway._router.runtime.last_races.appendleft({
        "group": "qwen", "started_at": "2026-09-20T10:00:00+00:00", "winner": "qwen-b",
        "participants": {"qwen-b": {"endpoint": "source-b", "success": True, "latency_ms": 120.0, "kind": None}},
    })
    page = asyncio.run(request(app, "GET", "/status/data", headers=AUTH)).json()
    assert page["routing"]["races"][0]["winner"] == "qwen-b"
    for public in ("/healthz", "/readyz"):
        body = asyncio.run(request(app, "GET", public)).text
        assert "qwen-b" not in body and "races" not in body
