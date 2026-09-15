from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import service


@pytest.fixture(autouse=True)
def prohibit_real_service_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Service tests must never invoke host commands")

    monkeypatch.setattr(service.subprocess, "run", blocked)


@pytest.fixture
def install_paths(tmp_path: Path) -> tuple[Path, Path]:
    install_dir = tmp_path / "installation"
    python = install_dir / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return install_dir, tmp_path / "configuration"


def env_values(path: Path) -> dict[str, str]:
    return {
        name: value.strip().strip('"')
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for name, separator, value in [line.partition("=")]
        if separator
    }


def test_write_service_creates_safe_defaults_and_unit(
    install_paths: tuple[Path, Path],
) -> None:
    install_dir, config_home = install_paths

    unit_path, env_path = service.write_user_service(install_dir, config_home)

    assert unit_path == config_home / "systemd" / "user" / "llm-router.service"
    assert env_path == config_home / "llm-router" / "router.env"
    values = env_values(env_path)
    assert values["LLM_ROUTER_HOST"] == "127.0.0.1"
    assert values["LLM_ROUTER_PORT"] == "8088"
    assert values["LLM_ROUTER_AUTO_PROVISION"] == "0"
    assert len(values["LLM_ROUTER_GATEWAY_API_KEY"]) >= 32
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert not any(
        "example" in value or "golemframe" in value or "pantheon" in value
        for value in values.values()
    )
    unit = unit_path.read_text()
    assert f'ExecStart=:"{install_dir / "venv/bin/python"}" -m llm_router.gateway' in unit
    assert f'WorkingDirectory={config_home / "llm-router"}\n' in unit
    assert f"EnvironmentFile={env_path}\n" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_fresh_installations_receive_distinct_api_keys(tmp_path: Path) -> None:
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)
    _, first = service.write_user_service(tmp_path, tmp_path / "config-one")
    _, second = service.write_user_service(tmp_path, tmp_path / "config-two")

    assert env_values(first)["LLM_ROUTER_GATEWAY_API_KEY"] != env_values(second)[
        "LLM_ROUTER_GATEWAY_API_KEY"
    ]


def test_rerun_preserves_key_env_config_and_unchanged_unit(
    install_paths: tuple[Path, Path],
) -> None:
    install_dir, config_home = install_paths
    unit_path, env_path = service.write_user_service(install_dir, config_home)
    config_path = env_path.parent / "router.toml"
    config_path.write_bytes(b"# User configuration\n[router]\ndefault_model = 'mine'\n")
    env_path.write_bytes(
        b"# Keep comments and CRLF\r\nLLM_ROUTER_GATEWAY_API_KEY=my-existing-key\r\n"
        b"LLM_ROUTER_HOST=0.0.0.0\r\nLLM_ROUTER_PORT=9123\r\n"
    )
    expected = {path: path.read_bytes() for path in (unit_path, env_path, config_path)}
    expected_unit_files = set(unit_path.parent.iterdir())

    assert service.write_user_service(install_dir, config_home) == (unit_path, env_path)

    assert {path: path.read_bytes() for path in expected} == expected
    assert set(unit_path.parent.iterdir()) == expected_unit_files


def test_existing_empty_env_is_not_replaced(
    install_paths: tuple[Path, Path],
) -> None:
    install_dir, config_home = install_paths
    env_path = config_home / "llm-router/router.env"
    env_path.parent.mkdir(parents=True)
    env_path.touch(mode=0o600)

    service.write_user_service(install_dir, config_home)

    assert env_path.read_bytes() == b""


def test_changed_unit_is_backed_up_alongside_replacement(
    install_paths: tuple[Path, Path],
) -> None:
    install_dir, config_home = install_paths
    unit_path, env_path = service.write_user_service(install_dir, config_home)
    original_env = env_path.read_bytes()
    old_unit = b"# Existing custom service\n[Service]\nExecStart=/old/router\n"
    unit_path.write_bytes(old_unit)

    service.write_user_service(install_dir, config_home)

    assert unit_path.read_bytes() != old_unit
    backups = [path for path in unit_path.parent.iterdir() if path != unit_path]
    assert any(path.is_file() and path.read_bytes() == old_unit for path in backups)
    assert env_path.read_bytes() == original_env
    previous_backups = {path: path.read_bytes() for path in backups if path.is_file()}
    second_old_unit = b"# A second custom version must not erase the first backup\n"
    unit_path.write_bytes(second_old_unit)

    service.write_user_service(install_dir, config_home)

    assert all(path.read_bytes() == content for path, content in previous_backups.items())
    assert any(
        path != unit_path and path.is_file() and path.read_bytes() == second_old_unit
        for path in unit_path.parent.iterdir()
    )


