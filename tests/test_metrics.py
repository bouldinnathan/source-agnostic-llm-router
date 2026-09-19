from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import sqlite3
import stat
import time

import pytest

from llm_router import metrics
from llm_router.metrics import MetricsStore
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig


def endpoint(**changes):
    return replace(EndpointConfig(name="worker-ollama", adapter="ollama", base_url="http://worker:11434", machine_id="worker"), **changes)


def model(**changes):
    return replace(ModelConfig(id="qwen-worker", endpoint="worker-ollama", upstream_model="qwen:latest"), **changes)


def record(store, observation=None, *, success=True, target=None, deployment=None):
    store.record(target or endpoint(), deployment or model(), observation or {}, success=success)


def only(store):
    snapshot = store.snapshot()
    assert snapshot["available"] is True, snapshot
    assert snapshot["error"] is None
    assert len(snapshot["deployments"]) == 1
    return snapshot["deployments"][0]


def test_empty_snapshot_and_constructor_do_not_write(tmp_path):
    path = tmp_path / "missing" / "metrics.sqlite3"
    store = MetricsStore(path)
    empty_bucket = {
        "requests_ok": 0, "requests_failed": 0, "reroutes_ok": 0, "reroutes_failed": 0,
        "input_tokens": 0, "output_tokens": 0, "failures": {},
    }
    assert store.snapshot() == {
        "available": True, "error": None, "updated_at": None, "deployments": [],
        "traffic": {
            "available": True, "retention_hours": 720, "since": None, "totals": empty_bucket,
            "windows": {"24h": empty_bucket, "7d": empty_bucket}, "hourly": [],
        },
    }
    assert not path.parent.exists()
    assert not list(tmp_path.iterdir())


def test_default_state_path_and_explicit_override(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_ROUTER_METRICS_FILE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert MetricsStore().path == tmp_path / "state" / "llm-router" / "metrics.sqlite3"
    monkeypatch.setenv("LLM_ROUTER_METRICS_FILE", str(tmp_path / "custom" / "observed.sqlite3"))
    assert MetricsStore().path == tmp_path / "custom" / "observed.sqlite3"
    assert not list(tmp_path.iterdir())


def test_metrics_survive_restart_with_expected_schema_and_private_files(tmp_path):
    path = tmp_path / "state" / "metrics.sqlite3"
    store = MetricsStore(path)
    record(store, {
        "input_tokens": 100, "output_tokens": 20,
        "input_tokens_per_second": 500, "output_tokens_per_second": 25,
        "load_duration_ms": 1500, "request_duration_ms": 2000,
    })
    row = only(MetricsStore(path))
    assert row["machine"] == "worker"
    assert row["endpoint"] == "worker-ollama"
    assert row["model"] == "qwen:latest"
    assert row["address"] == "http://worker:11434"
    assert row["adapter"] == "ollama"
    assert len(row["id"]) == 64
    assert row["successes"] == 1
    assert row["failures"] == 0
    assert row["input_tokens_total"] == 100
    assert row["output_tokens_total"] == 20
    assert row["slow_load_count"] == 1
    assert row["slow_load_threshold_ms"] == 1000
    assert set(row["metrics"]) == {"input_tokens_per_second", "output_tokens_per_second", "load_duration_ms", "request_duration_ms"}
    for metric in row["metrics"].values():
        assert metric["samples"] == 1
        assert metric["ewma"] == metric["latest"]
        assert metric["updated_at"] == row["last_seen_at"]
    assert store.snapshot()["updated_at"] == row["last_seen_at"]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode) == 0o600


def test_rolling_samples_use_point_two_ewma_and_keep_missing_history(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"input_tokens": 10, "output_tokens": 2, "input_tokens_per_second": 100, "output_tokens_per_second": 10, "load_duration_ms": 1500})
    first = only(store)
    record(store, {"input_tokens": 20, "output_tokens": 4, "input_tokens_per_second": 200, "output_tokens_per_second": 30})
    second = only(store)
    assert second["successes"] == 2
    assert second["input_tokens_total"] == 30
    assert second["output_tokens_total"] == 6
    assert second["metrics"]["input_tokens_per_second"]["ewma"] == 120
    assert second["metrics"]["output_tokens_per_second"]["ewma"] == 14
    assert second["metrics"]["input_tokens_per_second"]["samples"] == 2
    assert second["metrics"]["load_duration_ms"] == first["metrics"]["load_duration_ms"]
    record(store, {"input_tokens_per_second": None})
    third = only(store)
    assert third["metrics"] == second["metrics"]
    assert third["successes"] == 3
    assert third["slow_load_count"] == 1


