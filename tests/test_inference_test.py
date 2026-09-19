from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm_router import inference_test as checks
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, RouterConfig


def endpoint(**kwargs):
    return EndpointConfig(**{
        "name": "worker", "adapter": "ollama", "base_url": "http://worker.local:11434",
        "auth": AuthConfig(scheme="none"), **kwargs,
    })


def config_for(models=("small:1b", "large:7b"), *, ep=None):
    ep = ep or endpoint()
    return RouterConfig(endpoints={ep.name: ep}, models=tuple(
        model if isinstance(model, ModelConfig) else ModelConfig(id=model, endpoint=ep.name, upstream_model=model)
        for model in models
    ))


def ollama_ok():
    return {"done": True, "message": {"content": "private generated reply", "role": "assistant"}, "eval_count": 3}


def run(config, handler, **kwargs):
    return asyncio.run(checks.run_inference_checks(config, transport=httpx.MockTransport(handler), **kwargs))


def test_smallest_reported_bytes_win_over_name_estimates_and_only_one_tiny_fixed_request():
    ep = endpoint(options={"path": "/api/pull", "max_tokens_field": "infinite", "extra_body": {"tools": []}}, health_path="/api/pull")
    config = config_for(ep=ep)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/api/tags"
            assert not request.content
            return httpx.Response(200, json={"models": [{"model": "small:1b", "size": 500}, {"model": "large:7b", "size": 100}]})
        assert request.url.path == "/api/chat"
        assert json.loads(request.content) == {
            "model": "large:7b", "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False, "think": False, "options": {"num_predict": 16, "temperature": 0},
        }
        return httpx.Response(200, json=ollama_ok())

    rows = run(config, handler)
    assert len(requests) == 2
    row = rows[0]
    assert row["status"] == "pass"
    assert row["model"] == "large:7b"
    assert "100 bytes" in row["selection"]
    assert row["target"] == "http://worker.local:11434"
    assert row["elapsed_ms"] >= 0
    assert row["http_status"] == 200
    assert "private generated reply" not in json.dumps(rows)
    assert checks.PROMPT not in json.dumps(rows)
    assert set(row) == {"name", "target", "status", "model", "selection", "detail", "elapsed_ms", "http_status"}


@pytest.mark.parametrize(("entries", "expected", "wording"), [
    ([{"model": "small:1b", "size": 500}, {"model": "large:7b"}], "small:1b", "other model sizes are unknown"),
    ([{"model": "small:1b"}, {"model": "large:7b"}], "small:1b", "Estimated smallest"),
    ([{"model": "small:1b", "details": {"parameter_size": "50B"}}, {"model": "large:7b", "details": {"parameter_size": "3B"}}], "large:7b", "3B"),
])
def test_fallback_selection_is_explicit_and_estimates_not_claimed_as_sizes(entries, expected, wording):
    def handler(request):
        return httpx.Response(200, json={"models": entries} if request.method == "GET" else ollama_ok())
    row = run(config_for(), handler)[0]
    assert row["model"] == expected
    assert wording in row["selection"]


def test_unknown_size_fallback_deterministic_independent_of_config_and_catalog_order():
    def handler(request):
        return httpx.Response(200, json={"models": [{"model": "zeta"}, {"model": "alpha"}]} if request.method == "GET" else ollama_ok())
    for names in [("zeta", "alpha"), ("alpha", "zeta")]:
        row = run(config_for(names), handler)[0]
        assert row["model"] == "alpha"
        assert "not a verified smallest model" in row["selection"]


@pytest.mark.parametrize(("value", "expected"), [("mixtral-8x7b", 56e9), ("qwen3-235b-a22b", 235e9), ("smollm-135m", 135e6), ("model2B", None), ("qwen-0.5b", .5e9)])
def test_parameter_estimate_total_not_active_parameter_count(value, expected):
    assert checks._parameter_estimate(value, {}) == expected


