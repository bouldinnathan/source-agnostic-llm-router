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
        payload = {"version": "private-version"} if request.url.path == "/api/version" else {"data": [{"id": "private-model"}]}
        return httpx.Response(200, json=payload, headers={"set-cookie": "private-cookie=secret"})

    result = asyncio.run(check_saved_host({"id": "entry", "address": address}, transport=httpx.MockTransport(handler)))
    assert [str(request.url) for request in calls] == [origins[0] + "/api/version", origins[1] + "/v1/models"]
    assert result["id"] == "entry"
    assert result["address"] == normalize_address(address)
    assert result["checked_at"].endswith("+00:00")
    assert [check["status"] for check in result["checks"]] == ["pass", "pass"]
    assert [check["base_url"] for check in result["checks"]] == [origins[0], origins[1] + "/v1"]
    assert [check["provider"] for check in result["checks"]] == ["Ollama", "LM Studio / OpenAI-compatible"]
    assert all(check["elapsed_ms"] >= 0 for check in result["checks"])
    assert all(options["trust_env"] is False and options["follow_redirects"] is False for options in client_options)
    assert len(client_options) == 2
    assert "private-" not in json.dumps(result)
    assert "gateway-secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 500, 503])
def test_failed_statuses_never_follow_redirects_or_echo_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-body", headers={"location": "http://other.invalid/api/pull"})

    result = asyncio.run(check_saved_host({"id": "a", "address": "worker"}, transport=httpx.MockTransport(handler)))
    assert len(calls) == 2
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
    assert len(closed) == 2


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
            return httpx.Response(200, json={"version": "1", "data": []})

        transport = httpx.MockTransport(handler)
        results = await asyncio.gather(*[
            check_saved_host({"id": str(index), "address": f"worker-{index}"}, transport=transport)
            for index in range(16)
        ])
        assert peak == 8
        assert count == 32
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
        assert len(closed) == 2

    asyncio.run(scenario())
