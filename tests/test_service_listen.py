from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from llm_router import service


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Listener tests must never invoke host service commands")

    monkeypatch.setattr(service.subprocess, "run", forbidden)


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    install_dir = tmp_path / "installation"
    python = install_dir / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)
    config_home = tmp_path / "configuration"
    env_path = config_home / "llm-router/router.env"
    return install_dir, config_home, env_path


def existing(env_path: Path, contents: bytes) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_bytes(contents)


def settings(env_path: Path) -> dict[str, str]:
    return {name: assignment.value for name, assignment in service._parse_environment(env_path.read_bytes())[1].items()}


@pytest.mark.parametrize("mode,host", [(None, "0.0.0.0"), ("lan", "0.0.0.0"), ("localhost", "127.0.0.1")])
def test_new_service_mode_has_private_generated_key(paths: tuple[Path, Path, Path], mode: str | None, host: str) -> None:
    install_dir, config_home, env_path = paths

    service.write_user_service(install_dir, config_home, listen=mode)

    values = settings(env_path)
    assert values["LLM_ROUTER_HOST"] == host
    assert len(values["LLM_ROUTER_GATEWAY_API_KEY"]) >= 32
    assert values["LLM_ROUTER_PORT"] == "8088"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.parent.stat().st_mode) == 0o700
    assert list(env_path.parent.glob("router.env.backup-*")) == []


@pytest.mark.parametrize("mode,host", [("lan", "0.0.0.0"), ("localhost", "127.0.0.1")])
def test_explicit_mode_preserves_key_port_backend_comments_crlf_and_private_backup(
    paths: tuple[Path, Path, Path], mode: str, host: str,
) -> None:
    install_dir, config_home, env_path = paths
    original = (
        b"# User settings, keep every other byte\r\n"
        b" LLM_ROUTER_HOST = '10.0.0.8'  \r\n"
        b'LLM_ROUTER_GATEWAY_API_KEY="custom key # $ value"\r\n'
        b"LLM_ROUTER_PORT=9214\r\n"
        b'LLM_ROUTER_DISCOVERY_URLS="openai@laptop=http://laptop:1234/v1"\r\n'
        b"LLM_ROUTER_AUTO_PROVISION=0\r\n; keep this comment\r\n"
    )
    existing(env_path, original)
    old_inode = env_path.stat().st_ino

    service.write_user_service(install_dir, config_home, listen=mode)

    assert env_path.read_bytes() == original.replace(b"'10.0.0.8'", f"'{host}'".encode())
    assert env_path.stat().st_ino != old_inode
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    backups = list(env_path.parent.glob("router.env.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    paths_before = set(config_home.rglob("*"))
    bytes_before = env_path.read_bytes()

    service.write_user_service(install_dir, config_home, listen=mode)

    assert env_path.read_bytes() == bytes_before
    assert set(config_home.rglob("*")) == paths_before


@pytest.mark.parametrize("key_line", [b"", b"LLM_ROUTER_GATEWAY_API_KEY=\n", b'LLM_ROUTER_GATEWAY_API_KEY=""\n', b"LLM_ROUTER_GATEWAY_API_KEY=''\n", b'LLM_ROUTER_GATEWAY_API_KEY="   "\n'])
def test_lan_generates_missing_empty_or_whitespace_only_key(paths: tuple[Path, Path, Path], key_line: bytes) -> None:
    install_dir, config_home, env_path = paths
    original = b"LLM_ROUTER_HOST=127.0.0.1\nLLM_ROUTER_PORT=9345\n" + key_line
    existing(env_path, original)

    service.write_user_service(install_dir, config_home, listen="lan")

    values = settings(env_path)
    assert values["LLM_ROUTER_HOST"] == "0.0.0.0"
    assert values["LLM_ROUTER_PORT"] == "9345"
    assert len(values["LLM_ROUTER_GATEWAY_API_KEY"]) >= 32
    generated = env_path.read_bytes()
    service.write_user_service(install_dir, config_home, listen="lan")
    assert env_path.read_bytes() == generated


@pytest.mark.parametrize("key_line", [b"LLM_ROUTER_GATEWAY_API_KEY=keep-this-key\n", b'LLM_ROUTER_GATEWAY_API_KEY="keep-this-key"\n', b"LLM_ROUTER_GATEWAY_API_KEY='keep-this-key'\n", b"LLM_ROUTER_GATEWAY_API_KEY=#not-a-shell-comment\n"])
def test_lan_preserves_nonempty_keys_exactly(paths: tuple[Path, Path, Path], key_line: bytes) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, b"LLM_ROUTER_HOST=127.0.0.1\n" + key_line)

    service.write_user_service(install_dir, config_home, listen="lan")

    assert env_path.read_bytes() == b"LLM_ROUTER_HOST=0.0.0.0\n" + key_line


def test_localhost_does_not_create_missing_authentication(paths: tuple[Path, Path, Path]) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, b"# Keep deliberate local-only unauthenticated use\r\nLLM_ROUTER_HOST=0.0.0.0")

    service.write_user_service(install_dir, config_home, listen="localhost")

    assert env_path.read_bytes() == b"# Keep deliberate local-only unauthenticated use\r\nLLM_ROUTER_HOST=127.0.0.1"


