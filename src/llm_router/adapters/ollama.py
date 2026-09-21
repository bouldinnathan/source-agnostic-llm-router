"""Ollama chat adapter for local or remote workers."""

from __future__ import annotations

from contextlib import aclosing
from typing import Any, AsyncIterator, Mapping

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import (
    FIRST_TOKEN_KEY,
    BaseHTTPAdapter,
    StreamDelta,
    final_result,
    merge_payload,
    message_text,
    neutral_tool_calls,
)


class OllamaChatAdapter(BaseHTTPAdapter):
    default_auth_scheme = "none"

    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        return await final_result(self.stream(endpoint, model, request))

    async def stream(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> AsyncIterator[StreamDelta | UpstreamResult]:
        options: dict[str, Any] = {}
        if request.max_tokens_specified:
            options["num_predict"] = request.max_tokens
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.min_context_window is not None:
            # A client's context-window request (Home Assistant's num_ctx) is
            # both a routing constraint and what Ollama should actually use.
            options["num_ctx"] = request.min_context_window
        messages: list[dict[str, Any]] = []
        pending_names: list[str] = []
        for raw in request.messages:
            message = dict(raw)
            calls = neutral_tool_calls(message)
            if calls:
                ollama_calls: list[dict[str, Any]] = []
                for call in calls:
                    function = call["function"]
                    name = str(function["name"])
                    pending_names.append(name)
                    ollama_calls.append(
                        {
                            "function": {
                                "name": name,
                                "arguments": dict(function.get("arguments", {})),
                            }
                        }
                    )
                message["tool_calls"] = ollama_calls
            if message.get("role") == "tool" and not message.get("tool_name"):
                if pending_names:
                    message["tool_name"] = pending_names.pop(0)
                message.pop("tool_call_id", None)
            messages.append(message)

        streaming = endpoint.options.get("stream", True) is not False
        body: dict[str, Any] = {
            "model": model.upstream_model,
            "messages": messages,
            "stream": streaming,
            "options": options,
        }
        if request.keep_alive is not None:
            body["keep_alive"] = request.keep_alive
        if request.think is not None:
            body["think"] = request.think
        if request.tools:
            body["tools"] = [dict(tool) for tool in request.tools]
        if request.response_format is not None:
            response_format = request.response_format
            if response_format.get("type") == "json_object":
                body["format"] = "json"
            elif response_format.get("type") == "json_schema" and isinstance(
                response_format.get("json_schema"), Mapping
            ):
                schema = response_format["json_schema"].get("schema")
                body["format"] = dict(schema) if isinstance(schema, Mapping) else "json"
            else:
                body["format"] = dict(response_format)
        path = str(endpoint.options.get("path", "/api/chat"))
        payload = merge_payload(request.extra_body, body)
        if not streaming:
            yield self._result(dict(await self.post_json(endpoint, path, payload)))
            return
        async with aclosing(self.stream_json(endpoint, path, payload, stream_format="ollama-ndjson")) as items:
            async for item in items:
                yield item if isinstance(item, StreamDelta) else self._result(dict(item))

    @staticmethod
    def _result(data: dict[str, Any]) -> UpstreamResult:
        first_token_ms = data.pop(FIRST_TOKEN_KEY, None)
        message = data.get("message")
        if not isinstance(message, Mapping):
            raise UpstreamError("Ollama response has no message", retryable=False)
        tool_calls = tuple(neutral_tool_calls(message))
        text = message_text(message.get("content"))
        if not text and not tool_calls:
            if data.get("done_reason") == "length" or message.get("thinking"):
                raise UpstreamError(
                    "Ollama response ended before any answer text: the model used its whole output "
                    "budget on reasoning; raise or remove the output token limit (num_predict)",
                    retryable=False,
                )
            raise UpstreamError("Ollama response has neither text nor tool calls", retryable=False)
        usage = {
            key: data[key]
            for key in ("prompt_eval_count", "eval_count", "total_duration")
            if key in data
        }
        return UpstreamResult(
            text=text,
            usage=usage,
            finish_reason=str(data["done_reason"]) if data.get("done_reason") else None,
            raw=data,
            tool_calls=tool_calls,
            first_token_ms=first_token_ms if isinstance(first_token_ms, (int, float)) else None,
        )
