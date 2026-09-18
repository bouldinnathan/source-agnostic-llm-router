from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from uvicorn.protocols.utils import get_path_with_query_string

import llm_router.gateway as gateway_module
from llm_router.discovery import DiscoveryReport, ProbeResult
from llm_router.gateway import RedactStatusQueryKey, RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.saved_hosts import SavedHostStore

from test_status_page import gateway_with_router


KEY = "private-router-key-for-public-status"
AUTH = {"Authorization": f"Bearer {KEY}"}
MUTATE = {**AUTH, "X-LLM-Router-Hosts": "1"}
EMPTY_SUMMARY = {"servers": 0, "models": 0, "last_verified_at": None, "models_truncated": False}


async def request(app, path="/healthz", *, method="GET", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture(autouse=True)
def no_network_or_model_work(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", KEY)

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Public status must not discover, probe, provision, or invoke models")

    for name in ("refresh", "check_health", "provision", "router"):
        monkeypatch.setattr(RouterGateway, name, forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    for name in ("bootstrap_router", "probe_endpoints", "run_backend_checks", "check_saved_host"):
        monkeypatch.setattr(gateway_module, name, forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def public_app(tmp_path: Path):
    tmp_path.chmod(0o700)
    store = SavedHostStore(tmp_path / "private-saved-hosts.json")
    gateway = RouterGateway(discovery=False)
    return create_app(gateway=gateway, saved_host_store=store), store, gateway


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_public_empty_summary_needs_no_key_and_creates_no_storage(public_app, path):  # type: ignore[no-untyped-def]
    app, store, gateway = public_app
    gateway.config_path = "/private/config/router.toml"
    gateway._last_error = "private-backend-error-with-secret"
    response = asyncio.run(request(app, path))
    assert response.status_code == 503
    assert set(response.json()) == {"status", "version", "summary"}
    assert response.json()["summary"] == EMPTY_SUMMARY
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    for secret in (KEY, "private-backend-error-with-secret", gateway.config_path, str(store.path)):
        assert secret not in response.text
        assert secret not in str(response.headers)
    assert not store.path.exists()


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_public_configured_summary_contains_counts_not_private_fleet_data(tmp_path, path):  # type: ignore[no-untyped-def]
    gateway, router = gateway_with_router()
    gateway.config_path = "/private/config/router.toml"
    gateway._last_error = "private-discovery-error"
    router.runtime.record_endpoint_probe("source-a", True)
    router.runtime.record_endpoint_probe("source-b", False, "private-probe-error")
    app = create_app(gateway=gateway, saved_host_store=SavedHostStore(tmp_path / "state.json"))
    response = asyncio.run(request(app, path))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "version", "summary"}
    summary = payload["summary"]
    assert set(summary) == set(EMPTY_SUMMARY)
    assert summary["servers"] == 2, "Public counts describe known configured servers, not live inference capacity"
    assert summary["models"] == 2
    assert summary["models_truncated"] is False
    assert summary["last_verified_at"] is None, "Endpoint health callbacks do not imply a successful catalog discovery"
    for secret in (KEY, "source-a", "source-b", "qwen", "golemframe", "pantheon", "private", ".invalid"):
        assert secret not in response.text


def test_protected_details_include_same_public_summary_and_require_bearer(public_app):  # type: ignore[no-untyped-def]
    app, _, _ = public_app

    async def exercise():
        assert (await request(app, "/status/data")).status_code == 401
        public = await request(app)
        private = await request(app, "/status/data", headers=AUTH)
        assert private.status_code == 200
        assert private.json()["summary"] == public.json()["summary"] == EMPTY_SUMMARY
        assert KEY not in private.text

    asyncio.run(exercise())


@pytest.mark.parametrize("reachable,failed_refresh", [(True, False), (False, False), (True, True)])
def test_cached_discovery_verification_is_public_without_source_details(tmp_path, reachable, failed_refresh):  # type: ignore[no-untyped-def]
    gateway, router = gateway_with_router()
    gateway._discovery = DiscoveryReport(router.config, (ProbeResult(
        source="private-discovery-source", provider="private-provider",
        base_url="http://private-discovery.invalid:1234", reachable=reachable,
        error="private-source-error" if not reachable else None,
    ),))
    gateway._last_refresh = 946684800.0
    gateway._last_error = "private-refresh-failure" if failed_refresh else None
    app = create_app(gateway=gateway, saved_host_store=SavedHostStore(tmp_path / "state.json"))
    response = asyncio.run(request(app))
    verified = response.json()["summary"]["last_verified_at"]
    if reachable and not failed_refresh:
        assert datetime.fromisoformat(verified).year == 2000
        assert datetime.fromisoformat(verified).tzinfo is not None
    else:
        assert verified is None
    assert "private" not in response.text
    assert KEY not in response.text


def test_public_and_private_status_reads_never_open_saved_storage(public_app, monkeypatch):  # type: ignore[no-untyped-def]
    app, store, _ = public_app

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Status must use cached saved-host results, not storage")

    for method in ("list", "add", "remove"):
        monkeypatch.setattr(store, method, forbidden)

    async def exercise():
        for path in ("/healthz", "/readyz", "/status/data", "/status", "/"):
            response = await request(app, path, headers={**AUTH, "Accept": "text/html"})
            assert response.status_code == (503 if path in ("/healthz", "/readyz") else 200)
        assert not store.path.exists()

    asyncio.run(exercise())


def saved_result(entry):  # type: ignore[no-untyped-def]
    base = "http://private-machine.invalid:1234"
    return {
        **entry,
        "checked_at": "2000-01-01T12:00:00+00:00",
        "checks": [
            {"provider": "Ollama", "base_url": base, "status": "pass", "detail": "private-version-detail",
             "catalog_status": "error", "catalog_detail": "private-catalog-error", "catalog_url": base + "/api/tags",
             "models": [], "model_count": None, "models_truncated": False},
            {"provider": "LM Studio / OpenAI-compatible", "base_url": base + "/v1", "status": "pass", "detail": "private-model-detail",
             "catalog_status": "ok", "catalog_detail": "private-model-detail", "catalog_url": base + "/v1/models",
             "models": [{"id": "private-qwen", "address": base + "/v1"}, {"id": "private-llama", "address": base + "/v1"}],
             "model_count": 2, "models_truncated": False},
        ],
    }


def test_saved_enrollment_summary_is_cached_private_and_removed_with_address(public_app, monkeypatch):  # type: ignore[no-untyped-def]
    app, store, _ = public_app
    calls = []

    async def check(entry):  # type: ignore[no-untyped-def]
        calls.append(dict(entry))
        return saved_result(entry)

    monkeypatch.setattr(gateway_module, "check_saved_host", check)

    async def exercise():
        saved = await request(app, "/status/hosts", method="POST", headers=MUTATE, json={"address": "private-machine.invalid:1234"})
        assert saved.status_code == 201
        identifier = saved.json()["host"]["id"]
        assert saved.json()["host"]["routing"]["status"] == "active"
        assert saved.json()["host"]["routing"]["model_count"] == 2
        assert len(calls) == 1, "Saving performs one bounded metadata check and immediately enrolls chat models"
        original = store.path.read_bytes()
        for path in ("/healthz", "/readyz"):
            response = await request(app, path)
            assert response.status_code == 200, "Successful saved catalogs now enroll usable routes"
            summary = response.json()["summary"]
            assert summary["servers"] == 1, "The two compatible APIs share one normalized server origin"
            assert summary["models"] == 2
            assert summary["last_verified_at"] == "2000-01-01T12:00:00+00:00"
            assert summary["models_truncated"] is False
            for secret in (KEY, "private", "invalid", "qwen", "llama", "1234", "Ollama"):
                assert secret not in response.text
        private = await request(app, "/status/data", headers=AUTH)
        assert private.json()["summary"] == summary
        assert len(calls) == 1
        assert store.path.read_bytes() == original
        fresh = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(store.path))
        assert (await request(fresh)).json()["summary"] == EMPTY_SUMMARY
        assert (await request(app, f"/status/hosts/{identifier}", method="DELETE", headers=MUTATE)).status_code == 200
        removed = await request(app)
        assert removed.status_code == 503
        assert removed.json()["summary"] == EMPTY_SUMMARY
        assert len(calls) == 1, "Status reads and removal must not trigger probes"

    asyncio.run(exercise())


@pytest.mark.parametrize("truncated", [False, True])
def test_public_saved_catalog_count_remains_aggregate_only(public_app, monkeypatch, truncated):  # type: ignore[no-untyped-def]
    app, store, _ = public_app
    entry = store.add("private-machine.invalid:1234")

    async def check(host):  # type: ignore[no-untyped-def]
        result = saved_result(host)
        result["checks"][0]["status"] = "fail"
        result["checks"][1]["models_truncated"] = truncated
        if truncated:
            result["checks"][1]["model_count"] = 201
        return result

    monkeypatch.setattr(gateway_module, "check_saved_host", check)

    async def exercise():
        assert (await request(app, "/status/hosts/check", method="POST", headers=MUTATE, json={"id": entry["id"]})).status_code == 200
        response = await request(app)
        summary = response.json()["summary"]
        assert summary["servers"] == 1
        assert summary["models"] == 2, "Only actual cached model IDs are counted, not untrusted aggregate fields"
        assert summary["models_truncated"] is truncated
        assert "private" not in response.text

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["/status/data", "/router/status", "/status/hosts", "/v1/models", "/api/tags"])
@pytest.mark.parametrize("parameter", ["api_key", "%61pi_key", "api%5fkey"])
def test_url_api_key_never_authenticates_api_routes(public_app, path, parameter):  # type: ignore[no-untyped-def]
    app, _, _ = public_app
    response = asyncio.run(request(app, f"{path}?{parameter}={KEY}"))
    assert response.status_code == 401
    assert KEY not in response.text
    assert KEY not in str(response.headers)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/chat", "/api/pull"])
def test_url_key_never_authorizes_inference_or_model_download(public_app, path):  # type: ignore[no-untyped-def]
    app, _, _ = public_app
    response = asyncio.run(request(app, f"{path}?api_key={KEY}", method="POST", json={"model": "auto"}))
    assert response.status_code == 401
    assert KEY not in response.text


@pytest.mark.parametrize("path", ["/status", "/", "/status/assets/app.js", "/status/assets/style.css", "/healthz", "/readyz"])
def test_url_key_is_not_echoed_in_public_pages_json_assets_or_headers(public_app, path):  # type: ignore[no-untyped-def]
    app, _, _ = public_app
    response = asyncio.run(request(app, f"{path}?api_key={KEY}", headers={"Accept": "text/html"}))
    assert response.status_code == (503 if path in ("/healthz", "/readyz") else 200)
    assert KEY not in response.text
    assert KEY not in str(response.headers)
    assert "set-cookie" not in response.headers


def test_query_key_cannot_override_a_valid_bearer_header(public_app):  # type: ignore[no-untyped-def]
    app, _, _ = public_app
    response = asyncio.run(request(app, "/status/data?api_key=wrong-url-key", headers=AUTH))
    assert response.status_code == 200
    assert response.json()["summary"] == EMPTY_SUMMARY
    assert "wrong-url-key" not in response.text
    assert KEY not in response.text


@pytest.mark.parametrize("path,body", [("/status/hosts", {"address": "worker.invalid"}), ("/status/hosts/check", {})])
def test_saved_mutations_still_reject_queries_after_key_redaction(public_app, path, body):  # type: ignore[no-untyped-def]
    app, store, _ = public_app
    response = asyncio.run(request(app, f"{path}?api_key={KEY}", method="POST", headers=MUTATE, json=body))
    assert response.status_code == 400
    assert KEY not in response.text
    assert not store.path.exists()


@pytest.mark.parametrize("query,expected", [
    (b"api_key=private-url-secret", b"api_key=REDACTED"),
    (b"%61pi_key=private-url-secret", b"api_key=REDACTED"),
    (b"api%5fkey=private-url-secret", b"api_key=REDACTED"),
    (b"theme=dark&api_key=private-url-secret&x=1%2F2&api_key=second-secret", b"theme=dark&api_key=REDACTED&x=1%2F2&api_key=REDACTED"),
    (b"api_key=private%2Durl%2Dsecret&%61pi%5Fkey=second-secret", b"api_key=REDACTED&api_key=REDACTED"),
    (b"api_key&api_key=&theme=dark", b"api_key=REDACTED&api_key=REDACTED&theme=dark"),
    (b"theme=dark&other=1%2F2", b"theme=dark&other=1%2F2"),
    (b"", b""),
])
@pytest.mark.parametrize("fail", [False, True])
def test_redaction_mutates_original_logging_scope_before_app_and_error(query, expected, fail):  # type: ignore[no-untyped-def]
    headers = [(b"authorization", f"Bearer {KEY}".encode())]
    scope = {
        "type": "http", "path": "/status", "raw_path": b"/status", "query_string": query,
        "headers": headers, "method": "GET", "scheme": "http", "http_version": "1.1",
    }
    seen = []

    async def downstream(current, receive, send):  # type: ignore[no-untyped-def]
        assert current is scope, "The original Uvicorn logging scope must be redacted, not copied"
        assert current["headers"] is headers
        assert current["headers"][0][1] == f"Bearer {KEY}".encode()
        seen.append(get_path_with_query_string(current))
        assert current["query_string"] == expected
        if fail:
            raise RuntimeError("downstream-error")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # type: ignore[no-untyped-def]
        pass

    async def exercise():
        if fail:
            with pytest.raises(RuntimeError, match="downstream-error"):
                await RedactStatusQueryKey(downstream)(scope, receive, send)
        else:
            await RedactStatusQueryKey(downstream)(scope, receive, send)

    asyncio.run(exercise())
    assert seen
    assert scope["query_string"] == expected
    logged_path = get_path_with_query_string(scope)
    assert "private-url-secret" not in logged_path
    assert "second-secret" not in logged_path
    assert "private%2Durl%2Dsecret" not in logged_path


def test_redactor_leaves_non_http_scopes_untouched():
    scope = {"type": "lifespan"}
    seen = []

    async def downstream(current, receive, send):  # type: ignore[no-untyped-def]
        seen.append(current)

    asyncio.run(RedactStatusQueryKey(downstream)(scope, None, None))
    assert seen == [scope]
    assert scope == {"type": "lifespan"}


def test_installed_app_middleware_redacts_original_scope_even_for_authentication_error(public_app):  # type: ignore[no-untyped-def]
    app, _, _ = public_app
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "path": "/status/data", "raw_path": b"/status/data", "root_path": "", "scheme": "http",
        "query_string": f"api_key={KEY}".encode(), "headers": [(b"host", b"router.test")],
        "server": ("router.test", 80), "client": ("127.0.0.1", 12345),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # type: ignore[no-untyped-def]
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 401
    assert scope["query_string"] == b"api_key=REDACTED"
    assert KEY not in get_path_with_query_string(scope)
    assert KEY not in str(messages)
