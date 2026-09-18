"""Saved page addresses become usable routing deployments without model work."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.aliases import build_aliases
from llm_router.gateway import RouterGateway, create_app
from llm_router.provisioning import ProvisioningSettings
from llm_router.public_status import public_summary
from llm_router.router import LLMRouter
from llm_router.saved_hosts import SavedHostStore, check_saved_host
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, QueryRequest, RouterConfig


AUTH = {"Authorization": "Bearer enrollment-test-key", "X-LLM-Router-Hosts": "1"}


@pytest.fixture(autouse=True)
def no_inference_or_network(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "enrollment-test-key")
    monkeypatch.setattr(gateway_module, "SAVED_HOST_CHECK_COOLDOWN_SECONDS", 0.0)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Enrollment must not infer, provision, or use real networking")

    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    monkeypatch.setattr(RouterGateway, "provision", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def gateway():
    return RouterGateway(discovery=False, provisioning_settings=ProvisioningSettings(enabled=False))


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://router.test", headers=AUTH)


def catalog(entry, names=("qwen3-coder:30b",), *, ok=True, blocked=False):
    base = entry["address"] if "://" in entry["address"] else "http://" + entry["address"] + ":11434"
    return {**entry, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": [{
        "provider": "Ollama", "base_url": base, "status": "pass" if ok else "fail",
        "catalog_status": "ok" if ok else "error", "models": [{"id": name, "address": base} for name in names] if ok else [],
        "model_count": len(names) if ok else None, "enrollment_blocked": blocked,
    }]}


def test_user_88_model_ollama_server_is_enrolled_on_save_and_restart(tmp_path, monkeypatch):
    """The reported 503 + Found/88 models mismatch, through real HTTP handlers."""
    observed = []
    names = ["nemotron-3.5-lightning:30b", "qwen3-coder:30b"] + [f"qwen-copy-{index}:8b" for index in range(86)]

    async def backend(request):
        observed.append(request)
        assert request.method == "GET" and request.content == b""
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.url.host == "192.168.42.43"
        if request.url.port == 1234:
            raise httpx.ConnectError("LM Studio not running")
        assert request.url.port == 11434
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": name} for name in names]})

    async def checked(entry):
        return await check_saved_host(entry, transport=httpx.MockTransport(backend))

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)
    store = SavedHostStore(tmp_path / "private" / "saved-hosts.json")

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=store)
        async with client(app) as http:
            assert (await http.get("/api/tags")).status_code == 503
            saved = await http.post("/status/hosts", json={"address": "192.168.42.43"})
            assert saved.status_code == 201
            assert saved.json()["host"]["routing"]["status"] == "active"
            assert saved.json()["host"]["routing"]["model_count"] == 88
            assert len(service._router.config.models) == 88
            for path, field, id_field in (("/api/tags", "models", "name"), ("/v1/models", "data", "id")):
                response = await http.get(path)
                assert response.status_code == 200
                identifiers = {item[id_field] for item in response.json()[field]}
                assert "qwen3-coder-30b-ha" in identifiers
                assert "qwen3-coder-30b-192-168-42-43-nofailover" in identifiers
            assert len(observed) == 3, "Catalog enrollment must reuse the checker, not do 88 model detail calls"
            assert await service.refresh(), "Ordinary refresh must preserve saved models"
            assert len(service._router.config.models) == 88
            old_ids = {model.id for model in service._router.config.models}

        restored = gateway()
        new_app = create_app(gateway=restored, saved_host_store=SavedHostStore(store.path))
        async with new_app.router.lifespan_context(new_app):
            assert len(observed) == 6
            assert {model.id for model in restored._router.config.models} == old_ids
            async with client(new_app) as http:
                assert (await http.get("/api/tags")).status_code == 200
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "llm-router-saved-hosts"]

    asyncio.run(scenario())
    assert "qwen" not in store.path.read_text() and "enrollment-test-key" not in store.path.read_text()


def test_saved_models_offline_recover_and_empty_catalog_prunes(tmp_path, monkeypatch):
    state = {"ok": True, "names": ("qwen3-coder:30b",)}

    async def checked(entry):
        return catalog(entry, **state)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=SavedHostStore(tmp_path / "private" / "hosts.json"))
        async with client(app) as http:
            row = (await http.post("/status/hosts", json={"address": "192.168.42.43"})).json()["host"]
            original = service._router.config.models[0]
            runtime = service._router.runtime
            runtime.record_success(original.id, 42.0)
            state["ok"] = False
            offline = await http.post("/status/hosts/check", json={})
            assert offline.json()["hosts"][0]["routing"]["status"] == "offline"
            assert service._router.config.models == (original,)
            assert not runtime.endpoint_available(original.endpoint)
            await service.refresh()
            assert not runtime.endpoint_available(original.endpoint)
            state["ok"] = True
            await http.post("/status/hosts/check", json={})
            assert runtime.endpoint_available(original.endpoint)
            assert runtime.state(original.id).successes == 1
            state["names"] = ()
            empty = await http.post("/status/hosts/check", json={})
            assert empty.json()["hosts"][0]["routing"]["status"] == "empty"
            assert service._router.config.models == ()
            assert (await http.delete("/status/hosts/" + row["id"])).status_code == 200
            assert service._router.config.endpoints == {}
            await service.refresh()
            assert service._router.config.endpoints == {}, "Failed empty refresh must not restore removed routes"

    asyncio.run(scenario())


def test_same_model_two_saved_machines_joins_ha_and_routes_around_offline(tmp_path, monkeypatch):
    offline = set()

    async def checked(entry):
        return catalog(entry, ok=entry["address"] not in offline)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=SavedHostStore(tmp_path / "private" / "hosts.json"))
        async with client(app) as http:
            await http.post("/status/hosts", json={"address": "192.168.42.43"})
            await http.post("/status/hosts", json={"address": "192.168.42.44"})
            alias = build_aliases(service._router.config)["qwen3-coder-30b-ha"]
            assert len(alias.models) == 2
            offline.add("192.168.42.43")
            await http.post("/status/hosts/check", json={})
            query = alias.apply(QueryRequest(messages=({"role": "user", "content": "not sent"},)))
            decision = service._router.route(query)  # Ranking only, never inference.
            assert len(decision.candidates) == 1
            endpoint = service._router.config.endpoints[decision.candidates[0].model.endpoint]
            assert endpoint.machine_id == "192.168.42.44"

    asyncio.run(scenario())


def test_inflight_save_delete_readd_does_not_publish_old_generation(tmp_path, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def checked(entry):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return catalog(entry, ("stale-model:8b",))
            return catalog(entry, ("fresh-model:8b",))

        monkeypatch.setattr(gateway_module, "check_saved_host", checked)
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        service = gateway()
        app = create_app(gateway=service, saved_host_store=store)
        async with client(app) as http:
            pending = asyncio.create_task(http.post("/status/hosts", json={"address": "192.168.42.43"}))
            await asyncio.wait_for(started.wait(), 1.0)
            identifier = store.list()[0]["id"]
            await http.delete("/status/hosts/" + identifier)
            added = await http.post("/status/hosts", json={"address": "192.168.42.43"})
            assert added.json()["host"]["routing"]["status"] == "pending"
            release.set()
            await pending
            assert service._router.config.models == ()
            assert (await http.get("/status/hosts")).json()["hosts"][0]["checked_at"] is None
            await http.post("/status/hosts/check", json={})
            assert {model.upstream_model for model in service._router.config.models} == {"fresh-model:8b"}

    asyncio.run(scenario())


def test_periodic_scan_enrolls_previously_offline_saved_host_without_browser(tmp_path, monkeypatch):
    state = {"ok": False}
    calls = []

    async def checked(entry):
        calls.append(entry)
        return catalog(entry, **state)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)
    monkeypatch.setattr(gateway_module, "SAVED_HOST_REFRESH_SECONDS", 0.01)
    store = SavedHostStore(tmp_path / "private" / "hosts.json")
    store.add("laptop.home.arpa")

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app):
            assert calls and service._router.config.models == ()
            state["ok"] = True

            async def enrolled():
                while not service._router.config.models:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(enrolled(), 1.0)
            assert service._router.config.endpoints[service._router.config.models[0].endpoint].machine_id == "laptop.home.arpa"
        count = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == count

    asyncio.run(scenario())


def test_saved_updates_preserve_configured_credentials_and_disabled_models(tmp_path, monkeypatch):
    endpoint = EndpointConfig("protected", "ollama-chat", "http://192.168.42.43:11434", auth=AuthConfig(key_env="BACKEND_KEY", scheme="bearer"))
    model = ModelConfig("disabled", endpoint.name, "qwen3-coder:30b", enabled=False)
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=(model,))
    monkeypatch.setattr(gateway_module, "load_optional_config", lambda path: configured)

    async def checked(entry):
        return catalog(entry)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=SavedHostStore(tmp_path / "private" / "hosts.json"))
        async with client(app) as http:
            row = (await http.post("/status/hosts", json={"address": "192.168.42.43"})).json()["host"]
            assert row["routing"]["status"] == "managed"
            assert service._router.config == replace(configured, source_path="explicit config")
            await http.delete("/status/hosts/" + row["id"])
            assert service._router.config.models == (model,)
            assert service._router.config.endpoints == {endpoint.name: endpoint}

    asyncio.run(scenario())


def test_saved_revocation_cannot_be_restored_by_refresh_or_health(tmp_path, monkeypatch):
    state = {"blocked": False}

    async def checked(entry):
        return catalog(entry, **state)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)

    async def scenario():
        service = gateway()
        app = create_app(gateway=service, saved_host_store=SavedHostStore(tmp_path / "private" / "hosts.json"))
        async with client(app) as http:
            await http.post("/status/hosts", json={"address": "192.168.42.43"})
            assert service._router.config.models
            state["blocked"] = True
            await http.post("/status/hosts/check", json={})
            assert service._router.config.models == ()
            await service.refresh()
            await service.check_health()
            assert service._router.config.models == ()

    asyncio.run(scenario())


def test_cached_refresh_does_not_advance_saved_verification_time(tmp_path):
    entry = SavedHostStore(tmp_path / "private" / "hosts.json").add("192.168.42.43")
    row = {**catalog(entry), "checked_at": "2000-01-01T00:00:00+00:00"}
    results = {entry["id"]: row}

    async def scenario():
        service = gateway()
        await service.apply_saved_hosts(results)
        router = service._router
        endpoint = router.config.models[0].endpoint
        observed = router.runtime.snapshot(router.config.models)["endpoints"][endpoint]["last_checked_at"]
        assert observed == 946684800.0
        assert public_summary(service, results)["last_verified_at"] == row["checked_at"]
        assert await service.refresh()
        assert public_summary(service, results)["last_verified_at"] == row["checked_at"]
        assert router.runtime.snapshot(router.config.models)["endpoints"][endpoint]["last_checked_at"] == observed
        await service.apply_saved_hosts(results)
        assert router.runtime.snapshot(router.config.models)["endpoints"][endpoint]["last_checked_at"] == observed

    asyncio.run(scenario())
