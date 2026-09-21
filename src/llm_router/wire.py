"""Inbound Anthropic Messages and OpenAI Responses wire formats.

Agent clients speak different dialects: Claude Code speaks the Anthropic
Messages API, Codex speaks the OpenAI Responses API, and most others speak
Chat Completions or Ollama. This module translates the first two into the
router's neutral request (OpenAI chat-style messages and function tools) and
renders neutral completions and fragments back in each dialect, so every
client can use every backend.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping

from .adapters.base import StreamDelta, message_text, tool_arguments
from .errors import RequestError
from .schema import RoutedCompletion, whole_number


def _text_parts(content: Any, *text_types: str) -> str:
    """Join the text of a string or a list of blocks whose type is one of ``text_types``."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, Mapping) and part.get("type") in text_types and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _content_with_images(text: str, images: list[dict[str, Any]]) -> Any:
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    parts.extend(images)
    return parts


def _temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 2 else None


def _json_response_format(text: Any) -> Mapping[str, Any] | None:
    """OpenAI Responses ``text.format`` to the neutral response format."""
    if not isinstance(text, Mapping) or not isinstance(text.get("format"), Mapping):
        return None
    fmt = text["format"]
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), Mapping):
        schema: dict[str, Any] = {"name": str(fmt.get("name") or "response"), "schema": dict(fmt["schema"])}
        if isinstance(fmt.get("strict"), bool):
            schema["strict"] = fmt["strict"]
        return {"type": "json_schema", "json_schema": schema}
    return None


def _neutral_tools(tools: Any, *, flat: bool) -> tuple[dict[str, Any], ...]:
    """Function tools in the neutral (OpenAI chat) shape; other tool types are dropped."""
    if tools is None:
        return ()
    if not isinstance(tools, list):
        raise RequestError("tools must be an array")
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise RequestError("every tool must be an object")
        if flat:
            if tool.get("type", "function") != "function":
                continue
            name, description = tool.get("name"), tool.get("description")
            parameters = tool.get("parameters")
        else:
            name, description = tool.get("name"), tool.get("description")
            parameters = tool.get("input_schema")
        if not isinstance(name, str) or not name:
            raise RequestError("every tool needs a name")
        function: dict[str, Any] = {"name": name, "parameters": dict(parameters) if isinstance(parameters, Mapping) else {"type": "object"}}
        if isinstance(description, str):
            function["description"] = description
        converted.append({"type": "function", "function": function})
    return tuple(converted)


def _query_fields(
    messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...], *, max_tokens: Any, temperature: Any,
    response_format: Mapping[str, Any] | None, has_images: bool, think: bool | None = None,
) -> dict[str, Any]:
    if not messages:
        raise RequestError("the conversation has no messages")
    required: list[str] = []
    if tools:
        required.append("tool_use")
    if response_format is not None:
        required.append("structured_output")
    if has_images:
        required.append("vision")
    limit = whole_number("max_tokens", max_tokens) if max_tokens is not None else None
    if limit is not None and limit <= 0:
        raise RequestError("max_tokens must be greater than zero")
    return {
        "messages": tuple(messages),
        "tools": tools,
        "required_capabilities": tuple(required),
        "max_tokens": limit if limit is not None else 2048,
        "max_tokens_specified": limit is not None,
        "temperature": _temperature(temperature),
        "response_format": response_format,
        "think": think,
    }


# ---------------------------------------------------------------- Anthropic


