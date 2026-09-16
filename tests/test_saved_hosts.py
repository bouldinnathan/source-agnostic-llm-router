from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import time

import httpx
import pytest

from llm_router import saved_hosts
from llm_router.saved_hosts import SavedHostStore, check_saved_host, normalize_address


@pytest.mark.parametrize(("value", "expected"), [
    ("192.168.194.0", "192.168.194.0"),
    (" Worker.LAN ", "worker.lan"),
    ("worker.lan.", "worker.lan"),
    ("worker", "worker"),
    ("127.0.0.1", "127.0.0.1"),
    ("worker:1234", "http://worker:1234"),
    ("worker:80", "http://worker"),
    ("HTTP://WORKER:80/v1/", "http://worker"),
    ("https://WORKER:443/v1", "https://worker"),
    ("https://worker:4321/", "https://worker:4321"),
    ("http://192.168.194.0:1234/v1", "http://192.168.194.0:1234"),
    ("fd12::001", "[fd12::1]"),
    ("[fd12::001]", "[fd12::1]"),
    ("[fd12::001]:1234", "http://[fd12::1]:1234"),
    ("http://[::1]:1234/v1/", "http://[::1]:1234"),
])
def test_normalize_address(value, expected):
    assert normalize_address(value) == expected
    assert normalize_address(expected) == expected


@pytest.mark.parametrize("value", [
    "", None, 123, "a" * 257, " ", "worker\n", "worker\t", "work\x00er",
    "two hosts", "worker\x7f", "worker:0", "worker:65536", "worker:abc", "worker:",
    "http://worker:", "http://", "//worker", "ftp://worker", "file:///etc/passwd",
    "http://user:secret@worker", "user@worker", "http://worker?key=secret",
    "http://worker?", "http://worker#", "http://worker#fragment", "http://%77orker",
    "http://worker\\@evil", "http://worker/api/generate", "http://worker/v1/../api/pull",
    "worker/24", "192.168.1.0/24", "192.168.1.1-10", "192.168.1.*", "worker/v1",
    "http://worker//v1", "http://worker/%2fv1", "[fe80::1%eth0]", "fe80::1",
    "0.0.0.0", "http://0.0.0.0:1234", "::", "http://[::]", "224.0.0.1",
    "ff02::1", "169.254.169.254", "http://[::ffff:169.254.169.254]", "255.255.255.255",
    "127.1", "999.0.0.1", "-worker", "worker-", "worker..lan", "worker..", "_worker", "💻.lan",
    "http://worker\u2028.lan", "[bad-ip]", "http://[::1]:bad", "http://[::1]evil",
])
def test_reject_ambiguous_or_unsafe_addresses(value):
    with pytest.raises(ValueError):
        normalize_address(value)


def test_constructor_and_empty_list_do_not_create_files(tmp_path):
    path = tmp_path / "not-created" / "saved-hosts.json"
    store = SavedHostStore(path)
    assert store.list() == []
    assert not path.parent.exists()


def test_default_path_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_ROUTER_SAVED_HOSTS_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SavedHostStore().path == tmp_path / "llm-router" / "saved-hosts.json"
    override = tmp_path / "override" / "hosts.json"
    monkeypatch.setenv("LLM_ROUTER_SAVED_HOSTS_FILE", str(override))
    assert SavedHostStore().path == override
    assert not override.exists()


