"""Ollama chat adapter for local or remote workers."""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import BaseHTTPAdapter, merge_payload, message_text, neutral_tool_calls


class OllamaChatAdapter(BaseHTTPAdapter):
    default_auth_scheme = "none"

    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        options: dict[str, Any] = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
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

        body: dict[str, Any] = {
            "model": model.upstream_model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
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
        data = await self.post_json(
            endpoint,
            str(endpoint.options.get("path", "/api/chat")),
            merge_payload(request.extra_body, body),
        )
        message = data.get("message")
        if not isinstance(message, Mapping):
            raise UpstreamError("Ollama response has no message", retryable=False)
        tool_calls = tuple(neutral_tool_calls(message))
        text = message_text(message.get("content"))
        if not text and not tool_calls:
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
        )
