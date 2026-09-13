"""Bounded, authenticated API health checks without loading or invoking models."""

from __future__ import annotations

import asyncio
from typing import Mapping
from urllib.parse import urlsplit

import httpx

from .adapters import BUILTIN_ADAPTERS
from .adapters.base import BaseHTTPAdapter
from .errors import UpstreamError
from .runtime import RuntimeRegistry
from .schema import EndpointConfig, RouterConfig


async def probe_endpoints(
    config: RouterConfig,
    runtime: RuntimeRegistry,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_concurrency: int = 8,
) -> dict[str, bool | None]:
    """Probe enabled deployments and discoverable endpoints once.

    ``None`` denotes endpoints without a known API health path; those retain
    inference-based circuit breaking. A successful API probe only restores
    endpoint reachability, never a model's inference circuit. Each probe uses a
    fresh HTTP client so subsequent checks resolve the configured hostname again.
    """

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be greater than zero")
    enabled = {model.endpoint for model in config.models if model.enabled}
    semaphore = asyncio.Semaphore(max_concurrency)

    async def probe(endpoint: EndpointConfig) -> tuple[str, bool | None]:
        async with semaphore:
            try:
                path, list_field = _probe_path(endpoint)
                if path is None:
                    runtime.record_endpoint_probe(endpoint.name, None)
                    return endpoint.name, None
                await asyncio.wait_for(
                    _check_endpoint(
                        endpoint,
                        path,
                        list_field,
                        timeout=config.policy.health_check_timeout_seconds,
                        transport=transport,
                    ),
                    timeout=config.policy.health_check_timeout_seconds,
                )
            except asyncio.TimeoutError:
                reachable, error = False, "Health probe timed out"
            except httpx.HTTPStatusError as exc:
                reachable, error = False, f"Health probe returned HTTP {exc.response.status_code}"
            except httpx.HTTPError as exc:
                # Exception messages may contain URLs, credentials or response bodies.
                reachable, error = False, f"Health probe HTTP failure: {type(exc).__name__}"
            except (UpstreamError, ValueError):
                reachable, error = False, "Health probe configuration or response is invalid"
            except Exception as exc:
                reachable, error = False, f"Health probe failed: {type(exc).__name__}"
            else:
                reachable, error = True, None
            runtime.record_endpoint_probe(endpoint.name, reachable, error)
            return endpoint.name, reachable

    return dict(
        await asyncio.gather(
            *(
                probe(endpoint)
                for name, endpoint in config.endpoints.items()
                if name in enabled or endpoint.discover
            )
        )
    )


def _probe_path(endpoint: EndpointConfig) -> tuple[str | None, str | None]:
    if endpoint.health_path is not None:
        return endpoint.health_path, None
    if endpoint.adapter in {"ollama", "ollama-chat"}:
        return "/api/tags", "models"
    if endpoint.adapter in {"openai-chat", "openai-compatible", "openai-responses"}:
        return "/models", "data"
    if endpoint.adapter in {"anthropic", "anthropic-messages"}:
        base_path = urlsplit(endpoint.base_url).path.rstrip("/")
        return ("/models" if base_path.endswith("/v1") else "/v1/models"), "data"
    if endpoint.adapter in {"gemini", "gemini-generate"}:
        return "/models", "models"
    return None, None


async def _check_endpoint(
    endpoint: EndpointConfig,
    path: str,
    list_field: str | None,
    *,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    if not endpoint.base_url:
        raise ValueError("Missing base URL")
    # Custom health paths are appended to the configured base, just like inference.
    # This prevents a health path from redirecting authentication to another host.
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("Health path must be a relative API path")
    adapter_type = BUILTIN_ADAPTERS.get(endpoint.adapter, BaseHTTPAdapter)
    adapter = adapter_type()
    default_headers = {"Accept": "application/json"}
    if endpoint.adapter in {"anthropic", "anthropic-messages"}:
        default_headers["anthropic-version"] = str(
            endpoint.options.get("api_version", "2023-06-01")
        )
    headers, params = adapter.connection_metadata(endpoint, default_headers)
    url = endpoint.base_url.rstrip("/") + path
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=endpoint.verify_tls,
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        if list_field is not None:
            payload = response.json()
            if not isinstance(payload, Mapping) or not isinstance(payload.get(list_field), list):
                raise ValueError("Health API response does not contain a model list")