def test_save_deduplicate_restart_and_remove(tmp_path):
    path = tmp_path / "router" / "saved-hosts.json"
    store = SavedHostStore(path)
    first = store.add("HTTP://WORKER:1234/v1/")
    assert first == store.add("worker:1234")
    second = store.add("worker")
    restarted = SavedHostStore(path)
    assert restarted.list() == [first, second]
    assert set(first) == {"id", "address"}
    assert len(first["id"]) == 24
    assert first["id"] != second["id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"version": 1, "hosts": [first, second]}
    assert restarted.remove(first["id"])
    assert not restarted.remove(first["id"])
    assert SavedHostStore(path).list() == [second]
    assert restarted.remove(second["id"])
    assert restarted.list() == []
    assert restarted.add(first["address"]) == first


def test_invalid_input_never_creates_storage(tmp_path):
    path = tmp_path / "router" / "hosts.json"
    store = SavedHostStore(path)
    with pytest.raises(ValueError):
        store.add("http://secret@worker")
    with pytest.raises(ValueError):
        store.remove("../../other")
    assert not store.remove("0" * 24)
    assert not path.parent.exists()


def test_host_limit_allows_existing_entry(tmp_path):
    store = SavedHostStore(tmp_path / "saved-hosts.json")
    hosts = [store.add(f"worker-{index}") for index in range(16)]
    assert store.add("worker-0") == hosts[0]
    with pytest.raises(ValueError, match="16"):
        store.add("worker-16")
    assert store.list() == hosts


@pytest.mark.parametrize("payload", [
    b"not-json-private-secret", b"\xff", b"[]", b"null", b'{"version":true,"hosts":[]}',
    b'{"version":2,"hosts":[]}', b'{"version":1,"hosts":"secret"}',
    b'{"version":1,"hosts":[],"secret":"hidden"}',
    b'{"version":1,"hosts":[{"id":"bad","address":"worker"}]}',
    b'{"version":1,"hosts":[{"id":"bad","address":123}]}',
    b'{"version":1,"hosts":["private-secret"],"hosts":[]}',
    b'{"version":1,"version":1,"hosts":[]}',
    b"[" * 2000, b"x" * (32 * 1024 + 1),
])
def test_corrupt_state_never_overwritten_or_echoed(tmp_path, payload):
    path = tmp_path / "hosts.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    store = SavedHostStore(path)
    for action in (store.list, lambda: store.add("worker"), lambda: store.remove("0" * 24)):
        with pytest.raises(RuntimeError, match="invalid") as caught:
            action()
        assert "secret" not in str(caught.value)
        assert path.read_bytes() == payload


def test_corrupt_duplicate_state_refused(tmp_path):
    path = tmp_path / "hosts.json"
    store = SavedHostStore(path)
    entry = store.add("worker")
    path.write_text(json.dumps({"version": 1, "hosts": [entry, entry]}))
    with pytest.raises(RuntimeError):
        store.list()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo", "public"])
def test_unsafe_state_files_refused_and_preserved(tmp_path, unsafe_kind):
    path = tmp_path / "hosts.json"
    target = tmp_path / "original.json"
    target.write_text('{"version":1,"hosts":[]}')
    target.chmod(0o600)
    if unsafe_kind == "symlink":
        path.symlink_to(target)
    elif unsafe_kind == "hardlink":
        os.link(target, path)
    elif unsafe_kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text(target.read_text())
        path.chmod(0o644)
    store = SavedHostStore(path)
    with pytest.raises((RuntimeError, OSError)):
        store.add("worker")
    assert target.read_text() == '{"version":1,"hosts":[]}'
    if unsafe_kind == "symlink":
        assert path.is_symlink()
    elif unsafe_kind == "fifo":
        assert stat.S_ISFIFO(path.stat().st_mode)


def test_symlinked_parent_or_lock_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError):
        SavedHostStore(linked / "hosts.json").add("worker")
    assert list(real.iterdir()) == []
    target = tmp_path / "sensitive"
    target.write_text("private")
    (real / "hosts.json.lock").symlink_to(target)
    with pytest.raises(OSError):
        SavedHostStore(real / "hosts.json").add("worker")
    assert target.read_text() == "private"


def test_nonprivate_directory_is_not_silently_changed(tmp_path):
    path = tmp_path / "public"
    path.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="private"):
        SavedHostStore(path / "hosts.json").add("worker")
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert list(path.iterdir()) == []


def test_failed_atomic_replace_preserves_original(monkeypatch, tmp_path):
    path = tmp_path / "hosts.json"
    store = SavedHostStore(path)
    first = store.add("worker")
    original = path.read_bytes()

    def failed_replace(*args):
        raise OSError("disk error")

    monkeypatch.setattr(saved_hosts.os, "replace", failed_replace)
    with pytest.raises(OSError):
        store.add("another")
    assert path.read_bytes() == original
    assert store.list() == [first]
    assert not list(tmp_path.glob(".saved-hosts-*"))


