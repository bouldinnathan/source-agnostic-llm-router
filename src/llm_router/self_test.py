"""Explicit, bounded backend metadata checks that never invoke a model.

These diagnostics are deliberately independent of discovery and runtime health:
running a self-test must not alter routing, load models, or call custom paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .adapters import BUILTIN_ADAPTERS
from .errors import UpstreamError
from .schema import EndpointConfig, RouterConfig


PROBE_TIMEOUT_SECONDS = 3.0
MAX_CONCURRENCY = 4
MAX_ENDPOINT_PROBES = 16
MAX_RESPONSE_BYTES = 1024 * 1024


async def run_backend_checks(
    config: RouterConfig | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Check configured services with safe GETs, without touching model state.

    Only built-in metadata APIs are supported. In particular, ``health_path``
    and custom adapter code are never used here. No supplied URL, response body,
    credential, or exception text is included in diagnostic results.
    """

    if config is None or not config.endpoints:
        return [_row("Backend services", "Configured backends", "skip", "No backends are configured.")]

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    probes_scheduled = 0

    async def check(endpoint: EndpointConfig) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            row = _row(endpoint.name, _display_target(endpoint), "fail", "Backend check failed.")
            try:
                await asyncio.wait_for(
                    _probe(endpoint, row, transport=transport),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                row["detail"] = "Backend metadata request timed out."
            except httpx.HTTPError:
                row["detail"] = "Backend connection or TLS check failed."
            except (UpstreamError, ValueError, TypeError):
                row["detail"] = "Backend authentication, address, or response is invalid."
            except Exception:
                # Adapter metadata/header errors must never expose values or
                # interrupt diagnostic results for other configured services.
                row["detail"] = "Backend metadata check could not be completed."
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            return row

    async def skipped(endpoint: EndpointConfig, detail: str) -> dict[str, Any]:
        return _row(endpoint.name, _display_target(endpoint), "skip", detail)

    pending = []
    for endpoint in config.endpoints.values():
        if _metadata_path(endpoint) is None:
            pending.append(skipped(endpoint, "No supported model-free metadata check for this adapter."))
        elif probes_scheduled >= MAX_ENDPOINT_PROBES:
            pending.append(skipped(endpoint, "Per-run backend probe limit reached."))
        else:
            probes_scheduled += 1
            pending.append(check(endpoint))
    return list(await asyncio.gather(*pending))


def _row(name: str, target: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "name": name,
        "target": target,
        "status": status,
        "detail": detail,
        "elapsed_ms": 0,
        "http_status": None,
    }


def _metadata_path(endpoint: EndpointConfig) -> tuple[str, str] | None:
    if endpoint.adapter in {"ollama", "ollama-chat"}:
        return "/api/version", "version"
    if endpoint.adapter in {"openai-chat", "openai-compatible", "openai-responses"}:
        return "/models", "data"
    if endpoint.adapter in {"anthropic", "anthropic-messages"}:
        # This is only path selection, not URL validation; validation happens
        # before creating a client or constructing a request.
        try:
            base_path = urlsplit(endpoint.base_url).path.rstrip("/")
        except ValueError:
            base_path = ""
        return ("/models" if base_path.endswith("/v1") else "/v1/models"), "data"
    if endpoint.adapter in {"gemini", "gemini-generate"}:
        return "/models", "models"
    return None


def _validated_base_url(value: str) -> tuple[str, str]:
    """Reject ambiguous URL forms before sending any configured credentials."""

    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid base URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or "\\" in value
        or "%" in parsed.netloc
        or "%" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise ValueError("Invalid base URL")
    # Accessing port rejects malformed/out-of-range values. HTTPX additionally
    # validates authority syntax and normalizes IDNA hosts for the request.
    port = parsed.port
    url = httpx.URL(value)
    if not url.is_absolute_url or not url.host:
        raise ValueError("Invalid base URL")
    host = url.host
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parsed.scheme}://{host}"
    if port is not None:
        origin += f":{port}"
    return value.rstrip("/"), origin


def _display_target(endpoint: EndpointConfig) -> str:
    try:
        _, origin = _validated_base_url(endpoint.base_url)
        return origin
    except (ValueError, TypeError, httpx.InvalidURL):
        return "Configured backend"


async def _probe(
    endpoint: EndpointConfig,
    row: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    base_url, _ = _validated_base_url(endpoint.base_url)
    path_and_field = _metadata_path(endpoint)
    if path_and_field is None:
        return  # Unsupported adapters are skipped before scheduling.
    path, field = path_and_field
    adapter = BUILTIN_ADAPTERS[endpoint.adapter]()
    default_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if endpoint.adapter in {"anthropic", "anthropic-messages"}:
        default_headers["anthropic-version"] = str(endpoint.options.get("api_version", "2023-06-01"))
    headers, params = adapter.connection_metadata(endpoint, default_headers)
    # One fresh client per request: no cookies, redirects, or proxy/netrc
    # credentials inherited from previous probes or the host environment.
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT_SECONDS,
        verify=endpoint.verify_tls,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        async with client.stream("GET", base_url + path, headers=headers, params=params) as response:
            row["http_status"] = response.status_code
            if not 200 <= response.status_code < 300:
                row["detail"] = f"Metadata API returned HTTP {response.status_code}."
                return
            # Refuse compressed payloads rather than risk an unbounded
            # decompressor allocation before the decoded-size check.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                row["detail"] = "Metadata API returned an unsupported compressed response."
                return
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    row["detail"] = "Metadata API response exceeds the self-test size limit."
                    return
                body.extend(chunk)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                row["detail"] = "Metadata API did not return valid JSON."
                return
            expected_type = str if field == "version" else list
            if not isinstance(payload, Mapping) or not isinstance(payload.get(field), expected_type):
                row["detail"] = "Metadata API response has an unexpected format."
                return
            if field == "version" and not payload[field].strip():
                row["detail"] = "Metadata API response has an unexpected format."
                return
    row["status"] = "pass"
    row["detail"] = "Metadata API reachable; no model was invoked."
