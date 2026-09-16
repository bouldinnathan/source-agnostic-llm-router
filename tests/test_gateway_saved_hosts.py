from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.saved_hosts import SavedHostStore


AUTH = {"Authorization": "Bearer router-test-key"}
MUTATE = {**AUTH, "X-LLM-Router-Hosts": "1"}
UNKNOWN_ID = "a" * 24
OPERATIONS = [
    ("GET", "/status/hosts"),
    ("POST", "/status/hosts"),
    ("POST", "/status/hosts/check"),
    ("DELETE", f"/status/hosts/{UNKNOWN_ID}"),
]


async def request(app, method="GET", path="/status/hosts", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.request(method, path, **kwargs)


def checked(entry):  # type: ignore[no-untyped-def]
    return {
        **entry,
        "checked_at": "2026-09-16T12:00:00+00:00",
        "checks": [{
            "provider": "Ollama", "base_url": "http://backend.invalid:11434",
            "status": "pass", "detail": "Metadata only.",
            "http_status": 200, "elapsed_ms": 1.0,
        }],
    }


@pytest.fixture(autouse=True)
def protect_models_and_real_network(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "router-test-key")

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Saved-address API must not use models, discovery, provisioning or real networking")

    for name in ("refresh", "check_health", "provision", "router"):
        monkeypatch.setattr(RouterGateway, name, forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    monkeypatch.setattr(gateway_module, "bootstrap_router", forbidden)
    monkeypatch.setattr(gateway_module, "probe_endpoints", forbidden)
    monkeypatch.setattr(gateway_module, "run_backend_checks", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def setup_hosts(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    tmp_path.chmod(0o700)
    store = SavedHostStore(tmp_path / "saved-hosts.json")
    calls = []

    async def fake_check(entry):  # type: ignore[no-untyped-def]
        calls.append(dict(entry))
        return checked(entry)

    monkeypatch.setattr(gateway_module, "check_saved_host", fake_check)
    app = create_app(gateway=RouterGateway(discovery=False), saved_host_store=store)
    return app, store, calls


@pytest.mark.parametrize("method,path", OPERATIONS)
@pytest.mark.parametrize("key", [None, "", "   "])
def test_saved_address_management_requires_configured_nonblank_key(setup_hosts, monkeypatch, method, path, key):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    if key is None:
        monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", key)
    response = asyncio.run(request(app, method, path, headers=MUTATE, json={"address": "backend.invalid"}))
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("method,path", OPERATIONS)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic router-test-key"])
def test_saved_hosts_require_valid_bearer_key(setup_hosts, method, path, authorization):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    headers = {"X-LLM-Router-Hosts": "1"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = asyncio.run(request(app, method, path, headers=headers, json={}))
    assert response.status_code == 401
    assert "router-test-key" not in response.text
    assert "saved-hosts.json" not in response.text
    assert not store.path.exists()
    assert calls == []


def test_nonascii_incorrect_bearer_is_rejected_without_server_error(setup_hosts):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, headers=[(b"Authorization", "Bearer wr\u00f6ng".encode("latin-1"))]))
    assert response.status_code == 401
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("method,path", OPERATIONS[1:])
@pytest.mark.parametrize("value", [None, "0", "true", "2"])
def test_mutations_require_explicit_dashboard_header(setup_hosts, method, path, value):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    headers = dict(AUTH)
    if value is not None:
        headers["X-LLM-Router-Hosts"] = value
    response = asyncio.run(request(app, method, path, headers=headers, json={"address": "backend.invalid"}))
    assert response.status_code == 403
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("method,path", OPERATIONS[1:])
@pytest.mark.parametrize("origin", ["https://attacker.invalid", "null", "https://router.test", "http://router.test:9999"])
def test_mutations_reject_cross_origin_requests(setup_hosts, method, path, origin):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, method, path, headers={**MUTATE, "Origin": origin}, json={}))
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("method,path", OPERATIONS)
@pytest.mark.parametrize("query", ["address=http://attacker.invalid", "api_key=router-test-key"])
def test_saved_host_queries_never_supply_addresses_or_credentials(setup_hosts, method, path, query):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, method, path + "?" + query, headers=MUTATE, json={}))
    assert response.status_code == 400
    assert "router-test-key" not in response.text
    assert "attacker.invalid" not in response.text
    assert not store.path.exists()
    assert calls == []