def anthropic_query_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request body into QueryRequest fields."""
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise RequestError("messages must be a non-empty array")
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    system_text = _text_parts(system, "text") if system is not None else ""
    if system_text:
        messages.append({"role": "system", "content": system_text})
    has_images = False
    for raw in raw_messages:
        if not isinstance(raw, Mapping):
            raise RequestError("every message must be an object")
        role = raw.get("role")
        if role not in {"user", "assistant"}:
            raise RequestError("message role must be user or assistant")
        content = raw.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise RequestError("message content must be a string or an array of blocks")
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                raise RequestError("every content block must be an object")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "image" and isinstance(block.get("source"), Mapping):
                source = block["source"]
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    media = str(source.get("media_type") or "image/png")
                    images.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{source['data']}"}})
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    images.append({"type": "image_url", "image_url": {"url": source["url"]}})
                has_images = has_images or bool(images)
            elif kind == "tool_use" and role == "assistant":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    raise RequestError("tool_use blocks need a name")
                calls.append({
                    "id": str(block.get("id") or f"call_{len(calls)}"), "type": "function",
                    "function": {"name": name, "arguments": tool_arguments(block.get("input"))},
                })
            elif kind == "tool_result" and role == "user":
                result = block.get("content")
                tool_messages.append({
                    "role": "tool", "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _text_parts(result, "text") if result is not None else "",
                })
            # thinking, redacted_thinking, and unknown blocks carry nothing the backend needs
        text = "\n".join(texts)
        if role == "assistant":
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = calls
            if text or calls:
                messages.append(message)
            continue
        messages.extend(tool_messages)
        if text or images:
            messages.append({"role": "user", "content": _content_with_images(text, images)})
    if "max_tokens" not in body:
        raise RequestError("max_tokens is required")
    thinking = body.get("thinking")
    think = None
    if isinstance(thinking, Mapping) and thinking.get("type") in {"enabled", "disabled"}:
        think = thinking["type"] == "enabled"
    return _query_fields(
        messages, _neutral_tools(body.get("tools"), flat=False), max_tokens=body["max_tokens"],
        temperature=body.get("temperature"), response_format=None, has_images=has_images, think=think,
    )


def anthropic_session_hint(body: Mapping[str, Any]) -> str | None:
    """Claude Code tags requests with a per-session user_id; use it when present."""
    metadata = body.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, Mapping) else None
    if isinstance(user_id, str) and user_id:
        return "anthropic:" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return None


def _anthropic_stop_reason(completion: RoutedCompletion) -> str:
    if completion.tool_calls:
        return "tool_use"
    if completion.finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def _anthropic_usage(completion: RoutedCompletion) -> dict[str, int]:
    usage = completion.usage
    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return 0
    return {
        "input_tokens": number("input_tokens", "prompt_tokens", "prompt_eval_count"),
        "output_tokens": number("output_tokens", "completion_tokens", "eval_count"),
    }


def anthropic_completion(model: str, completion: RoutedCompletion, *, message_id: str | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if completion.text:
        content.append({"type": "text", "text": completion.text})
    for index, call in enumerate(completion.tool_calls):
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        content.append({
            "type": "tool_use", "id": str(call.get("id") or f"call_{index}"),
            "name": str(function.get("name", "tool")), "input": dict(function.get("arguments", {})),
        })
    return {
        "id": message_id or "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": _anthropic_stop_reason(completion), "stop_sequence": None,
        "usage": _anthropic_usage(completion),
        "router": {"deployment": completion.deployment, "endpoint": completion.endpoint, "upstream_model": completion.upstream_model},
    }


def anthropic_error(message: str, error_type: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _sse(event: str, payload: Mapping[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


class AnthropicRelay:
    """Render router fragments as Anthropic Messages streaming events."""

    media_type = "text/event-stream"

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = "msg_" + uuid.uuid4().hex
        self.started = False
        self.block: str | None = None  # the open content block's type
        self.index = -1

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        message = {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        return [_sse("message_start", {"type": "message_start", "message": message})]

    def _open(self, kind: str, block: Mapping[str, Any]) -> list[bytes]:
        chunks = self._close()
        self.index += 1
        self.block = kind
        chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": dict(block)}))
        return chunks

    def _close(self) -> list[bytes]:
        if self.block is None:
            return []
        self.block = None
        return [_sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def heartbeat(self) -> bytes:
        return b"".join(self._start()) + _sse("ping", {"type": "ping"})

    def delta(self, fragment: StreamDelta) -> bytes:
        chunks = self._start()
        if fragment.thinking:
            if self.block != "thinking":
                chunks.extend(self._open("thinking", {"type": "thinking", "thinking": ""}))
            chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {"type": "thinking_delta", "thinking": fragment.thinking}}))
        if fragment.text:
            if self.block != "text":
                chunks.extend(self._open("text", {"type": "text", "text": ""}))
            chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {"type": "text_delta", "text": fragment.text}}))
        return b"".join(chunks)

    def finish(self, completion: RoutedCompletion, sent: int) -> list[bytes]:
        chunks = self._start()
        remainder = completion.text[sent:]
        if remainder:
            chunks.append(self.delta(StreamDelta(text=remainder)))
        chunks.extend(self._close())
        for index, call in enumerate(completion.tool_calls):
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            block = {"type": "tool_use", "id": str(call.get("id") or f"call_{index}"), "name": str(function.get("name", "tool")), "input": {}}
            chunks.extend(self._open("tool_use", block))
            arguments = json.dumps(dict(function.get("arguments", {})), separators=(",", ":"))
            chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {"type": "input_json_delta", "partial_json": arguments}}))
            chunks.extend(self._close())
        usage = _anthropic_usage(completion)
        chunks.append(_sse("message_delta", {
            "type": "message_delta", "delta": {"stop_reason": _anthropic_stop_reason(completion), "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"], "input_tokens": usage["input_tokens"]},
        }))
        chunks.append(_sse("message_stop", {"type": "message_stop"}))
        return chunks

    def error(self, message: str, error_type: str) -> bytes:
        kind = "overloaded_error" if error_type == "router_error" else "api_error"
        return b"".join(self._start()) + _sse("error", anthropic_error(message, kind))


def anthropic_token_estimate(body: Mapping[str, Any]) -> int:
    """A rough count for clients that budget context: about four characters per token."""
    characters = len(json.dumps(body.get("system", ""), ensure_ascii=False)) + len(json.dumps(body.get("messages", []), ensure_ascii=False))
    characters += len(json.dumps(body.get("tools", []), ensure_ascii=False))
    return max(1, characters // 4)


# ---------------------------------------------------------------- OpenAI Responses


def responses_query_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI Responses request body into QueryRequest fields."""
    if body.get("previous_response_id"):
        raise RequestError("previous_response_id is not supported; send the whole conversation in input")
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        raise RequestError("input must be a string or an array of items")
    has_images = False
    for item in raw_input:
        if not isinstance(item, Mapping):
            raise RequestError("every input item must be an object")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant"}:
                raise RequestError("message role must be user, assistant, system or developer")
            content = item.get("content")
            text = _text_parts(content, "input_text", "output_text", "text")
            images: list[dict[str, Any]] = []
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping) and part.get("type") == "input_image":
                        url = part.get("image_url")
                        if isinstance(url, str) and url:
                            images.append({"type": "image_url", "image_url": {"url": url}})
            has_images = has_images or bool(images)
            if text or images:
                messages.append({"role": role, "content": _content_with_images(text, images) if role == "user" else text})
        elif kind == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise RequestError("function_call items need a name")
            call = {"id": str(item.get("call_id") or item.get("id") or f"call_{len(messages)}"), "type": "function",
                    "function": {"name": name, "arguments": tool_arguments(item.get("arguments"))}}
            if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls") is not None:
                messages[-1]["tool_calls"].append(call)  # parallel calls share one assistant turn
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append({"role": "tool", "tool_call_id": str(item.get("call_id") or ""), "content": output if isinstance(output, str) else _text_parts(output, "input_text", "text")})
        # reasoning, item_reference and other item types carry nothing a backend can use
    reasoning = body.get("reasoning")
    think = None
    if isinstance(reasoning, Mapping) and isinstance(reasoning.get("effort"), str):
        think = reasoning["effort"] != "none"
    return _query_fields(
        messages, _neutral_tools(body.get("tools"), flat=True), max_tokens=body.get("max_output_tokens"),
        temperature=body.get("temperature"), response_format=_json_response_format(body.get("text")),
        has_images=has_images, think=think,
    )


