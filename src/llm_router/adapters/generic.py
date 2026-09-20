"""Configurable JSON-over-HTTP adapter for otherwise unsupported sources."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import BaseHTTPAdapter, merge_payload


class GenericJSONAdapter(BaseHTTPAdapter):
    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        options = endpoint.options
        static_body = options.get("static_body", {})
        if not isinstance(static_body, Mapping):
            raise UpstreamError("generic-json static_body must be an object", retryable=False)
        body: dict[str, Any] = deepcopy(dict(static_body))
        _set_path(body, str(options.get("model_field", "model")), model.upstream_model)
        mode = str(options.get("request_mode", "messages"))
        if mode == "messages":
            _set_path(
                body,
                str(options.get("messages_field", "messages")),
                [dict(message) for message in request.messages],
            )
        elif mode == "prompt":
            _set_path(body, str(options.get("prompt_field", "prompt")), request.prompt_text)
        else:
            raise UpstreamError(
                "generic-json request_mode must be 'messages' or 'prompt'", retryable=False
            )
        if request.max_tokens_specified:
            _set_path(body, str(options.get("max_tokens_field", "max_tokens")), request.max_tokens)
        if request.temperature is not None:
            _set_path(
                body,
                str(options.get("temperature_field", "temperature")),
                request.temperature,
            )
        data = await self.post_json(
            endpoint,
            str(options.get("path", "/generate")),
            merge_payload(request.extra_body, body),
            default_headers={"Content-Type": "application/json"},
        )
        response_path = str(options.get("response_path", "text"))
        text = _get_path(data, response_path)
        if not isinstance(text, str):
            raise UpstreamError(
                f"generic-json response_path '{response_path}' did not resolve to text",
                retryable=False,
            )
        usage_path = options.get("usage_path")
        usage = _get_path(data, str(usage_path)) if usage_path else {}
        finish_path = options.get("finish_reason_path")
        finish = _get_path(data, str(finish_path)) if finish_path else None
        return UpstreamResult(
            text=text,
            usage=usage if isinstance(usage, Mapping) else {},
            finish_reason=str(finish) if finish is not None else None,
            raw=data,
        )


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise UpstreamError("generic-json field path cannot be empty", retryable=False)
    cursor = target
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise UpstreamError(
                f"generic-json field path '{path}' crosses a non-object", retryable=False
            )
        cursor = child
    cursor[parts[-1]] = value


def _get_path(value: Any, path: str) -> Any:
    cursor = value
    for part in (item for item in path.split(".") if item):
        if isinstance(cursor, Mapping):
            if part not in cursor:
                return None
            cursor = cursor[part]
        elif isinstance(cursor, list) and part.isdigit():
            index = int(part)
            if index >= len(cursor):
                return None
            cursor = cursor[index]
        else:
            return None
    return cursor
