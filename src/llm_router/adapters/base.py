"""Adapter protocol and shared HTTP/authentication helpers."""

from __future__ import annotations

import os
import re
import json
from typing import Any, Mapping, Protocol

import httpx

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Adapter(Protocol):
    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult: ...


class BaseHTTPAdapter:
    default_auth_scheme = "bearer"
    default_auth_header = "Authorization"
    default_auth_prefix = "Bearer "
    default_auth_query_param = "key"

    async def post_json(
        self,
        endpoint: EndpointConfig,
        path: str,
        payload: Mapping[str, Any],
        *,
        default_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        if not endpoint.base_url:
            raise UpstreamError(
                f"Endpoint '{endpoint.name}' has no base_url", retryable=False
            )
        headers, params = self.connection_metadata(endpoint, default_headers or {})
        url = endpoint.base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=endpoint.timeout_seconds,
                verify=endpoint.verify_tls,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, json=dict(payload), headers=headers, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = status in {408, 409, 425, 429} or status >= 500
            raise UpstreamError(
                f"Upstream returned HTTP {status}",
                status_code=status,
                retryable=retryable,
            ) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(f"Upstream network failure: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream HTTP failure: {type(exc).__name__}") from exc

        try:
            decoded = response.json()
        except ValueError as exc:
            raise UpstreamError("Upstream returned invalid JSON", retryable=False) from exc
        if not isinstance(decoded, Mapping):
            raise UpstreamError("Upstream JSON response is not an object", retryable=False)
        return decoded

    def connection_metadata(
        self,
        endpoint: EndpointConfig,
        default_headers: Mapping[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        headers = {key: _expand_env(value) for key, value in default_headers.items()}
        headers.update({key: _expand_env(value) for key, value in endpoint.headers.items()})
        params: dict[str, str] = {}
        auth = endpoint.auth
        scheme = (auth.scheme or self.default_auth_scheme).lower()
        if scheme == "none":
            return headers, params
        if not auth.key_env:
            raise UpstreamError(
                f"Endpoint '{endpoint.name}' requires auth.key_env", retryable=False
            )
        secret = os.environ.get(auth.key_env)
        if not secret:
            raise UpstreamError(
                f"Missing credential environment variable {auth.key_env}", retryable=False
            )
        if scheme == "query":
            params[auth.query_param or self.default_auth_query_param] = secret
        elif scheme in {"bearer", "header", "api-key", "x-api-key"}:
            header = auth.header or self.default_auth_header
            if auth.prefix is not None:
                prefix = auth.prefix
            elif scheme == "bearer":
                prefix = self.default_auth_prefix
            else:
                prefix = ""
            headers[header] = prefix + secret
        else:
            raise UpstreamError(
                f"Endpoint '{endpoint.name}' has unsupported auth scheme '{scheme}'",
                retryable=False,
            )
        return headers, params


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise UpstreamError(
                f"Missing environment variable {name} used by an endpoint header",
                retryable=False,
            )
        return resolved

    return _ENV_PATTERN.sub(replace, value)


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return str(content) if content is not None else ""


def merge_payload(extra: Mapping[str, Any], protected: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(extra)
    payload.update(protected)
    return payload


def tool_arguments(value: Any) -> dict[str, Any]:
    """Normalize provider-specific function arguments to a JSON object."""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {"raw": value}
        return dict(decoded) if isinstance(decoded, Mapping) else {"value": decoded}
    return {} if value is None else {"value": value}


def neutral_tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read OpenAI/Ollama-style tool calls into the router's neutral shape."""

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        call_id = raw.get("id")
        calls.append(
            {
                "id": str(call_id) if call_id else f"call_{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": tool_arguments(function.get("arguments")),
                },
            }
        )
    return calls


def openai_messages(messages: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    """Convert neutral/Ollama history into valid OpenAI chat messages."""

    converted: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    pending_by_name: dict[str, list[str]] = {}
    used_ids: set[str] = set()
    call_counter = 0
    for raw in messages:
        message = dict(raw)
        role = str(message.get("role", "user"))
        calls = neutral_tool_calls(message)
        if role == "assistant" and calls:
            openai_calls: list[dict[str, Any]] = []
            for call in calls:
                call_id = str(call.get("id") or f"call_{call_counter}")
                while call_id in used_ids:
                    call_id = f"call_{call_counter}"
                    call_counter += 1
                used_ids.add(call_id)
                call_counter += 1
                function = call["function"]
                name = str(function["name"])
                pending_ids.append(call_id)
                pending_by_name.setdefault(name, []).append(call_id)
                openai_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                function.get("arguments", {}), separators=(",", ":")
                            ),
                        },
                    }
                )
            message["tool_calls"] = openai_calls
        elif role == "tool":
            name = message.get("tool_name") or message.get("name")
            call_id = message.get("tool_call_id")
            if not call_id and isinstance(name, str) and pending_by_name.get(name):
                call_id = pending_by_name[name].pop(0)
                if call_id in pending_ids:
                    pending_ids.remove(call_id)
            if not call_id and pending_ids:
                call_id = pending_ids.pop(0)
            message["tool_call_id"] = str(call_id or f"call_result_{call_counter}")
            if isinstance(name, str) and name:
                message["name"] = name
            message.pop("tool_name", None)
        converted.append(message)
    return converted


def openai_tools(tools: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    """Copy the common OpenAI/Ollama function-tool schema."""

    return [dict(tool) for tool in tools]
