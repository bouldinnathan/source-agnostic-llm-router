"""Peer gateways: fleet listings, enrollment, hop counting and instance identity."""

from __future__ import annotations

import pytest

from llm_router import peers
from llm_router.adapters.base import HOP_HEADER, REQUEST_HOPS, BaseHTTPAdapter
from llm_router.aliases import build_aliases
from llm_router.config import config_from_mapping
from llm_router.peers import (
    PeerSpec, enroll_peer, fleet_listing, hops_from_header, instance_id, lan_peer_specs, machine_available,
    parse_peers,
)
from llm_router.router import LLMRouter
from llm_router.schema import AuthConfig, EndpointConfig, PolicyConfig, RouterConfig


def fleet_router() -> LLMRouter:
    return LLMRouter(config_from_mapping({
        "endpoints": {
            "ollama-a": {"adapter": "ollama-chat", "base_url": "http://golemframe.test:11434", "machine_id": "golemframe", "max_concurrent_requests": 2},
            "studio-a": {"adapter": "openai-compatible", "base_url": "http://golemframe.test:1234/v1", "machine_id": "golemframe", "auth": {"scheme": "none"}, "max_concurrent_requests": 1},
            "studio-b": {"adapter": "openai-compatible", "base_url": "http://pantheon.test:1234/v1", "machine_id": "pantheon", "auth": {"scheme": "none"}},
        },
        "models": [
            {"id": "qwen-a", "endpoint": "ollama-a", "upstream_model": "qwen3:14b", "quality": 0.84, "context_window": 40960, "estimated_latency_ms": 1500, "capabilities": {"general": 0.9, "tool_use": 0.9}, "tags": ["discovered", "local", "ollama"]},
            {"id": "qwen-a-studio", "endpoint": "studio-a", "upstream_model": "qwen3-14b", "quality": 0.84, "context_window": 32768, "capabilities": {"general": 0.9}},
            {"id": "big-b", "endpoint": "studio-b", "upstream_model": "gpt-oss-120b", "quality": 0.92, "context_window": 131072, "capabilities": {"general": 0.95, "tool_use": 0.9}},
            {"id": "off", "endpoint": "studio-b", "upstream_model": "disabled-model", "enabled": False},
        ],
    }))


