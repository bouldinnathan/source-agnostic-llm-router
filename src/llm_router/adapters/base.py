"""Adapter protocol and shared HTTP/authentication helpers."""

from __future__ import annotations

import asyncio
import os
import re
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class StreamTimeouts:
    """Limits for a streamed upstream answer, each measured where it matters.

    ``first_token`` covers model loading and prompt evaluation, during which a
    backend is silent. ``idle`` is the longest pause allowed between tokens once
    generation has started. ``total`` caps the whole answer; ``None`` means no cap.
    """

    first_token: float = 300.0
    idle: float = 90.0
    total: float | None = None


# The router sets this before each attempt; adapters read it. A context variable
# keeps the Adapter protocol unchanged for custom adapters.
STREAM_TIMEOUTS: ContextVar[StreamTimeouts] = ContextVar("llm_router_stream_timeouts", default=StreamTimeouts())
FIRST_TOKEN_KEY = "_router_first_token_ms"
_MAX_STREAM_LINE = 1024 * 1024
_MAX_STREAM_BYTES = 64 * 1024 * 1024


def _clean_upstream_text(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else ""
    text = "".join(char for char in text if char.isprintable())
    return text[:limit]


class _NdjsonAggregator:
    """Fold Ollama's newline-delimited chat chunks into one final response object."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.calls: list[Mapping[str, Any]] = []
        self.final: dict[str, Any] | None = None
        self.done = False

    def feed(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return False
        try:
            chunk = json.loads(line)
        except ValueError as exc:
            raise UpstreamError("Upstream returned invalid JSON", retryable=False, kind="invalid_response") from exc
        if not isinstance(chunk, Mapping):
            raise UpstreamError("Upstream JSON chunk is not an object", retryable=False, kind="invalid_response")
        if chunk.get("error"):
            raise UpstreamError(f"Upstream reported an error while streaming: {_clean_upstream_text(chunk['error'])}")
        carried = False
        message = chunk.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str) and content:
                self.text.append(content)
                carried = True
            thinking = message.get("thinking")
            if isinstance(thinking, str) and thinking:
                self.thinking.append(thinking)
                carried = True
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                self.calls.extend(call for call in calls if isinstance(call, Mapping))
                carried = True
        if chunk.get("done"):
            self.final = dict(chunk)
            self.done = True
        return carried

    def result(self) -> dict[str, Any]:
        final = dict(self.final or {"done": True})
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text)}
        if self.thinking:
            message["thinking"] = "".join(self.thinking)
        if self.calls:
            message["tool_calls"] = list(self.calls)
        final["message"] = message
        return final


class _SseAggregator:
    """Fold OpenAI-compatible chat.completion.chunk events into one completion object."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish: str | None = None
        self.usage: Mapping[str, Any] | None = None
        self.meta: dict[str, Any] = {}
        self.done = False

    def feed(self, line: str) -> bool:
        line = line.rstrip("\r")
        if not line or line.startswith(":") or not line.startswith("data:"):
            return False  # comments, event/id lines, and blank separators carry nothing
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return False
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise UpstreamError("Upstream returned invalid JSON", retryable=False, kind="invalid_response") from exc
        if not isinstance(chunk, Mapping):
            raise UpstreamError("Upstream JSON chunk is not an object", retryable=False, kind="invalid_response")
        if chunk.get("error"):
            error = chunk["error"]
            detail = error.get("message") if isinstance(error, Mapping) else error
            raise UpstreamError(f"Upstream reported an error while streaming: {_clean_upstream_text(detail)}")
        for key in ("id", "model", "created", "system_fingerprint"):
            if key in chunk and key not in self.meta:
                self.meta[key] = chunk[key]
        if isinstance(chunk.get("usage"), Mapping):
            self.usage = chunk["usage"]
        carried = False
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            choice = choices[0]
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    self.text.append(content)
                    carried = True
                for key in ("reasoning_content", "reasoning"):
                    reasoning = delta.get(key)
                    if isinstance(reasoning, str) and reasoning:
                        self.reasoning.append(reasoning)
                        carried = True
                calls = delta.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, Mapping):
                            continue
                        index = call.get("index")
                        if not isinstance(index, int) or isinstance(index, bool):
                            index = len(self.calls)
                        entry = self.calls.setdefault(index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                        if call.get("id"):
                            entry["id"] = str(call["id"])
                        function = call.get("function")
                        if isinstance(function, Mapping):
                            name = function.get("name")
                            if isinstance(name, str) and name and not entry["function"]["name"]:
                                entry["function"]["name"] = name
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                entry["function"]["arguments"] += arguments
                        carried = True
            if choice.get("finish_reason"):
                self.finish = str(choice["finish_reason"])
        return carried

    def result(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text)}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.calls:
            message["tool_calls"] = [
                {**entry, "id": entry["id"] or f"call_{index}"} for index, entry in sorted(self.calls.items())
            ]
        result: dict[str, Any] = {**self.meta, "object": "chat.completion", "choices": [
            {"index": 0, "message": message, "finish_reason": self.finish},
        ]}
        if self.usage is not None:
            result["usage"] = dict(self.usage)
        return result