def test_concurrent_store_instances_do_not_lose_addresses(tmp_path):
    path = tmp_path / "hosts.json"

    def add_with_retry(index):
        for attempt in range(1000):
            try:
                return SavedHostStore(path).add(f"worker-{index}")
            except BlockingIOError:
                time.sleep(0.001)
        pytest.fail("Concurrent storage remained busy")

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(add_with_retry, range(16)))
    assert sorted(SavedHostStore(path).list(), key=lambda item: item["id"]) == sorted(entries, key=lambda item: item["id"])


def test_busy_store_fails_without_waiting_or_changing_file(tmp_path):
    path = tmp_path / "hosts.json"
    store = SavedHostStore(path)
    first = store.add("worker")
    with store._locked():
        started = time.monotonic()
        with pytest.raises(BlockingIOError):
            SavedHostStore(path).add("another")
        assert time.monotonic() - started < 0.1
    assert store.list() == [first]


def test_unsupported_locking_fails_cleanly_before_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(saved_hosts, "fcntl", None)
    path = tmp_path / "not-created" / "hosts.json"
    with pytest.raises(RuntimeError, match="POSIX"):
        SavedHostStore(path).add("worker")
    assert not path.parent.exists()


@pytest.mark.parametrize(("address", "origins"), [
    ("192.168.194.0", ["http://192.168.194.0:11434", "http://192.168.194.0:1234"]),
    ("[fd12::1]", ["http://[fd12::1]:11434", "http://[fd12::1]:1234"]),
    ("worker:1234", ["http://worker:1234", "http://worker:1234"]),
    ("https://worker/v1", ["https://worker", "https://worker"]),
])
def test_probes_only_fixed_metadata_without_credentials(address, origins, monkeypatch):
    calls = []
    client_options = []
    original_client = httpx.AsyncClient

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(saved_hosts.httpx, "AsyncClient", client)
    monkeypatch.setenv("HTTP_PROXY", "http://credentials@proxy.invalid:4444")
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "gateway-secret")

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert not request.content
        assert not request.url.query
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "proxy-authorization" not in request.headers
        payload = {
            "/api/version": {"version": "private-version"},
            "/api/tags": {"models": [{"name": "qwen:latest", "private": "private-metadata"}]},
            "/v1/models": {"data": [{"id": "qwen", "private": "private-metadata"}]},
        }[request.url.path]
        return httpx.Response(200, json=payload, headers={"set-cookie": "private-cookie=secret"})

    result = asyncio.run(check_saved_host({"id": "entry", "address": address}, transport=httpx.MockTransport(handler)))
    assert sorted(str(request.url) for request in calls) == sorted([
        origins[0] + "/api/version", origins[0] + "/api/tags", origins[1] + "/v1/models",
    ])
    assert result["id"] == "entry"
    assert result["address"] == normalize_address(address)
    assert result["checked_at"].endswith("+00:00")
    assert [check["status"] for check in result["checks"]] == ["pass", "pass"]
    assert [check["base_url"] for check in result["checks"]] == [origins[0], origins[1] + "/v1"]
    assert [check["provider"] for check in result["checks"]] == ["Ollama", "LM Studio / OpenAI-compatible"]
    assert all(check["elapsed_ms"] >= 0 for check in result["checks"])
    assert all(options["trust_env"] is False and options["follow_redirects"] is False for options in client_options)
    assert len(client_options) == 3
    assert result["checks"][0]["models"] == [{"id": "qwen:latest", "address": origins[0]}]
    assert result["checks"][1]["models"] == [{"id": "qwen", "address": origins[1] + "/v1"}]
    assert "private-" not in json.dumps(result)
    assert "gateway-secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 500, 503])
def test_failed_statuses_never_follow_redirects_or_echo_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-body", headers={"location": "http://other.invalid/api/pull"})

    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler)))
    assert len(calls) == 3
    assert all(check["status"] == "fail" and check["http_status"] == status for check in result["checks"])
    assert "private-body" not in json.dumps(result)
    assert "other.invalid" not in json.dumps(result)