def test_failures_update_only_attempt_count_and_request_duration(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"input_tokens": 10, "output_tokens": 5, "output_tokens_per_second": 20, "request_duration_ms": 100})
    before = only(store)
    record(store, {
        "input_tokens": 9000, "output_tokens": 9999,
        "input_tokens_per_second": 999, "output_tokens_per_second": 1000,
        "load_duration_ms": 9000, "request_duration_ms": 200,
    }, success=False)
    after = only(store)
    assert after["successes"] == after["failures"] == 1
    assert after["input_tokens_total"] == 10
    assert after["output_tokens_total"] == 5
    assert after["slow_load_count"] == 0
    assert after["metrics"]["load_duration_ms"] is None
    assert after["metrics"]["input_tokens_per_second"] is None
    assert after["metrics"]["output_tokens_per_second"] == before["metrics"]["output_tokens_per_second"]
    assert after["metrics"]["request_duration_ms"]["latest"] == 200
    assert after["metrics"]["request_duration_ms"]["ewma"] == 120
    assert after["metrics"]["request_duration_ms"]["samples"] == 2


def test_missing_reported_metrics_do_not_infer_load_or_token_rates(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"request_duration_ms": 10000})
    row = only(store)
    assert row["slow_load_count"] == 0
    assert row["input_tokens_total"] == row["output_tokens_total"] == 0
    assert row["metrics"]["load_duration_ms"] is None
    assert row["metrics"]["input_tokens_per_second"] is None
    assert row["metrics"]["output_tokens_per_second"] is None


@pytest.mark.parametrize(("duration", "expected"), [(0, 0), (999.9, 0), (1000, 1), (1000.1, 1)])
def test_only_reported_slow_load_threshold_is_counted(tmp_path, duration, expected):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"load_duration_ms": duration})
    assert only(store)["slow_load_count"] == expected


def test_zero_observations_are_valid_samples(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {name: 0 for name in (*metrics._METRICS, "input_tokens", "output_tokens")})
    row = only(store)
    assert row["input_tokens_total"] == row["output_tokens_total"] == 0
    for sample in row["metrics"].values():
        assert sample["latest"] == sample["ewma"] == 0
        assert sample["samples"] == 1


@pytest.mark.parametrize("value", [None, True, False, "100", "private-secret", [], {}, -1, float("nan"), float("inf"), float("-inf"), 10**1000, 1e16])
def test_invalid_numeric_observations_are_ignored_without_history_loss(tmp_path, value):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"input_tokens": 10, "output_tokens_per_second": 20})
    previous = only(store)
    record(store, {name: value for name in (*metrics._METRICS, "input_tokens", "output_tokens")})
    row = only(store)
    assert row["successes"] == 2
    assert row["input_tokens_total"] == 10
    assert row["output_tokens_total"] == 0
    assert row["metrics"] == previous["metrics"]


def test_roaming_address_change_and_alias_change_keep_actual_deployment_history(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"output_tokens_per_second": 10}, target=endpoint(base_url="http://192.168.1.2:11434"))
    previous = only(store)
    record(store, {"output_tokens_per_second": 20}, target=endpoint(base_url="http://10.2.3.4:11434"), deployment=model(id="qwen-ha"))
    row = only(store)
    assert row["id"] == previous["id"]
    assert row["successes"] == 2
    assert row["address"] == "http://10.2.3.4:11434"
    assert row["model"] == "qwen:latest"
    assert row["metrics"]["output_tokens_per_second"]["ewma"] == 12


def test_replicas_and_different_upstream_models_keep_separate_history(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"output_tokens_per_second": 10})
    record(store, {"output_tokens_per_second": 20}, target=endpoint(machine_id="laptop", name="laptop-ollama", base_url="http://laptop:11434"))
    record(store, {"output_tokens_per_second": 30}, deployment=model(upstream_model="llama"))
    rows = store.snapshot()["deployments"]
    assert len(rows) == 3
    assert len({row["id"] for row in rows}) == 3
    assert sorted(row["metrics"]["output_tokens_per_second"]["latest"] for row in rows) == [10, 20, 30]


def test_missing_machine_id_falls_back_to_endpoint_name(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, target=endpoint(machine_id=None))
    assert only(store)["machine"] == "worker-ollama"


@pytest.mark.parametrize(("source", "expected"), [
    ("HTTP://User:password@WORKER:80/private/v1?api_key=secret#token", "http://worker"),
    ("https://worker:443/v1", "https://worker"),
    ("http://[fd12:0:0:0:0:0:0:1]:1234/v1", "http://[fd12::1]:1234"),
    ("file:///private/secret", ""),
    ("http://worker:99999", ""),
    ("http://worker\n", ""),
])
def test_only_sanitized_origin_is_persisted(tmp_path, source, expected):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, target=endpoint(base_url=source))
    assert only(store)["address"] == expected
    assert b"password" not in store.path.read_bytes()
    assert b"api_key" not in store.path.read_bytes()
    assert b"private" not in store.path.read_bytes()