def test_service_paths_are_absolute_and_escape_systemd_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    install_dir = Path("install space %n $HOME")
    python = install_dir / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)
    config_home = Path("config space %u $USER")

    unit_path, env_path = service.write_user_service(install_dir, config_home)

    assert unit_path.is_absolute()
    assert env_path.is_absolute()
    unit_lines = unit_path.read_text().splitlines()
    exec_line = next(line for line in unit_lines if line.startswith("ExecStart="))
    assert exec_line.startswith(f'ExecStart=:"{tmp_path}/')
    assert "%%n" in exec_line
    assert "$HOME" in exec_line
    assert "$$HOME" not in exec_line
    for prefix in ("WorkingDirectory=", "EnvironmentFile="):
        line = next(line for line in unit_lines if line.startswith(prefix))
        assert line.startswith(f"{prefix}{tmp_path}/")
        assert "%%u" in line
        assert "$USER" in line
        assert '"' not in line


@pytest.mark.parametrize("character", ["'", '"', "\\", "*", "?", "[", "]", "\n", "\r", "\t", "\x01", "\x7f"])
@pytest.mark.parametrize("invalid_path", ["installation", "configuration"])
def test_unsupported_service_path_characters_fail_before_writing_configuration(
    tmp_path: Path, character: str, invalid_path: str,
) -> None:
    install_dir = tmp_path / (f"install{character}path" if invalid_path == "installation" else "installation")
    config_home = tmp_path / (f"config{character}path" if invalid_path == "configuration" else "configuration")
    python = install_dir / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)

    with pytest.raises(ValueError):
        service.write_user_service(install_dir, config_home)

    assert not config_home.exists()


def test_missing_python_fails_before_writing_files(tmp_path: Path) -> None:
    config_home = tmp_path / "configuration"

    with pytest.raises((RuntimeError, FileNotFoundError), match="[Pp]ython|venv"):
        service.write_user_service(tmp_path / "missing-install", config_home)

    assert not config_home.exists()


class FakeUserManager:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.manager_available = True
        self.linger = True
        self.enable_allowed = True
        self.sudo_allowed = True
        self.enable_changes_linger = True
        self.missing_tools: set[str] = set()

    def which(self, name: str) -> str | None:
        return None if name in self.missing_tools else f"/usr/bin/{name}"

    def run(
        self, args: list[str], *, check: bool = False, **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(arg) for arg in args]
        self.calls.append((command, check))
        privileged = Path(command[0]).name == "sudo"
        action = command[1:] if privileged else command[:]
        action[0] = Path(action[0]).name
        code = 0
        stdout = ""
        stderr = ""
        if action == ["systemctl", "--user", "show-environment"]:
            if not self.manager_available:
                code, stderr = 1, "Failed to connect to user bus"
        elif action == [
            "loginctl", "show-user", "router-test", "--property=Linger", "--value",
        ]:
            stdout = "yes\n" if self.linger else "no\n"
        elif action == ["loginctl", "enable-linger", "router-test"]:
            allowed = self.sudo_allowed if privileged else self.enable_allowed
            if allowed and self.enable_changes_linger:
                self.linger = True
            elif not allowed:
                code, stderr = 1, "Access denied"
        else:
            raise AssertionError(f"Unexpected service command: {command!r}")
        if check and code:
            raise subprocess.CalledProcessError(code, command, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)


@pytest.fixture
def user_manager(monkeypatch: pytest.MonkeyPatch) -> FakeUserManager:
    manager = FakeUserManager()
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)

    def user_for_uid(uid: int) -> SimpleNamespace:
        assert uid == 1000
        return SimpleNamespace(pw_name="router-test")

    monkeypatch.setattr(service.pwd, "getpwuid", user_for_uid)
    monkeypatch.setattr(service.shutil, "which", manager.which)
    monkeypatch.setattr(service.subprocess, "run", manager.run)
    return manager