@pytest.mark.parametrize("payload", [[], None, {}, {"data": {}}, {"version": ""}, {"version": 1}, {"version": "  "}])
def test_metadata_shape_validation(payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
    assert all(check["status"] == "fail" for check in result["checks"])


@pytest.mark.parametrize("body", [b"private-not-json", b"\xff", b"[" * 2000, b"x" * (1024 * 1024 + 1)])
def test_invalid_or_large_body_is_sanitized(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
    assert all(check["status"] == "fail" for check in result["checks"])
    assert "private-not-json" not in json.dumps(result)


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, closed):
        self.closed = closed

    async def __aiter__(self):
        yield b'{"data":'
        await asyncio.sleep(10)
        yield b"[]}"

    async def aclose(self):
        self.closed.append(True)


def test_deadline_includes_response_body_and_closes_stream(monkeypatch):
    monkeypatch.setattr(saved_hosts, "PROBE_TIMEOUT_SECONDS", 0.03)
    closed = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream(closed)))
    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
    assert all(check["status"] == "fail" and "timeout" in check["detail"] for check in result["checks"])
    assert len(closed) == 3


class LargeStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * (1024 * 1024)
        yield b"x" * 65536


def test_chunked_body_size_is_bounded_without_content_length():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=LargeStream()))
    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
    assert all(check["status"] == "fail" for check in result["checks"])


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_compression_rejected_without_reading_body(encoding):
    reads = []

    class UnreadStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield b"compressed-secret"

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=UnreadStream(), headers={"content-encoding": encoding}))
    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
    assert all(check["status"] == "fail" for check in result["checks"])
    assert not reads


@pytest.mark.parametrize("failure", [httpx.ConnectError("secret-key"), httpx.ReadTimeout("secret-key"), RuntimeError("secret-key")])
def test_transport_errors_do_not_leak_messages(failure):
    def handler(request):
        raise failure

    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler)))
    assert all(check["status"] == "fail" for check in result["checks"])
    assert "secret-key" not in json.dumps(result)


def test_invalid_input_produces_no_requests():
    def handler(request):
        pytest.fail("Invalid address must not be probed")

    with pytest.raises(ValueError):
        asyncio.run(check_saved_host({"id": "a", "address": "http://user:secret@worker"}, transport=httpx.MockTransport(handler)))


def test_concurrency_is_shared_across_hosts_and_reusable_across_event_loops():
    async def scenario():
        active = peak = count = 0

        async def handler(request):
            nonlocal active, peak, count
            active += 1
            count += 1
            peak = max(peak, active)
            await asyncio.sleep(0.003)
            active -= 1
            return httpx.Response(200, json={"version": "1", "data": [], "models": []})

        transport = httpx.MockTransport(handler)
        results = await asyncio.gather(*[
            check_saved_host({"id": str(index), "address": f"worker-{index}"}, transport=transport)
            for index in range(16)
        ])
        assert peak == 8
        assert count == 48
        assert all(check["status"] == "pass" for result in results for check in result["checks"])

    asyncio.run(scenario())
    asyncio.run(scenario())


def test_cancellation_closes_streams():
    async def scenario():
        closed = []
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream(closed)))
        task = asyncio.create_task(check_saved_host({"id": "a", "address": "worker"}, transport=transport))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(closed) == 3

    asyncio.run(scenario())


def catalog_check(*, ollama_models=None, openai_models=None, address="worker", version_status=200):
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(version_status, json={"version": "1"})
        if request.url.path == "/api/tags":
            payload = {"models": [] if ollama_models is None else ollama_models}
        else:
            assert request.url.path == "/v1/models"
            payload = {"data": [] if openai_models is None else openai_models}
        # Default JSON encoding represents surrogate test inputs safely so the
        # catalog validator, rather than the mock transport, rejects them.
        return httpx.Response(200, content=json.dumps(payload).encode())

    return asyncio.run(check_saved_host({"id": "catalog", "address": address}, transport=httpx.MockTransport(handler)))


