"""The real HTTP -> job -> engine workflow, using fake backends only."""

from __future__ import annotations

import asyncio
import json

import httpx

from llm_router.gateway import RouterGateway, create_app
from llm_router.inference_test import run_inference_checks
from llm_router.router import LLMRouter
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, RouterConfig


def test_one_click_tests_smallest_model_on_each_exact_backend_without_failover(monkeypatch):
    key = "private-router-inference-key"
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", key)
    endpoints = {
        "golemframe": EndpointConfig("golemframe", "ollama-chat", "http://golemframe.invalid:11434"),
        "lm-studio-pantheon": EndpointConfig("lm-studio-pantheon", "openai-chat", "http://pantheon.invalid:1234/v1", auth=AuthConfig(scheme="none")),
        "broken": EndpointConfig("broken", "ollama-chat", "http://broken.invalid:11434"),
    }
    models = tuple(ModelConfig(f"{endpoint}-{name}", endpoint, name) for endpoint in endpoints for name in ("big:30b", "tiny:0.6b"))
    service = RouterGateway(discovery=False)
    service._router = LLMRouter(RouterConfig(endpoints=endpoints, models=models))
    calls = []

    async def backend(request):
        calls.append((request.method, request.url.host, request.url.path))
        assert key not in str(request.headers) + str(request.url) + request.content.decode()
        assert request.url.host in {"golemframe.invalid", "pantheon.invalid", "broken.invalid"}
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "big:30b", "size": 20_000_000_000}, {"name": "tiny:0.6b", "size": 500_000_000}]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "big:30b"}, {"id": "tiny:0.6b"}]})
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{"key": "big:30b", "type": "llm", "size_bytes": 20_000_000_000}, {"key": "tiny:0.6b", "type": "llm", "size_bytes": 500_000_000}]})
        assert request.method == "POST"
        assert request.url.path in {"/api/chat", "/v1/chat/completions"}
        body = json.loads(request.content)
        assert body["model"] == "tiny:0.6b"
        assert body["stream"] is False
        assert "tools" not in body
        if request.url.host == "broken.invalid":
            return httpx.Response(503, json={"error": "private-upstream-error"})
        if request.url.path == "/api/chat":
            assert body["options"]["num_predict"] <= 16
            return httpx.Response(200, json={"done": True, "message": {"content": "private-reply-content"}})
        assert body["max_tokens"] <= 16
        return httpx.Response(200, json={"choices": [{"message": {"content": "private-reply-content"}}]})

    async def runner(config, *, on_result):
        return await run_inference_checks(config, on_result=on_result, transport=httpx.MockTransport(backend))

    app = create_app(gateway=service, inference_test_runner=runner)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://router.test", headers={"Authorization": f"Bearer {key}"}) as client:
            for path in ("/status", "/healthz", "/status/data", "/status/inference-test"):
                assert (await client.get(path)).status_code == 200
            assert calls == []
            accepted = await client.post("/status/inference-test", headers={"X-LLM-Router-Inference-Test": "1"})
            assert accepted.status_code == 202
            assert accepted.json()["total"] == 3
            await app.state.inference_jobs._task
            response = await client.get("/status/inference-test")
            result = response.json()
            assert result["run_id"] == accepted.json()["run_id"]
            assert result["state"] == "complete"
            assert result["completed"] == result["total"] == 3
            checked = {entry["name"]: entry for entry in result["checks"]}
            for name in endpoints:
                assert checked[name]["model"] == "tiny:0.6b"
                assert "Smallest reported" in checked[name]["selection"]
            assert checked["golemframe"]["status"] == checked["lm-studio-pantheon"]["status"] == "pass"
            assert checked["broken"]["status"] == "fail"
            assert checked["broken"]["http_status"] == 503
            for secret in (key, "private-upstream-error", "private-reply-content"):
                assert secret not in response.text
            posts = [call for call in calls if call[0] == "POST"]
            assert len(posts) == 3
            assert len({call[1] for call in posts}) == 3
            prior = list(calls)
            await client.get("/status/inference-test")
            assert (await client.post("/status/inference-test", headers={"X-LLM-Router-Inference-Test": "1"})).status_code == 429
            assert calls == prior
        await app.state.inference_jobs.close()
    asyncio.run(scenario())
