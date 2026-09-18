"""A user's save/check/restart/remove workflow through the actual HTTP handlers."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.saved_hosts import SavedHostStore, check_saved_host


def test_saved_lm_studio_address_is_fast_model_free_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-private-test-key")
    path = tmp_path / "private-state" / "saved-hosts.json"
    observed = []

    async def backend(request):
        observed.append(request)
        assert request.method == "GET"
        assert request.content == b""
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.url.host == "192.168.194.0"
        if request.url.port == 1234 and request.url.path == "/v1/models":
            # An empty model list still proves the metadata API is reachable.
            return httpx.Response(200, json={"data": []})
        assert request.url.port == 11434 and request.url.path in {"/api/version", "/api/tags"}
        raise httpx.ConnectError("Ollama is not installed here")

    transport = httpx.MockTransport(backend)

    async def checked(entry):
        return await check_saved_host(entry, transport=transport)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Saved address checks must not refresh, provision, or use a model")

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)
    for method in ("provision", "check_health"):
        monkeypatch.setattr(RouterGateway, method, forbidden)
    headers = {"Authorization": "Bearer router-private-test-key", "X-LLM-Router-Hosts": "1"}

    async def scenario():
        app = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test", headers=headers) as client:
            saved = await client.post("/status/hosts", json={"address": "192.168.194.0"})
            assert saved.status_code == 201
            identifier = saved.json()["host"]["id"]
            result = saved.json()["host"]
            assert result["checked_at"]
            assert [probe["status"] for probe in result["checks"]] == ["fail", "pass"]
            assert result["checks"][1]["base_url"] == "http://192.168.194.0:1234/v1"
            assert result["checks"][1]["catalog_status"] == "ok"
            assert result["checks"][1]["model_count"] == 0
            assert result["checks"][1]["models"] == []
            assert result["routing"]["status"] == "empty"
            assert len(observed) == 3
            cached = await client.get("/status/hosts")
            assert cached.json()["hosts"] == [result]
            assert len(observed) == 3

        restarted = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://router.test", headers=headers) as client:
            restored = (await client.get("/status/hosts")).json()["hosts"]
            assert restored[0]["id"] == identifier
            assert restored[0]["address"] == "192.168.194.0"
            assert restored[0]["checked_at"] is None
            assert restored[0]["routing"]["status"] == "pending"
            assert len(observed) == 3
            removed = await client.delete(f"/status/hosts/{identifier}")
            assert removed.status_code == 200
            assert SavedHostStore(path).list() == []

    asyncio.run(scenario())
    assert "router-private-test-key" not in path.read_text()


def test_saved_ollama_ip_lists_model_ids_and_addresses_without_inference(tmp_path, monkeypatch):
    """Regression for '192.168.42.43: Ollama Found' with no model information."""
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "catalog-private-test-key")
    path = tmp_path / "private-state" / "saved-hosts.json"
    observed = []
    catalog_available = True

    async def backend(request):
        observed.append(request)
        assert request.method == "GET"
        assert request.content == b""
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.url.host == "192.168.42.43"
        assert request.url.port == 11434
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        if request.url.path == "/api/tags":
            if not catalog_available:
                return httpx.Response(503, text="Do not expose private backend diagnostics")
            return httpx.Response(200, json={"models": [
                {"name": "qwen3:8b", "model": "qwen3:8b", "digest": "not-required"},
                {"name": "nomic-embed-text:latest", "size": 123456},
            ]})
        assert request.url.path == "/v1/models"
        return httpx.Response(404, json={"error": "Compatibility API unavailable"})

    async def checked(entry):
        return await check_saved_host(entry, transport=httpx.MockTransport(backend))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Reading model catalogs must not use models, discovery or provisioning")

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)
    for method in ("provision", "check_health"):
        monkeypatch.setattr(RouterGateway, method, forbidden)
    headers = {"Authorization": "Bearer catalog-private-test-key", "X-LLM-Router-Hosts": "1"}

    async def scenario():
        nonlocal catalog_available
        gateway = RouterGateway(discovery=False)
        app = create_app(gateway=gateway, saved_host_store=SavedHostStore(path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test", headers=headers) as client:
            saved = await client.post("/status/hosts", json={"address": "192.168.42.43:11434"})
            assert saved.status_code == 201
            identifier = saved.json()["host"]["id"]
            row = saved.json()["host"]["checks"][0]
            assert row["status"] == "pass"
            assert row["catalog_status"] == "ok"
            assert row["model_count"] == 2
            assert row["models_truncated"] is False
            assert {item["id"] for item in row["models"]} == {"qwen3:8b", "nomic-embed-text:latest"}
            assert {item["address"] for item in row["models"]} == {"http://192.168.42.43:11434"}
            assert row["catalog_url"] == "http://192.168.42.43:11434/api/tags"
            assert {model.upstream_model for model in gateway._router.config.models} == {"qwen3:8b"}
            assert saved.json()["host"]["routing"]["status"] == "active"
            tags = await client.get("/api/tags")
            assert tags.status_code == 200
            assert "qwen3-8b-ha" in {model["name"] for model in tags.json()["models"]}
            assert "nomic-embed-text-latest-ha" not in {model["name"] for model in tags.json()["models"]}
            assert {request.url.path for request in observed} == {"/api/version", "/api/tags", "/v1/models"}
            assert len(observed) == 3
            cached = (await client.get("/status/hosts")).json()["hosts"][0]["checks"][0]
            assert cached == row
            assert len(observed) == 3

            # A later failed list request must not preserve the previous success
            # or turn an unknown catalog into an apparently empty list.
            catalog_available = False
            monkeypatch.setattr(gateway_module, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + 5.0))
            response = await client.post("/status/hosts/check", json={"id": identifier})
            assert response.status_code == 200
            row = response.json()["hosts"][0]["checks"][0]
            assert row["status"] == "pass"
            assert row["catalog_status"] == "error"
            assert row["model_count"] is None
            assert row["models"] == []
            assert response.json()["hosts"][0]["routing"]["status"] == "offline"
            assert len(gateway._router.config.models) == 1
            assert not gateway._router.runtime.endpoint_available(gateway._router.config.models[0].endpoint)
            assert "private backend diagnostics" not in response.text
            assert "catalog-private-test-key" not in response.text
            assert len(observed) == 6

    asyncio.run(scenario())
    assert "qwen3" not in path.read_text()  # Models remain a cached snapshot only.
