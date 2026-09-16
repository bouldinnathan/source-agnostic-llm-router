from __future__ import annotations

import asyncio
from dataclasses import asdict, replace

import httpx
import pytest

from llm_router import self_test
from llm_router.runtime import RuntimeRegistry
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, RouterConfig
from llm_router.self_test import run_backend_checks


def config_for(*endpoints: EndpointConfig) -> RouterConfig:
    return RouterConfig(
        endpoints={endpoint.name: endpoint for endpoint in endpoints},
        models=tuple(
            ModelConfig(id=f"model-{endpoint.name}", endpoint=endpoint.name, upstream_model="unused")
            for endpoint in endpoints
        ),
    )


def endpoint(**kwargs) -> EndpointConfig:
    return EndpointConfig(**{
        "name": "worker",
        "adapter": "ollama",
        "base_url": "http://worker.local:11434",
        **kwargs,
    })


@pytest.mark.parametrize(
    ("adapter", "base", "path", "payload"),
    [
        ("ollama", "http://worker.local:11434", "/api/version", {"version": "0.9.1"}),
        ("ollama-chat", "http://worker.local/", "/api/version", {"version": "0.9.1"}),
        ("openai-chat", "http://worker.local/v1", "/v1/models", {"data": []}),
        ("openai-compatible", "http://worker.local:1234/v1", "/v1/models", {"data": []}),
        ("openai-responses", "https://worker.local/v1/", "/v1/models", {"data": []}),
        ("anthropic", "https://worker.local", "/v1/models", {"data": []}),
        ("anthropic-messages", "https://worker.local/v1", "/v1/models", {"data": []}),
        ("gemini", "https://worker.local/v1beta", "/v1beta/models", {"models": []}),
        ("gemini-generate", "https://worker.local/v1", "/v1/models", {"models": []}),
    ],
)
def test_only_safe_metadata_gets_are_used(adapter, base, path, payload) -> None:
    config = config_for(endpoint(adapter=adapter, base_url=base, auth=AuthConfig(scheme="none")))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == path
        assert not request.content
        assert not request.url.query
        assert "authorization" not in request.headers
        return httpx.Response(200, json=payload)

    result = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
    assert len(requests) == 1
    assert result[0]["status"] == "pass"
    assert result[0]["http_status"] == 200
    assert result[0]["elapsed_ms"] >= 0
    assert "no model was invoked" in result[0]["detail"]
    assert set(result[0]) == {"name", "target", "status", "detail", "elapsed_ms", "http_status"}


@pytest.mark.parametrize("config", [None, RouterConfig(endpoints={}, models=())])
def test_missing_backends_are_skipped_without_requests(config) -> None:
    def handler(request):
        pytest.fail("No backends must mean no requests")

    rows = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
    assert len(rows) == 1
    assert rows[0]["status"] == "skip"
    assert rows[0]["http_status"] is None


@pytest.mark.parametrize("adapter", ["generic-json", "custom", "malicious.module:Adapter"])
def test_unknown_adapters_and_custom_health_paths_never_execute(adapter) -> None:
    config = config_for(endpoint(adapter=adapter, health_path="/api/pull"))

    def handler(request):
        pytest.fail("Custom adapters and health paths must not be invoked")

    rows = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
    assert rows[0]["status"] == "skip"


@pytest.mark.parametrize("health_path", ["/api/pull", "/api/generate", "/api/v1/models/load", "https://other.local/unsafe"])
def test_builtin_checks_ignore_custom_health_paths(health_path) -> None:
    config = config_for(endpoint(health_path=health_path))

    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "http://worker.local:11434/api/version"
        return httpx.Response(200, json={"version": "1.0"})

    assert asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]["status"] == "pass"


