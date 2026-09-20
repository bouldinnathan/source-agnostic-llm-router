from __future__ import annotations

import asyncio
import json

import pytest

from llm_router.adapters.base import BaseHTTPAdapter
from llm_router.adapters.anthropic import AnthropicMessagesAdapter
from llm_router.adapters.gemini import GeminiGenerateContentAdapter
from llm_router.adapters.generic import GenericJSONAdapter
from llm_router.adapters.ollama import OllamaChatAdapter
from llm_router.adapters.openai import OpenAIChatAdapter
from llm_router.errors import UpstreamError
import httpx

from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, QueryRequest


class CapturingOpenAI(OpenAIChatAdapter):
    def __init__(self) -> None:
        self.path = ""
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.path = path
        self.payload = dict(payload)
        return {
            "choices": [
                {"message": {"content": "hello"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class CapturingGeneric(GenericJSONAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {"data": {"outputs": [{"value": "generic answer"}]}}


class CapturingToolOpenAI(OpenAIChatAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "turn_on",
                                    "arguments": '{"entity":"light.kitchen"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }


class CapturingToolOllama(OllamaChatAdapter):
    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "turn_on", "arguments": {"room": "kitchen"}}}
                ],
            },
            "done_reason": "stop",
        }


class CapturingToolAnthropic(AnthropicMessagesAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "turn_on", "input": {"room": "kitchen"}}
            ],
            "stop_reason": "tool_use",
        }


class CapturingToolGemini(GeminiGenerateContentAdapter):
    def __init__(self) -> None:
        self.payload = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "turn_on", "args": {"room": "kitchen"}}}
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }


MODEL = ModelConfig(id="test", endpoint="source", upstream_model="upstream")


def test_openai_adapter_translates_and_parses() -> None:
    adapter = CapturingOpenAI()
    endpoint = EndpointConfig(name="source", adapter="openai-chat")
    request = QueryRequest.from_prompt("hello", max_tokens=99, temperature=0.2)

    result = asyncio.run(adapter.complete(endpoint, MODEL, request))

    assert adapter.path == "/chat/completions"
    assert adapter.payload["model"] == "upstream"
    assert adapter.payload["messages"][-1]["content"] == "hello"
    assert adapter.payload["max_tokens"] == 99
    assert result.text == "hello"
    assert result.usage["completion_tokens"] == 1


def test_generic_adapter_supports_nested_paths() -> None:
    adapter = CapturingGeneric()
    endpoint = EndpointConfig(
        name="source",
        adapter="generic-json",
        options={
            "request_mode": "prompt",
            "prompt_field": "input.prompt",
            "model_field": "input.model",
            "max_tokens_field": "generation.limit",
            "response_path": "data.outputs.0.value",
        },
    )

    result = asyncio.run(
        adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("nested"))
    )

    assert adapter.payload["input"] == {"prompt": "nested", "model": "upstream"}
    assert adapter.payload["generation"]["limit"] == 1024
    assert result.text == "generic answer"


def test_auth_uses_environment_without_exposing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_TEST_KEY", "super-secret")
    endpoint = EndpointConfig(
        name="source",
        adapter="openai-chat",
        auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
    )

    headers, params = BaseHTTPAdapter().connection_metadata(endpoint, {})

    assert headers["Authorization"] == "Bearer super-secret"
    assert params == {}


def test_missing_auth_variable_has_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_ROUTER_KEY", raising=False)
    endpoint = EndpointConfig(
        name="source",
        adapter="openai-chat",
        auth=AuthConfig(key_env="MISSING_ROUTER_KEY"),
    )

    with pytest.raises(UpstreamError, match="MISSING_ROUTER_KEY") as captured:
        BaseHTTPAdapter().connection_metadata(endpoint, {})

    assert "secret" not in str(captured.value).lower()