def test_list_empty_hosts_does_not_create_storage_or_probe(setup_hosts):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, headers=AUTH))
    assert response.status_code == 200
    assert response.json() == {"hosts": [], "limit": 16}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("origin", [None, "http://router.test"])
def test_crud_persists_normalized_addresses_but_never_probes(setup_hosts, origin):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    headers = dict(MUTATE)
    if origin:
        headers["Origin"] = origin

    async def exercise():
        saved = await request(app, "POST", headers=headers, json={"address": "http://BACKEND.invalid:1234/v1"})
        assert saved.status_code == 201
        row = saved.json()["host"]
        assert row == {"id": row["id"], "address": "http://backend.invalid:1234", "checked_at": None, "checks": []}
        duplicate = await request(app, "POST", headers=headers, json={"address": "http://backend.invalid:1234"})
        assert duplicate.json()["host"] == row
        listed = await request(app, headers=AUTH)
        assert listed.json() == {"hosts": [row], "limit": 16}
        fresh = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(store.path))
        assert (await request(fresh, headers=AUTH)).json()["hosts"] == [row]
        assert json.loads(store.path.read_text())["hosts"] == [{"id": row["id"], "address": row["address"]}]
        deleted = await request(fresh, "DELETE", f"/status/hosts/{row['id']}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json() == {"removed": True}
        assert (await request(app, headers=AUTH)).json()["hosts"] == []
        assert (await request(app, "DELETE", f"/status/hosts/{row['id']}", headers=headers)).status_code == 404

    asyncio.run(exercise())
    assert calls == []
    assert "router-test-key" not in store.path.read_text()


@pytest.mark.parametrize("body", [{}, {"address": None}, {"address": 123}, {"address": ["host"]}, {"address": "host", "token": "secret"}, [], "host"])
def test_add_rejects_incorrect_json_fields(setup_hosts, body):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, "POST", headers=MUTATE, json=body))
    assert response.status_code == 400
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("address", [
    "", "http://user:secret@backend.invalid", "http://backend.invalid?token=secret",
    "file:///etc/passwd", "http://backend.invalid/api/generate", "192.168.1.0/24",
    "backend.invalid\nother.invalid", "backend.invalid other.invalid",
])
def test_add_rejects_unsafe_or_nonindividual_addresses(setup_hosts, address):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, "POST", headers=MUTATE, json={"address": address}))
    assert response.status_code == 400
    assert "secret" not in response.text
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("path", ["/status/hosts", "/status/hosts/check"])
@pytest.mark.parametrize("content,content_type", [
    (b"{}", "text/plain"), (b"{bad json", "application/json"),
    (b"\xff", "application/json"), (b"x" * 4097, "application/json"),
])
def test_mutations_reject_wrong_content_type_invalid_or_large_json(setup_hosts, path, content, content_type):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, "POST", path, headers={**MUTATE, "Content-Type": content_type}, content=content))
    assert response.status_code == 400
    assert not store.path.exists()
    assert calls == []


def test_chunked_body_limit_is_enforced_without_content_length(setup_hosts):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts

    async def chunks():
        yield b'{"address":"'
        yield b"a" * 4096
        yield b'"}'

    response = asyncio.run(request(app, "POST", headers={**MUTATE, "Content-Type": "application/json"}, content=chunks()))
    assert response.status_code == 400
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("path", ["/status/hosts", "/status/hosts/check"])
def test_excessively_nested_but_small_json_is_a_client_error(setup_hosts, path):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(
        app, "POST", path, headers={**MUTATE, "Content-Type": "application/json"},
        content=b"[" * 1200 + b"]" * 1200,
    ))
    assert response.status_code == 400
    assert not store.path.exists()
    assert calls == []


