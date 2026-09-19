"""No real installs, service commands or network calls in dashboard controls."""

from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from llm_router import update_control as control
from llm_router.update_control import UpdateController, UpdateRequestError
from llm_router.update_progress import UpdateProgress, STATUS_FILE

OLD, NEW = "a" * 40, "b" * 40
OLD_RUN, NEW_RUN = "1" * 32, "2" * 32


@pytest.fixture
def managed(tmp_path, monkeypatch):
    install = tmp_path / "router"
    runtime = install / "venv"
    source = runtime / "lib" / "update_control.py"
    source.parent.mkdir(parents=True)
    source.touch()
    monkeypatch.setattr(control.sys, "platform", "linux")
    monkeypatch.setattr(control.sys, "prefix", str(runtime))
    monkeypatch.setattr(control, "__file__", str(source))
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    update = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success", "WorkingDirectory": str(install), "InvocationID": OLD_RUN}
    gateway = {"LoadState": "loaded", "ActiveState": "active", "MainPID": str(os.getpid())}
    calls = []
    validated = []

    def properties(unit):
        assert unit in {control.UPDATE_UNIT, control.GATEWAY_UNIT}
        return dict(update if unit == control.UPDATE_UNIT else gateway)

    def command(*arguments):
        assert arguments == ("start", "--no-block", control.UPDATE_UNIT)
        calls.append(arguments)
        return ""

    def revision(path, *, timeout):
        assert path == install and timeout == 5
        validated.append(path)
        return OLD

    def forbidden(*args, **kwargs):
        raise AssertionError("No real service command may execute in tests")

    monkeypatch.setattr(control, "_properties", properties)
    monkeypatch.setattr(control, "_systemctl", command)
    monkeypatch.setattr(control, "installed_revision", revision)
    monkeypatch.setattr(control.subprocess, "run", forbidden)
    return SimpleNamespace(install=install, runtime=runtime, update=update, gateway=gateway,
                           calls=calls, validated=validated, controller=UpdateController())


def write_progress(managed, monkeypatch, *, run_id=NEW_RUN, stage="downloading", state="running"):
    monkeypatch.setenv("INVOCATION_ID", run_id)
    progress = UpdateProgress(managed.install)
    progress.advance(stage, state=state, current_commit=OLD, target_commit=NEW)
    return progress


def test_status_is_read_only_and_missing_progress_does_not_create_storage(managed):
    managed.update["InvocationID"] = ""
    result = managed.controller.status()
    assert result["available"] and result["state"] == "idle" and not result["busy"]
    assert result["current_commit"] == OLD
    assert managed.calls == []
    assert not (managed.install / STATUS_FILE).exists()
    managed.controller.status()
    assert len(managed.validated) == 1, "Bounded local provenance should be cached for passive polling"


def test_start_queues_exact_fixed_unit_and_guards_repeated_requests(managed):
    result = managed.controller.start()
    assert result["state"] == "queued" and result["busy"] and result["run_id"] is None
    assert managed.calls == [("start", "--no-block", "llm-router-update.service")]
    assert managed.controller.status()["state"] == "queued"
    with pytest.raises(UpdateRequestError) as rejected:
        managed.controller.start()
    assert rejected.value.status_code == 409
    assert len(managed.calls) == 1


def test_old_success_is_not_completion_of_new_queued_run(managed, monkeypatch):
    write_progress(managed, monkeypatch, run_id=OLD_RUN, stage="complete", state="succeeded")
    assert managed.controller.status()["state"] == "succeeded"
    managed.controller.start()
    assert managed.controller.status()["state"] == "queued"
    managed.update.update(ActiveState="activating", InvocationID=NEW_RUN)
    pending = managed.controller.status()
    assert pending["state"] == "queued" and pending["run_id"] == NEW_RUN
    progress = write_progress(managed, monkeypatch)
    assert managed.controller.status()["stage"] == "downloading"
    progress.advance("restarting")
    assert UpdateController().status()["stage"] == "restarting", "Gateway restarts recover the updater's durable job"
    progress.advance("complete", state="succeeded")
    managed.update["ActiveState"] = "inactive"
    result = UpdateController().status()
    assert result["state"] == "succeeded" and not result["busy"]


@pytest.mark.parametrize("active", ["activating", "active", "reloading", "deactivating"])
def test_existing_timer_update_blocks_another_start(managed, active):
    managed.update["ActiveState"] = active
    assert managed.controller.status()["busy"]
    with pytest.raises(UpdateRequestError) as error:
        managed.controller.start()
    assert error.value.status_code == 409 and not managed.calls


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_crashed_updater_is_interrupted_not_perpetually_running(managed, monkeypatch, state):
    write_progress(managed, monkeypatch)
    managed.update.update(ActiveState=state, InvocationID=NEW_RUN)
    result = managed.controller.status()
    assert result["state"] == "interrupted" and not result["busy"]


def test_new_invocation_exiting_without_progress_never_reuses_prior_success(managed, monkeypatch):
    write_progress(managed, monkeypatch, run_id=OLD_RUN, stage="complete", state="succeeded")
    managed.update.update(ActiveState="inactive", InvocationID=NEW_RUN)
    result = managed.controller.status()
    assert result["state"] == "interrupted" and result["run_id"] == NEW_RUN
    assert not result["busy"]