def test_missing_host_is_appended_with_existing_line_endings(paths: tuple[Path, Path, Path]) -> None:
    install_dir, config_home, env_path = paths
    original = b"# Preserve CRLF\r\nLLM_ROUTER_GATEWAY_API_KEY='present'"
    existing(env_path, original)

    service.write_user_service(install_dir, config_home, listen="lan")

    assert env_path.read_bytes() == original + b"\r\nLLM_ROUTER_HOST=0.0.0.0\r\n"


@pytest.mark.parametrize("original", [
    b"", b"LLM_ROUTER_HOST=127.0.0.1\r\nLLM_ROUTER_GATEWAY_API_KEY=\r\n",
    b'CUSTOM_BACKEND="complex\nmultiline"\nLLM_ROUTER_HOST=127.0.0.1\n',
])
def test_no_flag_keeps_existing_contents_exactly_even_if_complex(paths: tuple[Path, Path, Path], original: bytes) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, original)

    service.write_user_service(install_dir, config_home)

    assert env_path.read_bytes() == original
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert list(env_path.parent.glob("router.env.backup-*")) == []


@pytest.mark.parametrize("ambiguous", [
    b"LLM_ROUTER_HOST=127.0.0.1\nLLM_ROUTER_HOST=10.0.0.1\n",
    b"LLM_ROUTER_HOST=127.0.0.1\nLLM_ROUTER_HOST=127.0.0.1\n",
    b"LLM_ROUTER_GATEWAY_API_KEY=secret\nLLM_ROUTER_GATEWAY_API_KEY=\n",
    b"LLM_ROUTER_PORT=8088\nLLM_ROUTER_PORT=8089\n",
    b'LLM_ROUTER_GATEWAY_API_KEY="secret\nmore"\n',
    b"LLM_ROUTER_GATEWAY_API_KEY=secret\\\nmore\n",
    b"LLM_ROUTER_GATEWAY_API_KEY='secret' 'more'\n",
    b"export LLM_ROUTER_GATEWAY_API_KEY=secret\n",
    b'BACKEND="unclosed\nLLM_ROUTER_GATEWAY_API_KEY=secret\n',
    b"LLM_ROUTER_GATEWAY_API_KEY=secret\x00\n",
    b"LLM_ROUTER_GATEWAY_API_KEY=\xff\n",
    b"# continued \\\nLLM_ROUTER_GATEWAY_API_KEY=secret\nLLM_ROUTER_HOST=127.0.0.1\n",
    b"; continued \\\nLLM_ROUTER_GATEWAY_API_KEY=secret\nLLM_ROUTER_HOST=127.0.0.1\n",
])
@pytest.mark.parametrize("mode", ["lan", "localhost"])
def test_ambiguous_syntax_is_refused_without_rewriting_or_leaking_values(
    paths: tuple[Path, Path, Path], ambiguous: bytes, mode: str,
) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, ambiguous)

    with pytest.raises(RuntimeError, match="Edit router.env manually") as caught:
        service.write_user_service(install_dir, config_home, listen=mode)

    assert "secret" not in str(caught.value)
    assert env_path.read_bytes() == ambiguous
    assert list(env_path.parent.glob("router.env.backup-*")) == []
    assert not (config_home / "systemd/user/llm-router.service").exists()


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_separators_cannot_invent_nonexistent_authentication(
    paths: tuple[Path, Path, Path], separator: str,
) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, f"BACKEND=value{separator}LLM_ROUTER_GATEWAY_API_KEY=not-really-an-assignment\n".encode())

    service.write_user_service(install_dir, config_home, listen="lan")

    assert len(settings(env_path)["LLM_ROUTER_GATEWAY_API_KEY"]) >= 32
    assert settings(env_path)["LLM_ROUTER_GATEWAY_API_KEY"] != "not-really-an-assignment"


@pytest.mark.parametrize("mode", [None, "lan", "localhost"])
@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "directory", "fifo", "hardlink"])
def test_unsafe_environment_file_is_not_followed_or_replaced(
    paths: tuple[Path, Path, Path], mode: str | None, kind: str,
) -> None:
    install_dir, config_home, env_path = paths
    env_path.parent.mkdir(parents=True)
    target = config_home / "do-not-change"
    target.write_bytes(b"sensitive-original\n")
    if kind == "symlink":
        env_path.symlink_to(target)
    elif kind == "dangling-symlink":
        env_path.symlink_to(config_home / "missing")
    elif kind == "directory":
        env_path.mkdir()
    elif kind == "fifo":
        os.mkfifo(env_path)
    else:
        os.link(target, env_path)
    original_info = env_path.lstat()

    with pytest.raises((RuntimeError, OSError)):
        service.write_user_service(install_dir, config_home, listen=mode)

    assert env_path.lstat().st_ino == original_info.st_ino
    assert target.read_bytes() == b"sensitive-original\n"
    assert list(env_path.parent.glob("router.env.backup-*")) == []