@pytest.mark.parametrize("value", [0, -1, True, False, "100", float("nan"), float("inf"), 10 ** 100])
def test_invalid_size_does_not_rank_as_real_bytes(value):
    assert checks._size({"size": value}) is None


@pytest.mark.parametrize("model", [
    ModelConfig(id="disabled", endpoint="worker", upstream_model="disabled", enabled=False),
    ModelConfig(id="embedding", endpoint="worker", upstream_model="nomic-embed-text"),
    ModelConfig(id="rerank", endpoint="worker", upstream_model="bge-reranker"),
    ModelConfig(id="audio", endpoint="worker", upstream_model="whisper"),
    ModelConfig(id="tagged", endpoint="worker", upstream_model="tagged", tags=("embedding",)),
    ModelConfig(id="capability", endpoint="worker", upstream_model="capability", capabilities={"embedding": 1}),
])
def test_nonchat_or_disabled_only_models_skip_without_even_catalog_request(model):
    row = run(config_for((model,)), lambda request: pytest.fail("No requests expected"))[0]
    assert row["status"] == "skip"
    assert row["model"] is None


@pytest.mark.parametrize("metadata", [{"type": "embedding"}, {"capabilities": ["embedding"]}, {"details": {"family": "bert"}}])
def test_catalog_embedding_declarations_are_excluded(metadata):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"models": [{"model": "small:1b", **metadata}]})
    row = run(config_for(), handler)[0]
    assert row["status"] == "skip"
    assert row["model"] is None


def test_catalog_cannot_enable_disabled_or_unconfigured_smaller_model():
    config = config_for(("small:1b", ModelConfig(id="disabled", endpoint="worker", upstream_model="disabled", enabled=False)))
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "small:1b", "size": 100}, {"model": "disabled", "size": 1}, {"model": "unconfigured", "size": 2}]})
        assert json.loads(request.content)["model"] == "small:1b"
        return httpx.Response(200, json=ollama_ok())
    assert run(config, handler)[0]["status"] == "pass"


def test_ollama_latest_alias_is_matched_without_changing_configured_model_id():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "small:latest", "size": 10}]})
        assert json.loads(request.content)["model"] == "small"
        return httpx.Response(200, json=ollama_ok())
    assert run(config_for(("small",)), handler)[0]["model"] == "small"


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 405, 429, 500, 503])
def test_catalog_failure_never_invokes_ollama_and_never_follows_redirect(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "upstream secret"}, headers={"Location": "https://other.invalid/api/pull"})
    row = run(config_for(), handler)[0]
    assert len(requests) == 1
    assert row["status"] == "fail"
    assert row["model"] is None
    assert row["http_status"] == status
    assert "upstream secret" not in str(row)


@pytest.mark.parametrize("status", [301, 401, 404, 429, 500, 503])
def test_generation_failure_does_not_try_larger_model_or_retry(status):
    posts = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "small:1b"}, {"model": "large:7b"}]})
        posts.append(json.loads(request.content))
        return httpx.Response(status, json={"error": "private backend traceback and credentials"}, headers={"Location": "https://other.invalid/api/chat"})
    row = run(config_for(), handler)[0]
    assert len(posts) == 1
    assert posts[0]["model"] == row["model"] == "small:1b"
    assert row["status"] == "fail"
    assert row["http_status"] == status
    assert "private backend" not in str(row)


@pytest.mark.parametrize("payload", [{}, {"done": True, "message": {"content": " "}}, {"done": False, "message": {"content": "OK"}}, {"done": True, "message": {"thinking": "OK"}}, {"error": "secret", **ollama_ok()}, {"done": True, "message": {"content": 123}}])
def test_http_200_without_valid_generated_text_is_not_a_pass(payload):
    def handler(request):
        return httpx.Response(200, json={"models": [{"model": "small:1b"}]} if request.method == "GET" else payload)
    row = run(config_for(), handler)[0]
    assert row["status"] == "fail"
    assert row["model"] == "small:1b"


