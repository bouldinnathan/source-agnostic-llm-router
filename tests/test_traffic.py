"""Per-request traffic counters: requests, reroutes, tokens, and failure kinds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from llm_router import metrics
from llm_router.metrics import MetricsStore, _SCHEMA


def traffic(store: MetricsStore) -> dict:
    snapshot = store.snapshot()
    assert snapshot["available"] is True, snapshot
    assert snapshot["error"] is None
    return snapshot["traffic"]


def hour_ago(hours: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.replace(minute=0, second=0, microsecond=0).isoformat()


def insert_hour(store: MetricsStore, hour: str, requests_ok: int, failures: dict | None = None) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO traffic_hourly VALUES (?, ?, 0, 0, 0, 0, 0, ?)",
            (hour, requests_ok, json.dumps(failures or {}, separators=(",", ":"))),
        )


def test_request_outcomes_count_requests_not_attempts(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False, input_tokens=100, output_tokens=20)
    store.record_request(success=True, rerouted=True, input_tokens=50, output_tokens=5, failure_kinds=["timeout"])
    store.record_request(success=False, rerouted=True, input_tokens=999, output_tokens=999, failure_kinds=["http_5xx", "connection"])
    store.record_request(success=False, rerouted=False, failure_kinds=["no_eligible_model"])
    result = traffic(store)
    totals = result["totals"]
    assert totals["requests_ok"] == 2 and totals["requests_failed"] == 2
    assert totals["reroutes_ok"] == 1 and totals["reroutes_failed"] == 1
    assert totals["input_tokens"] == 150 and totals["output_tokens"] == 25, "Failed requests never add tokens"
    assert totals["failures"] == {"timeout": 1, "http_5xx": 1, "connection": 1, "no_eligible_model": 1}
    assert result["windows"]["24h"] == totals and result["windows"]["7d"] == totals
    assert len(result["hourly"]) == 1
    assert result["hourly"][0]["hour"] == hour_ago(0)
    assert {key: value for key, value in result["hourly"][0].items() if key != "hour"} == totals
    assert datetime.fromisoformat(result["since"]).tzinfo is not None
    assert result["retention_hours"] == 720
    assert traffic(MetricsStore(store.path)) == result, "Traffic survives a restart and reads identically"


def test_unknown_kinds_and_invalid_tokens_are_sanitized(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(
        success=True, rerouted=False, input_tokens=float("nan"), output_tokens=-1,
        failure_kinds=['<img src=x onerror="alert(1)">', 5, None, "timeout"],
    )
    totals = traffic(store)["totals"]
    assert totals["input_tokens"] == 0 and totals["output_tokens"] == 0
    assert totals["failures"] == {"other": 3, "timeout": 1}
    assert "onerror" not in json.dumps(store.snapshot())
    store.record_request(success=True, rerouted=False, failure_kinds=["timeout"] * 500)
    assert traffic(store)["totals"]["failures"]["timeout"] == 65, "Per-request failure lists are bounded"


@pytest.mark.parametrize("kwargs", [{"success": "yes", "rerouted": False}, {"success": True, "rerouted": 1}])
def test_invalid_outcome_types_are_rejected_without_exposing_paths(tmp_path, kwargs):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False)
    original = store.path.read_bytes()
    with pytest.raises(RuntimeError) as error:
        store.record_request(**kwargs)
    assert str(store.path) not in str(error.value)
    assert store.path.read_bytes() == original
    assert store.snapshot()["available"] is False, "A failed write is visible until the next success"
    store.record_request(success=True, rerouted=False)
    assert traffic(store)["totals"]["requests_ok"] == 2


def test_windows_only_include_recent_hours_and_hourly_is_ordered(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False)
    insert_hour(store, hour_ago(30), 7, {"timeout": 2})
    insert_hour(store, hour_ago(3 * 24), 11)
    insert_hour(store, hour_ago(8 * 24), 1000)
    result = traffic(store)
    assert result["windows"]["24h"]["requests_ok"] == 1
    assert result["windows"]["24h"]["failures"] == {}
    assert result["windows"]["7d"]["requests_ok"] == 19
    assert result["windows"]["7d"]["failures"] == {"timeout": 2}
    assert [row["hour"] for row in result["hourly"]] == [hour_ago(3 * 24), hour_ago(30), hour_ago(0)]
    assert result["totals"]["requests_ok"] == 1, "Totals come from the all-time row, not a re-sum of retained hours"


def test_hourly_buckets_older_than_retention_are_pruned_on_write(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False)
    insert_hour(store, hour_ago(31 * 24), 5)
    insert_hour(store, hour_ago(29 * 24), 6)
    assert store.snapshot()["available"] is True, "Old rows are read without modification"
    store.record_request(success=False, rerouted=False, failure_kinds=["connection"])
    with sqlite3.connect(store.path) as connection:
        hours = sorted(row[0] for row in connection.execute("SELECT hour FROM traffic_hourly"))
    assert hours == [hour_ago(29 * 24), hour_ago(0)]
    assert traffic(store)["totals"]["requests_failed"] == 1


def test_version_one_database_is_read_as_is_and_upgraded_only_on_write(tmp_path):
    path = tmp_path / "metrics.sqlite3"
    store = MetricsStore(path)
    store.record_request(success=True, rerouted=False)
    from llm_router.schema import EndpointConfig, ModelConfig
    store.record(
        EndpointConfig(name="worker", adapter="ollama", base_url="http://worker:11434", machine_id="worker"),
        ModelConfig(id="qwen", endpoint="worker", upstream_model="qwen:latest"), {"input_tokens": 10}, success=True,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE traffic_hourly")
        connection.execute("DROP TABLE traffic_totals")
        connection.execute("PRAGMA user_version=1")
    legacy = path.read_bytes()
    snapshot = MetricsStore(path).snapshot()
    assert snapshot["available"] is True
    assert snapshot["deployments"][0]["successes"] == 1
    assert snapshot["traffic"]["totals"]["requests_ok"] == 0 and snapshot["traffic"]["hourly"] == []
    assert path.read_bytes() == legacy, "Reading an older database never modifies it"
    MetricsStore(path).record_request(success=True, rerouted=True, failure_kinds=["timeout"])
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"deployments", "traffic_hourly", "traffic_totals"}
    upgraded = MetricsStore(path).snapshot()
    assert upgraded["deployments"][0]["successes"] == 1, "Deployment history survives the upgrade"
    assert upgraded["traffic"]["totals"] == {
        "requests_ok": 1, "requests_failed": 0, "reroutes_ok": 1, "reroutes_failed": 0,
        "input_tokens": 0, "output_tokens": 0, "failures": {"timeout": 1},
    }


def test_deployment_write_also_upgrades_a_version_one_database(tmp_path):
    path = tmp_path / "metrics.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(_SCHEMA)
        connection.execute(f"PRAGMA application_id={metrics._APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
    path.chmod(0o600)
    from llm_router.schema import EndpointConfig, ModelConfig
    store = MetricsStore(path)
    store.record(
        EndpointConfig(name="worker", adapter="ollama", base_url="http://worker:11434", machine_id="worker"),
        ModelConfig(id="qwen", endpoint="worker", upstream_model="qwen:latest"), {}, success=True,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert traffic(store)["totals"]["requests_ok"] == 0


@pytest.mark.parametrize("sql", [
    "DROP TABLE traffic_totals",
    "ALTER TABLE traffic_hourly ADD COLUMN raw_prompt TEXT",
    "CREATE INDEX unexpected ON traffic_hourly(requests_ok)",
    "UPDATE traffic_hourly SET failures_json='{\"private-kind\": 1}'",
    "UPDATE traffic_hourly SET failures_json='{\"timeout\": 0}'",
    "UPDATE traffic_totals SET requests_ok=-1",
    "UPDATE traffic_totals SET id=2",
    "UPDATE traffic_totals SET since='private-not-a-time'",
])
def test_unexpected_traffic_layout_or_rows_are_refused_not_reset(tmp_path, sql):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False, failure_kinds=["timeout"])
    with sqlite3.connect(store.path) as connection:
        connection.execute(sql)
    original = store.path.read_bytes()
    snapshot = store.snapshot()
    assert snapshot["available"] is False
    assert snapshot["traffic"]["available"] is False
    assert "private" not in json.dumps(snapshot)
    with pytest.raises(RuntimeError):
        store.record_request(success=True, rerouted=False)
    assert store.path.read_bytes() == original


def test_malformed_hour_outside_the_written_bucket_is_reported_and_left_alone(tmp_path):
    store = MetricsStore(tmp_path / "metrics.sqlite3")
    store.record_request(success=True, rerouted=False)
    bad_hour = hour_ago(1).replace(":00:00+", ":30:00+")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE traffic_hourly SET hour=?", (bad_hour,))
    assert store.snapshot()["available"] is False, "Reads validate every retained hour they return"
    store.record_request(success=True, rerouted=False)  # Writes touch only the current hour and the totals row.
    with sqlite3.connect(store.path) as connection:
        hours = sorted(row[0] for row in connection.execute("SELECT hour FROM traffic_hourly"))
    assert hours == sorted([bad_hour, hour_ago(0)]), "The malformed row is neither overwritten nor pruned"
    assert store.snapshot()["available"] is False