@pytest.mark.parametrize("body", [
    {"id": ""}, {"id": None}, {"id": 123}, {"id": "a" * 65},
    {"address": "http://attacker.invalid"}, {"id": UNKNOWN_ID, "url": "http://attacker.invalid"},
    {"headers": {"Authorization": "Bearer secret"}}, [],
])
def test_checks_accept_only_an_optional_saved_identifier(setup_hosts, body):  # type: ignore[no-untyped-def]
    app, _, calls = setup_hosts
    response = asyncio.run(request(app, "POST", "/status/hosts/check", headers=MUTATE, json=body))
    assert response.status_code == 400
    assert "attacker.invalid" not in response.text
    assert "secret" not in response.text
    assert calls == []


def test_unknown_saved_identifier_does_not_probe(setup_hosts):  # type: ignore[no-untyped-def]
    app, _, calls = setup_hosts
    response = asyncio.run(request(app, "POST", "/status/hosts/check", headers=MUTATE, json={"id": UNKNOWN_ID}))
    assert response.status_code == 404
    assert calls == []


@pytest.mark.parametrize("identifier", ["bad-id", "a" * 23, "a" * 25, "Z" * 24])
def test_invalid_delete_identifier_is_a_client_error_not_storage_failure(setup_hosts, identifier):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, "DELETE", f"/status/hosts/{identifier}", headers=MUTATE))
    assert response.status_code == 400
    assert not store.path.exists()
    assert calls == []


def test_checks_use_stored_targets_only_and_cache_results_without_persisting_them(setup_hosts, monkeypatch):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    first = store.add("first.invalid")
    second = store.add("second.invalid:1234")
    disk_before = store.path.read_bytes()
    clock = [100.0]
    monkeypatch.setattr(gateway_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exercise():
        response = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={"id": first["id"]})
        assert response.status_code == 200
        assert response.json() == {"hosts": [checked(first)]}
        assert calls == [first]
        assert set(calls[0]) == {"id", "address"}
        listed = (await request(app, headers=AUTH)).json()["hosts"]
        assert listed == [checked(first), {**second, "checked_at": None, "checks": []}]
        assert calls == [first]
        assert store.path.read_bytes() == disk_before
        fresh = create_app(gateway=RouterGateway(discovery=False), saved_host_store=SavedHostStore(store.path))
        restarted = (await request(fresh, headers=AUTH)).json()["hosts"]
        assert all(row["checked_at"] is None and row["checks"] == [] for row in restarted)
        clock[0] += 4.0
        response = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={})
        assert response.status_code == 200
        assert response.json()["hosts"] == [checked(first), checked(second)]
        assert calls == [first, first, second]
        assert store.path.read_bytes() == disk_before

    asyncio.run(exercise())


def test_completed_checks_have_three_second_global_cooldown(setup_hosts, monkeypatch):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    first = store.add("first.invalid")
    second = store.add("second.invalid")
    clock = [100.0]
    monkeypatch.setattr(gateway_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exercise():
        first_reply = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={"id": first["id"]})
        assert first_reply.status_code == 200
        blocked = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={"id": second["id"]})
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "3"
        assert calls == [first]
        clock[0] += 3.0
        allowed = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={"id": second["id"]})
        assert allowed.status_code == 200
        assert calls == [first, second]

    asyncio.run(exercise())