def _responses_usage(completion: RoutedCompletion) -> dict[str, Any]:
    usage = _anthropic_usage(completion)
    return {
        "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
        "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0},
    }


def _responses_output(completion: RoutedCompletion, message_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if completion.text:
        output.append({
            "type": "message", "id": message_id, "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": completion.text, "annotations": []}],
        })
    for index, call in enumerate(completion.tool_calls):
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        call_id = str(call.get("id") or f"call_{index}")
        output.append({
            "type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "status": "completed",
            "name": str(function.get("name", "tool")), "arguments": json.dumps(dict(function.get("arguments", {})), separators=(",", ":")),
        })
    return output


def responses_completion(model: str, completion: RoutedCompletion, *, response_id: str | None = None, created_at: int | None = None) -> dict[str, Any]:
    response_id = response_id or "resp_" + uuid.uuid4().hex
    return {
        "id": response_id, "object": "response", "created_at": created_at or int(time.time()), "status": "completed",
        "error": None, "incomplete_details": {"reason": "max_output_tokens"} if completion.finish_reason == "length" else None,
        "model": model, "output": _responses_output(completion, "msg_" + uuid.uuid4().hex),
        "usage": _responses_usage(completion), "parallel_tool_calls": True, "store": False,
        "router": {"deployment": completion.deployment, "endpoint": completion.endpoint, "upstream_model": completion.upstream_model},
    }


