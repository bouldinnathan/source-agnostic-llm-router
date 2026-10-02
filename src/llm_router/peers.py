"""Other llm-router gateways as backends, so a fleet of routers routes as one.

A gateway publishes the deployments it can reach, its own backends and,
recursively, its peers', with their real metadata (``GET /router/fleet``).
Another router enrolls those as deployments of its own under the same model
names, so a client sees ``qwen3-14b-ha`` whether the model runs next door or
two routers away, and ``auto`` ranks the whole fleet by real quality, context
window and observed latency. A request for such a deployment is forwarded
pinned to exactly that deployment (``deployment:<id>``); failover across
replicas stays with the router that talks to the client, so its latency
figures and session affinity describe real machines.

Loops are prevented twice. At enrollment, every published deployment names the
router that owns it and the routers a request would pass through, and a router
never enrolls a deployment that would come back to itself. At request time,
``X-LLM-Router-Hops`` counts the routers a request has passed through and a
gateway refuses to forward beyond ``LLM_ROUTER_MAX_HOPS``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urlparse

from .adapters.base import HOP_HEADER
from .aliases import DEPLOYMENT_PREFIX, _slug as _alias_slug
from .schema import AuthConfig, EndpointConfig, ModelConfig

DEFAULT_MAX_HOPS = 3
DEFAULT_PEER_PORT = 8088
FLEET_PATH = "/router/fleet"
MACHINE_HEALTH_PATH = "/router/machines/{machine}/healthz"
# A forwarded request crosses one more network hop and one more router.
PEER_HOP_LATENCY_MS = 150.0
MAX_PUBLISHED_DEPLOYMENTS = 1000

_instance: str | None = None


def _slug(value: str) -> str:
    return _alias_slug(value, fallback="machine")


# --- identity and hop counting ------------------------------------------------


def instance_id() -> str:
    """A stable identity for this gateway, so peers can tell its deployments from their own.

    Taken from ``LLM_ROUTER_INSTANCE_ID``, else kept in the config directory so
    it survives restarts (a peer may still publish this router's deployments
    from before a restart), else generated for this process.
    """
    global _instance
    if _instance is not None:
        return _instance
    configured = os.environ.get("LLM_ROUTER_INSTANCE_ID", "").strip()
    if configured and len(configured) <= 128 and configured.isprintable():
        _instance = configured
        return _instance
    path = _instance_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if 8 <= len(existing) <= 128 and existing.isprintable():
            _instance = existing
            return _instance
    except OSError:
        pass
    generated = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated + "\n", encoding="utf-8")
    except OSError:
        pass
    _instance = generated
    return generated


def _instance_path() -> Path:
    override = os.environ.get("LLM_ROUTER_INSTANCE_ID_FILE")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "llm-router" / "instance-id"


def max_hops() -> int:
    """How many routers a request may pass through (``LLM_ROUTER_MAX_HOPS``)."""
    value = os.environ.get("LLM_ROUTER_MAX_HOPS", "").strip()
    try:
        parsed = int(value) if value else DEFAULT_MAX_HOPS
    except ValueError:
        parsed = DEFAULT_MAX_HOPS
    return min(16, max(1, parsed))


def hops_from_header(value: str | None) -> int:
    """Routers a request has passed through so far; refuse one that has gone far enough.

    The gateway adds one for itself before forwarding, so a request that
    arrives having passed through the limit already is a loop or a chain
    deeper than the operator allows, and is answered with an error instead of
    being forwarded again.
    """
    text = (value or "").strip()
    if not text:
        return 0
    if not text.isdigit() or len(text) > 3:
        raise ValueError(f"{HOP_HEADER} must be a small whole number")
    hops = int(text)
    limit = max_hops()
    if hops >= limit:
        raise ValueError(
            f"request has already passed through {hops} router{'s' if hops != 1 else ''} "
            f"({HOP_HEADER}) and the limit is {limit}, so it is not forwarded again; "
            "a routing loop between peers or a chain deeper than LLM_ROUTER_MAX_HOPS"
        )
    return hops


# --- what a gateway publishes ------------------------------------------------


def fleet_listing(router: Any, *, version: str) -> dict[str, Any]:
    """Every enabled deployment this gateway can reach, with real metadata, for peers."""
    me = instance_id()
    config = router.config
    runtime = router.runtime
    deployments: list[dict[str, Any]] = []
    for model in config.models:
        if not model.enabled:
            continue
        endpoint = config.endpoints.get(model.endpoint)
        if endpoint is None:
            continue
        origin = endpoint.options.get("peer_origin")
        inherited = endpoint.options.get("peer_via") or ()
        deployments.append({
            "id": model.id,
            "upstream_model": model.upstream_model,
            "replica_group": model.replica_group or model.upstream_model,
            "endpoint": endpoint.name,
            "machine_id": endpoint.machine_id or endpoint.name,
            "adapter": endpoint.adapter,
            "backend": endpoint.options.get("peer_backend") or endpoint.base_url,
            "quality": model.quality,
            "context_window": model.context_window,
            "max_output_tokens": model.max_output_tokens,
            "input_cost_per_million": model.input_cost_per_million,
            "output_cost_per_million": model.output_cost_per_million,
            "estimated_latency_ms": model.estimated_latency_ms,
            "reliability": model.reliability,
            "priority": model.priority,
            "routing_weight": model.routing_weight,
            "capabilities": dict(model.capabilities),
            "tags": list(model.tags),
            "max_concurrent_requests": endpoint.max_concurrent_requests,
            "available": bool(runtime.is_available(model.id) and runtime.endpoint_available(model.endpoint)),
            # The router that owns the backend, and the routers a request sent
            # to this gateway passes through on its way there, this one first.
            "origin": origin if isinstance(origin, str) and origin else me,
            "via": [me, *(str(item) for item in inherited)],
        })
    return {"router": "llm-router", "version": version, "instance": me, "deployments": deployments}


def machine_available(router: Any, machine: str) -> bool | None:
    """Whether any enabled deployment on that machine can take a request now; None if unknown."""
    wanted = _slug(machine)
    known = False
    for model in router.config.models:
        if not model.enabled:
            continue
        endpoint = router.config.endpoints.get(model.endpoint)
        if endpoint is None or _slug(endpoint.machine_id or endpoint.name) != wanted:
            continue
        known = True
        if router.runtime.endpoint_available(endpoint.name) and router.runtime.is_available(model.id):
            return True
    return False if known else None


# --- enrolling a peer's deployments ------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerSpec:
    name: str
    base_url: str
    key_env: str | None
    scanned: bool = False


def peer_key_env() -> str | None:
    """The environment variable holding the key peers are called with.

    ``LLM_ROUTER_PEER_KEY`` when set; otherwise this gateway's own key, which is
    the simplest fleet setup: one key everywhere.
    """
    for name in ("LLM_ROUTER_PEER_KEY", "LLM_ROUTER_GATEWAY_API_KEY"):
        if os.environ.get(name, "").strip():
            return name
    return None


def parse_peers(values: Iterable[str], key_env: str | None) -> list[PeerSpec]:
    """``name@host``, ``host``, ``host:port`` or a full URL; a bare host means port 8088."""
    specs: list[PeerSpec] = []
    names: set[str] = set()
    origins: set[str] = set()
    for raw in values:
        value = raw.strip()
        name: str | None = None
        match = re.match(r"^([^@/:]+)@(.+)$", value)
        if match:
            name, value = match.group(1).strip(), match.group(2).strip()
        explicit_scheme = "://" in value
        parsed = urlparse(value if explicit_scheme else "http://" + value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        try:
            port = parsed.port
        except ValueError:
            continue
        if port is None:
            port = (443 if parsed.scheme == "https" else 80) if explicit_scheme else DEFAULT_PEER_PORT
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        base_url = f"{parsed.scheme}://{host}:{port}"
        if base_url in origins:
            continue
        label = _slug(name or f"{parsed.hostname}-{port}")
        if label in names:
            label += "-" + hashlib.sha256(base_url.encode()).hexdigest()[:8]
        names.add(label)
        origins.add(base_url)
        specs.append(PeerSpec(name=label, base_url=base_url, key_env=key_env))
    return specs


def lan_peer_specs(
    cidrs: Iterable[str], max_hosts_per_cidr: int, port: int, key_env: str | None, *, exclude: Iterable[str] = (),
) -> list[PeerSpec]:
    """One candidate per host of the opted-in ranges, on the fleet port."""
    skip = set(exclude)
    specs: list[PeerSpec] = []
    for raw in list(cidrs)[:8]:
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        for index, address in enumerate(network.hosts()):
            if index >= max_hosts_per_cidr:
                break
            host = f"[{address}]" if address.version == 6 else str(address)
            base_url = f"http://{host}:{port}"
            if base_url in skip:
                continue
            specs.append(PeerSpec(name=_slug(f"{address}-{port}"), base_url=base_url, key_env=key_env, scanned=True))
    return specs


def enroll_peer(
    listing: Any, peer: PeerSpec, *, own_instance: str, timeout_seconds: float = 120.0,
) -> list[tuple[EndpointConfig, tuple[ModelConfig, ...]]]:
    """Turn a peer's fleet listing into endpoints (one per machine) and deployments.

    Raises ValueError when the document is not a fleet listing. Deployments the
    peer got from this very router, or that would pass back through it, are
    left out so nothing loops. A peer that turns out to be this router itself
    contributes nothing.
    """
    if not isinstance(listing, Mapping) or listing.get("router") != "llm-router":
        raise ValueError("not an llm-router fleet listing")
    deployments = listing.get("deployments")
    if not isinstance(deployments, list):
        raise ValueError("fleet listing has no deployments array")
    peer_instance = listing.get("instance")
    if not isinstance(peer_instance, str) or not peer_instance:
        raise ValueError("fleet listing names no instance")
    if peer_instance == own_instance:
        return []
    endpoints: dict[str, EndpointConfig] = {}
    identities: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    models: dict[str, list[ModelConfig]] = {}
    for raw in deployments[:MAX_PUBLISHED_DEPLOYMENTS]:
        item = _published_deployment(raw, own_instance)
        if item is None:
            continue
        machine = item["machine_id"]
        identity = (_slug(machine), item["origin"], item["via"])
        name = f"peer-{peer.name}-{identity[0]}"
        if name in identities and identities[name] != identity:
            # The same machine name reached by a different path stays a separate endpoint.
            name += "-" + hashlib.sha256(repr(identity).encode()).hexdigest()[:8]
        identities.setdefault(name, identity)
        endpoint = endpoints.get(name)
        if endpoint is None:
            endpoint = EndpointConfig(
                name=name,
                adapter="ollama-chat",
                base_url=peer.base_url,
                auth=AuthConfig(key_env=peer.key_env, scheme="bearer" if peer.key_env else "none"),
                options={
                    "peer": peer.name, "peer_instance": peer_instance, "peer_origin": item["origin"],
                    "peer_via": item["via"], "peer_machine": machine, "peer_backend": item["backend"],
                },
                timeout_seconds=timeout_seconds,
                machine_id=machine,
                discover=True,
                health_path=MACHINE_HEALTH_PATH.format(machine=quote(machine, safe="")),
                max_concurrent_requests=item["max_concurrent_requests"],
            )
        elif item["max_concurrent_requests"] is not None:
            # Separate servers on one machine each generate their own share.
            combined = (endpoint.max_concurrent_requests or 0) + item["max_concurrent_requests"]
            endpoint = replace(endpoint, max_concurrent_requests=combined)
        endpoints[name] = endpoint
        inherited = [tag for tag in item["tags"] if tag not in {"discovered", "peer"} and not tag.startswith("peer-")]
        models.setdefault(name, []).append(ModelConfig(
            id=f"{name}:{item['id']}",
            endpoint=name,
            upstream_model=DEPLOYMENT_PREFIX + item["id"],
            capabilities=item["capabilities"],
            quality=item["quality"],
            context_window=item["context_window"],
            max_output_tokens=item["max_output_tokens"],
            input_cost_per_million=item["input_cost_per_million"],
            output_cost_per_million=item["output_cost_per_million"],
            estimated_latency_ms=item["estimated_latency_ms"] + PEER_HOP_LATENCY_MS,
            reliability=item["reliability"],
            enabled=True,
            priority=item["priority"],
            routing_weight=item["routing_weight"],
            tags=tuple(dict.fromkeys(("discovered", "peer", f"peer-{peer.name}", *inherited))),
            replica_group=item["replica_group"],
        ))
    return [(endpoints[name], tuple(models[name])) for name in endpoints]


def _published_deployment(raw: Any, own_instance: str) -> dict[str, Any] | None:
    """Validate one published deployment; None for anything unusable or looping back."""
    if not isinstance(raw, Mapping) or raw.get("enabled", True) is not True:
        return None
    deployment_id = raw.get("id")
    if not isinstance(deployment_id, str) or not deployment_id.strip() or len(deployment_id) > 256:
        return None
    origin = raw.get("origin")
    via_raw = raw.get("via")
    if not isinstance(origin, str) or not origin or not isinstance(via_raw, list):
        return None
    via = tuple(str(item) for item in via_raw[:16] if isinstance(item, str) and item)
    if origin == own_instance or own_instance in via:
        return None
    machine = raw.get("machine_id")
    if not isinstance(machine, str) or not machine.strip() or len(machine) > 128:
        machine = "machine"
    capabilities: dict[str, float] = {}
    raw_capabilities = raw.get("capabilities")
    if isinstance(raw_capabilities, Mapping):
        for key, value in list(raw_capabilities.items())[:32]:
            if isinstance(key, str) and key and _is_number(value) and 0 <= value <= 1:
                capabilities[key] = float(value)
    tags_raw = raw.get("tags")
    tags = tuple(
        tag for tag in (tags_raw if isinstance(tags_raw, list) else [])[:32]
        if isinstance(tag, str) and 0 < len(tag) <= 64
    )
    group = raw.get("replica_group")
    backend = raw.get("backend")
    return {
        "id": deployment_id.strip(),
        "origin": origin,
        "via": via,
        "machine_id": machine.strip(),
        "backend": backend if isinstance(backend, str) and len(backend) <= 512 else "",
        "quality": _bounded(raw.get("quality"), 0.5),
        "context_window": _positive_int(raw.get("context_window"), 8_192),
        "max_output_tokens": _positive_int(raw.get("max_output_tokens"), 2_048),
        "input_cost_per_million": _optional_cost(raw.get("input_cost_per_million")),
        "output_cost_per_million": _optional_cost(raw.get("output_cost_per_million")),
        "estimated_latency_ms": _positive_float(raw.get("estimated_latency_ms"), 2_000.0),
        "reliability": _bounded(raw.get("reliability"), 0.95),
        "priority": raw["priority"] if isinstance(raw.get("priority"), int) and not isinstance(raw.get("priority"), bool) else 0,
        "routing_weight": _positive_float(raw.get("routing_weight"), 1.0),
        "capabilities": capabilities,
        "tags": tags,
        "replica_group": group if isinstance(group, str) and 0 < len(group) <= 256 else None,
        "max_concurrent_requests": (
            raw["max_concurrent_requests"]
            if isinstance(raw.get("max_concurrent_requests"), int)
            and not isinstance(raw.get("max_concurrent_requests"), bool)
            and raw["max_concurrent_requests"] > 0
            else None
        ),
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _bounded(value: Any, default: float) -> float:
    return float(value) if _is_number(value) and 0 <= value <= 1 else default


def _positive_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _positive_float(value: Any, default: float) -> float:
    return float(value) if _is_number(value) and value > 0 else default


def _optional_cost(value: Any) -> float | None:
    return float(value) if _is_number(value) and value >= 0 else None


__all__ = [
    "DEFAULT_MAX_HOPS",
    "DEFAULT_PEER_PORT",
    "FLEET_PATH",
    "PeerSpec",
    "enroll_peer",
    "fleet_listing",
    "hops_from_header",
    "instance_id",
    "lan_peer_specs",
    "machine_available",
    "max_hops",
    "parse_peers",
    "peer_key_env",
]