def test_inflight_check_is_not_duplicated_and_delete_does_not_resurrect_results(setup_hosts, monkeypatch):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    entry = store.add("backend.invalid")

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_check(host):  # type: ignore[no-untyped-def]
            calls.append(dict(host))
            started.set()
            await release.wait()
            return checked(host)

        monkeypatch.setattr(gateway_module, "check_saved_host", slow_check)
        pending = asyncio.create_task(request(app, "POST", "/status/hosts/check", headers=MUTATE, json={}))
        try:
            await asyncio.wait_for(started.wait(), 1.0)
            busy = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={})
            assert busy.status_code == 429
            assert busy.headers["retry-after"] == "3"
            assert calls == [entry]
            assert (await request(app, "DELETE", f"/status/hosts/{entry['id']}", headers=MUTATE)).status_code == 200
        finally:
            release.set()
        completed = await pending
        assert completed.status_code == 200
        assert completed.json() == {"hosts": []}
        assert (await request(app, headers=AUTH)).json()["hosts"] == []
        assert store.list() == []

    asyncio.run(exercise())


@pytest.mark.parametrize("method,path", OPERATIONS)
@pytest.mark.parametrize("original", [
    b"private-corrupt-content-secret",
    b'{"version":1,"hosts":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaa","address":{"secret":"private-corrupt-content-secret"}}]}',
    b'{"version":999,"hosts":[],"secret":"private-corrupt-content-secret"}',
])
def test_corrupt_storage_is_preserved_and_error_is_sanitized(setup_hosts, method, path, original):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    store.path.write_bytes(original)
    store.path.chmod(0o600)
    body = {"address": "backend.invalid"} if path == "/status/hosts" else {}
    response = asyncio.run(request(app, method, path, headers=MUTATE, json=body))
    assert response.status_code == 503
    assert "private-corrupt-content-secret" not in response.text
    assert str(store.path) not in response.text
    assert "router-test-key" not in response.text
    assert store.path.read_bytes() == original
    assert calls == []


@pytest.mark.parametrize("error,status", [(OSError("private-network-secret"), 503), (RuntimeError("private-runtime-secret"), 503), (asyncio.TimeoutError("private-timeout-secret"), 504)])
def test_probe_failures_do_not_leak_exceptions_and_still_enforce_cooldown(setup_hosts, monkeypatch, error, status):  # type: ignore[no-untyped-def]
    app, store, _ = setup_hosts
    store.add("backend.invalid")

    async def broken_check(entry):  # type: ignore[no-untyped-def]
        raise error

    monkeypatch.setattr(gateway_module, "check_saved_host", broken_check)

    async def exercise():
        response = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={})
        assert response.status_code == status
        assert "private-" not in response.text
        assert "router-test-key" not in response.text
        cooldown = await request(app, "POST", "/status/hosts/check", headers=MUTATE, json={})
        assert cooldown.status_code == 429
        listed = (await request(app, headers=AUTH)).json()["hosts"]
        assert listed[0]["checked_at"] is None
        assert listed[0]["checks"] == []

    asyncio.run(exercise())


def test_empty_explicit_check_has_no_targets(setup_hosts):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    response = asyncio.run(request(app, "POST", "/status/hosts/check", headers=MUTATE, json={}))
    assert response.status_code == 200
    assert response.json() == {"hosts": []}
    assert not store.path.exists()
    assert calls == []


def test_host_limit_rejects_seventeenth_without_probing_or_replacing_entries(setup_hosts):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    for index in range(16):
        store.add(f"host-{index}.invalid")
    previous = store.path.read_bytes()
    response = asyncio.run(request(app, "POST", headers=MUTATE, json={"address": "extra.invalid"}))
    assert response.status_code == 400
    assert "16" in response.text
    assert store.path.read_bytes() == previous
    assert calls == []


@pytest.mark.parametrize("path", ["/status", "/status/data", "/healthz", "/status/assets/app.js", "/status/hosts"])
def test_passive_status_reads_never_check_saved_addresses(setup_hosts, path):  # type: ignore[no-untyped-def]
    app, store, calls = setup_hosts
    store.add("backend.invalid")
    response = asyncio.run(request(app, path=path, headers=AUTH))
    assert response.status_code in (200, 503)
    assert calls == []