def test_preflight_rejects_root_before_running_commands(
    user_manager: FakeUserManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="root|sudo|regular|normal"):
        service.preflight_user_service()

    assert user_manager.calls == []


@pytest.mark.parametrize("tool", ["systemctl", "loginctl"])
def test_preflight_requires_systemd_tools(
    user_manager: FakeUserManager, tool: str,
) -> None:
    user_manager.missing_tools.add(tool)

    with pytest.raises(RuntimeError, match="systemd|systemctl|loginctl"):
        service.preflight_user_service()


def test_preflight_explains_missing_user_manager(user_manager: FakeUserManager) -> None:
    user_manager.manager_available = False

    with pytest.raises(RuntimeError, match="user|session|bus|login"):
        service.preflight_user_service()

    assert not any("enable-linger" in command for command, _ in user_manager.calls)


def test_preflight_already_lingering_needs_no_privileged_change(
    user_manager: FakeUserManager,
) -> None:
    service.preflight_user_service()

    assert any("show-environment" in command for command, _ in user_manager.calls)
    assert any("show-user" in command for command, _ in user_manager.calls)
    assert not any("enable-linger" in command for command, _ in user_manager.calls)
    assert not any(Path(command[0]).name == "sudo" for command, _ in user_manager.calls)


def test_preflight_enables_linger_for_boot_and_logout_without_sudo_when_possible(
    user_manager: FakeUserManager,
) -> None:
    user_manager.linger = False

    service.preflight_user_service()

    assert user_manager.linger
    enables = [command for command, _ in user_manager.calls if "enable-linger" in command]
    assert len(enables) == 1
    assert Path(enables[0][0]).name == "loginctl"
    assert sum("show-user" in command for command, _ in user_manager.calls) == 2


def test_preflight_uses_uid_account_not_misleading_username_environment(
    user_manager: FakeUserManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGNAME", "wrong-logname-account")
    monkeypatch.setenv("USER", "wrong-user-account")
    monkeypatch.setenv("USERNAME", "wrong-username-account")
    user_manager.linger = False

    service.preflight_user_service()

    queries = [command for command, _ in user_manager.calls if "show-user" in command]
    enables = [command for command, _ in user_manager.calls if "enable-linger" in command]
    assert len(queries) == 2
    assert all(command[command.index("show-user") + 1] == "router-test" for command in queries)
    assert len(enables) == 1
    assert enables[0][-1] == "router-test"


def test_preflight_uses_sudo_only_when_linger_enable_requires_it(
    user_manager: FakeUserManager,
) -> None:
    user_manager.linger = False
    user_manager.enable_allowed = False

    service.preflight_user_service()

    assert user_manager.linger
    enables = [command for command, _ in user_manager.calls if "enable-linger" in command]
    assert len(enables) == 2
    assert Path(enables[0][0]).name == "loginctl"
    assert Path(enables[1][0]).name == "sudo"
    assert enables[1][-2:] == ["enable-linger", "router-test"]
    assert sum("show-user" in command for command, _ in user_manager.calls) == 2


@pytest.mark.parametrize("failure", ["missing-sudo", "sudo-denied", "not-enabled"])
def test_preflight_fails_actionably_if_linger_cannot_be_enabled(
    user_manager: FakeUserManager, failure: str,
) -> None:
    user_manager.linger = False
    if failure == "not-enabled":
        user_manager.enable_changes_linger = False
    else:
        user_manager.enable_allowed = False
        if failure == "missing-sudo":
            user_manager.missing_tools.add("sudo")
        else:
            user_manager.sudo_allowed = False

    with pytest.raises(RuntimeError, match="linger|Linger|loginctl"):
        service.preflight_user_service()


def test_install_runs_preflight_then_writes_then_activates_checked_commands(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir, config_home = install_paths
    events: list[object] = []
    monkeypatch.setattr(service, "preflight_user_service", lambda: events.append("preflight"))

    def write(installation: Path, configuration: Path) -> tuple[Path, Path]:
        assert (installation, configuration) == install_paths
        events.append("write")
        return config_home / "unit", config_home / "environment"

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert check is True
        events.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(service, "write_user_service", write)
    monkeypatch.setattr(service.subprocess, "run", run)

    service.install_user_service(install_dir, config_home)

    assert events == [
        "preflight",
        "write",
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "llm-router.service"],
        ["systemctl", "--user", "restart", "llm-router.service"],
        ["systemctl", "--user", "is-active", "--quiet", "llm-router.service"],
    ]


def test_install_does_not_write_when_preflight_fails(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir, config_home = install_paths

    def fail_preflight() -> None:
        raise RuntimeError("User manager is unavailable")

    monkeypatch.setattr(service, "preflight_user_service", fail_preflight)

    with pytest.raises(RuntimeError, match="User manager"):
        service.install_user_service(install_dir, config_home)

    assert not config_home.exists()


@pytest.mark.parametrize("failed_action", ["daemon-reload", "enable", "restart", "is-active"])
def test_install_does_not_report_success_when_service_command_fails(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, failed_action: str,
) -> None:
    install_dir, config_home = install_paths
    monkeypatch.setattr(service, "preflight_user_service", lambda: None)
    commands: list[list[str]] = []

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        assert check is True
        if failed_action in args:
            raise subprocess.CalledProcessError(1, args, stderr="Service failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        service.install_user_service(install_dir, config_home)

    assert failed_action in commands[-1]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("No user manager"),
        OSError("Cannot write unit"),
        ValueError("Invalid service path"),
        subprocess.CalledProcessError(1, ["systemctl", "--user", "restart", "llm-router.service"]),
    ],
)
def test_service_cli_reports_install_failures_without_success(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], error: Exception,
) -> None:
    install_dir, config_home = install_paths

    def fail(installation: Path, configuration: Path) -> None:
        assert (installation, configuration) == install_paths
        raise error

    monkeypatch.setattr(service, "install_user_service", fail)

    result = service.main([
        "--install-dir", str(install_dir), "--config-home", str(config_home),
    ])

    output = capsys.readouterr()
    assert result == 2
    assert "Service installation failed" in output.err
    assert str(error) in output.err
    assert "journalctl --user -u llm-router.service" in output.err
    assert "enabled and running" not in output.out
    assert not config_home.exists()