def test_openai_tool_calls_and_ollama_history_are_normalized() -> None:
    adapter = CapturingToolOpenAI()
    endpoint = EndpointConfig(name="source", adapter="openai-chat")
    request = QueryRequest(
        messages=(
            {"role": "user", "content": "turn it on"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "turn_on", "arguments": {"room": "kitchen"}}}
                ],
            },
            {"role": "tool", "tool_name": "turn_on", "content": "done"},
        )
    )

    result = asyncio.run(adapter.complete(endpoint, MODEL, request))

    assert result.text == ""
    assert result.tool_calls[0]["function"]["arguments"] == {
        "entity": "light.kitchen"
    }
    sent_call = adapter.payload["messages"][1]["tool_calls"][0]
    assert json.loads(sent_call["function"]["arguments"]) == {"room": "kitchen"}
    assert adapter.payload["messages"][2]["tool_call_id"] == sent_call["id"]


def test_provider_tool_only_responses_are_valid() -> None:
    tools = (
        {
            "type": "function",
            "function": {
                "name": "turn_on",
                "description": "Turn on a room",
                "parameters": {"type": "object"},
            },
        },
    )
    request = QueryRequest.from_prompt("turn it on", tools=tools)

    ollama = asyncio.run(
        CapturingToolOllama().complete(
            EndpointConfig(name="source", adapter="ollama-chat"), MODEL, request
        )
    )
    anthropic_adapter = CapturingToolAnthropic()
    anthropic = asyncio.run(
        anthropic_adapter.complete(
            EndpointConfig(
                name="source",
                adapter="anthropic-messages",
                auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
            ),
            MODEL,
            request,
        )
    )
    gemini_adapter = CapturingToolGemini()
    gemini = asyncio.run(
        gemini_adapter.complete(
            EndpointConfig(
                name="source",
                adapter="gemini-generate",
                auth=AuthConfig(key_env="ROUTER_TEST_KEY"),
            ),
            MODEL,
            request,
        )
    )

    assert ollama.tool_calls[0]["function"]["name"] == "turn_on"
    assert anthropic.tool_calls[0]["function"]["arguments"] == {"room": "kitchen"}
    assert anthropic_adapter.payload["tools"][0]["input_schema"] == {"type": "object"}
    assert gemini.tool_calls[0]["function"]["arguments"] == {"room": "kitchen"}
    assert gemini_adapter.payload["tools"][0]["functionDeclarations"][0]["name"] == "turn_on"


@pytest.mark.parametrize(
    "outcome,kind,retryable",
    [
        (httpx.ReadTimeout("slow"), "timeout", True),
        (httpx.ConnectError("refused"), "connection", True),
        (httpx.RemoteProtocolError("closed"), "connection", True),
        (httpx.Response(503, json={"error": "busy"}), "http_5xx", True),
        (httpx.Response(404, json={"error": "missing"}), "http_4xx", False),
        (httpx.Response(200, content=b"<html>not json</html>"), "invalid_response", False),
        (httpx.Response(200, json=["not", "an", "object"]), "invalid_response", False),
    ],
)
def test_http_failures_carry_a_traffic_failure_kind(monkeypatch, outcome, kind, retryable) -> None:
    from llm_router.adapters import base

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    real_client = httpx.AsyncClient

    def client(**kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", client)
    adapter = base.BaseHTTPAdapter()
    endpoint = EndpointConfig(name="source", adapter="openai-chat", base_url="http://source.invalid/v1", auth=AuthConfig(scheme="none"))
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(adapter.post_json(endpoint, "/chat/completions", {"model": "x"}))
    assert captured.value.kind == kind
    assert captured.value.retryable is retryable
    assert "private" not in str(captured.value)


def test_configuration_problems_are_configuration_failures(monkeypatch) -> None:
    from llm_router.adapters import base

    adapter = base.BaseHTTPAdapter()
    with pytest.raises(UpstreamError) as missing_url:
        asyncio.run(adapter.post_json(EndpointConfig(name="source", adapter="openai-chat"), "/x", {}))
    assert missing_url.value.kind == "configuration"
    monkeypatch.delenv("ROUTER_TEST_MISSING_KEY", raising=False)
    endpoint = EndpointConfig(
        name="source", adapter="openai-chat", base_url="http://source.invalid/v1",
        auth=AuthConfig(key_env="ROUTER_TEST_MISSING_KEY", scheme="bearer"),
    )
    with pytest.raises(UpstreamError) as missing_key:
        adapter.connection_metadata(endpoint, {})
    assert missing_key.value.kind == "configuration"
    assert UpstreamError("parse problem", retryable=False).kind == "invalid_response"
    assert UpstreamError("unknown", status_code=429).kind == "http_4xx"
    assert UpstreamError("unknown").kind == "other"
    assert UpstreamError("unknown", kind="not-a-kind").kind == "other"


class _PayloadOllama(OllamaChatAdapter):
    def __init__(self) -> None:
        self.payload: dict = {}

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.payload = dict(payload)
        return {"message": {"role": "assistant", "content": "OK"}, "done_reason": "stop"}


def test_ollama_adapter_forwards_requested_context_window() -> None:
    adapter = _PayloadOllama()
    endpoint = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid")
    asyncio.run(adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", min_context_window=8192.0, max_tokens=32)))
    assert adapter.payload["options"] == {"num_predict": 32, "num_ctx": 8192}
    assert type(adapter.payload["options"]["num_ctx"]) is int
    asyncio.run(adapter.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=32)))
    assert "num_ctx" not in adapter.payload["options"], "No context request means Ollama keeps its own default"