def _stream_timeout_error(*, first_token_seen: bool, timeouts: StreamTimeouts, capped: bool) -> UpstreamError:
    if capped and timeouts.total is not None:
        reason = f"Upstream answer exceeded the request cap of {timeouts.total:g} s"
    elif not first_token_seen:
        reason = f"Upstream produced no first token within {timeouts.first_token:g} s (model loading or prompt evaluation)"
    else:
        reason = f"Upstream produced no token for {timeouts.idle:g} s during generation"
    return UpstreamError(reason, kind="timeout")


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
        stream_format: str | None = None,
    ) -> Mapping[str, Any]:
        """POST JSON and return the decoded object.

        With ``stream_format`` ("ollama-ndjson" or "openai-sse") the backend is
        read as a token stream and folded back into the same object shape a
        non-streamed call returns, so callers parse one thing. Streaming lets
        the timeouts in :data:`STREAM_TIMEOUTS` apply to silences rather than to
        the whole answer, and records time to first token under
        :data:`FIRST_TOKEN_KEY`.
        """
        if not endpoint.base_url:
            raise UpstreamError(
                f"Endpoint '{endpoint.name}' has no base_url", retryable=False, kind="configuration"
            )
        headers, params = self.connection_metadata(endpoint, default_headers or {})
        url = endpoint.base_url.rstrip("/") + "/" + path.lstrip("/")
        if stream_format is not None:
            return await self._post_stream(endpoint, url, payload, headers, params, stream_format)
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
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"Upstream network failure: {type(exc).__name__}", kind="timeout") from exc
        except httpx.NetworkError as exc:
            raise UpstreamError(f"Upstream network failure: {type(exc).__name__}", kind="connection") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream HTTP failure: {type(exc).__name__}", kind="connection") from exc

        try:
            decoded = response.json()
        except ValueError as exc:
            raise UpstreamError("Upstream returned invalid JSON", retryable=False, kind="invalid_response") from exc
        if not isinstance(decoded, Mapping):
            raise UpstreamError("Upstream JSON response is not an object", retryable=False, kind="invalid_response")
        return decoded

    async def _post_stream(
        self,
        endpoint: EndpointConfig,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        params: Mapping[str, str],
        stream_format: str,
    ) -> dict[str, Any]:
        timeouts = STREAM_TIMEOUTS.get()
        started = time.monotonic()
        deadline = None if timeouts.total is None else started + timeouts.total
        aggregator: _NdjsonAggregator | _SseAggregator = (
            _NdjsonAggregator() if stream_format == "ollama-ndjson" else _SseAggregator()
        )
        first_token_at: float | None = None

        def budget(limit: float) -> float:
            remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
            return max(0.0, remaining)

        def timed_out() -> UpstreamError:
            capped = deadline is not None and time.monotonic() >= deadline
            return _stream_timeout_error(first_token_seen=first_token_at is not None, timeouts=timeouts, capped=capped)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(endpoint.timeout_seconds, read=None),
                verify=endpoint.verify_tls,
                follow_redirects=False,
            ) as client:
                request = client.build_request("POST", url, json=dict(payload), headers=dict(headers), params=dict(params))
                try:
                    response = await asyncio.wait_for(client.send(request, stream=True), budget(timeouts.first_token))
                except asyncio.TimeoutError:
                    raise timed_out() from None
                try:
                    if response.status_code >= 400:
                        await response.aread()
                        status = response.status_code
                        retryable = status in {408, 409, 425, 429} or status >= 500
                        raise UpstreamError(f"Upstream returned HTTP {status}", status_code=status, retryable=retryable)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type == "application/json":
                        # The backend (or a proxy in front of it) ignored ``stream`` and
                        # answered in one piece. Treat the whole body as the final chunk.
                        try:
                            await asyncio.wait_for(response.aread(), budget(timeouts.first_token))
                        except asyncio.TimeoutError:
                            raise timed_out() from None
                        if len(response.content) > _MAX_STREAM_BYTES:
                            raise UpstreamError("Upstream response is too large", retryable=False, kind="invalid_response")
                        try:
                            whole = json.loads(response.content)
                        except ValueError as exc:
                            raise UpstreamError("Upstream returned invalid JSON", retryable=False, kind="invalid_response") from exc
                        if not isinstance(whole, Mapping):
                            raise UpstreamError("Upstream returned a non-object JSON body", retryable=False, kind="invalid_response")
                        result = dict(whole)
                        result[FIRST_TOKEN_KEY] = round((time.monotonic() - started) * 1000, 2)
                        return result
                    lines = response.aiter_lines()
                    received = 0
                    while not aggregator.done:
                        limit = timeouts.idle if first_token_at is not None else timeouts.first_token
                        try:
                            line = await asyncio.wait_for(lines.__anext__(), budget(limit))
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise timed_out() from None
                        received += len(line)
                        if len(line) > _MAX_STREAM_LINE or received > _MAX_STREAM_BYTES:
                            raise UpstreamError("Upstream stream is too large", retryable=False, kind="invalid_response")
                        if aggregator.feed(line) and first_token_at is None:
                            first_token_at = time.monotonic()
                finally:
                    await response.aclose()
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"Upstream network failure: {type(exc).__name__}", kind="timeout") from exc
        except httpx.NetworkError as exc:
            raise UpstreamError(f"Upstream network failure: {type(exc).__name__}", kind="connection") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream HTTP failure: {type(exc).__name__}", kind="connection") from exc
        result = aggregator.result()
        result[FIRST_TOKEN_KEY] = None if first_token_at is None else round((first_token_at - started) * 1000, 2)
        return result

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
                f"Endpoint '{endpoint.name}' requires auth.key_env", retryable=False, kind="configuration"
            )
        secret = os.environ.get(auth.key_env)
        if not secret:
            raise UpstreamError(
                f"Missing credential environment variable {auth.key_env}", retryable=False, kind="configuration"
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
                retryable=False, kind="configuration",
            )
        return headers, params


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise UpstreamError(
                f"Missing environment variable {name} used by an endpoint header",
                retryable=False, kind="configuration",
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
