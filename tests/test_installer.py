"""Exercise the actual shell entry point without downloads or host service writes."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


@pytest.fixture
def installer_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "test-python"
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, shutil, sys\n"
        "with open(os.environ['ROUTER_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(['python', *sys.argv[1:]]) + '\\n')\n"
        "if sys.argv[1:3] == ['-m', 'venv']:\n"
        "    target = pathlib.Path(sys.argv[3]) / 'bin'\n"
        "    target.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copy2(__file__, target / 'python')\n"
        "    for name in ('llm-router', 'llm-router-gateway', 'llm-router-mcp'):\n"
        "        command = target / name\n"
        "        command.write_text('#!/bin/sh\\nexit 0\\n')\n"
        "        command.chmod(0o755)\n"
    )
    fake_python.chmod(0o755)
    for name in ("systemctl", "loginctl", "git", "id"):
        command = bin_dir / name
        command.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "name = pathlib.Path(sys.argv[0]).name\n"
            "with open(os.environ['ROUTER_TEST_LOG'], 'a') as log:\n"
            "    log.write(json.dumps([name, *sys.argv[1:]]) + '\\n')\n"
            "if name == 'id':\n"
            "    print(os.environ.get('ROUTER_TEST_UID', '1000'))\n"
            "if name == 'systemctl':\n"
            "    sys.exit(int(os.environ.get('ROUTER_TEST_SYSTEMCTL_EXIT', '0')))\n"
        )
        command.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith('LLM_ROUTER_')}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        LLM_ROUTER_PYTHON=str(fake_python),
        LLM_ROUTER_INSTALL_DIR=str(tmp_path / "installed router"),
        LLM_ROUTER_BIN_DIR=str(tmp_path / "router commands"),
        XDG_CONFIG_HOME=str(tmp_path / "service config"),
        ROUTER_TEST_LOG=str(tmp_path / "calls.jsonl"),
    )
    return env


def run_installer(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=15,
    )


def calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["ROUTER_TEST_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_package_install_defaults_to_main_without_service_changes(installer_environment):
    result = run_installer(installer_environment)
    assert result.returncode == 0, result.stderr
    recorded = calls(installer_environment)
    assert any(call[-1] == "git+https://github.com/bouldinnathan/source-agnostic-llm-router.git@main"
               for call in recorded)
    assert not any(call[0] in {"systemctl", "loginctl"} for call in recorded)
    assert not any("llm_router.service" in call for call in recorded)
    assert (Path(installer_environment["LLM_ROUTER_BIN_DIR"]) / "llm-router").is_symlink()
    assert "serve --host 127.0.0.1 --port 8088" in result.stdout


def test_service_flag_delegates_to_installed_helper_with_exact_paths(installer_environment):
    result = run_installer(installer_environment, "--service")
    assert result.returncode == 0, result.stderr
    recorded = calls(installer_environment)
    assert ["systemctl", "--user", "show-environment"] in recorded
    assert [
        "python", "-m", "llm_router.service",
        "--install-dir", installer_environment["LLM_ROUTER_INSTALL_DIR"],
        "--config-home", installer_environment["XDG_CONFIG_HOME"],
    ] in recorded


def test_auto_update_flag_is_forwarded_only_when_requested(installer_environment):
    result = run_installer(installer_environment, "--service", "--auto-update")
    assert result.returncode == 0, result.stderr
    service_calls = [call for call in calls(installer_environment) if "llm_router.service" in call]
    assert len(service_calls) == 1
    assert service_calls[0][-1] == "--auto-update"


@pytest.mark.parametrize("listen_flag", ["--lan", "--localhost"])
@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("listen_first", [False, True])
def test_listen_choice_is_forwarded_to_service_helper(
    installer_environment, listen_flag, auto_update, listen_first,
):
    arguments = [listen_flag, "--service"] if listen_first else ["--service", listen_flag]
    if auto_update:
        arguments.append("--auto-update")
    result = run_installer(installer_environment, *arguments)
    assert result.returncode == 0, result.stderr
    service_calls = [call for call in calls(installer_environment) if "llm_router.service" in call]
    assert len(service_calls) == 1
    service_call = service_calls[0]
    assert service_call[:7] == [
        "python", "-m", "llm_router.service",
        "--install-dir", installer_environment["LLM_ROUTER_INSTALL_DIR"],
        "--config-home", installer_environment["XDG_CONFIG_HOME"],
    ]
    expected_flags = [listen_flag, "--auto-update"] if auto_update else [listen_flag]
    assert sorted(service_call[7:]) == sorted(expected_flags)


@pytest.mark.parametrize("listen_flag", ["--lan", "--localhost"])
def test_listen_choice_requires_service_before_any_installation(installer_environment, listen_flag):
    result = run_installer(installer_environment, listen_flag)
    assert result.returncode == 2
    assert "requires --service" in result.stderr
    assert not calls(installer_environment)
    assert not Path(installer_environment["LLM_ROUTER_INSTALL_DIR"]).exists()


@pytest.mark.parametrize("listen_flags", [("--lan", "--localhost"), ("--localhost", "--lan")])
@pytest.mark.parametrize("service_flags", [(), ("--service",), ("--service", "--auto-update")])
def test_conflicting_listen_choices_fail_before_any_installation(
    installer_environment, listen_flags, service_flags,
):
    result = run_installer(installer_environment, *service_flags, *listen_flags)
    assert result.returncode == 2
    assert "--lan" in result.stderr
    assert "--localhost" in result.stderr
    assert not calls(installer_environment)
    assert not Path(installer_environment["LLM_ROUTER_INSTALL_DIR"]).exists()


def test_help_explains_service_listen_default_and_explicit_choices(installer_environment):
    result = run_installer(installer_environment, "--help")
    assert result.returncode == 0, result.stderr
    assert "--lan" in result.stdout
    assert "--localhost" in result.stdout
    assert "0.0.0.0" in result.stdout
    assert "127.0.0.1" in result.stdout
    assert "default" in result.stdout.lower()
    assert not calls(installer_environment)


def test_auto_update_requires_service_before_any_installation(installer_environment):
    result = run_installer(installer_environment, "--auto-update")
    assert result.returncode == 2
    assert "requires --service" in result.stderr
    assert not calls(installer_environment)


@pytest.mark.parametrize("override", [
    {"LLM_ROUTER_VERSION": "v0.3.0"},
    {"LLM_ROUTER_VERSION": "a" * 40},
    {"LLM_ROUTER_SOURCE": "/local/source"},
    {"LLM_ROUTER_REPO_URL": "https://example.invalid/fork.git"},
])
def test_auto_update_refuses_local_custom_and_pinned_sources(installer_environment, override):
    installer_environment.update(override)
    result = run_installer(installer_environment, "--service", "--auto-update")
    assert result.returncode == 2
    assert "official repository main branch" in result.stderr
    assert not calls(installer_environment)
    assert not Path(installer_environment["LLM_ROUTER_INSTALL_DIR"]).exists()


def test_installer_respects_updater_lock_without_touching_active_runtime(installer_environment):
    installation = Path(installer_environment["LLM_ROUTER_INSTALL_DIR"])
    installation.mkdir()
    sentinel = installation / "venv" / ".llm-router-update.json"
    sentinel.parent.mkdir()
    sentinel.write_text("preserve until lock is acquired")
    with (installation / ".update.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_installer(installer_environment)
    assert result.returncode == 2
    assert "Another router installation/update" in result.stderr
    assert not any(call[0] == "python" for call in calls(installer_environment))
    assert sentinel.read_text() == "preserve until lock is acquired"


def test_manual_reinstall_clears_prior_auto_update_provenance(installer_environment):
    # A deliberate SHA pin must not be considered auto-managed just because the
    # previous automatic update installed that exact same commit.
    installer_environment["LLM_ROUTER_VERSION"] = "a" * 40
    installation = Path(installer_environment["LLM_ROUTER_INSTALL_DIR"])
    provenance = installation / "venv" / ".llm-router-update.json"
    provenance.parent.mkdir(parents=True)
    provenance.write_text('{"commit": "previous auto-update"}')
    result = run_installer(installer_environment)
    assert result.returncode == 0, result.stderr
    assert not provenance.exists()


def test_rerun_reuses_existing_runtime_without_replacing_its_python(installer_environment):
    first = run_installer(installer_environment)
    assert first.returncode == 0, first.stderr
    second = run_installer(installer_environment)
    assert second.returncode == 0, second.stderr
    recorded = calls(installer_environment)
    assert sum(call[1:3] == ["-m", "venv"] for call in recorded) == 1
    assert ["python", "-m", "pip", "--version"] in recorded


def test_relative_install_directories_produce_working_absolute_links(
    installer_environment, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    installer_environment.update(
        LLM_ROUTER_INSTALL_DIR="relative installation", LLM_ROUTER_BIN_DIR="relative commands",
    )
    result = run_installer(installer_environment)
    assert result.returncode == 0, result.stderr
    command = tmp_path / "relative commands" / "llm-router"
    assert command.is_file()
    assert Path(os.readlink(command)).is_absolute()


@pytest.mark.parametrize("argument, exit_code", [("--help", 0), ("--unknown", 2)])
def test_help_and_invalid_options_do_not_install(installer_environment, argument, exit_code):
    result = run_installer(installer_environment, argument)
    assert result.returncode == exit_code
    assert not calls(installer_environment)
    assert not Path(installer_environment["LLM_ROUTER_INSTALL_DIR"]).exists()


@pytest.mark.parametrize(
    "failure_env, message",
    [({"ROUTER_TEST_UID": "0"}, "normal user"),
     ({"ROUTER_TEST_SYSTEMCTL_EXIT": "1"}, "systemd user session")],
)
@pytest.mark.parametrize("listen_flags", [(), ("--lan",), ("--localhost",)])
def test_service_preflight_fails_before_package_mutation(
    installer_environment, failure_env, message, listen_flags,
):
    installer_environment.update(failure_env)
    result = run_installer(installer_environment, "--service", *listen_flags)
    assert result.returncode == 2
    assert message in result.stderr
    assert not any(call[0] == "python" for call in calls(installer_environment))
    assert not Path(installer_environment["LLM_ROUTER_INSTALL_DIR"]).exists()


@pytest.mark.parametrize(
    "override, expected",
    [({"LLM_ROUTER_VERSION": "abc123"},
      "git+https://github.com/bouldinnathan/source-agnostic-llm-router.git@abc123"),
     ({"LLM_ROUTER_SOURCE": "/local/package with spaces"}, "/local/package with spaces")],
)
def test_explicit_revision_and_local_source_still_work(installer_environment, override, expected):
    installer_environment.update(override)
    result = run_installer(installer_environment)
    assert result.returncode == 0, result.stderr
    assert any(call[-1] == expected for call in calls(installer_environment))