def responses_error(message: str, error_type: str, *, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": None, "code": code or error_type}}


class ResponsesRelay:
    """Render router fragments as OpenAI Responses streaming events."""

    media_type = "text/event-stream"

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = "resp_" + uuid.uuid4().hex
        self.message_id = "msg_" + uuid.uuid4().hex
        self.created_at = int(time.time())
        self.sequence = 0
        self.started = False
        self.message_open = False
        self.text: list[str] = []
        self.output_index = -1

    def _event(self, kind: str, **payload: Any) -> bytes:
        self.sequence += 1
        return _sse(kind, {"type": kind, "sequence_number": self.sequence, **payload})

    def _response(self, status: str) -> dict[str, Any]:
        return {
            "id": self.id, "object": "response", "created_at": self.created_at, "status": status, "error": None,
            "incomplete_details": None, "model": self.model, "output": [], "usage": None, "parallel_tool_calls": True, "store": False,
        }

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [self._event("response.created", response=self._response("in_progress")),
                self._event("response.in_progress", response=self._response("in_progress"))]

    def _open_message(self) -> list[bytes]:
        if self.message_open:
            return []
        self.message_open = True
        self.output_index += 1
        item = {"type": "message", "id": self.message_id, "status": "in_progress", "role": "assistant", "content": []}
        return [self._event("response.output_item.added", output_index=self.output_index, item=item),
                self._event("response.content_part.added", item_id=self.message_id, output_index=self.output_index, content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []})]

    def _close_message(self) -> list[bytes]:
        if not self.message_open:
            return []
        self.message_open = False
        text = "".join(self.text)
        item = {"type": "message", "id": self.message_id, "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        return [self._event("response.output_text.done", item_id=self.message_id, output_index=self.output_index, content_index=0, text=text),
                self._event("response.content_part.done", item_id=self.message_id, output_index=self.output_index, content_index=0,
                            part={"type": "output_text", "text": text, "annotations": []}),
                self._event("response.output_item.done", output_index=self.output_index, item=item)]

    def heartbeat(self) -> bytes:
        return b"".join(self._start()) + b": keepalive\n\n"

    def delta(self, fragment: StreamDelta) -> bytes:
        chunks = self._start()
        if fragment.text:
            chunks.extend(self._open_message())
            self.text.append(fragment.text)
            chunks.append(self._event("response.output_text.delta", item_id=self.message_id, output_index=self.output_index, content_index=0, delta=fragment.text))
        # Reasoning is not relayed: Responses clients expect signed summaries the backend cannot provide.
        return b"".join(chunks)

    def finish(self, completion: RoutedCompletion, sent: int) -> list[bytes]:
        chunks = self._start()
        remainder = completion.text[sent:]
        if remainder:
            chunks.append(self.delta(StreamDelta(text=remainder)))
        chunks.extend(self._close_message())
        final = responses_completion(self.model, completion, response_id=self.id, created_at=self.created_at)
        for item in final["output"]:
            if item["type"] != "function_call":
                continue
            self.output_index += 1
            chunks.append(self._event("response.output_item.added", output_index=self.output_index, item={**item, "status": "in_progress", "arguments": ""}))
            chunks.append(self._event("response.function_call_arguments.delta", item_id=item["id"], output_index=self.output_index, delta=item["arguments"]))
            chunks.append(self._event("response.function_call_arguments.done", item_id=item["id"], output_index=self.output_index, arguments=item["arguments"]))
            chunks.append(self._event("response.output_item.done", output_index=self.output_index, item=item))
        chunks.append(self._event("response.completed", response=final))
        return chunks

    def error(self, message: str, error_type: str) -> bytes:
        return b"".join(self._start()) + self._event("error", code=error_type, message=message, param=None)
