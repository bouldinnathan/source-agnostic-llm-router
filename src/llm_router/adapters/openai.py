"""OpenAI-compatible Chat Completions and Responses adapters."""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import (
    FIRST_TOKEN_KEY,
    BaseHTTPAdapter,
    merge_payload,
    message_text,
    neutral_tool_calls,
    openai_messages,
    openai_tools,
    tool_arguments,
)


class OpenAIChatAdapter(BaseHTTPAdapter):
    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        body: dict[str, Any] = {
            "model": model.upstream_model,
            "messages": openai_messages(request.messages),
        }
        if request.max_tokens_specified:
            body[str(endpoint.options.get("max_tokens_field", "max_tokens"))] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = openai_tools(request.tools)
        if request.response_format is not None:
            body["response_format"] = dict(request.response_format)
        streaming = endpoint.options.get("stream", True) is not False
        if streaming:
            body["stream"] = True
            if endpoint.options.get("stream_usage", True) is not False:
                body["stream_options"] = {"include_usage": True}
        payload = merge_payload(request.extra_body, body)
        data = dict(await self.post_json(
            endpoint,
            str(endpoint.options.get("path", "/chat/completions")),
            payload,
            default_headers={"Content-Type": "application/json"},
            stream_format="openai-sse" if streaming else None,
        ))
        first_token_ms = data.pop(FIRST_TOKEN_KEY, None)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise UpstreamError("OpenAI-compatible response has no choices", retryable=False)
        choice = choices[0]
        message = choice.get("message", {})
        if not isinstance(message, Mapping):
            raise UpstreamError("OpenAI-compatible choice has no message", retryable=False)
        tool_calls = tuple(neutral_tool_calls(message))
        text = message_text(message.get("content"))
        if not text and not tool_calls:
            if choice.get("finish_reason") == "length" or message.get("reasoning_content") or message.get("reasoning"):
                raise UpstreamError(
                    "OpenAI-compatible response ended before any answer text: the model used its whole "
                    "output budget on reasoning; raise or remove the output token limit (max_tokens)",
                    retryable=False,
                )
            raise UpstreamError(
                "OpenAI-compatible response has neither text nor tool calls", retryable=False
            )
        return UpstreamResult(
            text=text,
            usage=data.get("usage", {}) if isinstance(data.get("usage"), Mapping) else {},
            finish_reason=str(choice["finish_reason"]) if choice.get("finish_reason") else None,
            raw=data,
            tool_calls=tool_calls,
            first_token_ms=first_token_ms if isinstance(first_token_ms, (int, float)) else None,
        )


class OpenAIResponsesAdapter(BaseHTTPAdapter):
    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        body: dict[str, Any] = {
            "model": model.upstream_model,
            "input": [dict(message) for message in request.messages],
        }
        if request.max_tokens_specified:
            body["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [dict(tool) for tool in request.tools]
        if request.response_format is not None:
            response_format = request.response_format
            if response_format.get("type") == "json_schema" and isinstance(
                response_format.get("json_schema"), Mapping
            ):
                body["text"] = {
                    "format": {
                        "type": "json_schema",
                        **dict(response_format["json_schema"]),
                    }
                }
            else:
                body["text"] = {"format": dict(response_format)}
        data = await self.post_json(
            endpoint,
            str(endpoint.options.get("path", "/responses")),
            merge_payload(request.extra_body, body),
            default_headers={"Content-Type": "application/json"},
        )
        text = data.get("output_text")
        if not isinstance(text, str):
            chunks: list[str] = []
            output = data.get("output", [])
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, Mapping):
                        continue
                    content = item.get("content", [])
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                                chunks.append(str(part["text"]))
            text = "\n".join(chunks)
        tool_calls: list[dict[str, Any]] = []
        output = data.get("output", [])
        if isinstance(output, list):
            for index, item in enumerate(output):
                if not isinstance(item, Mapping) or item.get("type") != "function_call":
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or f"call_{index}"),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": tool_arguments(item.get("arguments")),
                        },
                    }
                )
        if not text and not tool_calls:
            raise UpstreamError(
                "Responses-compatible response has neither text nor tool calls",
                retryable=False,
            )
        return UpstreamResult(
            text=text or "",
            usage=data.get("usage", {}) if isinstance(data.get("usage"), Mapping) else {},
            finish_reason=str(data["status"]) if data.get("status") else None,
            raw=data,
            tool_calls=tuple(tool_calls),
        )