def test_output_limit_is_forwarded_only_when_the_client_set_one() -> None:
    ollama = _PayloadOllama()
    endpoint = EndpointConfig(name="source", adapter="ollama-chat", base_url="http://source.invalid")
    asyncio.run(ollama.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=2048, max_tokens_specified=False)))
    assert "num_predict" not in ollama.payload["options"], "No client limit means Ollama's own (unlimited) default"
    asyncio.run(ollama.complete(endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=64)))
    assert ollama.payload["options"]["num_predict"] == 64
    openai = CapturingOpenAI()
    openai_endpoint = EndpointConfig(name="source", adapter="openai-chat")
    asyncio.run(openai.complete(openai_endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=2048, max_tokens_specified=False)))
    assert "max_tokens" not in openai.payload
    asyncio.run(openai.complete(openai_endpoint, MODEL, QueryRequest.from_prompt("hi", max_tokens=64)))
    assert openai.payload["max_tokens"] == 64


class _ReasoningOnlyOpenAI(OpenAIChatAdapter):
    def __init__(self, response: dict) -> None:
        self.response = response

    async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
        return self.response


@pytest.mark.parametrize("response", [
    {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "private thoughts"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"role": "assistant", "content": "", "reasoning": "private thoughts"}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}]},
])
def test_reasoning_only_openai_responses_are_diagnosed(response) -> None:
    adapter = _ReasoningOnlyOpenAI(response)
    with pytest.raises(UpstreamError) as captured:
        asyncio.run(adapter.complete(EndpointConfig(name="source", adapter="openai-chat"), MODEL, QueryRequest.from_prompt("hi")))
    assert "output budget on reasoning" in str(captured.value)
    assert "private thoughts" not in str(captured.value)
    assert captured.value.kind == "invalid_response"


def test_reasoning_only_ollama_response_is_diagnosed() -> None:
    class ThinkingOllama(OllamaChatAdapter):
        async def post_json(self, endpoint, path, payload, **kwargs):  # type: ignore[no-untyped-def]
            return {"message": {"role": "assistant", "content": "", "thinking": "private thoughts"}, "done_reason": "length"}

    with pytest.raises(UpstreamError) as captured:
        asyncio.run(ThinkingOllama().complete(EndpointConfig(name="source", adapter="ollama-chat", base_url="http://s.invalid"), MODEL, QueryRequest.from_prompt("hi")))
    assert "output budget on reasoning" in str(captured.value) and "private thoughts" not in str(captured.value)
