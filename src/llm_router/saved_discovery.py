"""Pure enrollment of saved-address catalogs that were already checked.

This module performs no discovery requests, model detail calls, inference, or
downloads. Saved ownership is explicit so deletion can prune routes without
touching configured servers or ordinary discovery sources.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import ipaddress
from itertools import islice
from typing import Mapping
from urllib.parse import urlsplit

from .discovery import (
    DiscoveryReport, ProbeResult, ProbeSpec, _endpoint_for_probe, _is_chat_model,
    _is_private_host, _model_config, _url_identity_label,
)
from .saved_hosts import MAX_CATALOG_MODELS, MAX_HOSTS, _catalog, normalize_address
from .schema import EndpointConfig, PolicyConfig, RouterConfig


def is_saved_endpoint(endpoint: EndpointConfig) -> bool:
    """Use ownership metadata rather than guessing from human-readable names."""
    return endpoint.options.get("saved_host_source") is True


def _origin(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname.lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii").removesuffix(".")
            if not hostname or any(char.isspace() for char in hostname):
                return None
        else:
            hostname = f"[{address.compressed}]" if address.version == 6 else address.compressed
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            hostname += f":{port}"
        return f"{parsed.scheme}://{hostname}"
    except (ValueError, UnicodeError):
        return None


def _probe(origin: str, kind: str) -> ProbeSpec:
    base = origin if kind == "ollama" else origin + "/v1"
    return ProbeSpec(
        name=f"saved-{kind}-{_url_identity_label(base)}",
        provider="ollama" if kind == "ollama" else "openai-compatible",
        kind=kind,
        base_url=base,
        list_url=origin + ("/api/tags" if kind == "ollama" else "/v1/models"),
        adapter="ollama-chat" if kind == "ollama" else "openai-chat",
        local=_is_private_host(urlsplit(origin).hostname),
        timeout_seconds=1.5,
        discover=True,
    )


def _names(check: Mapping) -> tuple[str, ...] | None:
    if check.get("status") != "pass" or check.get("catalog_status") != "ok":
        return None
    entries = check.get("models")
    if not isinstance(entries, list) or len(entries) > MAX_CATALOG_MODELS:
        return None
    try:
        models, _ = _catalog({"data": entries}, ollama=False, address="")
    except (ValueError, TypeError):
        return None
    return tuple(sorted(model["id"] for model in models if _is_chat_model(model["id"], {})))


def _owned_endpoint(probe: ProbeSpec, owners: set[str]) -> EndpointConfig:
    endpoint = _endpoint_for_probe(probe)
    return replace(endpoint, options={
        **endpoint.options,
        "saved_host_source": True,
        "saved_host_ids": tuple(sorted(owners)),
    })


def _checked_at(value: object) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


def saved_hosts_report(
    results: Mapping[str, dict], previous: DiscoveryReport | None = None,
) -> DiscoveryReport:
    """Enroll bounded cached chat catalogs; retain failed catalogs offline."""
    # Each row is one explicit destination with at most two API-family checks.
    grouped: dict[str, list[tuple[str, Mapping, tuple[str, ...] | None, datetime]]] = {}
    owners: dict[str, set[str]] = {}
    active_ids: set[str] = set()
    if isinstance(results, Mapping):
        for host_id, row in islice(results.items(), MAX_HOSTS):
            if not isinstance(host_id, str) or not host_id or len(host_id) > 128 or not isinstance(row, Mapping):
                continue
            active_ids.add(host_id)
            checks = row.get("checks", ())
            if not isinstance(checks, (list, tuple)):
                continue
            for check in islice(checks, 2):
                if not isinstance(check, Mapping):
                    continue
                provider = check.get("provider")
                if provider not in {"Ollama", "LM Studio / OpenAI-compatible"}:
                    continue
                value = check.get("base_url")
                try:
                    # Unlike configured sources, saved checks can only have an
                    # origin or /v1 base and cannot carry embedded credentials.
                    canonical = normalize_address(value)
                except (TypeError, ValueError):
                    continue
                if "://" not in canonical:
                    continue
                origin = _origin(canonical)
                if origin is None:
                    continue
                kind = "ollama" if provider == "Ollama" else "openai"
                grouped.setdefault(origin, []).append((kind, check, _names(check), _checked_at(row.get("checked_at"))))
                owners.setdefault(origin, set()).add(host_id)

    prior_endpoints = {}
    if previous is not None:
        for endpoint in previous.config.endpoints.values():
            origin = _origin(endpoint.base_url)
            if is_saved_endpoint(endpoint) and origin is not None:
                old_owners = endpoint.options.get("saved_host_ids", ())
                retained_owners = active_ids.intersection(old_owners) if isinstance(old_owners, (list, tuple)) else set()
                if retained_owners:
                    prior_endpoints[origin] = endpoint
                    owners.setdefault(origin, set()).update(retained_owners)
                    grouped.setdefault(origin, [])

    endpoints = {}
    models = {}
    probes = []
    for origin, candidates in sorted(grouped.items()):
        old = prior_endpoints.get(origin)
        old_kind = "ollama" if old is not None and old.adapter in {"ollama", "ollama-chat"} else "openai"
        if candidates:
            newest = max(candidate[3] for candidate in candidates)
            candidates = [candidate for candidate in candidates if candidate[3] == newest]
        # The checker marks router proxy origins as revoked, not ordinary
        # transient failures. Never retain their formerly enrolled routes.
        blocked = any(check.get("enrollment_blocked") is True for _, check, _, _ in candidates)
        if candidates:
            kind, selected, names, _ = min(
                candidates,
                key=lambda candidate: (
                    candidate[2] is None,
                    0 if candidate[2] is not None and candidate[0] == "ollama" else 1,
                    0 if candidate[2] is None and old is not None and candidate[0] == old_kind else 1,
                    candidate[0],
                ),
            )
        else:
            kind, selected, names = old_kind, {}, None
        probe = _probe(origin, kind)
        endpoint = _owned_endpoint(probe, owners[origin])
        enrolled = []
        if blocked:
            reachable, error = False, "Router proxy endpoints cannot be enrolled as their own backends."
        elif names is not None:
            endpoints[endpoint.name] = endpoint
            for name in names:
                model = _model_config(probe, endpoint.name, name, {})
                enrolled.append(replace(model, tags=(*model.tags, "saved-host")))
            reachable = True
            error = None if enrolled else "reachable but no chat-capable models were listed"
        elif old is not None:
            # Preserve exactly the last successful source identity/catalog; a
            # failed check must not turn one physical service into a new route.
            endpoint = replace(old, options={**old.options, "saved_host_ids": tuple(sorted(owners[origin]))})
            probe = _probe(origin, old_kind)
            endpoints[endpoint.name] = endpoint
            enrolled = [model for model in previous.config.models if model.endpoint == old.name]
            reachable, error = False, "Saved-address catalog check failed; retaining the last catalog offline."
        else:
            # A version response proves a server exists, but not that any model
            # can be routed. Completely unreachable new ports are probes only.
            if selected.get("status") == "pass":
                endpoints[endpoint.name] = endpoint
            reachable, error = False, "Saved-address model catalog is unavailable."
        for model in enrolled:
            models.setdefault(model.id, model)
        probes.append(ProbeResult(
            source=probe.name, provider=probe.provider, base_url=endpoint.base_url,
            reachable=reachable, enrolled_models=len(enrolled), error=error,
            endpoint=endpoint.name,
        ))
    return DiscoveryReport(
        config=RouterConfig(
            endpoints=endpoints, models=tuple(models.values()),
            policy=previous.config.policy if previous is not None else PolicyConfig(),
            source_path="saved-address discovery",
        ),
        probes=tuple(probes),
    )


def _saved_probe(probe: ProbeResult, endpoints: Mapping[str, EndpointConfig]) -> bool:
    endpoint = endpoints.get(probe.endpoint)
    if endpoint is not None:
        return is_saved_endpoint(endpoint)
    return probe.source.startswith("saved-") and (probe.endpoint or "").startswith("auto-saved-")


def merge_saved_discovery(
    base: DiscoveryReport, saved: DiscoveryReport, configured: RouterConfig | None,
) -> DiscoveryReport:
    """Merge saved sources without overriding configured/ordinary discovery.

    Any saved-owned routes already carried in ``base`` are replaced by ``saved``;
    this makes deleting a saved address authoritative instead of resurrecting an
    older catalog during later refreshes.
    """
    configured_endpoints = configured.endpoints if configured is not None else {}
    configured_origins = {_origin(endpoint.base_url) for endpoint in configured_endpoints.values()}
    configured_origins.discard(None)
    endpoints = {}
    for name, endpoint in base.config.endpoints.items():
        if is_saved_endpoint(endpoint):
            continue
        origin = _origin(endpoint.base_url)
        explicit = configured_endpoints.get(name)
        if explicit is not None:
            if origin != _origin(explicit.base_url):
                continue
            endpoints[name] = explicit
        elif origin not in configured_origins:
            endpoints[name] = endpoint
    occupied = set(configured_origins)
    occupied.update(_origin(endpoint.base_url) for endpoint in endpoints.values())
    base_probes = [
        probe for probe in base.probes
        if not _saved_probe(probe, base.config.endpoints)
        and (
            _origin(probe.base_url) not in configured_origins
            or (
                probe.endpoint in configured_endpoints
                and _origin(probe.base_url) == _origin(configured_endpoints[probe.endpoint].base_url)
            )
        )
    ]
    accepted_saved = set()
    accepted_origins = set()
    for name, endpoint in saved.config.endpoints.items():
        origin = _origin(endpoint.base_url)
        if origin is None or origin in occupied or name in endpoints or name in configured_endpoints:
            continue
        endpoints[name] = endpoint
        accepted_saved.add(name)
        accepted_origins.add(origin)
        occupied.add(origin)

    models = {}
    pairs = set()
    for candidates, from_saved in ((base.config.models, False), (saved.config.models, True)):
        for candidate in candidates:
            if candidate.endpoint not in endpoints:
                continue
            if from_saved and candidate.endpoint not in accepted_saved:
                continue
            previous_endpoint = base.config.endpoints.get(candidate.endpoint)
            if not from_saved and previous_endpoint is not None and is_saved_endpoint(previous_endpoint):
                continue
            pair = (candidate.endpoint, candidate.upstream_model)
            if candidate.id not in models and pair not in pairs:
                models[candidate.id] = candidate
                pairs.add(pair)

    probes = []
    probe_keys = set()
    for probe in base_probes:
        if _origin(probe.base_url) in accepted_origins and probe.endpoint not in endpoints:
            continue
        key = (probe.endpoint, _origin(probe.base_url))
        if key not in probe_keys:
            probes.append(probe)
            probe_keys.add(key)
    for probe in saved.probes:
        origin = _origin(probe.base_url)
        if origin in configured_origins:
            continue
        if probe.endpoint not in accepted_saved and origin in occupied:
            continue
        key = (probe.endpoint, origin)
        if key not in probe_keys:
            probes.append(probe)
            probe_keys.add(key)
    return DiscoveryReport(
        config=RouterConfig(
            endpoints=endpoints, models=tuple(models.values()), policy=base.config.policy,
            source_path=(base.config.source_path or "automatic discovery") + " + saved addresses",
        ),
        probes=tuple(probes),
    )


__all__ = ["saved_hosts_report", "merge_saved_discovery", "is_saved_endpoint"]
