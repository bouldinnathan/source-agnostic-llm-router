from __future__ import annotations

import fcntl
import json
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import updater

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture(autouse=True)
def prohibit_host_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Updater tests must not install packages or modify host services")
    monkeypatch.setattr(updater.subprocess, "run", blocked)
    monkeypatch.setattr(updater.time, "sleep", lambda _: None)


@pytest.fixture
def installation(tmp_path: Path) -> Path:
    install = tmp_path / "router install"
    python = install / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    (install / "venv/local-state").write_text("old runtime")
    (install / "router.env").write_text("PRIVATE_KEY=preserve")
    (install / "releases").mkdir()
    return install


def metadata(commit: str = OLD, requested: str = "main", url: str = updater.REPOSITORY) -> dict:
    return {"url": url, "vcs_info": {"vcs": "git", "requested_revision": requested, "commit_id": commit}}


def test_official_main_metadata_is_accepted(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    monkeypatch.setattr(updater, "_metadata", lambda _: metadata())
    assert updater.installed_revision(installation) == OLD


@pytest.mark.parametrize("value", [
    metadata(url="https://example.com/fork.git"), metadata(requested="v0.3.0"),
    metadata(requested=OLD), metadata(commit="not-a-commit"), {"url": updater.REPOSITORY},
    {"url": updater.REPOSITORY, "dir_info": {"editable": True}},
])
def test_non_main_sources_are_rejected(monkeypatch: pytest.MonkeyPatch, installation: Path, value: dict) -> None:
    monkeypatch.setattr(updater, "_metadata", lambda _: value)
    with pytest.raises(RuntimeError):
        updater.installed_revision(installation)


def test_updater_sha_requires_matching_runtime_provenance(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    monkeypatch.setattr(updater, "_metadata", lambda _: metadata(NEW, NEW))
    marker = installation / "venv" / updater.PROVENANCE_FILE
    marker.write_text(json.dumps({"repository": updater.REPOSITORY, "branch": "main", "commit": OLD}))
    with pytest.raises(RuntimeError, match="Pinned"):
        updater.installed_revision(installation)
    marker.write_text(json.dumps({"repository": updater.REPOSITORY, "branch": "main", "commit": NEW}))
    assert updater.installed_revision(installation) == NEW


def test_metadata_queries_active_python_in_isolation(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(metadata()))
    monkeypatch.setattr(updater, "_run", run)
    assert updater.installed_revision(installation) == OLD
    assert calls[0][:3] == [str(installation / "venv/bin/python"), "-I", "-c"]


@pytest.mark.parametrize("output", ["", "notsha\trefs/heads/main", NEW + "\trefs/heads/other", NEW + "\trefs/heads/main\n" + OLD + "\trefs/heads/main"])
def test_remote_metadata_must_be_exact(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(updater, "_run", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(RuntimeError):
        updater._remote_revision()


def test_unchanged_commit_does_not_stage_or_contact_systemd(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: OLD)
    updater.run_update(installation)
    assert not (installation / "venv").is_symlink()
    assert (installation / "venv/local-state").read_text() == "old runtime"


def test_update_lock_skips_all_work(monkeypatch: pytest.MonkeyPatch, installation: Path, capsys: pytest.CaptureFixture) -> None:
    with (installation / ".update.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        updater.run_update(installation)
    assert "skipping" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["download", "metadata", "smoke"])
def test_stage_failure_never_changes_active_or_restarts(monkeypatch: pytest.MonkeyPatch, installation: Path, failure: str) -> None:
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    def fail(*args):
        raise RuntimeError(failure)
    monkeypatch.setattr(updater, "_populate_release", fail)
    with pytest.raises(RuntimeError, match=failure):
        updater.run_update(installation)
    assert list((installation / "releases").iterdir()) == []
    assert (installation / "venv/local-state").read_text() == "old runtime"
    assert (installation / "router.env").read_text() == "PRIVATE_KEY=preserve"


def staged_runtime(installation: Path) -> Path:
    runtime = installation / "releases/new/venv"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").touch()
    (runtime / "local-state").write_text("new runtime")
    return runtime


@pytest.mark.parametrize("was_active", [False, True])
def test_first_activation_preserves_previous_runtime_and_stopped_state(monkeypatch: pytest.MonkeyPatch, installation: Path, was_active: bool) -> None:
    runtime = staged_runtime(installation)
    calls = []
    monkeypatch.setattr(updater, "_run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(updater, "_restart_and_verify", lambda: calls.append("restart"))
    updater._activate(installation, runtime, was_active)
    assert (installation / "venv").is_symlink()
    assert (installation / "venv").resolve() == runtime
    backups = list((installation / "releases").glob("pre-update-*/venv/local-state"))
    assert len(backups) == 1 and backups[0].read_text() == "old runtime"
    assert (installation / "router.env").read_text() == "PRIVATE_KEY=preserve"
    if was_active:
        assert calls == [["systemctl", "--user", "stop", updater.SERVICE_NAME], "restart"]
    else:
        assert calls == []


@pytest.mark.parametrize("existing_link", [False, True])
def test_failed_activation_rolls_back_and_restarts_previous_runtime(monkeypatch: pytest.MonkeyPatch, installation: Path, existing_link: bool) -> None:
    active = installation / "venv"
    original = active
    if existing_link:
        original = installation / "releases/original"
        active.rename(original)
        active.symlink_to(original, target_is_directory=True)
    runtime = staged_runtime(installation)
    calls = []
    monkeypatch.setattr(updater, "_run", lambda *a, **k: None)
    def restart():
        calls.append(active.resolve())
        if len(calls) == 1:
            raise RuntimeError("new runtime failed")
    monkeypatch.setattr(updater, "_restart_and_verify", restart)
    with pytest.raises(RuntimeError, match="previous runtime was restored"):
        updater._activate(installation, runtime, True)
    assert active.resolve() == original
    assert active.is_symlink() == existing_link
    assert (active / "local-state").read_text() == "old runtime"
    assert calls == [runtime, original]
    assert runtime.exists()


def test_first_link_failure_restores_real_directory(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    runtime = staged_runtime(installation)
    def fail(*args):
        raise OSError("link failed")
    monkeypatch.setattr(updater, "_point_to", fail)
    with pytest.raises(RuntimeError, match="previous runtime was restored"):
        updater._activate(installation, runtime, False)
    assert not (installation / "venv").is_symlink()
    assert (installation / "venv/local-state").read_text() == "old runtime"


def test_running_state_checked_after_staging(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    events = []
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    runtime = staged_runtime(installation)
    def stage(*args):
        events.append("stage")
        return runtime
    def state():
        events.append("state")
        return False
    monkeypatch.setattr(updater, "_stage", stage)
    monkeypatch.setattr(updater, "_service_active", state)
    monkeypatch.setattr(updater, "_activate", lambda *args: events.append(("activate", args[-1])))
    updater.run_update(installation)
    assert events == ["stage", "state", ("activate", False)]


@pytest.mark.parametrize("change", ["MainPID=88", "NRestarts=1", "ActiveState=failed", "SubState=auto-restart"])
def test_startup_verification_detects_restart_loops(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    healthy = "ActiveState=active\nSubState=running\nMainPID=42\nNRestarts=0"
    key = change.split("=", 1)[0]
    unhealthy = "\n".join(change if line.startswith(key + "=") else line for line in healthy.splitlines())
    replies = iter(["", healthy, unhealthy])
    monkeypatch.setattr(updater, "_run", lambda *a, **k: SimpleNamespace(stdout=next(replies)))
    with pytest.raises(RuntimeError, match="remain running"):
        updater._restart_and_verify()


def test_startup_verification_observes_stable_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="ActiveState=active\nSubState=running\nMainPID=42\nNRestarts=0")
    monkeypatch.setattr(updater, "_run", run)
    updater._restart_and_verify()
    assert len(calls) == 6


def test_commands_are_bounded_noninteractive_and_do_not_log_secrets(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-provider")
    monkeypatch.setenv("LLM_ROUTER_CONFIG", "/private/config")
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "secret-gateway")
    calls = []
    def fail(args, **kwargs):
        calls.append(kwargs)
        raise subprocess.CalledProcessError(1, args, stderr="https://secret-pip-token@example.com")
    monkeypatch.setattr(updater.subprocess, "run", fail)
    with pytest.raises(RuntimeError) as error:
        updater._run(["git", "ls-remote"])
    assert "secret" not in str(error.value)
    env = calls[0]["env"]
    assert "OPENAI_API_KEY" not in env and "LLM_ROUTER_CONFIG" not in env
    assert "LLM_ROUTER_GATEWAY_API_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["PIP_NO_INPUT"] == "1"
    assert calls[0]["timeout"] == 60 and calls[0]["capture_output"] is True
    assert "secret" not in capsys.readouterr().err


def test_stage_installs_exact_commit_and_smokes_every_entrypoint(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "venv" in args:
            python = Path(args[-1]) / "bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(updater, "_metadata", lambda _: metadata(NEW, NEW))
    runtime = updater._stage(installation, NEW)
    assert runtime.parent.parent == installation / "releases"
    assert f"git+{updater.REPOSITORY}@{NEW}" in calls[1][0]
    assert calls[1][1]["timeout"] == 1800
    assert [args[-2] for args, _ in calls if args[-1] == "--help"] == [
        "llm_router.cli", "llm_router.gateway", "llm_router.service", "llm_router.updater",
    ]
    assert "lifespan_context" in calls[-1][0][-1]
    assert "bootstrap.DEFAULT_CONFIG_LOCATIONS = ()" in calls[-1][0][-1]
    assert all(kwargs.get("smoke") for _, kwargs in calls[2:])
    marker = json.loads((runtime / updater.PROVENANCE_FILE).read_text())
    assert marker["commit"] == NEW


def test_cli_reports_failure_without_traceback(monkeypatch: pytest.MonkeyPatch, installation: Path, capsys: pytest.CaptureFixture) -> None:
    def fail(_):
        raise RuntimeError("source rejected")
    monkeypatch.setattr(updater, "run_update", fail)
    assert updater.main(["--install-dir", str(installation)]) == 2
    output = capsys.readouterr().err
    assert "source rejected" in output and "Traceback" not in output


def test_smoke_never_loads_default_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from llm_router import bootstrap
    from llm_router.discovery import ModelDiscovery

    home_config = tmp_path / "private-router.toml"
    home_config.write_text("This private user config must never be loaded")
    monkeypatch.setattr(bootstrap, "DEFAULT_CONFIG_LOCATIONS", (home_config,))
    monkeypatch.delenv("LLM_ROUTER_CONFIG", raising=False)
    def fail(*args, **kwargs):
        raise AssertionError("The smoke test accessed user configuration or a backend")
    monkeypatch.setattr(bootstrap, "load_config", fail)
    monkeypatch.setattr(ModelDiscovery, "_probe", fail, raising=False)
    exec(updater._SMOKE_CODE, {})


def test_stop_timeout_attempts_to_restore_running_intent(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    runtime = staged_runtime(installation)
    restarted = []
    def fail(*args, **kwargs):
        raise RuntimeError("stop timed out after stopping the service")
    monkeypatch.setattr(updater, "_run", fail)
    monkeypatch.setattr(updater, "_restart_and_verify", lambda: restarted.append(True))
    with pytest.raises(RuntimeError, match="previous runtime was restored"):
        updater._activate(installation, runtime, True)
    assert restarted == [True]
    assert (installation / "venv/local-state").read_text() == "old runtime"


def test_cancellation_during_activation_restores_runtime_and_signal_handler(monkeypatch: pytest.MonkeyPatch, installation: Path) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    runtime = staged_runtime(installation)
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    monkeypatch.setattr(updater, "_stage", lambda *args: runtime)
    monkeypatch.setattr(updater, "_service_active", lambda: True)
    monkeypatch.setattr(updater, "_run", lambda *args, **kwargs: None)
    restarts = []
    def restart():
        restarts.append(True)
        if len(restarts) == 1:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
    monkeypatch.setattr(updater, "_restart_and_verify", restart)
    with pytest.raises(RuntimeError, match="previous runtime was restored"):
        updater.run_update(installation)
    assert restarts == [True, True]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert (installation / "venv/local-state").read_text() == "old runtime"


def test_release_directory_symlink_is_rejected(installation: Path, tmp_path: Path) -> None:
    (installation / "releases").rmdir()
    (installation / "releases").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        updater._stage(installation, NEW)


def test_drain_waits_for_in_flight_answers_then_restarts_anyway(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(updater, "_router_status_url", lambda: ("http://127.0.0.1:8088/router/status", "k"))
    counts = iter([2, 2, 1, 0])
    polled: list[tuple[str, str | None]] = []

    def in_flight(url: str, key: str | None) -> int | None:
        polled.append((url, key))
        return next(counts)

    monkeypatch.setattr(updater, "_in_flight", in_flight)
    monkeypatch.setattr(updater.time, "sleep", lambda _: None)
    updater._drain()
    assert len(polled) == 4 and polled[0] == ("http://127.0.0.1:8088/router/status", "k")
    output = capsys.readouterr().out
    assert output.count("Waiting for") == 2, "Progress is printed when the count changes, not every poll"
    assert "2 in-flight requests" in output and "1 in-flight request " in output

    monkeypatch.setattr(updater, "_in_flight", lambda url, key: 3)
    updater._drain(limit=0)
    assert "Restarting anyway" in capsys.readouterr().out

    monkeypatch.setattr(updater, "_in_flight", lambda url, key: None)
    updater._drain()
    monkeypatch.setattr(updater, "_router_status_url", lambda: None)
    updater._drain()


def test_router_status_url_comes_from_the_service_environment_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert updater._router_status_url() is None, "No service environment means no drain"
    env_dir = tmp_path / "llm-router"
    env_dir.mkdir()
    (env_dir / "router.env").write_text("# managed\nLLM_ROUTER_HOST=0.0.0.0\nLLM_ROUTER_PORT=9099\nLLM_ROUTER_GATEWAY_API_KEY=\"secret\"\n", encoding="utf-8")
    assert updater._router_status_url() == ("http://127.0.0.1:9099/router/status", "secret"), "A LAN listener is reached on loopback"
    (env_dir / "router.env").write_text("LLM_ROUTER_PORT=notaport\n", encoding="utf-8")
    assert updater._router_status_url() is None