def test_service_cli_passes_explicit_install_and_configuration_paths(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir, config_home = install_paths
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        service, "install_user_service", lambda installation, configuration: calls.append(
            (installation, configuration)
        ),
    )

    assert service.main([
        "--install-dir", str(install_dir), "--config-home", str(config_home),
    ]) == 0

    assert calls == [install_paths]


@pytest.fixture
def update_source(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    from llm_router import updater

    checked_paths: list[Path] = []

    def installed_revision(install_dir: Path) -> str:
        checked_paths.append(install_dir)
        return "a" * 40

    monkeypatch.setattr(updater, "installed_revision", installed_revision)
    return checked_paths


def test_write_update_units_creates_daily_persistent_timer_without_credentials(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir, config_home = install_paths
    secret = "private-test-key-never-copy-to-an-update-unit"
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    env_path = config_home / "llm-router/router.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f"LLM_ROUTER_GATEWAY_API_KEY={secret}\n")
    original_env = env_path.read_bytes()

    update_path, timer_path = service.write_update_units(install_dir, config_home)

    assert update_path == config_home / "systemd/user/llm-router-update.service"
    assert timer_path == config_home / "systemd/user/llm-router-update.timer"
    update = update_path.read_text()
    timer = timer_path.read_text()
    assert "Type=oneshot\n" in update
    assert "TimeoutStartSec=45min\n" in update
    assert f"WorkingDirectory={install_dir}\n" in update
    assert (
        f'ExecStart=:"{install_dir / "venv/bin/python"}" -m llm_router.updater '
        f'--install-dir "{install_dir}"\n'
    ) in update
    assert "OnCalendar=daily\n" in timer
    assert "RandomizedDelaySec=1h\n" in timer
    assert "Persistent=true\n" in timer
    assert "WantedBy=timers.target\n" in timer
    assert "EnvironmentFile=" not in update
    assert "router.env" not in update
    assert secret not in update + timer
    assert "API_KEY" not in update + timer
    active_update = "\n".join(line for line in update.splitlines() if not line.startswith("#"))
    assert "sudo" not in active_update
    assert "curl" not in active_update
    assert "bash" not in active_update
    assert env_path.read_bytes() == original_env


def test_write_update_units_is_idempotent_and_preserves_repeated_backups(
    install_paths: tuple[Path, Path],
) -> None:
    install_dir, config_home = install_paths
    gateway_path, env_path = service.write_user_service(install_dir, config_home)
    gateway_before = gateway_path.read_bytes()
    env_before = env_path.read_bytes()
    update_path, timer_path = service.write_update_units(install_dir, config_home)
    expected = {path: path.read_bytes() for path in (update_path, timer_path)}
    unit_files_before = set(update_path.parent.iterdir())

    assert service.write_update_units(install_dir, config_home) == (update_path, timer_path)

    assert {path: path.read_bytes() for path in expected} == expected
    assert set(update_path.parent.iterdir()) == unit_files_before
    for generation in (1, 2):
        old_units = {
            update_path: f"# User-edited updater, version {generation}\n".encode(),
            timer_path: f"# User-edited timer, version {generation}\n".encode(),
        }
        backups_before = {
            path: path.read_bytes()
            for path in update_path.parent.iterdir()
            if path not in unit_files_before and path.is_file()
        }
        for path, content in old_units.items():
            path.write_bytes(content)

        service.write_update_units(install_dir, config_home)

        assert {path: path.read_bytes() for path in expected} == expected
        backups = [path for path in update_path.parent.iterdir() if path not in unit_files_before]
        for content in old_units.values():
            assert any(path.is_file() and path.read_bytes() == content for path in backups)
        assert all(path.read_bytes() == content for path, content in backups_before.items())
    assert gateway_path.read_bytes() == gateway_before
    assert env_path.read_bytes() == env_before


def test_update_unit_paths_are_absolute_with_literal_dollars_and_escaped_specifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    install_dir = Path("install space %n $HOME")
    config_home = Path("config space %u $USER")
    python = install_dir / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)

    update_path, timer_path = service.write_update_units(install_dir, config_home)

    assert update_path.is_absolute()
    assert timer_path.is_absolute()
    unit = update_path.read_text()
    absolute_install = str(install_dir.resolve()).replace("%", "%%")
    assert f"WorkingDirectory={absolute_install}\n" in unit
    assert (
        f'ExecStart=:"{absolute_install}/venv/bin/python" -m llm_router.updater '
        f'--install-dir "{absolute_install}"\n'
    ) in unit
    assert "$$HOME" not in unit
    assert not (config_home / "llm-router/router.env").exists()


@pytest.mark.parametrize("character", ["'", '"', "\\", "*", "?", "[", "]", "\n", "\x7f"])
@pytest.mark.parametrize("invalid_path", ["installation", "configuration"])
def test_update_units_reject_unsafe_paths_before_writing(
    tmp_path: Path, character: str, invalid_path: str,
) -> None:
    install_dir = tmp_path / (f"install{character}path" if invalid_path == "installation" else "installation")
    config_home = tmp_path / (f"config{character}path" if invalid_path == "configuration" else "configuration")
    python = install_dir / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)

    with pytest.raises(ValueError):
        service.write_update_units(install_dir, config_home)

    assert not config_home.exists()


