"""A user's save/check/restart/remove workflow through the actual HTTP handlers."""

from __future__ import annotations

import asyncio

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
        assert request.url.port == 11434 and request.url.path == "/api/version"
        raise httpx.ConnectError("Ollama is not installed here")

    transport = httpx.MockTransport(backend)

    async def checked(entry):
        return await check_saved_host(entry, transport=transport)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Saved address checks must not refresh, provision, or use a model")

    monkeypatch.setattr(gateway_module, "check_saved_host", checked)
    for method in ("refresh", "router", "provision", "check_health"):
        monkeypatch.setattr(RouterGateway, method, forbidden)
    headers = {"Authorization": "Bearer router-private-test-key", "X-LLM-Router-Hosts": "1"}

    async def scenario():
        app = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test", headers=headers) as client:
            saved = await client.post("/status/hosts", json={"address": "192.168.194.0"})
            assert saved.status_code == 201
            identifier = saved.json()["host"]["id"]
            assert observed == []
            checked_response = await client.post("/status/hosts/check", json={"id": identifier})
            assert checked_response.status_code == 200
            result = checked_response.json()["hosts"][0]
            assert result["checked_at"]
            assert [probe["status"] for probe in result["checks"]] == ["fail", "pass"]
            assert result["checks"][1]["base_url"] == "http://192.168.194.0:1234/v1"
            assert len(observed) == 2
            cached = await client.get("/status/hosts")
            assert cached.json()["hosts"] == [result]
            assert len(observed) == 2

        restarted = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://router.test", headers=headers) as client:
            restored = (await client.get("/status/hosts")).json()["hosts"]
            assert restored == [{"id": identifier, "address": "192.168.194.0", "checked_at": None, "checks": []}]
            assert len(observed) == 2
            removed = await client.delete(f"/status/hosts/{identifier}")
            assert removed.status_code == 200
            assert SavedHostStore(path).list() == []

    asyncio.run(scenario())
    assert "router-private-test-key" not in path.read_text()