@pytest.mark.parametrize("adapter", ["openai-chat", "openai-compatible", "openai-responses"])
def test_openai_variants_use_only_fixed_paths_and_bounded_payload(adapter):
    ep = endpoint(adapter=adapter, base_url="https://worker.local/v1", options={"path": "/danger", "max_tokens_field": "arbitrary", "body": {"tools": ["danger"]}})
    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "small:1b"}, {"id": "large:7b"}]})
        body = json.loads(request.content)
        assert body["model"] == "small:1b"
        assert body["stream"] is False
        assert "tools" not in body
        if adapter == "openai-responses":
            assert request.url.path == "/v1/responses"
            assert body["max_output_tokens"] == 16 and body["store"] is False
            assert body["input"] == [{"role": "user", "content": checks.PROMPT}]
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]})
        assert request.url.path == "/v1/chat/completions"
        assert body["max_tokens"] == 16
        assert body["messages"] == [{"role": "user", "content": checks.PROMPT}]
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    assert run(config_for(ep=ep), handler)[0]["status"] == "pass"


@pytest.mark.parametrize("status", [404, 405])
def test_unsupported_openai_catalog_uses_enabled_configured_id_with_explicit_caveat(status):
    ep = endpoint(adapter="openai-compatible", base_url="http://worker.local/v1")
    def handler(request):
        return httpx.Response(status, json={}) if request.method == "GET" else httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    row = run(config_for(ep=ep), handler)[0]
    assert row["status"] == "pass"
    assert "Catalog unavailable" in row["selection"]


@pytest.mark.parametrize("version", [1, 0])
def test_lmstudio_native_metadata_size_and_loaded_aliases_without_load_or_download(version):
    ep = endpoint(adapter="openai-compatible", base_url="http://worker.local:1234/proxy/v1")
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/proxy/v1/models":
            return httpx.Response(200, json={"data": [{"id": "small:1b"}, {"id": "large:7b"}]})
        if request.url.path == "/proxy/api/v1/models":
            if version == 0:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"models": [{"key": "small:1b", "size_bytes": 400}, {"key": "large-key", "size_bytes": 100, "loaded_instances": [{"id": "large:7b"}]}]})
        if request.url.path == "/proxy/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "small:1b", "size_bytes": 400}, {"id": "large:7b", "size_bytes": 100}]})
        assert request.url.path == "/proxy/v1/chat/completions"
        assert json.loads(request.content)["model"] == "large:7b"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    row = run(config_for(ep=ep), handler)[0]
    assert row["status"] == "pass"
    assert "100 bytes" in row["selection"]
    assert sum(request.method == "POST" for request in requests) == 1
    assert all(request.url.host == "worker.local" for request in requests)


def test_optional_lmstudio_size_request_failure_still_runs_selected_model_with_estimate():
    ep = endpoint(adapter="openai-compatible", base_url="http://worker.local:1234/v1")
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "small:1b"}]})
        if request.url.path == "/api/v1/models":
            raise httpx.ConnectError("private token secret")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    row = run(config_for(ep=ep), handler)[0]
    assert row["status"] == "pass"
    assert "Estimated" in row["selection"]
    assert "secret" not in str(row)


@pytest.mark.parametrize("adapter", ["anthropic", "gemini", "generic-json", "custom.module:Malicious"])
def test_unsupported_adapters_never_import_or_invoke(adapter):
    row = run(config_for(ep=endpoint(adapter=adapter)), lambda request: pytest.fail("No request expected"))[0]
    assert row["status"] == "skip"


@pytest.mark.parametrize("config", [None, RouterConfig(endpoints={}, models=())])
def test_empty_configuration_returns_empty_rows(config):
    assert run(config, lambda request: pytest.fail("No request expected")) == []