@pytest.mark.parametrize(
    "base_url",
    [
        "", "file:///tmp/socket", "ftp://worker.local", "//worker.local", "http://",
        "http://user:secret@worker.local", "http://worker.local?token=secret",
        "http://worker.local#secret", "http://worker.local?", "http://worker.local#",
        "http://worker.local:99999", "http://worker.local:bad", "http://worker.local\\@other.local",
        "http://worker.local/v1/../api/pull", "http://worker.local/%2e%2e/api/pull",
        "http://worker.local/%252e%252e", "http://worker.local//api/pull",
        "http://worker.local\n", "http://worker.local/a b", "http://%77orker.local",
    ],
)
def test_ambiguous_or_unsafe_addresses_are_not_requested(base_url) -> None:
    config = config_for(endpoint(base_url=base_url))

    def handler(request):
        pytest.fail("Unsafe base URL must not be requested")

    row = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]
    assert row["status"] == "fail"
    assert row["target"] == "Configured backend"
    assert row["http_status"] is None
    assert "secret" not in str(row)


def test_result_target_hides_private_path_and_preserves_ipv6_port() -> None:
    config = config_for(endpoint(
        adapter="openai-compatible",
        base_url="http://[::1]:1234/private-tenant-secret/v1",
        auth=AuthConfig(scheme="none"),
    ))

    def handler(request):
        assert request.url.path == "/private-tenant-secret/v1/models"
        return httpx.Response(200, json={"data": [{"id": "private-model-name"}]})

    row = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]
    assert row["status"] == "pass"
    assert row["target"] == "http://[::1]:1234"
    assert "private-tenant-secret" not in str(row)
    assert "private-model-name" not in str(row)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 429, 500, 503])
def test_http_failures_are_sanitized_and_redirects_are_not_followed(status) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://secret.other/unsafe"}, text="secret-response")

    row = asyncio.run(run_backend_checks(config_for(endpoint()), transport=httpx.MockTransport(handler)))[0]
    assert len(calls) == 1
    assert row["status"] == "fail"
    assert row["http_status"] == status
    assert "secret" not in str(row)


@pytest.mark.parametrize("payload", [[], {}, {"version": 1}, {"version": None}, {"version": ""}, {"version": "   "}])
def test_ollama_version_must_be_nonempty_string(payload) -> None:
    rows = asyncio.run(run_backend_checks(
        config_for(endpoint()), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ))
    assert rows[0]["status"] == "fail"
    assert rows[0]["http_status"] == 200


@pytest.mark.parametrize("payload", [{}, {"data": "secret"}, {"data": None}, []])
def test_openai_metadata_must_have_list(payload) -> None:
    rows = asyncio.run(run_backend_checks(
        config_for(endpoint(adapter="openai-chat", auth=AuthConfig(scheme="none"))),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ))
    assert rows[0]["status"] == "fail"
    assert "secret" not in str(rows)


def test_invalid_json_is_reported_without_response_content() -> None:
    rows = asyncio.run(run_backend_checks(
        config_for(endpoint()),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret not JSON")),
    ))
    assert rows[0]["status"] == "fail"
    assert "valid JSON" in rows[0]["detail"]
    assert "secret" not in str(rows)


def test_authentication_headers_tls_and_no_inherited_proxy(monkeypatch) -> None:
    monkeypatch.setenv("SELF_TEST_KEY", "upstream-secret")
    monkeypatch.setenv("SELF_TEST_HEADER", "header-secret")
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy-secret.invalid")
    config = config_for(endpoint(
        adapter="anthropic",
        base_url="https://worker.local",
        auth=AuthConfig(key_env="SELF_TEST_KEY"),
        headers={"X-Private": "${SELF_TEST_HEADER}"},
        options={"api_version": "2023-06-01"},
        verify_tls=False,
    ))
    original_client = httpx.AsyncClient
    clients = []

    def client_factory(**kwargs):
        assert kwargs["verify"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"] == 3.0
        client = original_client(**kwargs)
        clients.append(client)
        return client

    def handler(request):
        assert request.headers["x-api-key"] == "upstream-secret"
        assert request.headers["X-Private"] == "header-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "router-secret" not in str(request.headers)
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"Set-Cookie": "session=cookie-secret"}, json={"data": []})

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    for _ in range(2):
        rows = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
        assert rows[0]["status"] == "pass"
        assert "secret" not in str(rows)
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


def test_router_key_is_only_sent_when_explicitly_configured(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "explicit-router-secret")
    config = config_for(endpoint(auth=AuthConfig(scheme="bearer", key_env="LLM_ROUTER_GATEWAY_API_KEY")))

    def handler(request):
        assert request.headers["authorization"] == "Bearer explicit-router-secret"
        return httpx.Response(200, json={"version": "1.0"})

    assert asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]["status"] == "pass"