def test_update_units_require_existing_python_before_writing(tmp_path: Path) -> None:
    config_home = tmp_path / "configuration"

    with pytest.raises((RuntimeError, FileNotFoundError), match="[Pp]ython|venv"):
        service.write_update_units(tmp_path / "missing-install", config_home)

    assert not config_home.exists()


def test_auto_update_validates_source_then_enables_timer_after_gateway_is_active(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_router import updater

    install_dir, config_home = install_paths
    events: list[object] = []

    def validate(installation: Path) -> str:
        assert installation == install_dir
        events.append("validate-source")
        return "a" * 40

    def write_gateway(installation: Path, configuration: Path) -> tuple[Path, Path]:
        assert (installation, configuration) == install_paths
        events.append("write-gateway")
        return config_home / "gateway-unit", config_home / "router.env"

    def write_updates(installation: Path, configuration: Path) -> tuple[Path, Path]:
        assert (installation, configuration) == install_paths
        events.append("write-updates")
        return config_home / "update-unit", config_home / "update-timer"

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert check is True
        events.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(updater, "installed_revision", validate)
    monkeypatch.setattr(service, "preflight_user_service", lambda: events.append("preflight"))
    monkeypatch.setattr(service, "write_user_service", write_gateway)
    monkeypatch.setattr(service, "write_update_units", write_updates)
    monkeypatch.setattr(service.subprocess, "run", run)

    service.install_user_service(install_dir, config_home, auto_update=True)

    assert events == [
        "validate-source", "preflight", "write-gateway", "write-updates",
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "llm-router.service"],
        ["systemctl", "--user", "restart", "llm-router.service"],
        ["systemctl", "--user", "is-active", "--quiet", "llm-router.service"],
        ["systemctl", "--user", "enable", "--now", "llm-router-update.timer"],
        ["systemctl", "--user", "is-active", "--quiet", "llm-router-update.timer"],
    ]