@pytest.mark.parametrize("base", ["http://user:secret@worker.local", "http://worker.local?api_key=secret", "http://worker.local#secret", "http://worker.local/%2e%2e", "http://worker.local/a/../b", "ftp://worker.local", "http://worker.local:99999", "http://worker.local\\@evil.invalid", "http://worker.local\n"])
def test_invalid_url_is_rejected_before_credentials_or_requests(base):
    row = run(config_for(ep=endpoint(base_url=base)), lambda request: pytest.fail("No request expected"))[0]
    assert row["status"] == "fail"
    assert row["target"] == "Configured backend"
    assert "secret" not in str(row)


def test_only_endpoint_credentials_no_cookies_environment_proxies_or_netrc(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key-not-for-backends")
    monkeypatch.setenv("BACKEND_TEST_KEY", "backend-only-key")
    real_client = httpx.AsyncClient
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)
    monkeypatch.setattr(checks.httpx, "AsyncClient", client)
    ep = endpoint(auth=AuthConfig(key_env="BACKEND_TEST_KEY", scheme="bearer"))
    def handler(request):
        assert request.headers["Authorization"] == "Bearer backend-only-key"
        assert "cookie" not in request.headers
        assert "router-key" not in str(request.headers)
        return httpx.Response(200, headers={"Set-Cookie": "catalog=must-not-propagate"}, json={"models": [{"model": "small:1b"}]} if request.method == "GET" else ollama_ok())
    row = run(config_for(ep=ep), handler)[0]
    assert row["status"] == "pass"
    assert len(options) == 2
    assert all(opt["trust_env"] is False and opt["follow_redirects"] is False for opt in options)
    assert "backend-only-key" not in str(row)


@pytest.mark.parametrize("ep", [
    endpoint(auth=AuthConfig(key_env="LLM_ROUTER_GATEWAY_API_KEY", scheme="bearer")),
    endpoint(headers={"Authorization": "Bearer ${LLM_ROUTER_GATEWAY_API_KEY}"}),
    endpoint(headers={"Authorization": "Bearer router-key-not-for-backends"}),
])
def test_router_key_is_never_forwarded_even_if_misconfigured_as_backend_credential(ep, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-key-not-for-backends")
    row = run(config_for(ep=ep), lambda request: pytest.fail("No request expected"))[0]
    assert row["status"] == "fail"
    assert "separately" in row["detail"]
    assert "router-key-not-for-backends" not in str(row)


@pytest.mark.parametrize("stage", ["catalog", "generation"])
@pytest.mark.parametrize("kind", ["oversize", "compressed", "invalid-json", "wrong-type"])
def test_response_limits_and_format_errors_are_sanitized(stage, kind, monkeypatch):
    monkeypatch.setattr(checks, "MAX_RESPONSE_BYTES", 150)
    monkeypatch.setattr(checks, "MAX_INFERENCE_RESPONSE_BYTES", 150)
    requests = []
    def handler(request):
        requests.append(request)
        if stage == "generation" and request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "small:1b"}]})
        if kind == "oversize":
            return httpx.Response(200, content=b"x" * 151)
        if kind == "compressed":
            return httpx.Response(200, content=b"", headers={"content-encoding": "gzip"})
        if kind == "invalid-json":
            return httpx.Response(200, content=b"secret raw diagnostic")
        return httpx.Response(200, json=["secret raw diagnostic"])
    row = run(config_for(), handler)[0]
    assert row["status"] == "fail"
    assert "secret raw diagnostic" not in str(row)
    assert len(requests) == (2 if stage == "generation" else 1)


def test_timeout_bounded_without_retry_even_if_backend_ignores_httpx_timeout(monkeypatch):
    monkeypatch.setattr(checks, "INFERENCE_TIMEOUT_SECONDS", .005)
    posts = []
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "small:1b"}]})
        posts.append(request)
        await asyncio.sleep(30)
        pytest.fail("Generation should be cancelled")
    row = run(config_for(), handler)[0]
    assert row["status"] == "fail"
    assert "timed out" in row["detail"]
    assert "may still be finishing" in row["detail"]
    assert len(posts) == 1