def test_query_auth_and_network_exceptions_do_not_leak_credentials(monkeypatch) -> None:
    monkeypatch.setenv("SELF_TEST_KEY", "query-secret")
    config = config_for(endpoint(
        adapter="gemini", base_url="https://worker.local/v1beta", auth=AuthConfig(key_env="SELF_TEST_KEY"),
    ))

    def handler(request):
        assert request.url.params["key"] == "query-secret"
        raise httpx.ConnectError(f"unsafe exception: {request.url}", request=request)

    row = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]
    assert row["status"] == "fail"
    assert row["target"] == "https://worker.local"
    assert "secret" not in str(row)
    assert "exception" not in str(row)


def test_missing_upstream_authentication_does_not_make_request(monkeypatch) -> None:
    monkeypatch.delenv("PRIVATE_MISSING_CREDENTIAL", raising=False)
    config = config_for(endpoint(adapter="openai-chat", auth=AuthConfig(key_env="PRIVATE_MISSING_CREDENTIAL")))

    def handler(request):
        pytest.fail("Missing authentication must fail before the request")

    row = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))[0]
    assert row["status"] == "fail"
    assert "PRIVATE_MISSING_CREDENTIAL" not in str(row)


def test_timeout_and_concurrency_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(self_test, "PROBE_TIMEOUT_SECONDS", 0.02)
    config = config_for(*(endpoint(name=f"worker-{index}") for index in range(10)))
    active = 0
    maximum = 0

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
        return httpx.Response(200, json={"version": "1.0"})

    rows = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
    assert maximum == 4
    assert active == 0
    assert all(row["status"] == "fail" and "timed out" in row["detail"] for row in rows)


def test_probe_count_is_bounded_and_unsupported_adapters_do_not_consume_limit() -> None:
    config = config_for(
        endpoint(name="generic", adapter="generic-json"),
        *(endpoint(name=f"worker-{index}") for index in range(20)),
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"version": "1.0"})

    rows = asyncio.run(run_backend_checks(config, transport=httpx.MockTransport(handler)))
    assert len(calls) == 16
    assert len(rows) == 21
    assert rows[0]["status"] == "skip"
    assert all(row["status"] == "pass" for row in rows[1:17])
    assert all(row["status"] == "skip" and "limit" in row["detail"] for row in rows[17:])


def test_response_size_is_bounded_and_stream_closed(monkeypatch) -> None:
    monkeypatch.setattr(self_test, "MAX_RESPONSE_BYTES", 1024)

    class LargeStream(httpx.AsyncByteStream):
        chunks_read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.chunks_read += 1
                yield b"x" * 16384

        async def aclose(self):
            self.closed = True

    stream = LargeStream()
    rows = asyncio.run(run_backend_checks(
        config_for(endpoint()),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ))
    assert rows[0]["status"] == "fail"
    assert "size limit" in rows[0]["detail"]
    assert stream.chunks_read == 1
    assert stream.closed


def test_compressed_responses_are_rejected_without_reading_body() -> None:
    class UnreadStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Compressed response must be rejected before decompression")
            yield b""

    rows = asyncio.run(run_backend_checks(
        config_for(endpoint()),
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=UnreadStream(),
        )),
    ))
    assert rows[0]["status"] == "fail"
    assert "compressed" in rows[0]["detail"]


def test_diagnostics_do_not_change_config_runtime_or_model_circuit() -> None:
    config = config_for(endpoint())
    config = replace(config, models=tuple(replace(model, enabled=False) for model in config.models))
    runtime = RuntimeRegistry(config.policy)
    runtime.record_endpoint_probe("worker", False, "existing health state")
    runtime.record_failure("model-worker", "existing model failure")
    before_config = asdict(config)
    before_runtime = runtime.snapshot(config.models)
    rows = asyncio.run(run_backend_checks(
        config, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"version": "1.0"})),
    ))
    assert rows[0]["status"] == "pass"
    assert asdict(config) == before_config
    assert runtime.snapshot(config.models) == before_runtime