@pytest.mark.parametrize("explicit_false", [False, True])
def test_auto_update_opt_out_does_not_validate_source_or_touch_existing_timer(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    update_source: list[Path], explicit_false: bool,
) -> None:
    install_dir, config_home = install_paths
    update_path = config_home / "systemd/user/llm-router-update.service"
    timer_path = update_path.with_suffix(".timer")
    update_path.parent.mkdir(parents=True)
    update_path.write_bytes(b"# Preserve an existing custom update service\n")
    timer_path.write_bytes(b"# Preserve the existing timer and its enabled state\n")
    before = {path: path.read_bytes() for path in (update_path, timer_path)}
    commands: list[list[str]] = []
    monkeypatch.setattr(service, "preflight_user_service", lambda: None)

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert check is True
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", run)
    if explicit_false:
        service.install_user_service(install_dir, config_home, auto_update=False)
    else:
        service.install_user_service(install_dir, config_home)

    assert update_source == []
    assert {path: path.read_bytes() for path in before} == before
    assert not any("llm-router-update.timer" in command for command in commands)
    assert not any("llm-router-update.service" in command for command in commands)


def test_auto_update_invalid_source_fails_before_preflight_or_gateway_changes(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_router import updater

    install_dir, config_home = install_paths
    gateway_path, env_path = service.write_user_service(install_dir, config_home)
    before = {path: path.read_bytes() for path in (gateway_path, env_path)}
    existing_paths = set(config_home.rglob("*"))

    def invalid_source(installation: Path) -> str:
        assert installation == install_dir
        raise RuntimeError("Automatic updates require a verified installed Git revision")

    def unexpected_preflight() -> None:
        raise AssertionError("Invalid source must fail before changing linger or services")

    monkeypatch.setattr(updater, "installed_revision", invalid_source)
    monkeypatch.setattr(service, "preflight_user_service", unexpected_preflight)

    with pytest.raises(RuntimeError, match="verified installed Git revision"):
        service.install_user_service(install_dir, config_home, auto_update=True)

    assert {path: path.read_bytes() for path in before} == before
    assert set(config_home.rglob("*")) == existing_paths


@pytest.mark.parametrize("failed_action", ["enable", "is-active"])
def test_auto_update_timer_failure_is_reported_by_cli(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    update_source: list[Path], capsys: pytest.CaptureFixture[str], failed_action: str,
) -> None:
    install_dir, config_home = install_paths
    commands: list[list[str]] = []
    monkeypatch.setattr(service, "preflight_user_service", lambda: None)

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert check is True
        commands.append(args)
        if "llm-router-update.timer" in args and failed_action in args:
            raise subprocess.CalledProcessError(1, args, stderr="Update timer failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.main([
        "--install-dir", str(install_dir), "--config-home", str(config_home), "--auto-update",
    ])

    output = capsys.readouterr()
    assert result == 2
    assert "Service installation failed" in output.err
    assert "llm-router-update.timer" in output.err
    assert commands[-1][2] == failed_action
    assert commands[-1][-1] == "llm-router-update.timer"
    assert update_source == [install_dir]


def test_service_cli_passes_auto_update_only_when_requested(
    install_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir, config_home = install_paths
    calls: list[tuple[Path, Path, dict[str, bool]]] = []

    def install(installation: Path, configuration: Path, **kwargs: bool) -> None:
        calls.append((installation, configuration, kwargs))

    monkeypatch.setattr(service, "install_user_service", install)

    assert service.main([
        "--install-dir", str(install_dir), "--config-home", str(config_home), "--auto-update",
    ]) == 0
    assert service.main([
        "--install-dir", str(install_dir), "--config-home", str(config_home),
    ]) == 0

    assert calls == [(install_dir, config_home, {"auto_update": True}), (install_dir, config_home, {})]
