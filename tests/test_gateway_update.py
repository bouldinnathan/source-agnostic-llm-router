from __future__ import annotations

import asyncio
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter


KEY = "update-private-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
MUTATE = {**AUTH, "X-LLM-Router-Update": "1"}
PRIVATE_ERROR = "private-subprocess-secret /private/install/status.json ?api_key=private-key"


@dataclass
class FakeController:
    status_payload: dict[str, Any] = field(default_factory=lambda: {
        "available": True, "running": False, "stage": "idle",
        "message": "Ready to check the official main branch.",
    })
    start_payload: dict[str, Any] = field(default_factory=lambda: {
        "available": True, "running": True, "stage": "queued",
        "message": "Update check queued.",
    })
    status_error: Exception | None = None
    start_error: Exception | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", threading.get_ident()))
        if self.status_error is not None:
            raise self.status_error
        return dict(self.status_payload)

    def start(self) -> dict[str, Any]:
        self.calls.append(("start", threading.get_ident()))
        if self.start_error is not None:
            raise self.start_error
        return dict(self.start_payload)


async def request(app, method="GET", path="/status/update", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.request(method, path, **kwargs)


def safe_headers(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    policy = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert directive in policy
    assert response.headers["content-type"].startswith("application/json")
    assert "access-control-allow-origin" not in response.headers
    assert "set-cookie" not in response.headers


def no_private_error(response: httpx.Response) -> None:
    public = response.text + str(dict(response.headers))
    for private in (KEY, PRIVATE_ERROR, "private-subprocess-secret", "/private/install", "private-key"):
        assert private not in public


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", KEY)

    def no_process(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Update API tests must never execute a real process")

    async def no_network_or_models(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Update API must not discover, provision, or invoke models")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network_or_models)
    monkeypatch.setattr(LLMRouter, "complete", no_network_or_models)
    for name in ("refresh", "check_health", "provision", "router"):
        monkeypatch.setattr(RouterGateway, name, no_network_or_models)
    for name in ("bootstrap_router", "probe_endpoints", "run_backend_checks", "check_saved_host"):
        monkeypatch.setattr(gateway_module, name, no_network_or_models)


@pytest.fixture
def setup_update():  # type: ignore[no-untyped-def]
    controller = FakeController()
    app = create_app(gateway=RouterGateway(discovery=False), update_controller=controller)
    return app, controller


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("key", [None, "", "   "])
def test_management_requires_configured_nonblank_key(setup_update, monkeypatch, method, key):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    if key is None:
        monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", key)
    response = asyncio.run(request(app, method, headers=MUTATE))
    assert response.status_code == 403
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer", "Basic " + KEY])
def test_bearer_auth_precedes_all_controller_access(setup_update, method, authorization):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    headers = {"X-LLM-Router-Update": "1"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = asyncio.run(request(app, method, headers=headers))
    assert response.status_code == 401
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_incorrect_nonascii_bearer_does_not_raise(setup_update, method):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, method, headers={
        b"Authorization": "Bearer \u2603".encode(), b"X-LLM-Router-Update": b"1",
    }))
    assert response.status_code == 401
    assert controller.calls == []
    safe_headers(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("query", [
    f"api_key={KEY}", f"%61pi_key={KEY}", "unit=other.service", "revision=evil",
    "install_dir=/private/install", "repo=https://untrusted.invalid/code", "empty=", "unknown",
])
def test_authenticated_queries_never_select_update_parameters(setup_update, method, query):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, method, "/status/update?" + query, headers=MUTATE))
    assert response.status_code == 400
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_url_key_never_authorizes_updates(setup_update, method):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, method, f"/status/update?api_key={KEY}", headers={
        "X-LLM-Router-Update": "1",
    }))
    assert response.status_code == 401
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("marker", [None, "0", "true", "2", "1 "])
def test_post_requires_explicit_csrf_header(setup_update, marker):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    headers = dict(AUTH)
    if marker is not None:
        headers["X-LLM-Router-Update"] = marker
    response = asyncio.run(request(app, "POST", headers=headers))
    assert response.status_code == 403
    assert controller.calls == []
    safe_headers(response)


@pytest.mark.parametrize("origin", [
    "http://attacker.invalid", "null", "https://router.test", "http://router.test:9999",
    "http://router.test.attacker.invalid", "http://router.test/",
])
def test_cross_origin_post_never_starts_updater(setup_update, origin):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, "POST", headers={**MUTATE, "Origin": origin}))
    assert response.status_code == 403
    assert controller.calls == []
    safe_headers(response)


@pytest.mark.parametrize("content", [
    b"{}", b" ", b"null", b"\x00", b'{"revision":"main"}',
    b'{"unit":"other.service"}', b"x" * 1024, b"x" * 1025, b"x" * 65536,
])
def test_post_accepts_no_body_or_client_update_arguments(setup_update, content):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, "POST", headers={
        **MUTATE, "Content-Type": "application/json",
    }, content=content))
    assert response.status_code == 400
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


