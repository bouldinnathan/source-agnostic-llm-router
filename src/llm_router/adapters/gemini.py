"""Google Gemini generateContent adapter."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from ..errors import UpstreamError
from ..schema import EndpointConfig, ModelConfig, QueryRequest, UpstreamResult
from .base import BaseHTTPAdapter, merge_payload, message_text, neutral_tool_calls


class GeminiGenerateContentAdapter(BaseHTTPAdapter):
    default_auth_scheme = "query"
    default_auth_query_param = "key"

    async def complete(
        self,
        endpoint: EndpointConfig,
        model: ModelConfig,
        request: QueryRequest,
    ) -> UpstreamResult:
        contents: list[dict[str, Any]] = []
        system_chunks: list[str] = []
        pending_names: list[str] = []
        for message in request.messages:
            role = str(message.get("role", "user"))
            text = message_text(message.get("content"))
            if role == "system":
                system_chunks.append(text)
                continue
            parts: list[dict[str, Any]] = []
            if text:
                parts.append({"text": text})
            if role == "assistant":
                for call in neutral_tool_calls(message):
                    function = call["function"]
                    name = str(function["name"])
                    pending_names.append(name)
                    parts.append(
                        {
                            "functionCall": {
                                "name": name,
                                "args": dict(function.get("arguments", {})),
                            }
                        }
                    )
            elif role == "tool":
                name = message.get("tool_name") or message.get("name")
                if not isinstance(name, str) or not name:
                    name = pending_names.pop(0) if pending_names else "tool"
                parts.append(
                    {
                        "functionResponse": {
                            "name": name,
                            "response": {"result": text},
                        }
                    }
                )
            if parts:
                contents.append(
                    {"role": "model" if role == "assistant" else "user", "parts": parts}
                )
        generation_config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.response_format is not None:
            generation_config["responseMimeType"] = "application/json"
            if request.response_format.get("type") == "json_schema" and isinstance(
                request.response_format.get("json_schema"), Mapping
            ):
                schema = request.response_format["json_schema"].get("schema")
                if isinstance(schema, Mapping):
                    generation_config["responseSchema"] = dict(schema)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_chunks:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_chunks)}]}
        if request.tools:
            declarations: list[dict[str, Any]] = []
            for tool in request.tools:
                function = tool.get("function")
                if not isinstance(function, Mapping) or not isinstance(
                    function.get("name"), str
                ):
                    continue
                declaration: dict[str, Any] = {
                    "name": function["name"],
                    "parameters": dict(function.get("parameters", {"type": "object"})),
                }
                if isinstance(function.get("description"), str):
                    declaration["description"] = function["description"]
                declarations.append(declaration)
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]
        path_template = str(
            endpoint.options.get("path", "/models/{model}:generateContent")
        )
        path = path_template.replace("{model}", quote(model.upstream_model, safe=""))
        data = await self.post_json(
            endpoint,
            path,
            merge_payload(request.extra_body, body),
            default_headers={"Content-Type": "application/json"},
        )
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], Mapping):
            raise UpstreamError("Gemini response has no candidates", retryable=False)
        candidate = candidates[0]
        content = candidate.get("content", {})
        parts = content.get("parts", []) if isinstance(content, Mapping) else []
        chunks = [
            str(part["text"])
            for part in parts
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        tool_calls = tuple(
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": str(function_call["name"]),
                    "arguments": dict(function_call.get("args", {})),
                },
            }
            for index, part in enumerate(parts)
            if isinstance(part, Mapping)
            and isinstance((function_call := part.get("functionCall")), Mapping)
            and isinstance(function_call.get("name"), str)
            and isinstance(function_call.get("args", {}), Mapping)
        )
        if not chunks and not tool_calls:
            raise UpstreamError("Gemini response has neither text nor tool calls", retryable=False)
        usage = data.get("usageMetadata", {})
        return UpstreamResult(
            text="\n".join(chunks),
            usage=usage if isinstance(usage, Mapping) else {},
            finish_reason=(
                str(candidate["finishReason"]) if candidate.get("finishReason") else None
            ),
            raw=data,
            tool_calls=tool_calls,
        )
