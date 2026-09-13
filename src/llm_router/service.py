"""Opt-in systemd user service installation; never run during package import."""

from __future__ import annotations

import argparse
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

SERVICE_NAME = "llm-router.service"


def _unit_path(value: Path) -> str:
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 or char in "\\\"'*?[]" for char in text):
        raise ValueError("Service paths cannot contain control characters, quotes, backslashes, or glob characters")
    # WorkingDirectory/EnvironmentFile parse raw paths, not shell-quoted words.
    # The latter also expands globs. Only ExecStart uses quoted word parsing.
    text.encode("utf-8")
    return text.replace("%", "%%")


def write_user_service(install_dir: Path, config_home: Path) -> tuple[Path, Path]:
    """Write managed service files, preserving credentials and local configuration."""

    install_dir = install_dir.expanduser().resolve()
    config_home = config_home.expanduser().resolve()
    python = install_dir / "venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"Installed virtual environment not found: {python}")
    config_dir = config_home / "llm-router"
    env_path = config_dir / "router.env"
    unit_dir = config_home / "systemd" / "user"
    unit_path = unit_dir / SERVICE_NAME
    unit = (
        "# Managed by source-agnostic-llm-router install.sh --service.\n"
        "# Use systemctl --user edit llm-router.service for persistent unit overrides.\n"
        "[Unit]\n"
        "Description=Source-Agnostic LLM Router\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=exec\n"
        f"WorkingDirectory={_unit_path(config_dir)}\n"
        f"EnvironmentFile={_unit_path(env_path)}\n"
        # ':' disables environment expansion; '$' in executable paths is literal.
        f'ExecStart=:"{_unit_path(python)}" -m llm_router.gateway\n'
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStopSec=30\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not env_path.exists():
        # Exclusive creation also prevents replacing a dangling symlink or a file
        # another installer created concurrently. Do not rewrite an existing key.
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as env_file:
            env_file.write(
                "# systemd EnvironmentFile syntax: KEY=value, no shell export commands.\n"
                "# Edit this file, then: systemctl --user restart llm-router.service\n"
                "LLM_ROUTER_HOST=127.0.0.1\n"
                "LLM_ROUTER_PORT=8088\n"
                f"LLM_ROUTER_GATEWAY_API_KEY={secrets.token_urlsafe(32)}\n"
                "# Proxy existing models only; set 1 to opt into model downloads.\n"
                "LLM_ROUTER_AUTO_PROVISION=0\n"
                "LLM_ROUTER_DISCOVERY_REFRESH=30\n"
                "# Replace these examples with your resolvable LAN/VPN hostnames:\n"
                '# LLM_ROUTER_DISCOVERY_URLS="ollama@golemframe=http://golemframe.home.arpa:11434,'
                'openai@pantheon=http://pantheon.example-vpn:1234/v1"\n'
                "# Optional provider keys or LLM_ROUTER_CONFIG=/absolute/path/router.toml go here.\n"
            )
    unit_dir.mkdir(parents=True, exist_ok=True)
    if unit_path.exists() and unit_path.read_text(encoding="utf-8") == unit:
        return unit_path, env_path
    if unit_path.exists():
        backup_fd, backup_name = tempfile.mkstemp(prefix=f"{SERVICE_NAME}.backup-", dir=unit_dir)
        os.close(backup_fd)
        shutil.copy2(unit_path, backup_name)
        print(f"Previous service definition backed up to {backup_name}")
    fd, staging_name = tempfile.mkstemp(prefix=f".{SERVICE_NAME}.", dir=unit_dir)
    staging_path = Path(staging_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as unit_file:
            unit_file.write(unit)
        staging_path.chmod(0o644)
        staging_path.replace(unit_path)
    finally:
        staging_path.unlink(missing_ok=True)
    return unit_path, env_path


def preflight_user_service() -> None:
    """Require a working user manager and verified boot/logout persistence."""

    if os.geteuid() == 0:
        raise RuntimeError("Run --service as your normal user, without sudo")
    for command in ("systemctl", "loginctl"):
        if shutil.which(command) is None:
            raise RuntimeError("Service installation requires Linux with systemd and loginctl")
    try:
        subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "No systemd user session; run the installer from a normal local or SSH login"
        ) from exc
    username = pwd.getpwuid(os.geteuid()).pw_name
    linger_query = ["loginctl", "show-user", username, "--property=Linger", "--value"]
    try:
        linger = subprocess.run(linger_query, check=True, capture_output=True, text=True)
        if linger.stdout.strip() != "yes":
            print("Enabling lingering so the service starts at boot and survives logout.")
            result = subprocess.run(["loginctl", "enable-linger", username], check=False)
            if result.returncode:
                if shutil.which("sudo") is None:
                    raise RuntimeError(
                        f"An administrator must run: loginctl enable-linger {username}"
                    )
                subprocess.run(["sudo", "loginctl", "enable-linger", username], check=True)
            linger = subprocess.run(linger_query, check=True, capture_output=True, text=True)
        if linger.stdout.strip() != "yes":
            raise RuntimeError(f"Lingering is not enabled; run: sudo loginctl enable-linger {username}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Could not enable boot persistence; run: sudo loginctl enable-linger {username}"
        ) from exc


def install_user_service(install_dir: Path, config_home: Path) -> None:
    preflight_user_service()
    unit_path, env_path = write_user_service(install_dir, config_home)
    for arguments in (
        ["daemon-reload"], ["enable", SERVICE_NAME], ["restart", SERVICE_NAME],
        ["is-active", "--quiet", SERVICE_NAME],
    ):
        subprocess.run(["systemctl", "--user", *arguments], check=True)
    print(f"Service enabled and running: {SERVICE_NAME}")
    print(f"Service definition: {unit_path}")
    print(f"Settings and client API key (preserved on updates): {env_path}")
    print("New installs listen on http://127.0.0.1:8088 (OpenAI base URL: /v1).")
    print("Existing settings are preserved. Backends can be added to router.env at any time.")
    print("Status: systemctl --user status llm-router.service")
    print("Logs: journalctl --user -u llm-router.service -f")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the router's systemd user service")
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--config-home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        install_user_service(args.install_dir, args.config_home)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Service installation failed: {exc}", file=sys.stderr)
        print("Inspect logs: journalctl --user -u llm-router.service -n 50", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