def test_symlinked_configuration_directory_is_refused(paths: tuple[Path, Path, Path]) -> None:
    install_dir, config_home, env_path = paths
    config_home.mkdir()
    elsewhere = config_home / "elsewhere"
    elsewhere.mkdir()
    env_path.parent.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RuntimeError, match="real directory"):
        service.write_user_service(install_dir, config_home, listen="lan")

    assert list(elsewhere.iterdir()) == []


def test_wrong_owner_is_refused(paths: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    install_dir, config_home, env_path = paths
    existing(env_path, b"LLM_ROUTER_HOST=127.0.0.1\n")
    monkeypatch.setattr(service.os, "geteuid", lambda: env_path.stat().st_uid + 1)

    with pytest.raises(RuntimeError, match="owned by this user"):
        service.write_user_service(install_dir, config_home, listen="lan")


def test_invalid_mode_does_not_write_files(paths: tuple[Path, Path, Path]) -> None:
    install_dir, config_home, env_path = paths

    with pytest.raises(ValueError, match="Listen mode"):
        service.write_user_service(install_dir, config_home, listen="world")

    assert not config_home.exists()


@pytest.mark.parametrize("mode", ["lan", "localhost"])
@pytest.mark.parametrize("automatic", [False, True])
def test_cli_forwards_explicit_mode_and_auto_update_only_when_selected(
    paths: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, mode: str, automatic: bool,
) -> None:
    install_dir, config_home, _ = paths
    calls: list[tuple[Path, Path, dict[str, object]]] = []

    def install(installation: Path, configuration: Path, **kwargs: object) -> None:
        calls.append((installation, configuration, kwargs))

    monkeypatch.setattr(service, "install_user_service", install)
    arguments = ["--install-dir", str(install_dir), "--config-home", str(config_home), f"--{mode}"]
    if automatic:
        arguments.append("--auto-update")

    assert service.main(arguments) == 0
    expected: dict[str, object] = {"listen": mode}
    if automatic:
        expected["auto_update"] = True
    assert calls == [(install_dir, config_home, expected)]


def test_cli_rejects_conflicting_modes_before_install(paths: tuple[Path, Path, Path]) -> None:
    install_dir, config_home, _ = paths

    with pytest.raises(SystemExit) as caught:
        service.main(["--install-dir", str(install_dir), "--config-home", str(config_home), "--lan", "--localhost"])

    assert caught.value.code == 2
    assert not config_home.exists()


@pytest.mark.parametrize("mode,address", [("lan", "http://ROUTER_IP:8088"), ("localhost", "http://127.0.0.1:8088")])
def test_install_forwards_mode_reports_correct_urls_without_secrets(
    paths: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], mode: str, address: str,
) -> None:
    install_dir, config_home, env_path = paths
    monkeypatch.setattr(service, "preflight_user_service", lambda: None)
    calls: list[list[str]] = []

    def run(args: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert check is True
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service.subprocess, "run", run)

    service.install_user_service(install_dir, config_home, listen=mode)

    output = capsys.readouterr().out
    assert f"{address}/status" in output
    assert f"{address}/v1" in output
    assert settings(env_path)["LLM_ROUTER_GATEWAY_API_KEY"] not in output
    assert "http://0.0.0.0" not in output
    assert ["systemctl", "--user", "restart", "llm-router.service"] in calls
    if mode == "lan":
        assert "all IPv4 interfaces" in output
        assert "No firewall rules were changed" in output
        assert "TLS or a VPN" in output
    else:
        assert "localhost only" in output


def test_existing_custom_port_is_used_for_display(paths: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _, _, env_path = paths
    existing(env_path, b"LLM_ROUTER_HOST=0.0.0.0\nLLM_ROUTER_PORT='9321'\nLLM_ROUTER_GATEWAY_API_KEY=private\n")

    service._print_listener(env_path)

    output = capsys.readouterr().out
    assert "http://ROUTER_IP:9321/status" in output
    assert "8088" not in output
    assert "private" not in output


def test_unknown_preserved_settings_do_not_claim_lan_or_local_default(
    paths: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, env_path = paths
    existing(env_path, b'CUSTOM="complex\nvalue"\nLLM_ROUTER_GATEWAY_API_KEY=private\n')

    service._print_listener(env_path)

    output = capsys.readouterr().out
    assert "Listener settings preserved; check" in output
    assert "127.0.0.1" not in output
    assert "0.0.0.0" not in output
    assert "private" not in output