def identity(monkeypatch, name: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_INSTANCE_ID", name)
    peers._instance = None


def test_fleet_listing_publishes_enabled_deployments_with_real_metadata(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    identity(monkeypatch, "router-b")
    router = fleet_router()
    router.runtime.record_endpoint_probe("studio-b", False, "down")

    listing = fleet_listing(router, version="9.9.9")

    assert listing["router"] == "llm-router" and listing["instance"] == "router-b" and listing["version"] == "9.9.9"
    published = {item["id"]: item for item in listing["deployments"]}
    assert set(published) == {"qwen-a", "qwen-a-studio", "big-b"}, "disabled deployments are not published"
    qwen = published["qwen-a"]
    assert qwen["machine_id"] == "golemframe" and qwen["context_window"] == 40960 and qwen["backend"] == "http://golemframe.test:11434"
    assert qwen["max_concurrent_requests"] == 2 and qwen["capabilities"]["tool_use"] == 0.9 and qwen["available"] is True
    assert qwen["origin"] == "router-b" and qwen["via"] == ["router-b"]
    assert published["big-b"]["available"] is False


def test_enroll_peer_flattens_machines_and_pins_deployments(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    identity(monkeypatch, "router-b")
    listing = fleet_listing(fleet_router(), version="x")
    peer = PeerSpec(name="b", base_url="http://router-b.test:8088", key_env="PEER_KEY")

    enrolled = {endpoint.name: (endpoint, models) for endpoint, models in enroll_peer(listing, peer, own_instance="laptop")}

    assert set(enrolled) == {"peer-b-golemframe", "peer-b-pantheon"}
    golemframe, golemframe_models = enrolled["peer-b-golemframe"]
    assert golemframe.base_url == "http://router-b.test:8088" and golemframe.adapter == "ollama-chat"
    assert golemframe.auth == AuthConfig(key_env="PEER_KEY", scheme="bearer")
    assert golemframe.machine_id == "golemframe" and golemframe.health_path == "/router/machines/golemframe/healthz"
    assert golemframe.max_concurrent_requests == 3, "two servers on one machine each generate their own share"
    assert golemframe.discover is True
    assert golemframe.options["peer"] == "b" and golemframe.options["peer_origin"] == "router-b"
    assert golemframe.options["peer_via"] == ("router-b",) and golemframe.options["peer_backend"] == "http://golemframe.test:11434"
    models = {model.id: model for model in golemframe_models}
    qwen = models["peer-b-golemframe:qwen-a"]
    assert qwen.upstream_model == "deployment:qwen-a" and qwen.replica_group == "qwen3:14b"
    assert qwen.context_window == 40960 and qwen.quality == 0.84 and qwen.capabilities["tool_use"] == 0.9
    assert qwen.estimated_latency_ms == 1650, "one more hop is estimated to cost a little"
    assert qwen.tags == ("discovered", "peer", "peer-b", "local", "ollama")

    config = RouterConfig(endpoints={name: endpoint for name, (endpoint, _) in enrolled.items()},
                          models=tuple(model for _, group in enrolled.values() for model in group), policy=PolicyConfig())
    aliases = build_aliases(config)
    assert set(aliases["qwen3-14b-ha"].deployment_ids) == {"peer-b-golemframe:qwen-a", "peer-b-golemframe:qwen-a-studio"}
    assert "qwen3-14b-golemframe" in aliases and "gpt-oss-120b-pantheon-nofailover" in aliases
    assert not any(name.startswith("auto") or "-ha-ha" in name for name in aliases)


def test_enroll_peer_skips_anything_that_would_loop_back() -> None:
    def deployment(id: str, **extra: object) -> dict[str, object]:
        return {"id": id, "machine_id": "m", "origin": "router-b", "via": ["router-b"], **extra}

    listing = {"router": "llm-router", "instance": "router-b", "deployments": [
        deployment("fine"),
        deployment("mine", origin="laptop"),
        deployment("through-me", via=["router-b", "laptop"]),
        deployment("disabled", enabled=False),
        {"id": 7}, "junk", {"id": "no-origin", "machine_id": "m", "via": []},
    ]}
    enrolled = enroll_peer(listing, PeerSpec("b", "http://b.test:8088", None), own_instance="laptop")
    assert [model.id for _, models in enrolled for model in models] == ["peer-b-m:fine"]
    assert enrolled[0][0].auth == AuthConfig(key_env=None, scheme="none")

    assert enroll_peer({**listing, "instance": "laptop"}, PeerSpec("b", "http://b.test:8088", None), own_instance="laptop") == []
    with pytest.raises(ValueError):
        enroll_peer({"models": []}, PeerSpec("b", "http://b.test:8088", None), own_instance="laptop")


def test_nested_paths_keep_their_identity_when_republished(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    listing = {"router": "llm-router", "instance": "router-b", "deployments": [
        {"id": "own", "machine_id": "golemframe", "origin": "router-b", "via": ["router-b"], "backend": "http://golemframe:11434"},
        {"id": "far", "machine_id": "golemframe", "origin": "router-c", "via": ["router-b", "router-c"], "backend": "http://far:11434",
         "quality": 0.7, "context_window": 8192, "replica_group": "far-model"},
    ]}
    enrolled = enroll_peer(listing, PeerSpec("b", "http://b.test:8088", None), own_instance="laptop")
    names = [endpoint.name for endpoint, _ in enrolled]
    assert names[0] == "peer-b-golemframe" and names[1].startswith("peer-b-golemframe-") and names[0] != names[1], (
        "the same machine name reached by another path is a separate endpoint"
    )

    identity(monkeypatch, "laptop")
    config = RouterConfig(endpoints={endpoint.name: endpoint for endpoint, _ in enrolled},
                          models=tuple(model for _, models in enrolled for model in models), policy=PolicyConfig())
    republished = {item["id"]: item for item in fleet_listing(LLMRouter(config), version="x")["deployments"]}
    far = republished[f"{names[1]}:far"]
    assert far["origin"] == "router-c" and far["via"] == ["laptop", "router-b", "router-c"]
    assert far["backend"] == "http://far:11434" and far["replica_group"] == "far-model"
    assert far["upstream_model"] == "deployment:far"
    assert republished[f"{names[0]}:own"]["via"] == ["laptop", "router-b"]


def test_hops_header_counts_routers_and_refuses_the_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert hops_from_header(None) == 0 and hops_from_header(" ") == 0
    assert hops_from_header("2") == 2
    for bad in ("3", "4", "999"):
        with pytest.raises(ValueError, match="not forwarded again"):
            hops_from_header(bad)
    for malformed in ("abc", "-1", "1.5", "1000"):
        with pytest.raises(ValueError):
            hops_from_header(malformed)
    monkeypatch.setenv("LLM_ROUTER_MAX_HOPS", "1")
    assert hops_from_header("0") == 0
    with pytest.raises(ValueError):
        hops_from_header("1")
    monkeypatch.setenv("LLM_ROUTER_MAX_HOPS", "nonsense")
    assert hops_from_header("2") == 2


def test_adapters_send_the_hop_header_only_while_forwarding() -> None:
    endpoint = EndpointConfig(name="x", adapter="ollama-chat", base_url="http://x.test", auth=AuthConfig(scheme="none"))
    headers, _ = BaseHTTPAdapter().connection_metadata(endpoint, {})
    assert HOP_HEADER not in headers, "health probes and discovery are not forwarded requests"
    token = REQUEST_HOPS.set(2)
    try:
        headers, _ = BaseHTTPAdapter().connection_metadata(endpoint, {"Accept": "application/json"})
    finally:
        REQUEST_HOPS.reset(token)
    assert headers[HOP_HEADER] == "2" and headers["Accept"] == "application/json"


def test_instance_id_survives_restarts(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "state" / "instance-id"
    monkeypatch.setenv("LLM_ROUTER_INSTANCE_ID_FILE", str(path))
    first = instance_id()
    assert len(first) == 32 and path.read_text().strip() == first
    peers._instance = None
    assert instance_id() == first, "a new process reads the same identity back"
    identity(monkeypatch, "named")
    assert instance_id() == "named"


def test_peer_addresses_parse_and_lan_candidates_use_the_fleet_port() -> None:
    specs = parse_peers(["b@router-b.test", "10.0.0.5:9000", "https://fleet.example/", "b@router-b.test", "x@", ""], "KEY")
    assert [(spec.name, spec.base_url, spec.key_env) for spec in specs] == [
        ("b", "http://router-b.test:8088", "KEY"),
        ("10-0-0-5-9000", "http://10.0.0.5:9000", "KEY"),
        ("fleet-example-443", "https://fleet.example:443", "KEY"),
    ]
    lan = lan_peer_specs(("192.0.2.0/30", "not a network"), 64, 8088, None, exclude={"http://192.0.2.1:8088"})
    assert [(spec.name, spec.base_url, spec.scanned) for spec in lan] == [("192-0-2-2-8088", "http://192.0.2.2:8088", True)]


def test_machine_availability_reflects_probes_and_circuits() -> None:
    router = fleet_router()
    assert machine_available(router, "golemframe") is True
    assert machine_available(router, "nowhere") is None
    router.runtime.record_endpoint_probe("ollama-a", False, "down")
    assert machine_available(router, "golemframe") is True, "the other server on that machine still answers"
    router.runtime.record_endpoint_probe("studio-a", False, "down")
    assert machine_available(router, "golemframe") is False
    for _ in range(router.config.policy.circuit_breaker_failures):
        router.runtime.record_failure("big-b", "boom")
    assert machine_available(router, "pantheon") is False, "an open circuit counts as unavailable"
