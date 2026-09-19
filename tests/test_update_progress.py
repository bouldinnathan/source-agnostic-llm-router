from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace

import pytest

from llm_router import update_progress, updater
from llm_router.update_progress import STATUS_FILE, UpdateProgress, read_update_status

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture(autouse=True)
def no_host_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Updater progress tests must not run actual git, pip, or systemd")
    monkeypatch.setattr(updater.subprocess, "run", forbidden)


@pytest.fixture
def install(tmp_path):
    directory = tmp_path / "router"
    (directory / "venv" / "bin").mkdir(parents=True)
    (directory / "venv" / "bin" / "python").touch()
    directory.chmod(0o755)
    return directory


def test_missing_status_does_not_create_files(tmp_path):
    assert read_update_status(tmp_path / "missing") is None
    assert read_update_status(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_private_status_survives_runtime_replacement(install):
    progress = UpdateProgress(install)
    initial = read_update_status(install)
    assert initial["schema_version"] == 1
    assert initial["state"] == "running" and initial["stage"] == "checking"
    assert initial["current_commit"] is initial["target_commit"] is None
    assert len(initial["run_id"]) == 32
    assert stat.S_IMODE((install / STATUS_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE(install.stat().st_mode) == 0o755
    progress.advance("downloading", current_commit=OLD, target_commit=NEW)
    progress.advance("validating")
    progress.advance("restarting")
    progress.advance("complete", state="succeeded", current_commit=NEW)
    complete = read_update_status(install)
    (install / "venv").rename(install / "old-runtime")
    (install / "venv").mkdir()
    assert read_update_status(install) == complete
    assert complete["state"] == "succeeded" and complete["stage"] == "complete"
    assert complete["current_commit"] == complete["target_commit"] == NEW
    assert complete["run_id"] == initial["run_id"]
    assert complete["started_at"] == initial["started_at"]
    assert complete["updated_at"] >= complete["started_at"]


def test_systemd_invocation_identifier_is_used(monkeypatch, install):
    monkeypatch.setenv("INVOCATION_ID", "1a" * 16)
    UpdateProgress(install)
    assert read_update_status(install)["run_id"] == "1a" * 16


@pytest.mark.parametrize("value", ["", "private-secret", "a" * 31, "a" * 33, "A" * 32, "\na" * 16])
def test_bad_invocation_identifier_is_replaced_not_echoed(monkeypatch, install, value):
    monkeypatch.setenv("INVOCATION_ID", value)
    UpdateProgress(install)
    payload = read_update_status(install)
    assert payload["run_id"] != value and len(payload["run_id"]) == 32
    assert "private-secret" not in json.dumps(payload)


def test_new_run_clears_old_status_and_revisions(monkeypatch, install):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    progress = UpdateProgress(install)
    progress.advance("complete", state="current", current_commit=OLD, target_commit=OLD)
    old = read_update_status(install)
    UpdateProgress(install)
    current = read_update_status(install)
    assert current["run_id"] != old["run_id"]
    assert current["state"] == "running"
    assert current["current_commit"] is current["target_commit"] is None


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"schema_version": True}, {"run_id": "private-secret"},
    {"state": "private-error"}, {"stage": "private-stage"}, {"state": []},
    {"message": "pip said private-secret"}, {"raw_error": "private-secret"},
    {"current_commit": "private-commit"}, {"target_commit": True},
    {"started_at": "not-a-date"}, {"updated_at": "2000-01-01T00:00:00+00:00"},
    {"updated_at": "2026-09-19T00:00:00"}, {"updated_at": "99999-01-01T00:00:00+00:00"},
])
def test_reader_rejects_malformed_fields_without_echoing_or_rewriting(install, change):
    UpdateProgress(install)
    payload = {**read_update_status(install), **change}
    path = install / STATUS_FILE
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    with pytest.raises(RuntimeError) as caught:
        read_update_status(install)
    assert "private" not in str(caught.value) and str(install) not in str(caught.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize("body", [b"private-invalid-json", b"\xff", b"[]", b"null", b"[" * 2000, b"x" * 8193])
def test_reader_bounds_and_sanitizes_invalid_json(install, body):
    path = install / STATUS_FILE
    path.write_bytes(body)
    path.chmod(0o600)
    with pytest.raises(RuntimeError) as caught:
        read_update_status(install)
    assert "private" not in str(caught.value)
    assert path.read_bytes() == body


def test_duplicate_json_fields_rejected(install):
    UpdateProgress(install)
    path = install / STATUS_FILE
    path.write_text(path.read_text().rstrip()[:-1] + ',"message":"private-secret"}')
    with pytest.raises(RuntimeError):
        read_update_status(install)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo", "directory"])
def test_unsafe_status_not_followed_or_overwritten(install, tmp_path, kind):
    target_dir = tmp_path / "target"
    target_dir.mkdir(mode=0o700)
    UpdateProgress(target_dir)
    target = target_dir / STATUS_FILE
    original = target.read_bytes()
    path = install / STATUS_FILE
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "public":
        path.write_bytes(original)
        path.chmod(0o644)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    with pytest.raises(RuntimeError):
        read_update_status(install)
    with pytest.raises(RuntimeError):
        UpdateProgress(install)
    assert target.read_bytes() == original


def test_symlinked_or_writable_directory_rejected(install, tmp_path):
    linked = tmp_path / "linked"
    linked.symlink_to(install, target_is_directory=True)
    with pytest.raises(RuntimeError):
        read_update_status(linked)
    install.chmod(0o777)
    with pytest.raises(RuntimeError):
        UpdateProgress(install)
    assert not (install / STATUS_FILE).exists()


def test_atomic_failure_preserves_previous_status(monkeypatch, install):
    progress = UpdateProgress(install)
    original = (install / STATUS_FILE).read_bytes()
    def fail(*args, **kwargs):
        raise OSError("private-filesystem-error")
    monkeypatch.setattr(update_progress.os, "replace", fail)
    with pytest.raises(RuntimeError) as caught:
        progress.advance("downloading", current_commit=OLD, target_commit=NEW)
    assert "private" not in str(caught.value)
    assert (install / STATUS_FILE).read_bytes() == original
    assert not list(install.glob(".update-status-*"))


def test_unknown_stage_and_revision_are_not_written(install):
    progress = UpdateProgress(install)
    original = (install / STATUS_FILE).read_bytes()
    with pytest.raises(RuntimeError):
        progress.advance("private-stage")
    with pytest.raises(RuntimeError):
        progress.advance("downloading", target_commit="private-secret")
    assert (install / STATUS_FILE).read_bytes() == original


def test_busy_lock_never_replaces_active_progress(monkeypatch, install):
    progress = UpdateProgress(install)
    progress.advance("downloading", current_commit=OLD, target_commit=NEW)
    original = (install / STATUS_FILE).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("Busy update must not inspect provenance, GitHub, or services")
    monkeypatch.setattr(updater, "installed_revision", forbidden)
    monkeypatch.setattr(updater, "_remote_revision", forbidden)
    with (install / ".update.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        updater.run_update(install)
    assert (install / STATUS_FILE).read_bytes() == original


def test_current_commit_reports_terminal_status_without_installs(monkeypatch, install):
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: OLD)
    def forbidden(*args, **kwargs):
        pytest.fail("Current commit must not stage or contact services")
    monkeypatch.setattr(updater, "_stage", forbidden)
    monkeypatch.setattr(updater, "_service_active", forbidden)
    updater.run_update(install)
    status = read_update_status(install)
    assert status["state"] == "current" and status["stage"] == "complete"
    assert status["current_commit"] == status["target_commit"] == OLD
    assert updater._PROGRESS.get() is None


def test_staging_boundaries_emit_real_progress(monkeypatch, install):
    events = []
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    def run(args, **kwargs):
        status = read_update_status(install)
        if "venv" in args:
            assert status["stage"] == "downloading"
            runtime = Path(args[-1])
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/python").touch()
        elif "pip" in args:
            assert status["stage"] == "downloading"
            assert f"git+{updater.REPOSITORY}@{NEW}" in args
        else:
            assert status["stage"] == "validating"
        events.append(status["stage"])
        return SimpleNamespace(stdout="")
    def metadata(runtime):
        assert read_update_status(install)["stage"] == "validating"
        return {"url": updater.REPOSITORY, "vcs_info": {"vcs": "git", "requested_revision": NEW, "commit_id": NEW}}
    def activate(directory, runtime, active):
        assert active is True
        status = read_update_status(directory)
        assert status["stage"] == "restarting"
        assert status["current_commit"] == OLD and status["target_commit"] == NEW
        events.append(status["stage"])
    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(updater, "_metadata", metadata)
    monkeypatch.setattr(updater, "_service_active", lambda: True)
    monkeypatch.setattr(updater, "_activate", activate)
    updater.run_update(install)
    status = read_update_status(install)
    assert status["state"] == "succeeded"
    assert status["current_commit"] == status["target_commit"] == NEW
    assert events[:2] == ["downloading", "downloading"]
    assert "validating" in events and events[-1] == "restarting"


@pytest.mark.parametrize("failure", ["provenance", "remote", "stage", "service", "activation"])
def test_failures_publish_only_safe_terminal_status(monkeypatch, install, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("private-pip-output https://user:secret@private-index.invalid")
    monkeypatch.setattr(updater, "installed_revision", fail if failure == "provenance" else lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", fail if failure == "remote" else lambda: NEW)
    monkeypatch.setattr(updater, "_stage", fail if failure == "stage" else lambda *args: install / "new-runtime")
    monkeypatch.setattr(updater, "_service_active", fail if failure == "service" else lambda: True)
    monkeypatch.setattr(updater, "_activate", fail if failure == "activation" else lambda *args: None)
    with pytest.raises(RuntimeError):
        updater.run_update(install)
    status = read_update_status(install)
    assert status["state"] == status["stage"] == "failed"
    assert "private" not in json.dumps(status) and "secret" not in json.dumps(status)
    assert status["current_commit"] == (None if failure == "provenance" else OLD)
    assert status["target_commit"] == (None if failure in {"provenance", "remote"} else NEW)
    assert updater._PROGRESS.get() is None


def test_cancellation_reports_failure_and_restores_signal_handler(monkeypatch, install):
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(updater, "_stage", interrupted)
    with pytest.raises(KeyboardInterrupt):
        updater.run_update(install)
    assert read_update_status(install)["state"] == "failed"
    assert signal.getsignal(signal.SIGTERM) == previous


def test_failed_rollback_does_not_claim_an_unconfirmed_active_commit(monkeypatch, install):
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    monkeypatch.setattr(updater, "_stage", lambda *args: install / "new-runtime")
    monkeypatch.setattr(updater, "_service_active", lambda: True)
    def failed_rollback(*args):
        raise updater._RollbackFailure("private-failure")
    monkeypatch.setattr(updater, "_activate", failed_rollback)
    with pytest.raises(RuntimeError):
        updater.run_update(install)
    status = read_update_status(install)
    assert status["state"] == "failed"
    assert status["current_commit"] is None
    assert status["target_commit"] == NEW


def test_provenance_timeout_preserves_default_function_signature(monkeypatch, install):
    calls = []
    def metadata(runtime, *, timeout=60):
        calls.append(timeout)
        return {"url": updater.REPOSITORY, "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": OLD}}
    monkeypatch.setattr(updater, "_metadata", metadata)
    assert updater.installed_revision(install, timeout=5) == OLD
    assert updater.installed_revision(install) == OLD
    assert calls == [5, 60]


def test_metadata_subprocess_receives_supplied_timeout(monkeypatch, install):
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=json.dumps({"url": updater.REPOSITORY}))
    monkeypatch.setattr(updater, "_run", run)
    updater._metadata(install / "venv", timeout=5)
    assert calls == [{"timeout": 5}]


def test_smoke_never_reads_or_probes_operator_saved_state(monkeypatch, tmp_path):
    from llm_router import bootstrap, gateway
    from llm_router.saved_hosts import SavedHostStore
    operator = tmp_path / "operator"
    saved_path = operator / "config" / "llm-router" / "saved-hosts.json"
    store = SavedHostStore(saved_path)
    store.add("private-backend.invalid:1234")
    saved_bytes = saved_path.read_bytes()
    router_config = operator / "config" / "router.toml"
    router_config.write_text("private operator configuration")
    metrics_file = operator / "state" / "metrics.sqlite3"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(operator / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(operator / "state"))
    monkeypatch.setenv("LLM_ROUTER_SAVED_HOSTS_FILE", str(saved_path))
    monkeypatch.setenv("LLM_ROUTER_METRICS_FILE", str(metrics_file))
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(router_config))
    monkeypatch.setattr(bootstrap, "DEFAULT_CONFIG_LOCATIONS", (router_config,))
    calls, constructed = [], []
    real_store = gateway.SavedHostStore
    def tracking_store(*args, **kwargs):
        result = real_store(*args, **kwargs)
        constructed.append(result.path)
        return result
    async def forbidden_probe(*args, **kwargs):
        calls.append("probe")
        raise AssertionError("Smoke attempted real saved-host probe")
    def forbidden_config(*args, **kwargs):
        calls.append("config")
        raise AssertionError("Smoke attempted private configuration")
    monkeypatch.setattr(gateway, "SavedHostStore", tracking_store)
    monkeypatch.setattr(gateway, "check_saved_host", forbidden_probe)
    monkeypatch.setattr(bootstrap, "load_config", forbidden_config)
    before = {name: os.environ.get(name) for name in (
        "XDG_CONFIG_HOME", "XDG_STATE_HOME", "LLM_ROUTER_SAVED_HOSTS_FILE", "LLM_ROUTER_METRICS_FILE", "LLM_ROUTER_CONFIG",
    )}
    exec(updater._SMOKE_CODE, {})
    assert calls == []
    assert len(constructed) == 1 and constructed[0] != saved_path
    assert "llm-router-update-smoke-" in str(constructed[0])
    assert not constructed[0].parent.exists()
    assert saved_path.read_bytes() == saved_bytes
    assert router_config.read_text() == "private operator configuration"
    assert not metrics_file.exists()
    assert {name: os.environ.get(name) for name in before} == before
    assert bootstrap.DEFAULT_CONFIG_LOCATIONS == (router_config,)