def test_raw_observation_and_credentials_are_never_persisted(tmp_path):
    path = tmp_path / "metrics.sqlite3"
    store = MetricsStore(path)
    target = endpoint(
        base_url="http://private-user:private-password@worker:11434/private-tenant?key=private-query",
        auth=AuthConfig(key_env="private-auth"), headers={"secret": "private-header"}, options={"raw": "private-option"},
    )
    record(store, {
        "input_tokens": 5, "output_tokens": 10, "output_tokens_per_second": 20,
        "prompt": "private-prompt", "response": "private-response", "api_key": "private-api-key",
        "raw_body": {"secret": "private-raw"}, "messages": ["private-message"],
    }, target=target)
    serialized = json.dumps(store.snapshot())
    assert "private-" not in serialized
    for item in path.parent.iterdir():
        if item.is_file():
            assert b"private-" not in item.read_bytes()


def test_concurrent_records_have_no_lost_updates(tmp_path):
    path = tmp_path / "metrics.sqlite3"

    def add(index):
        MetricsStore(path).record(endpoint(), model(), {"input_tokens": 2, "output_tokens": 1, "output_tokens_per_second": 10}, success=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(64)))
    row = only(MetricsStore(path))
    assert row["successes"] == 64
    assert row["input_tokens_total"] == 128
    assert row["output_tokens_total"] == 64
    assert row["metrics"]["output_tokens_per_second"]["samples"] == 64


def test_busy_private_lock_has_bounded_wait_and_no_data_loss(monkeypatch, tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store)
    original = store.snapshot()
    monkeypatch.setattr(metrics, "LOCK_TIMEOUT_SECONDS", 0.02)
    with store._lock():
        started = time.monotonic()
        with pytest.raises(RuntimeError) as error:
            record(MetricsStore(store.path))
        assert time.monotonic() - started < 0.2
        assert str(store.path) not in str(error.value)
        assert store.snapshot() == original


def test_external_sqlite_lock_fails_open_with_safe_error(monkeypatch, tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store)
    original = store.snapshot()
    monkeypatch.setattr(metrics, "LOCK_TIMEOUT_SECONDS", 0.01)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RuntimeError) as error:
            record(store)
        assert "locked" not in str(error.value)
        assert str(store.path) not in str(error.value)
        snapshot = store.snapshot()
        assert snapshot["available"] is False
        assert snapshot["deployments"] == []
    finally:
        connection.rollback()
        connection.close()
    degraded = store.snapshot()
    assert degraded["available"] is False
    assert degraded["deployments"] == original["deployments"]
    record(store)
    assert only(store)["successes"] == 2


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public", "directory"])
def test_unsafe_database_files_are_not_followed_changed_or_exposed(tmp_path, kind):
    target = tmp_path / "target.sqlite3"
    original_store = MetricsStore(target)
    record(original_store)
    original = target.read_bytes()
    path = tmp_path / "metrics.sqlite3"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "public":
        path.write_bytes(original)
        path.chmod(0o644)
    else:
        path.mkdir(mode=0o700)
    store = MetricsStore(path)
    snapshot = store.snapshot()
    assert snapshot["available"] is False
    assert str(path) not in snapshot["error"]
    with pytest.raises(RuntimeError):
        record(store)
    assert target.read_bytes() == original


def test_symlinked_parent_and_lock_are_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    store = MetricsStore(linked / "metrics.sqlite3")
    assert store.snapshot()["available"] is False
    with pytest.raises(RuntimeError):
        record(store)
    assert list(real.iterdir()) == []
    secret = tmp_path / "private-secret"
    secret.write_text("private-content")
    (real / "metrics.sqlite3.lock").symlink_to(secret)
    with pytest.raises(RuntimeError):
        record(MetricsStore(real / "metrics.sqlite3"))
    assert secret.read_text() == "private-content"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_unsafe_sqlite_sidecar_is_rejected_before_sqlite_connect(tmp_path, suffix):
    path = tmp_path / "metrics.sqlite3"
    store = MetricsStore(path)
    record(store)
    original = path.read_bytes()
    secret = tmp_path / "secret"
    secret.write_text("private")
    path.with_name(path.name + suffix).symlink_to(secret)
    assert store.snapshot()["available"] is False
    with pytest.raises(RuntimeError):
        record(store)
    assert secret.read_text() == "private"
    assert path.read_bytes() == original


