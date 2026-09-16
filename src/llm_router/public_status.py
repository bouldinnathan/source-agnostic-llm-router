"""Small public fleet counts derived only from already-cached metadata.

No configuration names, addresses, model identifiers, errors, or credentials are
returned. Counts describe known server origins and model copies, not guaranteed
online inference capacity. No router/discovery/probe method is called here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import math
from typing import Mapping
from urllib.parse import urlsplit


def _origin(value: object) -> tuple[str, str, int] | None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            host = host.encode("idna").decode("ascii").lower().removesuffix(".")
            if not host or any(char.isspace() for char in host):
                return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme.lower(), host, port
    except (ValueError, UnicodeError):
        return None


def _model_id(value: object) -> str | None:
    # Catalog IDs were validated before caching. Be defensive about malformed
    # cached/configured values without leaking any offending contents.
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _verified_time(value: object, *, now: datetime, epoch: bool = False) -> datetime | None:
    try:
        if epoch:
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                return None
            result = datetime.fromtimestamp(value, timezone.utc)
        else:
            if not isinstance(value, str) or not value or len(value) > 64:
                return None
            result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if result.tzinfo is None or result.utcoffset() is None:
                return None
            result = result.astimezone(timezone.utc)
        if result.year < 1970 or result > now:
            return None
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def public_summary(gateway, saved_results: Mapping[str, Mapping]) -> dict:
    """Return anonymous cached fleet counts without reads, requests, or probes."""
    servers: set[tuple[str, str, int]] = set()
    models: set[tuple[tuple[str, str, int], str]] = set()
    verified: list[datetime] = []
    truncated = False
    now = datetime.now(timezone.utc)

    router = getattr(gateway, "_router", None)
    config = getattr(router, "config", None)
    endpoints = getattr(config, "endpoints", {})
    origins = {}
    if isinstance(endpoints, Mapping):
        for name, endpoint in endpoints.items():
            origin = _origin(getattr(endpoint, "base_url", None))
            if origin is not None:
                origins[name] = origin
                servers.add(origin)
    configured_models = getattr(config, "models", ())
    if isinstance(configured_models, (list, tuple)):
        for model in configured_models:
            if getattr(model, "enabled", False) is not True:
                continue
            endpoint_name = getattr(model, "endpoint", None)
            if not isinstance(endpoint_name, str):
                continue
            origin = origins.get(endpoint_name)
            model_id = _model_id(getattr(model, "upstream_model", None))
            if origin is not None and model_id is not None:
                models.add((origin, model_id))

    if isinstance(saved_results, Mapping):
        for row in saved_results.values():
            if not isinstance(row, Mapping):
                continue
            checks = row.get("checks", ())
            if not isinstance(checks, (list, tuple)):
                continue
            any_success = False
            for check in checks:
                if not isinstance(check, Mapping) or check.get("status") != "pass":
                    continue
                origin = _origin(check.get("base_url"))
                if origin is None:
                    continue
                any_success = True
                servers.add(origin)
                if check.get("catalog_status") != "ok":
                    continue
                catalog_models = check.get("models", ())
                if not isinstance(catalog_models, (list, tuple)):
                    continue
                if check.get("models_truncated") is True:
                    truncated = True
                for model in catalog_models:
                    if not isinstance(model, Mapping):
                        continue
                    model_id = _model_id(model.get("id"))
                    if model_id is not None:
                        # Never use backend-returned model.address or model_count.
                        models.add((origin, model_id))
            if any_success:
                timestamp = _verified_time(row.get("checked_at"), now=now)
                if timestamp is not None:
                    verified.append(timestamp)

    discovery = getattr(gateway, "_discovery", None)
    probes = getattr(discovery, "probes", ())
    # A completed refresh can contain only failed probes (or none at all when
    # discovery is disabled). It must not claim to have verified a server.
    reachable = isinstance(probes, (tuple, list)) and any(
        getattr(probe, "reachable", None) is True for probe in probes
    )
    if discovery is not None and getattr(gateway, "_last_error", None) is None and reachable:
        timestamp = _verified_time(getattr(gateway, "_last_refresh", None), now=now, epoch=True)
        if timestamp is not None:
            verified.append(timestamp)

    return {
        "servers": len(servers),
        "models": len(models),
        "last_verified_at": max(verified).isoformat() if verified else None,
        "models_truncated": truncated,
    }