def test_catalog_lists_ollama_names_fallbacks_and_openai_ids():
    result = catalog_check(
        ollama_models=[{"name": "qwen:latest"}, {"model": "llama:8b"}, {"name": "qwen:latest", "model": "ignored"}],
        openai_models=[{"id": "qwen/qwen3"}, {"id": "embed-small"}, {"id": "qwen/qwen3"}],
    )
    ollama, openai = result["checks"]
    assert ollama["models"] == [
        {"id": "qwen:latest", "address": "http://worker:11434"},
        {"id": "llama:8b", "address": "http://worker:11434"},
    ]
    assert openai["models"] == [
        {"id": "qwen/qwen3", "address": "http://worker:1234/v1"},
        {"id": "embed-small", "address": "http://worker:1234/v1"},
    ]
    assert ollama["catalog_url"] == "http://worker:11434/api/tags"
    assert openai["catalog_url"] == "http://worker:1234/v1/models"
    for check in result["checks"]:
        assert check["catalog_status"] == "ok"
        assert check["model_count"] == 2
        assert check["models_truncated"] is False
        assert "without invoking, loading, or downloading" in check["catalog_detail"]


def test_valid_empty_catalog_is_distinct_from_unavailable():
    for check in catalog_check()["checks"]:
        assert check["status"] == "pass"
        assert check["catalog_status"] == "ok"
        assert check["model_count"] == 0
        assert check["models"] == []
        assert check["models_truncated"] is False


@pytest.mark.parametrize("provider", ["ollama", "openai"])
@pytest.mark.parametrize("bad_id", [
    "", " ", " leading", "trailing ", 123, True, None, [], {}, "x" * 513,
    "private\nsecret", "private\rsecret", "private\x00secret", "private\x7fsecret",
    "private\x85secret", "private\u202esecret", "private\u2028secret", "private\ud800secret",
])
def test_malformed_model_ids_fail_catalog_without_fake_zero(provider, bad_id):
    kwargs = {"ollama_models": [{"name": bad_id}]} if provider == "ollama" else {"openai_models": [{"id": bad_id}]}
    check = catalog_check(**kwargs)["checks"][0 if provider == "ollama" else 1]
    assert check["catalog_status"] == "error"
    assert check["model_count"] is None
    assert check["models"] == []
    assert check["models_truncated"] is False
    assert "malformed" in check["catalog_detail"]
    assert "private" not in json.dumps(check)
    if provider == "ollama":
        assert check["status"] == "pass"  # Valid version endpoint still establishes reachability.


@pytest.mark.parametrize("provider", ["ollama", "openai"])
@pytest.mark.parametrize("items", ["invalid", {}, [None], ["qwen"], [{}], [1]])
def test_malformed_catalog_shape_is_explicit_error(provider, items):
    kwargs = {"ollama_models": items} if provider == "ollama" else {"openai_models": items}
    check = catalog_check(**kwargs)["checks"][0 if provider == "ollama" else 1]
    assert check["catalog_status"] == "error"
    assert check["model_count"] is None
    assert check["models"] == []


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_catalog_models_bounded_deduplicated_and_counted(provider):
    field = "name" if provider == "ollama" else "id"
    items = [{field: f"model-{index}"} for index in range(203)] * 2
    kwargs = {"ollama_models": items} if provider == "ollama" else {"openai_models": items}
    check = catalog_check(**kwargs)["checks"][0 if provider == "ollama" else 1]
    assert check["catalog_status"] == "ok"
    assert check["model_count"] == 203
    assert len(check["models"]) == 200
    assert check["models_truncated"] is True
    assert [model["id"] for model in check["models"]] == [f"model-{index}" for index in range(200)]


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_invalid_item_after_display_limit_is_not_silently_ignored(provider):
    field = "name" if provider == "ollama" else "id"
    items = [{field: f"model-{index}"} for index in range(201)] + [{field: "\nprivate-secret"}]
    kwargs = {"ollama_models": items} if provider == "ollama" else {"openai_models": items}
    check = catalog_check(**kwargs)["checks"][0 if provider == "ollama" else 1]
    assert check["catalog_status"] == "error"
    assert check["model_count"] is None
    assert check["models"] == []
    assert "private-secret" not in json.dumps(check)