def test_chunked_nonempty_body_cannot_bypass_empty_body_rule(setup_update):  # type: ignore[no-untyped-def]
    app, controller = setup_update

    async def chunks():
        yield b""
        yield b"{"
        yield b"}"

    response = asyncio.run(request(app, "POST", headers=MUTATE, content=chunks()))
    assert response.status_code == 400
    assert controller.calls == []
    safe_headers(response)


def test_body_read_has_bounded_deadline_and_timeout_never_starts_update(setup_update, monkeypatch):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    original_wait_for = asyncio.wait_for
    deadlines = []

    async def quick_deadline(awaitable, timeout):  # type: ignore[no-untyped-def]
        deadlines.append(timeout)
        # Exercise real timeout/cancellation without spending three seconds on
        # an intentionally stalled transport in the regression suite.
        return await original_wait_for(awaitable, timeout=0.01)

    async def stalled():
        await asyncio.Event().wait()
        yield b""  # pragma: no cover

    monkeypatch.setattr(gateway_module.asyncio, "wait_for", quick_deadline)
    response = asyncio.run(request(app, "POST", headers=MUTATE, content=stalled()))
    assert deadlines == [3.0]
    assert response.status_code == 400
    assert controller.calls == []
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("headers,path,expected", [
    ({"X-LLM-Router-Update": "1"}, "/status/update", 401),
    (AUTH, "/status/update", 403),
    ({**MUTATE, "Origin": "http://attacker.invalid"}, "/status/update", 403),
    (MUTATE, "/status/update?revision=evil", 400),
])
def test_invalid_request_is_rejected_before_reading_body(setup_update, headers, path, expected):  # type: ignore[no-untyped-def]
    app, controller = setup_update

    async def unreadable():
        raise AssertionError("Unauthorized or malformed update request body must not be consumed")
        yield b""  # pragma: no cover

    response = asyncio.run(request(app, "POST", path, headers=headers, content=unreadable()))
    assert response.status_code == expected
    assert controller.calls == []
    safe_headers(response)


@pytest.mark.parametrize("origin", [None, "http://router.test"])
def test_empty_authenticated_post_queues_only_controller_start_in_worker(setup_update, origin):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    headers = dict(MUTATE)
    if origin is not None:
        headers["Origin"] = origin
    event_loop_thread = threading.get_ident()
    response = asyncio.run(request(app, "POST", headers=headers))
    assert response.status_code == 202
    assert response.json() == controller.start_payload
    assert len(controller.calls) == 1
    name, worker = controller.calls[0]
    assert name == "start"
    assert worker != event_loop_thread
    safe_headers(response)


@pytest.mark.parametrize("available", [True, False])
def test_get_reads_status_in_worker_without_starting_update(setup_update, available):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    controller.status_payload["available"] = available
    event_loop_thread = threading.get_ident()
    response = asyncio.run(request(app, headers=AUTH))
    assert response.status_code == 200
    assert response.json() == controller.status_payload
    assert len(controller.calls) == 1
    name, worker = controller.calls[0]
    assert name == "status"
    assert worker != event_loop_thread
    safe_headers(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TypeError, OSError])
def test_unexpected_controller_errors_are_generic_and_private(setup_update, method, error_type):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    setattr(controller, "status_error" if method == "GET" else "start_error", error_type(PRIVATE_ERROR))
    response = asyncio.run(request(app, method, headers=MUTATE))
    assert response.status_code == 503
    payload = response.json()
    assert isinstance(payload, dict) and payload.get("error")
    assert len(controller.calls) == 1
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("status_code,message", [
    (409, "An update is already running."),
    (503, "Updates are unavailable for this installation."),
])
def test_expected_start_errors_keep_safe_fixed_reason(setup_update, status_code, message):  # type: ignore[no-untyped-def]
    from llm_router.update_control import UpdateRequestError

    app, controller = setup_update
    controller.start_error = UpdateRequestError(message, status_code=status_code)
    response = asyncio.run(request(app, "POST", headers=MUTATE))
    assert response.status_code == status_code
    assert message in response.text
    assert len(controller.calls) == 1 and controller.calls[0][0] == "start"
    safe_headers(response)
    no_private_error(response)


@pytest.mark.parametrize("path", ["/", "/status", "/status/assets/app.js", "/healthz", "/readyz"])
def test_public_status_reads_do_not_inspect_or_trigger_updates(setup_update, path):  # type: ignore[no-untyped-def]
    app, controller = setup_update
    response = asyncio.run(request(app, path=path))
    assert response.status_code in {200, 503}
    assert controller.calls == []
    no_private_error(response)


def test_controllers_are_app_local_not_global():
    first, second = FakeController(), FakeController()
    app = create_app(gateway=RouterGateway(discovery=False), update_controller=first)
    other = create_app(gateway=RouterGateway(discovery=False), update_controller=second)
    assert asyncio.run(request(app, "POST", headers=MUTATE)).status_code == 202
    assert asyncio.run(request(other, headers=AUTH)).status_code == 200
    assert [name for name, _ in first.calls] == ["start"]
    assert [name for name, _ in second.calls] == ["status"]
