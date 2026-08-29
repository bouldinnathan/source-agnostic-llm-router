"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import BaseHTTPAdapter, merge_payload, message_text, neutral_tool_calls


class AnthropicMessagesAdapter(BaseHTTPAdapter):
    default_auth_scheme = "header"
    default_auth_header = "x-api-key"
    default_auth_prefix = ""

    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        system_chunks: list[str] = []
        messages: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        pending_by_name: dict[str, list[str]] = {}
        used_ids: set[str] = set()
        call_counter = 0
        for message in request.messages:
            role = str(message.get("role", "user"))
            if role == "system":
                system_chunks.append(message_text(message.get("content")))
                continue
            blocks: list[dict[str, Any]] = []
            text = message_text(message.get("content"))
            if role == "assistant":
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in neutral_tool_calls(message):
                    function = call["function"]
                    call_id = str(call["id"])
                    while call_id in used_ids:
                        call_id = f"call_{call_counter}"
                        call_counter += 1
                    used_ids.add(call_id)
                    call_counter += 1
                    name = str(function["name"])
                    pending_ids.append(call_id)
                    pending_by_name.setdefault(name, []).append(call_id)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": dict(function.get("arguments", {})),
                        }
                    )
                _append_anthropic_message(messages, "assistant", blocks)
                continue
            if role == "tool":
                name = message.get("tool_name") or message.get("name")
                call_id = message.get("tool_call_id")
                if not call_id and isinstance(name, str) and pending_by_name.get(name):
                    call_id = pending_by_name[name].pop(0)
                    if call_id in pending_ids:
                        pending_ids.remove(call_id)
                if not call_id and pending_ids:
                    call_id = pending_ids.pop(0)
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(call_id or "call_result"),
                        "content": text,
                    }
                )
                _append_anthropic_message(messages, "user", blocks)
                continue
            blocks.append({"type": "text", "text": text})
            _append_anthropic_message(messages, "user", blocks)
        body: dict[str, Any] = {
            "model": model.upstream_model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.response_format is not None:
            system_chunks.append(
                "Return only valid JSON matching this response format: "
                + json.dumps(request.response_format, separators=(",", ":"))
            )
        if system_chunks:
            body["system"] = "\n\n".join(system_chunks)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = _anthropic_tools(request.tools)
        data = await self.post_json(
            endpoint,
            str(endpoint.options.get("path", "/v1/messages")),
            merge_payload(request.extra_body, body),
            default_headers={
                "Content-Type": "application/json",
                "anthropic-version": str(endpoint.options.get("api_version", "2023-06-01")),
            },
        )
        content = data.get("content")
        if not isinstance(content, list):
            raise UpstreamError("Anthropic response has no content array", retryable=False)
        chunks = [
            str(item["text"])
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        tool_calls = tuple(
            {
                "id": str(item.get("id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(item["name"]),
                    "arguments": dict(item.get("input", {})),
                },
            }
            for index, item in enumerate(content)
            if isinstance(item, Mapping)
            and item.get("type") == "tool_use"
            and isinstance(item.get("name"), str)
            and isinstance(item.get("input", {}), Mapping)
        )
        if not chunks and not tool_calls:
            raise UpstreamError(
                "Anthropic response has neither text nor tool calls", retryable=False
            )
        return UpstreamResult(
            text="\n".join(chunks),
            usage=data.get("usage", {}) if isinstance(data.get("usage"), Mapping) else {},
            finish_reason=str(data["stop_reason"]) if data.get("stop_reason") else None,
            raw=data,
            tool_calls=tool_calls,
        )


def _append_anthropic_message(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def _anthropic_tools(tools: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        item: dict[str, Any] = {
            "name": function["name"],
            "input_schema": dict(function.get("parameters", {"type": "object"})),
        }
        if isinstance(function.get("description"), str):
            item["description"] = function["description"]
        converted.append(item)
    return converted
