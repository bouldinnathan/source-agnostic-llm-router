"""Publication failures and queued saves must not leak or strand saved routes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.errors import ConfigError
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.saved_hosts import SavedHostStore

from test_saved_host_enrollment import catalog, client, gateway


PRIVATE_ERROR = "private-publication-secret /private/config/router.toml"


@pytest.fixture(autouse=True)
def bounded_metadata_only(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "enrollment-test-key")
    monkeypatch.setattr(gateway_module, "SAVED_HOST_CHECK_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(gateway_module, "SAVED_HOST_REFRESH_SECONDS", 60.0)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Saved enrollment must not infer, provision, or use real networking")

    async def checked(entry):
        return catalog(entry)

    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    monkeypatch.setattr(RouterGateway, "provision", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(gateway_module, "check_saved_host", checked)


def assert_safe_storage_error(response):
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"error"}
    for secret in ("private-publication-secret", "/private/config", "enrollment-test-key"):
        assert secret not in response.text
        assert secret not in str(response.headers)


async def wait_for_models(service, expected):
    async def ready():
        while service._router is None or {model.upstream_model for model in service._router.config.models} != expected:
            await asyncio.sleep(0.002)
    await asyncio.wait_for(ready(), 1.0)


@pytest.mark.parametrize("exception_type", [ValueError, ConfigError, TypeError])
@pytest.mark.parametrize("operation", ["check", "delete"])
def test_publication_errors_are_sanitized_json_for_check_and_delete(tmp_path, monkeypatch, exception_type, operation):
    async def scenario():
        service = gateway()
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app), client(app) as http:
            saved = await http.post("/status/hosts", json={"address": "worker.invalid:11434"})
            assert saved.status_code == 201
            identifier = saved.json()["host"]["id"]
            assert service._router.config.models

            async def broken(results):
                raise exception_type(PRIVATE_ERROR)

            monkeypatch.setattr(service, "apply_saved_hosts", broken)
            response = (
                await http.post("/status/hosts/check", json={})
                if operation == "check" else await http.delete("/status/hosts/" + identifier)
            )
            assert_safe_storage_error(response)
            assert bool(store.list()) is (operation == "check")
            assert "private-publication-secret" not in store.path.read_text()
            listed = await http.get("/status/hosts")
            assert listed.status_code == 200
            assert "private-publication-secret" not in listed.text
            if operation == "check":
                row = listed.json()["hosts"][0]
                assert row["routing"]["status"] == "error"
                assert row["routing"]["model_count"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("exception_type", [ValueError, ConfigError, TypeError])
def test_successfully_saved_address_returns_201_and_safe_routing_error_on_publish_failure(tmp_path, monkeypatch, exception_type):
    async def scenario():
        service = gateway()
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app), client(app) as http:
            async def broken(results):
                raise exception_type(PRIVATE_ERROR)

            monkeypatch.setattr(service, "apply_saved_hosts", broken)
            response = await http.post("/status/hosts", json={"address": "worker.invalid:11434"})
            assert response.status_code == 201
            assert response.headers["cache-control"] == "no-store"
            row = response.json()["host"]
            assert row["checked_at"] and row["checks"]
            assert row["routing"]["status"] == "error"
            assert row["routing"]["model_count"] == 0
            assert store.list() == [{"id": row["id"], "address": "http://worker.invalid:11434"}]
            assert service._router is None
            for secret in ("private-publication-secret", "/private/config", "enrollment-test-key"):
                assert secret not in response.text
                assert secret not in store.path.read_text()

    asyncio.run(scenario())


def test_failed_last_host_deletion_is_republished_without_any_remaining_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_module, "SAVED_HOST_REFRESH_SECONDS", 0.01)
    check_calls = []

    async def checked(entry):
        check_calls.append(entry["id"])
        return catalog(entry)

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)

    async def scenario():
        service = gateway()
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app), client(app) as http:
            saved = await http.post("/status/hosts", json={"address": "worker.invalid:11434"})
            identifier = saved.json()["host"]["id"]
            assert service._router.config.models
            original = service.apply_saved_hosts
            empty_publications = 0

            async def fail_once(results):
                nonlocal empty_publications
                if not results:
                    empty_publications += 1
                    if empty_publications == 1:
                        assert service._router.config.models, "Simulate failure before old routes are pruned"
                        raise ConfigError(PRIVATE_ERROR)
                await original(results)

            monkeypatch.setattr(service, "apply_saved_hosts", fail_once)
            deleted = await http.delete("/status/hosts/" + identifier)
            assert_safe_storage_error(deleted)
            assert store.list() == []
            assert (await http.get("/status/hosts")).json()["hosts"] == []
            probes_after_delete = len(check_calls)
            await wait_for_models(service, set())
            assert empty_publications >= 2
            assert len(check_calls) == probes_after_delete, "Revocation retry has no targets to probe"
            assert service._router.config.endpoints == {}
            assert service._saved_discovery.config.endpoints == {}
            await service.refresh()
            assert service._router.config.models == ()
            assert service._router.config.endpoints == {}

    asyncio.run(scenario())


def test_cancelled_last_host_deletion_still_retries_empty_route_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_module, "SAVED_HOST_REFRESH_SECONDS", 0.01)

    async def scenario():
        service = gateway()
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app), client(app) as http:
            saved = await http.post("/status/hosts", json={"address": "worker.invalid:11434"})
            identifier = saved.json()["host"]["id"]
            original = service.apply_saved_hosts
            first_empty = asyncio.Event()
            never_release = asyncio.Event()
            empty_publications = 0

            async def blocked_once(results):
                nonlocal empty_publications
                if not results:
                    empty_publications += 1
                    if empty_publications == 1:
                        first_empty.set()
                        await never_release.wait()
                await original(results)

            monkeypatch.setattr(service, "apply_saved_hosts", blocked_once)
            pending = asyncio.create_task(http.delete("/status/hosts/" + identifier))
            try:
                await asyncio.wait_for(first_empty.wait(), 1.0)
                assert store.list() == []
            finally:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await wait_for_models(service, set())
            assert empty_publications >= 2
            assert service._saved_discovery.config.models == ()
            assert (await http.get("/status/hosts")).json()["hosts"] == []

    asyncio.run(scenario())


def test_save_queued_during_other_scan_runs_after_it_without_waiting_periodic_timer(tmp_path, monkeypatch):
    # The test finishes long before this timer. A lost Event wake-up cannot pass
    # merely because a periodic retry happened to rescue the second address.
    monkeypatch.setattr(gateway_module, "SAVED_HOST_REFRESH_SECONDS", 60.0)
    monkeypatch.setattr(gateway_module, "SAVED_HOST_CHECK_COOLDOWN_SECONDS", 0.015)

    async def scenario():
        started, release, wake_consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class ObservedWakeEvent(asyncio.Event):
            def clear(self):
                super().clear()
                wake_consumed.set()

        async def checked(entry):
            calls.append(entry["address"])
            if len(calls) == 1:
                started.set()
                await release.wait()
            name = "first-model:8b" if "first.invalid" in entry["address"] else "second-model:8b"
            return catalog(entry, (name,))

        monkeypatch.setattr(gateway_module, "check_saved_host", checked)
        service = gateway()
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        # Observe only create_app's wake event, not global asyncio behavior while
        # requests and real locks/tasks execute.
        with monkeypatch.context() as creation:
            creation.setattr(gateway_module.asyncio, "Event", ObservedWakeEvent)
            app = create_app(gateway=service, saved_host_store=store)
        async with app.router.lifespan_context(app), client(app) as http:
            pending = asyncio.create_task(http.post("/status/hosts", json={"address": "first.invalid:11434"}))
            try:
                await asyncio.wait_for(started.wait(), 1.0)
                second = await http.post("/status/hosts", json={"address": "second.invalid:11434"})
                assert second.status_code == 201
                assert second.json()["host"]["routing"]["status"] == "pending"
                await asyncio.wait_for(wake_consumed.wait(), 1.0)
                assert calls == ["http://first.invalid:11434"]
            finally:
                release.set()
                first = await pending
            assert first.status_code == 201
            await wait_for_models(service, {"first-model:8b", "second-model:8b"})
            assert "http://second.invalid:11434" in calls
            assert {entry["address"] for entry in store.list()} == {
                "http://first.invalid:11434", "http://second.invalid:11434",
            }
            assert all(row["routing"]["status"] == "active" for row in (await http.get("/status/hosts")).json()["hosts"])
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "llm-router-saved-hosts"]

    asyncio.run(scenario())


def test_cancelled_startup_saved_scan_stops_already_started_service_tasks(tmp_path, monkeypatch):
    async def scenario():
        started, stopped, never_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def checked(entry):
            started.set()
            try:
                await never_release.wait()
            finally:
                stopped.set()
            return catalog(entry)

        monkeypatch.setattr(gateway_module, "check_saved_host", checked)
        store = SavedHostStore(tmp_path / "private" / "hosts.json")
        entry = store.add("worker.invalid:11434")
        service = gateway()
        app = create_app(gateway=service, saved_host_store=store)
        lifespan = app.router.lifespan_context(app)
        startup = asyncio.create_task(lifespan.__aenter__())
        health_task = None
        try:
            await asyncio.wait_for(started.wait(), 1.0)
            health_task = service._health_task
            assert health_task is not None and not health_task.done()
        finally:
            startup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await startup
        assert stopped.is_set(), "Cancellation must reach the in-flight metadata scan"
        assert health_task is not None and health_task.done()
        assert service._health_task is None
        assert service._refresh_task is None
        assert service._provision_task is None
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("llm-router-")]
        assert store.list() == [entry], "Cancelled startup must not discard the saved destination"
        assert service._router is None, "An unfinished check must not publish a routing catalog"

    asyncio.run(scenario())
