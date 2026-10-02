"""The gateway as one router of a fleet: fleet listing, pinned forwarding, hop limits."""

from __future__ import annotations

import asyncio

import pytest

from llm_router import peers
from llm_router.adapters import AdapterRegistry
from llm_router.adapters.base import HOP_HEADER, REQUEST_HOPS
from llm_router.config import config_from_mapping
from llm_router.errors import UpstreamError
from llm_router.gateway import create_app
from llm_router.router import LLMRouter
from llm_router.schema import UpstreamResult

from test_gateway import StaticGateway, request


class PeerAdapter:
    """Records the hop count adapters see; answers like a peer gateway would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, endpoint, model, query):  # type: ignore[no-untyped-def]
        self.calls.append((endpoint.name, model.upstream_model, REQUEST_HOPS.get()))
        if model.upstream_model == "deployment:dead":
            raise UpstreamError("simulated outage")
        raw = {"router": {"deployment": "qwen-a", "endpoint": "ollama-a", "upstream_model": "qwen3:14b"}} if endpoint.name.startswith("peer-") else {}
        return UpstreamResult(text="answer", usage={"prompt_tokens": 3, "completion_tokens": 1}, raw=raw)


def fleet() -> tuple[LLMRouter, PeerAdapter]:
    config = config_from_mapping({
        "endpoints": {
            "local-ollama": {"adapter": "ollama-chat", "base_url": "http://127.0.0.1:11434", "machine_id": "laptop"},
            "peer-b-golemframe": {
                "adapter": "ollama-chat", "base_url": "http://router-b.test:8088", "machine_id": "golemframe",
                "health_path": "/router/machines/golemframe/healthz",
                "options": {"peer": "b", "peer_origin": "router-b", "peer_via": ["router-b"], "peer_backend": "http://golemframe:11434"},
            },
        },
        "models": [
            {"id": "local:qwen", "endpoint": "local-ollama", "upstream_model": "qwen3:8b", "quality": 0.5, "capabilities": {"general": 0.8}},
            {"id": "peer-b-golemframe:qwen-a", "endpoint": "peer-b-golemframe", "upstream_model": "deployment:qwen-a", "replica_group": "qwen3:14b", "quality": 0.9, "context_window": 40960, "capabilities": {"general": 0.9}, "tags": ["discovered", "peer", "peer-b"]},
            {"id": "peer-b-golemframe:dead", "endpoint": "peer-b-golemframe", "upstream_model": "deployment:dead", "replica_group": "dead-model", "quality": 0.9, "capabilities": {"general": 0.9}},
        ],
    })
    adapter = PeerAdapter()
    adapters = AdapterRegistry()
    adapters.register("ollama-chat", adapter)
    return LLMRouter(config, adapters=adapters), adapter


def test_fleet_listing_is_keyed_and_republishes_peer_paths(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "fleet-key")
    monkeypatch.setenv("LLM_ROUTER_INSTANCE_ID", "laptop")
    peers._instance = None
    router, _ = fleet()
    app = create_app(gateway=StaticGateway(router))

    assert asyncio.run(request(app, "GET", "/router/fleet")).status_code == 401
    response = asyncio.run(request(app, "GET", "/router/fleet", headers={"Authorization": "Bearer fleet-key"}))

    assert response.status_code == 200
    listing = response.json()
    assert listing["router"] == "llm-router" and listing["instance"] == "laptop"
    published = {item["id"]: item for item in listing["deployments"]}
    assert published["local:qwen"]["origin"] == "laptop" and published["local:qwen"]["via"] == ["laptop"]
    flattened = published["peer-b-golemframe:qwen-a"]
    assert flattened["origin"] == "router-b" and flattened["via"] == ["laptop", "router-b"]
    assert flattened["backend"] == "http://golemframe:11434" and flattened["replica_group"] == "qwen3:14b"
    assert flattened["machine_id"] == "golemframe" and flattened["context_window"] == 40960


def test_machine_health_answers_per_machine() -> None:
    router, _ = fleet()
    app = create_app(gateway=StaticGateway(router))

    assert asyncio.run(request(app, "GET", "/router/machines/golemframe/healthz")).status_code == 200
    assert asyncio.run(request(app, "GET", "/router/machines/elsewhere/healthz")).status_code == 404
    router.runtime.record_endpoint_probe("peer-b-golemframe", False, "peer down")
    response = asyncio.run(request(app, "GET", "/router/machines/golemframe/healthz"))
    assert response.status_code == 503 and response.json()["status"] == "unavailable"
    assert asyncio.run(request(app, "GET", "/router/machines/laptop/healthz")).status_code == 200


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_deployment_names_pin_one_deployment_without_failover(path: str) -> None:
    router, adapter = fleet()
    app = create_app(gateway=StaticGateway(router))
    body = {"stream": False, "messages": [{"role": "user", "content": "hello"}]}

    response = asyncio.run(request(app, "POST", path, json={**body, "model": "deployment:local:qwen"}))
    assert response.status_code == 200 and response.json()["model"] == "deployment:local:qwen"
    assert response.json()["router"] == {"deployment": "local:qwen", "endpoint": "local-ollama", "upstream_model": "qwen3:8b"}
    assert [(name, model) for name, model, _ in adapter.calls] == [("local-ollama", "qwen3:8b")]

    adapter.calls.clear()
    response = asyncio.run(request(app, "POST", path, json={**body, "model": "deployment:peer-b-golemframe:dead"}))
    assert response.status_code == 503, "a pinned deployment never fails over to another"
    assert [(name, model) for name, model, _ in adapter.calls] == [("peer-b-golemframe", "deployment:dead")]

    response = asyncio.run(request(app, "POST", path, json={**body, "model": "deployment:nope"}))
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_the_answer_names_what_the_peer_actually_used(path: str) -> None:
    router, _ = fleet()
    app = create_app(gateway=StaticGateway(router))
    response = asyncio.run(request(app, "POST", path, json={
        "model": "qwen3-14b-ha", "stream": False, "messages": [{"role": "user", "content": "hello"}],
    }))
    assert response.status_code == 200
    assert response.json()["router"] == {
        "deployment": "peer-b-golemframe:qwen-a", "endpoint": "peer-b-golemframe", "upstream_model": "deployment:qwen-a",
        "via": {"deployment": "qwen-a", "endpoint": "ollama-a", "upstream_model": "qwen3:14b"},
    }


BODIES = {
    "/api/chat": {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hello"}]},
    "/v1/chat/completions": {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
    "/v1/messages": {"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
    "/v1/responses": {"model": "auto", "input": "hello"},
}


@pytest.mark.parametrize("path", sorted(BODIES))
def test_every_api_counts_hops_and_refuses_the_limit(path: str) -> None:
    router, adapter = fleet()
    app = create_app(gateway=StaticGateway(router))

    assert asyncio.run(request(app, "POST", path, json=BODIES[path])).status_code == 200
    assert adapter.calls[-1][2] == 1, "a request from a client is forwarded as the first hop"
    assert asyncio.run(request(app, "POST", path, json=BODIES[path], headers={HOP_HEADER: "1"})).status_code == 200
    assert adapter.calls[-1][2] == 2, "a request from a peer is forwarded one hop further"

    adapter.calls.clear()
    response = asyncio.run(request(app, "POST", path, json=BODIES[path], headers={HOP_HEADER: "3"}))
    assert response.status_code == 400 and "not forwarded again" in response.text
    assert adapter.calls == [], "a request at the hop limit never reaches a backend"