def test_nonprivate_storage_directory_is_not_chmodded_or_used(tmp_path):
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    store = MetricsStore(parent / "metrics.sqlite3")
    assert store.snapshot()["available"] is False
    with pytest.raises(RuntimeError):
        record(store)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("contents", [b"", b"private-corruption", b"SQLite format 3\x00invalid-private-body"])
def test_corrupt_existing_database_is_never_reset(tmp_path, contents):
    path = tmp_path / "metrics.sqlite3"
    path.write_bytes(contents)
    path.chmod(0o600)
    store = MetricsStore(path)
    snapshot = store.snapshot()
    assert snapshot["available"] is False
    assert "private" not in snapshot["error"]
    with pytest.raises(RuntimeError):
        record(store)
    assert path.read_bytes() == contents


@pytest.mark.parametrize("sql", [
    "PRAGMA user_version=999", "PRAGMA application_id=123", "CREATE TABLE unrelated (secret TEXT)",
    "ALTER TABLE deployments ADD COLUMN raw_payload TEXT", "CREATE INDEX unexpected ON deployments(machine)",
    "CREATE TRIGGER unexpected AFTER INSERT ON deployments BEGIN SELECT 1; END",
])
def test_unknown_schema_is_preserved_not_migrated_silently(tmp_path, sql):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(sql)
    original = store.path.read_bytes()
    assert store.snapshot()["available"] is False
    with pytest.raises(RuntimeError):
        record(store)
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("sql", [
    "UPDATE deployments SET metrics_json='private-corruption'",
    "UPDATE deployments SET metrics_json='{}'",
    "UPDATE deployments SET successes=-1",
    "UPDATE deployments SET id='private-bad-id'",
    "UPDATE deployments SET address='https://user:private-key@worker/private'",
])
def test_malformed_row_is_not_overwritten_or_exposed(tmp_path, sql):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(sql)
    original = store.path.read_bytes()
    snapshot = store.snapshot()
    assert snapshot["available"] is False
    assert "private-" not in json.dumps(snapshot)
    with pytest.raises(RuntimeError):
        record(store)
    assert store.path.read_bytes() == original


def test_row_limit_preserves_existing_history(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "MAX_DEPLOYMENTS", 2)
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, deployment=model(upstream_model="one"))
    record(store, deployment=model(upstream_model="two"))
    with pytest.raises(RuntimeError):
        record(store, deployment=model(upstream_model="three"))
    record(store, deployment=model(upstream_model="one"))
    rows = store.snapshot()["deployments"]
    assert len(rows) == 2
    assert sum(row["successes"] for row in rows) == 3


def test_oversized_database_is_rejected_before_sqlite_reads(monkeypatch, tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store)
    original = store.path.read_bytes()
    monkeypatch.setattr(metrics, "MAX_DATABASE_BYTES", len(original) - 1)
    assert store.snapshot()["available"] is False
    with pytest.raises(RuntimeError):
        record(store)
    assert store.path.read_bytes() == original


def test_unsupported_locking_fails_without_creating_state(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "fcntl", None)
    store = MetricsStore(tmp_path / "missing" / "metrics.sqlite3")
    assert store.snapshot()["available"] is True
    with pytest.raises(RuntimeError):
        record(store)
    assert not store.path.parent.exists()


def test_observation_storage_error_does_not_expose_raw_paths(monkeypatch, tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("private-path private-key")

    monkeypatch.setattr(metrics.sqlite3, "connect", broken)
    with pytest.raises(RuntimeError) as caught:
        record(store)
    assert "private-" not in str(caught.value)
    assert "private-" not in json.dumps(store.snapshot())


def test_recent_write_failure_is_visible_until_a_successful_record(monkeypatch, tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    record(store, {"output_tokens_per_second": 10})
    previous = store.snapshot()
    monkeypatch.setattr(metrics, "LOCK_TIMEOUT_SECONDS", 0.01)
    with store._lock():
        with pytest.raises(RuntimeError):
            record(store, {"output_tokens_per_second": 20})
    degraded = store.snapshot()
    assert degraded["available"] is False
    assert degraded["error"]
    assert str(store.path) not in degraded["error"]
    assert degraded["deployments"] == previous["deployments"]
    assert degraded["updated_at"] == previous["updated_at"]
    assert store.snapshot()["available"] is False
    record(store, {"output_tokens_per_second": 30})
    recovered = store.snapshot()
    assert recovered["available"] is True
    assert recovered["error"] is None
    assert recovered["deployments"][0]["metrics"]["output_tokens_per_second"]["samples"] == 2