def test_model_identifier_maximum_and_non_ascii_names():
    result = catalog_check(ollama_models=[{"name": "x" * 512}], openai_models=[{"id": "模型/qwen-32b"}])
    assert result["checks"][0]["models"][0]["id"] == "x" * 512
    assert result["checks"][1]["models"][0]["id"] == "模型/qwen-32b"


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_version_reachable_catalog_error_does_not_claim_empty(status):
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        return httpx.Response(status, text="private-backend-error")

    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler)))
    ollama, openai = result["checks"]
    assert ollama["status"] == "pass"
    assert ollama["http_status"] == 200
    assert "catalog is unavailable" in ollama["detail"]
    assert openai["status"] == "fail"
    for check in result["checks"]:
        assert check["catalog_status"] == "error"
        assert check["model_count"] is None
        assert check["models"] == []
        assert "private-backend-error" not in json.dumps(check)


def test_ollama_catalog_alone_identifies_server_if_version_fails():
    check = catalog_check(ollama_models=[{"name": "qwen"}], version_status=404)["checks"][0]
    assert check["status"] == "pass"
    assert check["http_status"] == 200
    assert check["catalog_status"] == "ok"
    assert check["models"] == [{"id": "qwen", "address": "http://worker:11434"}]


@pytest.mark.parametrize("slow_path", ["/api/version", "/api/tags"])
def test_ollama_partial_timeout_preserves_other_success(monkeypatch, slow_path):
    monkeypatch.setattr(saved_hosts, "PROBE_TIMEOUT_SECONDS", 0.025)
    closed = []

    def handler(request):
        if request.url.path == slow_path:
            return httpx.Response(200, stream=SlowStream(closed))
        payload = {"/api/version": {"version": "1"}, "/api/tags": {"models": [{"name": "qwen"}]}, "/v1/models": {"data": []}}[request.url.path]
        return httpx.Response(200, json=payload)

    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler)))
    check = result["checks"][0]
    assert check["status"] == "pass"
    assert len(closed) == 1
    if slow_path == "/api/tags":
        assert check["catalog_status"] == "error"
        assert check["model_count"] is None
        assert "timeout" in check["catalog_detail"]
    else:
        assert check["catalog_status"] == "ok"
        assert check["model_count"] == 1


def test_ollama_metadata_requests_run_concurrently():
    async def scenario():
        requested = set()
        all_started = asyncio.Event()

        async def handler(request):
            requested.add(request.url.path)
            if len(requested) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.2)
            return httpx.Response(200, json={"version": "1", "models": [], "data": []})

        result = await check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler))
        assert requested == {"/api/version", "/api/tags", "/v1/models"}
        assert all(check["status"] == "pass" for check in result["checks"])

    asyncio.run(scenario())


@pytest.mark.parametrize("address", ["worker:11434", "https://worker:1234/v1", "http://[fd12::1]:4321"])
def test_explicit_port_uses_one_origin_for_both_api_types_and_ignores_returned_addresses(address):
    record = {
        "name": "qwen", "id": "qwen", "address": "https://attacker.invalid", "base_url": "https://attacker.invalid",
        "url": "javascript:alert(1)", "owned_by": "private-owner", "details": {"secret": "private-secret"},
    }
    result = catalog_check(ollama_models=[record], openai_models=[record], address=address)
    origin = normalize_address(address)
    assert result["checks"][0]["models"] == [{"id": "qwen", "address": origin}]
    assert result["checks"][1]["models"] == [{"id": "qwen", "address": origin + "/v1"}]
    assert result["checks"][0]["catalog_url"] == origin + "/api/tags"
    assert result["checks"][1]["catalog_url"] == origin + "/v1/models"
    for secret in ("attacker", "javascript", "private-owner", "private-secret"):
        assert secret not in json.dumps(result)


def test_catalog_results_are_not_saved_to_address_file(tmp_path):
    path = tmp_path / "saved.json"
    store = SavedHostStore(path)
    entry = store.add("worker")
    original = path.read_bytes()

    def handler(request):
        return httpx.Response(200, json={"version": "1", "models": [{"name": "qwen"}], "data": [{"id": "qwen"}]})

    result = asyncio.run(check_saved_host(entry, transport=httpx.MockTransport(handler)))
    assert all(check["models"] for check in result["checks"])
    assert path.read_bytes() == original
    assert "qwen" not in path.read_text()