@pytest.mark.parametrize("active,expected", [("inactive", "interrupted"), ("failed", "failed")])
def test_missing_progress_after_service_exit_has_terminal_job_identity(managed, active, expected):
    # Covers updating from an older version without progress reporting, or a
    # crash/lock contention before the first progress file could be written.
    managed.update.update(ActiveState="activating", InvocationID=NEW_RUN)
    queued = managed.controller.status()
    assert queued["busy"] and queued["run_id"] == NEW_RUN
    managed.update["ActiveState"] = active
    finished = managed.controller.status()
    assert finished["state"] == expected and finished["run_id"] == NEW_RUN
    assert not finished["busy"]


def test_unconfirmed_start_timeout_does_not_retry_mutation(managed, monkeypatch):
    def uncertain(*arguments):
        managed.calls.append(arguments)
        raise UpdateRequestError("Fixed safe failure")

    monkeypatch.setattr(control, "_systemctl", uncertain)
    with pytest.raises(UpdateRequestError, match="could not be confirmed"):
        managed.controller.start()
    assert managed.controller.status()["state"] == "queued"
    with pytest.raises(UpdateRequestError) as duplicate:
        managed.controller.start()
    assert duplicate.value.status_code == 409 and len(managed.calls) == 1
    started = managed.controller._last_request
    monkeypatch.setattr(control.time, "monotonic", lambda: started + 31)
    assert managed.controller.status()["state"] == "interrupted"
    assert len(managed.calls) == 1


@pytest.mark.parametrize("attribute,value", [("LoadState", "not-found"), ("WorkingDirectory", ""), ("WorkingDirectory", "relative"), ("WorkingDirectory", "/private\nsecret")])
def test_missing_or_invalid_unit_is_unavailable_without_start(managed, attribute, value):
    managed.update[attribute] = value
    result = managed.controller.status()
    assert result["available"] is False and "private" not in result["message"]
    with pytest.raises(UpdateRequestError):
        managed.controller.start()
    assert not managed.calls


@pytest.mark.parametrize("attribute,value", [("LoadState", "not-found"), ("ActiveState", "inactive"), ("MainPID", "0"), ("MainPID", "other")])
def test_unrelated_gateway_process_cannot_start_managed_updater(managed, attribute, value):
    managed.gateway[attribute] = value
    assert not managed.controller.status()["available"]
    assert not managed.validated and not managed.calls


def test_unrelated_runtime_or_source_tree_is_rejected(managed, tmp_path, monkeypatch):
    monkeypatch.setattr(control, "__file__", str(tmp_path / "source-tree.py"))
    assert not managed.controller.status()["available"]
    assert not managed.validated and not managed.calls


def test_insecure_install_directory_is_rejected(managed):
    managed.install.chmod(0o777)
    assert not managed.controller.status()["available"]
    assert not managed.validated and not managed.calls


@pytest.mark.parametrize("platform,root", [("win32", False), ("darwin", False), ("linux", True)])
def test_unsupported_platform_and_root_do_not_inspect_systemd(managed, monkeypatch, platform, root):
    monkeypatch.setattr(control.sys, "platform", platform)
    if root:
        monkeypatch.setattr(control.os, "geteuid", lambda: 0)

    def forbidden(*args):
        raise AssertionError("Must reject before inspecting service metadata")

    monkeypatch.setattr(control, "_properties", forbidden)
    assert not managed.controller.status()["available"]


def test_nonofficial_provenance_is_safely_rejected(managed, monkeypatch):
    def invalid(*args, **kwargs):
        raise RuntimeError("private-fork-url secret-key /private/path")

    monkeypatch.setattr(control, "installed_revision", invalid)
    status = managed.controller.status()
    assert not status["available"]
    assert "Pinned" in status["message"] and "secret-key" not in str(status)
    assert not managed.calls


def test_unsafe_progress_does_not_leak_or_allow_mutation(managed, monkeypatch):
    progress = managed.install / STATUS_FILE
    progress.write_text("private-secret")
    progress.chmod(0o600)
    result = managed.controller.status()
    assert not result["available"] and "private-secret" not in str(result)
    with pytest.raises(UpdateRequestError):
        managed.controller.start()
    assert not managed.calls and progress.read_text() == "private-secret"


def test_systemctl_uses_fixed_argv_short_timeout_and_no_router_secrets(monkeypatch):
    observed = []
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "private-router-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")
    monkeypatch.setenv("CUSTOM_SECRET", "private-custom-secret")

    def run(arguments, **options):
        observed.append(arguments)
        assert arguments == ["systemctl", "--user", "start", "--no-block", control.UPDATE_UNIT]
        assert options["timeout"] == 5 and options["check"]
        assert not any("private-" in value for value in options["env"].values())
        assert "shell" not in options
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(control.subprocess, "run", run)
    assert control._systemctl("start", "--no-block", control.UPDATE_UNIT) == ""
    assert len(observed) == 1


def test_systemctl_errors_never_expose_command_output(monkeypatch):
    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "systemctl", output="private-key", stderr="private-path")

    monkeypatch.setattr(control.subprocess, "run", failed)
    with pytest.raises(UpdateRequestError) as error:
        control._systemctl("show", control.UPDATE_UNIT)
    assert "private" not in str(error.value)