def test_backend_limit_concurrency_callbacks_and_independence(monkeypatch):
    monkeypatch.setattr(checks, "MAX_ENDPOINT_PROBES", 3)
    monkeypatch.setattr(checks, "MAX_CONCURRENCY", 2)
    endpoints = {f"worker-{i}": endpoint(name=f"worker-{i}", base_url=f"http://worker-{i}.local") for i in range(5)}
    config = RouterConfig(endpoints=endpoints, models=tuple(ModelConfig(id=name, endpoint=name, upstream_model="small:1b") for name in endpoints))
    active = 0
    peak = 0
    posts = []
    callbacks = []
    async def handler(request):
        nonlocal active, peak
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "small:1b"}]})
        posts.append(request.url.host)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.01)
        active -= 1
        if request.url.host == "worker-0.local":
            raise httpx.ConnectError("secret transport failure")
        return httpx.Response(200, json=ollama_ok())
    async def callback(row):
        await asyncio.sleep(0)
        callbacks.append(dict(row))
        row["status"] = "must-not-corrupt-final-result"
    rows = run(config, handler, on_result=callback)
    assert peak == 2
    assert len(posts) == len(set(posts)) == 3
    assert len(callbacks) == len({row["name"] for row in callbacks}) == 5
    assert [row["name"] for row in rows] == list(endpoints)
    assert [row["status"] for row in rows] == ["fail", "pass", "pass", "skip", "skip"]
    assert "secret" not in str(rows)


def test_cancellation_propagates_and_does_not_invoke_remaining_endpoints(monkeypatch):
    monkeypatch.setattr(checks, "MAX_CONCURRENCY", 1)
    config = config_for()
    async def handler(request):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        run(config, handler)


def test_callback_error_cancels_and_joins_other_generation_tasks():
    endpoints = {name: endpoint(name=name, base_url=f"http://{name}.local") for name in ("fast", "slow")}
    config = RouterConfig(endpoints=endpoints, models=tuple(ModelConfig(id=name, endpoint=name, upstream_model="small:1b") for name in endpoints))
    cancelled = []
    async def scenario():
        slow_started = asyncio.Event()
        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"models": [{"model": "small:1b"}]})
            if request.url.host == "slow.local":
                slow_started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.append("slow")
            await slow_started.wait()
            return httpx.Response(200, json=ollama_ok())
        async def callback(row):
            raise ValueError("Progress rejected")
        with pytest.raises(ValueError, match="Progress rejected"):
            await checks.run_inference_checks(config, transport=httpx.MockTransport(handler), on_result=callback)
        assert cancelled == ["slow"]
    asyncio.run(scenario())


def test_recognizable_router_catalog_is_not_treated_as_direct_backend():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": "small:1b", "owned_by": "llm-router"}]})
    row = run(config_for(ep=endpoint(adapter="openai-chat", base_url="http://router.local/v1")), handler)[0]
    assert row["status"] == "skip"
    assert "another LLM router" in row["detail"]


def test_tiny_output_respects_even_smaller_configured_model_limit():
    model = ModelConfig(id="tiny", endpoint="worker", upstream_model="tiny", max_output_tokens=2)
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"model": "tiny"}]})
        assert json.loads(request.content)["options"]["num_predict"] == 2
        return httpx.Response(200, json=ollama_ok())
    assert run(config_for((model,)), handler)[0]["status"] == "pass"


def test_no_runtime_health_metrics_or_config_mutation(monkeypatch):
    config = config_for()
    original = repr(config)
    def forbidden(*args, **kwargs):
        pytest.fail("Inference diagnostics must not affect routing runtime or metrics")
    from llm_router.runtime import RuntimeRegistry
    monkeypatch.setattr(RuntimeRegistry, "__init__", forbidden)
    def handler(request):
        return httpx.Response(200, json={"models": [{"model": "small:1b"}]} if request.method == "GET" else ollama_ok())
    assert run(config, handler)[0]["status"] == "pass"
    assert repr(config) == original
