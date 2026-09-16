from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.responses import JSONResponse

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter

from test_status_page import gateway_with_router


SELF_TEST_HEADERS = {"X-LLM-Router-Self-Test": "1"}
AUTH_HEADERS = {**SELF_TEST_HEADERS, "Authorization": "Bearer router-test-key"}


def backend_row(status: str = "pass") -> dict[str, Any]:
    return {
        "name": "Backend metadata", "target": "http://backend.invalid:1234/v1/models",
        "status": status, "detail": "Metadata check only.",
        "elapsed_ms": 0.1, "http_status": 200 if status == "pass" else None,
    }


async def request(app, method: str = "POST", path: str = "/status/self-test", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture(autouse=True)
def no_discovery_health_provisioning_or_inference(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Self-test must not invoke discovery, health changes, provisioning or models")

    for name in ("refresh", "check_health", "provision"):
        monkeypatch.setattr(RouterGateway, name, forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    monkeypatch.setattr(gateway_module, "bootstrap_router", forbidden)
    monkeypatch.setattr(gateway_module, "probe_endpoints", forbidden)


@pytest.fixture
def backend_checks(monkeypatch):  # type: ignore[no-untyped-def]
    calls = []

    async def check(config):  # type: ignore[no-untyped-def]
        calls.append(config)
        return [backend_row()]

    monkeypatch.setattr(gateway_module, "run_backend_checks", check)
    return calls


@pytest.mark.parametrize("method,path", [
    ("GET", "/status"), ("GET", "/status/data"),
    ("GET", "/status/assets/app.js"), ("GET", "/healthz"),
    ("GET", "/status/self-test"), ("HEAD", "/status/self-test"),
])
def test_page_refreshes_never_run_the_explicit_self_test(backend_checks, method, path) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    response = asyncio.run(request(create_app(gateway=gateway), method, path))
    assert response.status_code == (405 if path.endswith("self-test") else 200)
    assert backend_checks == []


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-key", "Basic router-test-key"])
def test_self_test_requires_router_authentication(monkeypatch, backend_checks, authorization) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, _ = gateway_with_router()
    headers = dict(SELF_TEST_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    response = asyncio.run(request(create_app(gateway=gateway), headers=headers))
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert "router-test-key" not in response.text
    assert "source-a" not in response.text
    assert backend_checks == []


@pytest.mark.parametrize("header_value", [None, "0", "true"])
@pytest.mark.parametrize("auth_enabled", [False, True])
def test_explicit_header_is_required_even_without_auth(monkeypatch, backend_checks, header_value, auth_enabled) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    headers = {}
    if auth_enabled:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
        headers["Authorization"] = "Bearer router-test-key"
    if header_value is not None:
        headers["X-LLM-Router-Self-Test"] = header_value
    response = asyncio.run(request(create_app(gateway=gateway), headers=headers))
    assert response.status_code == 403
    assert backend_checks == []


@pytest.mark.parametrize("origin", ["https://attacker.invalid", "null", "http://router.test:9999", "https://router.test"])
@pytest.mark.parametrize("auth_enabled", [False, True])
def test_self_test_rejects_cross_origin_requests(monkeypatch, backend_checks, origin, auth_enabled) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    if auth_enabled:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    response = asyncio.run(request(
        create_app(gateway=gateway), headers={**AUTH_HEADERS, "Origin": origin},
    ))
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert backend_checks == []


@pytest.mark.parametrize("origin", [None, "http://router.test"])
def test_same_origin_or_command_line_self_test_accepts_correct_key(monkeypatch, backend_checks, origin) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, router = gateway_with_router()
    headers = dict(AUTH_HEADERS)
    if origin is not None:
        headers["Origin"] = origin
    response = asyncio.run(request(create_app(gateway=gateway), headers=headers))
    payload = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert payload["status"] == "pass"
    assert payload["checked_at"]
    assert "No prompts" in payload["notice"]
    assert "router-test-key" not in response.text
    assert backend_checks == [router.config]
    auth_check = next(row for row in payload["checks"] if row["name"] == "Gateway authentication")
    assert auth_check["status"] == "pass"
    assert auth_check["http_status"] == 401


@pytest.mark.parametrize("query", ["url=http://attacker.invalid", "port=1234", "api_key=router-test-key"])
def test_query_parameters_cannot_select_targets_or_supply_credentials(backend_checks, query) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    response = asyncio.run(request(
        create_app(gateway=gateway), path=f"/status/self-test?{query}", headers=SELF_TEST_HEADERS,
    ))
    assert response.status_code == 400
    assert "attacker.invalid" not in response.text
    assert "router-test-key" not in response.text
    assert backend_checks == []


def test_arbitrary_json_body_cannot_select_backend_targets(backend_checks) -> None:  # type: ignore[no-untyped-def]
    gateway, router = gateway_with_router()
    response = asyncio.run(request(
        create_app(gateway=gateway), headers=SELF_TEST_HEADERS,
        json={"url": "http://attacker.invalid", "port": 9999, "path": "/api/generate"},
    ))
    assert response.status_code == 200
    assert backend_checks == [router.config]
    assert "attacker.invalid" not in response.text
    assert "/api/generate" not in response.text


def test_disabled_auth_is_explicitly_reported_as_skipped(backend_checks) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    response = asyncio.run(request(create_app(gateway=gateway), headers=SELF_TEST_HEADERS))
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    auth_check = next(row for row in response.json()["checks"] if row["name"] == "Gateway authentication")
    assert auth_check["status"] == "skip"
    assert "disabled" in auth_check["detail"]


def test_no_cached_fleet_skips_catalogs_without_attempting_discovery(monkeypatch, backend_checks) -> None:  # type: ignore[no-untyped-def]
    gateway = RouterGateway(discovery=False)

    async def forbidden_router():  # type: ignore[no-untyped-def]
        raise AssertionError("An empty fleet must not be initialized by self-test")

    monkeypatch.setattr(gateway, "router", forbidden_router)
    response = asyncio.run(request(create_app(gateway=gateway), headers=SELF_TEST_HEADERS))
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert backend_checks == [None]
    rows = {row["target"]: row for row in response.json()["checks"]}
    for target in ("/v1/models", "/api/tags"):
        assert rows[target]["status"] == "skip"
        assert rows[target]["http_status"] is None
        assert "discovery" in rows[target]["detail"]
    for target in ("/healthz", "/readyz"):
        assert rows[target]["status"] == "skip"
        assert rows[target]["http_status"] == 503
    assert rows["/"]["status"] == "pass"
    assert rows["/api/version"]["status"] == "pass"


def test_self_test_exercises_real_read_only_routes_and_preserves_runtime(monkeypatch, backend_checks) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, router = gateway_with_router()
    router.runtime.record_endpoint_probe("source-a", False, "offline reason")
    router.runtime.record_endpoint_probe("source-b", True)
    router.runtime.begin("qwen-b")
    router.runtime.record_success("qwen-b", 25)
    router.runtime.record_failure("qwen-a", "earlier inference failure")
    # Initialize lazily-created empty deployment records before comparing state.
    router.runtime.snapshot(router.config.models)
    before = deepcopy((router.runtime._states, router.runtime._endpoint_states))
    observed = []

    class ObserveRequests:
        def __init__(self, app):  # type: ignore[no-untyped-def]
            self.app = app

        async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
            if scope["type"] == "http":
                observed.append((scope["method"], scope["path"]))
            await self.app(scope, receive, send)

    app = create_app(gateway=gateway)
    app.add_middleware(ObserveRequests)
    response = asyncio.run(request(app, headers=AUTH_HEADERS))
    assert response.status_code == 200
    assert response.json()["status"] == "pass"
    assert observed == [
        ("POST", "/status/self-test"), ("GET", "/"), ("GET", "/healthz"),
        ("GET", "/readyz"), ("GET", "/status/data"), ("GET", "/api/version"),
        ("GET", "/v1/models"), ("GET", "/api/tags"), ("GET", "/status/data"),
    ]
    assert (router.runtime._states, router.runtime._endpoint_states) == before


@pytest.mark.parametrize("backend_status,expected", [("pass", "pass"), ("skip", "partial"), ("fail", "fail")])
def test_overall_result_accounts_for_backend_results(monkeypatch, backend_status, expected) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, _ = gateway_with_router()

    async def checks(config):  # type: ignore[no-untyped-def]
        return [backend_row(backend_status)]

    monkeypatch.setattr(gateway_module, "run_backend_checks", checks)
    response = asyncio.run(request(create_app(gateway=gateway), headers=AUTH_HEADERS))
    assert response.status_code == 200
    assert response.json()["status"] == expected


@pytest.mark.parametrize("bad_payload,status", [({"private": "upstream-secret"}, 200), ({"data": []}, 502)])
def test_actual_catalog_failure_is_reported_without_response_body_leak(monkeypatch, backend_checks, bad_payload, status) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, _ = gateway_with_router()
    app = create_app(gateway=gateway)
    route = next(route for route in app.routes if route.path == "/v1/models")

    async def broken(scope, receive, send):  # type: ignore[no-untyped-def]
        await JSONResponse(bad_payload, status_code=status)(scope, receive, send)

    route.app = broken
    response = asyncio.run(request(app, headers=AUTH_HEADERS))
    assert response.status_code == 200
    assert response.json()["status"] == "fail"
    assert "upstream-secret" not in response.text
    check = next(row for row in response.json()["checks"] if row["target"] == "/v1/models")
    assert check["status"] == "fail"
    assert check["http_status"] == status


def test_concurrent_self_tests_are_bounded_to_one_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()

    async def scenario():  # type: ignore[no-untyped-def]
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def checks(config):  # type: ignore[no-untyped-def]
            calls.append(config)
            entered.set()
            await release.wait()
            return [backend_row()]

        monkeypatch.setattr(gateway_module, "run_backend_checks", checks)
        app = create_app(gateway=gateway)
        first = asyncio.create_task(request(app, headers=SELF_TEST_HEADERS))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await request(app, headers=SELF_TEST_HEADERS)
            assert second.status_code == 429
            assert second.headers["retry-after"] == "5"
            assert len(calls) == 1
        finally:
            release.set()
            completed = await first
        assert completed.status_code == 200
        assert len(calls) == 1

    asyncio.run(scenario())


def test_immediate_repeat_is_rate_limited_then_recovers(monkeypatch, backend_checks) -> None:  # type: ignore[no-untyped-def]
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(gateway_module, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=gateway_module.time.time,
    ))
    gateway, _ = gateway_with_router()
    app = create_app(gateway=gateway)
    first = asyncio.run(request(app, headers=SELF_TEST_HEADERS))
    second = asyncio.run(request(app, headers=SELF_TEST_HEADERS))
    assert first.status_code == 200
    assert first.json()["status"] == "partial"
    assert second.status_code == 429
    assert second.headers["retry-after"] == "5"
    assert len(backend_checks) == 1
    clock.now += 6
    third = asyncio.run(request(app, headers=SELF_TEST_HEADERS))
    assert third.status_code == 200
    assert third.json()["status"] == "partial"
    assert len(backend_checks) == 2


def test_internal_exception_is_sanitized_and_still_enforces_cooldown(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    calls = []

    async def broken(config):  # type: ignore[no-untyped-def]
        calls.append(config)
        raise RuntimeError("private-token-value at http://user:password@private.invalid")

    monkeypatch.setattr(gateway_module, "run_backend_checks", broken)
    app = create_app(gateway=gateway)
    first = asyncio.run(request(app, headers=SELF_TEST_HEADERS))
    assert first.status_code == 500
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == {"error": "Self-test could not be completed; try again."}
    assert "private-token-value" not in first.text
    assert "password" not in first.text
    second = asyncio.run(request(app, headers=SELF_TEST_HEADERS))
    assert second.status_code == 429
    assert len(calls) == 1


def test_valid_api_key_in_query_is_not_accepted_as_authentication(monkeypatch, backend_checks) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")
    gateway, _ = gateway_with_router()
    response = asyncio.run(request(
        create_app(gateway=gateway), path="/status/self-test?api_key=router-test-key",
        headers=SELF_TEST_HEADERS,
    ))
    assert response.status_code == 401
    assert "router-test-key" not in response.text
    assert backend_checks == []
