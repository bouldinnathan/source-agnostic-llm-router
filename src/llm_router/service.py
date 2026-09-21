"""Opt-in systemd user service installation; never run during package import."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

SERVICE_NAME = "llm-router.service"
UPDATE_SERVICE_NAME = "llm-router-update.service"
UPDATE_TIMER_NAME = "llm-router-update.timer"


def _unit_path(value: Path) -> str:
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 or char in "\\\"'*?[]" for char in text):
        raise ValueError("Service paths cannot contain control characters, quotes, backslashes, or glob characters")
    # WorkingDirectory/EnvironmentFile parse raw paths, not shell-quoted words.
    # The latter also expands globs. Only ExecStart uses quoted word parsing.
    text.encode("utf-8")
    return text.replace("%", "%%")


def write_user_service(
    install_dir: Path, config_home: Path, *, listen: str | None = None,
) -> tuple[Path, Path]:
    """Write managed service files, preserving credentials and local configuration."""

    if listen not in (None, "lan", "localhost"):
        raise ValueError("Listen mode must be 'lan' or 'localhost'")
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
        # On stop the gateway keeps serving the answers already in flight;
        # give a long agent turn time to finish before systemd kills it.
        "TimeoutStopSec=900\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_info = config_dir.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid():
        raise RuntimeError(f"Configuration directory must be a real directory owned by this user: {config_dir}")
    config_dir.chmod(0o700)
    try:
        env_path.lstat()
    except FileNotFoundError:
        # Exclusive creation also prevents replacing a dangling symlink or a file
        # another installer created concurrently. Do not rewrite an existing key.
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as env_file:
            env_file.write(
                "# systemd EnvironmentFile syntax: KEY=value, no shell export commands.\n"
                "# Edit this file, then: systemctl --user restart llm-router.service\n"
                "# --lan listens on all IPv4 interfaces; --localhost restricts access to this machine.\n"
                f"LLM_ROUTER_HOST={'127.0.0.1' if listen == 'localhost' else '0.0.0.0'}\n"
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
    else:
        original, original_info = _read_environment(env_path)
        if listen is not None:
            replacement = _change_listen_mode(original, listen, env_path)
            if replacement != original:
                _replace_environment(env_path, original, original_info, replacement)
    _write_unit(unit_path, unit)
    return unit_path, env_path


def _read_environment(env_path: Path) -> tuple[bytes, os.stat_result]:
    """Read only a private, ordinary file, never following a credential symlink."""

    descriptor = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as env_file:
        info = os.fstat(env_file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise RuntimeError(f"Settings must be a regular, single-link file owned by this user: {env_path}")
        # This does not rewrite an existing configuration or its credentials.
        # In particular, an old world-readable file must not remain readable
        # when a user explicitly enables network access.
        os.fchmod(env_file.fileno(), 0o600)
        return env_file.read(), os.fstat(env_file.fileno())


@dataclass(frozen=True)
class _EnvironmentAssignment:
    index: int
    prefix: str
    value: str
    quote: str
    suffix: str
    ending: str


def _parse_environment(data: bytes) -> tuple[list[str], dict[str, _EnvironmentAssignment]]:
    """Recognize a conservative subset of systemd's EnvironmentFile syntax.

    Do not use a shell parser: systemd does not use shell expansion, inline
    comments or shell quote concatenation. Refusing complex existing files is
    safer than guessing whether authentication will actually be enabled.
    """

    contents = data.decode("utf-8")
    # Only LF separates systemd records. Python's splitlines() also separates
    # Unicode characters that could otherwise invent an authentication entry.
    segments = contents.split("\n")
    lines = [segment + "\n" for segment in segments[:-1]]
    if segments[-1]:
        lines.append(segments[-1])
    assignments: dict[str, _EnvironmentAssignment] = {}
    for index, line in enumerate(lines):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[:-len(ending)] if ending else line
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in body):
            raise ValueError("Unsupported control character in settings")
        stripped = body.strip(" \t")
        if stripped.startswith(("#", ";")) and stripped.endswith("\\"):
            # Older systemd versions continue escaped comment lines, which
            # could swallow the apparent API-key assignment on the next line.
            raise ValueError("Continued comments are not supported")
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = re.fullmatch(r"([ \t]*)([A-Za-z_][A-Za-z0-9_]*)([ \t]*=[ \t]*)(.*)", body)
        if match is None:
            raise ValueError("Unsupported settings assignment")
        leading, name, separator, raw = match.groups()
        value = raw.rstrip(" \t")
        suffix = raw[len(value):]
        quote = value[:1] if value.startswith(("'", '"')) else ""
        if quote:
            if len(value) < 2 or not value.endswith(quote):
                raise ValueError("Multiline or incomplete quoted settings value")
            value = value[1:-1]
            if quote in value or "\\" in value:
                raise ValueError("Unsupported quoting or escaping in settings")
        elif any(char in value for char in "\\\"'"):
            raise ValueError("Unsupported quoting or escaping in settings")
        if name in assignments and name in {
            "LLM_ROUTER_HOST", "LLM_ROUTER_PORT", "LLM_ROUTER_GATEWAY_API_KEY",
        }:
            raise ValueError("Duplicate listener or API key assignments")
        assignments[name] = _EnvironmentAssignment(index, leading + name + separator, value, quote, suffix, ending)
    return lines, assignments


def _change_listen_mode(original: bytes, listen: str, env_path: Path) -> bytes:
    try:
        lines, assignments = _parse_environment(original)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot safely change listener in {env_path}: {exc}. "
            "Edit router.env manually using one simple assignment per line for "
            "LLM_ROUTER_HOST and a nonempty LLM_ROUTER_GATEWAY_API_KEY, then rerun. "
            "No settings were rewritten."
        ) from exc
    newline = next(("\r\n" if line.endswith("\r\n") else "\n" for line in lines if line.endswith("\n")), "\n")

    def assign(name: str, value: str) -> None:
        previous = assignments.get(name)
        if previous is not None:
            lines[previous.index] = (
                previous.prefix + previous.quote + value + previous.quote + previous.suffix + previous.ending
            )
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += newline
            lines.append(name + "=" + value + newline)

    assign("LLM_ROUTER_HOST", "0.0.0.0" if listen == "lan" else "127.0.0.1")
    key = assignments.get("LLM_ROUTER_GATEWAY_API_KEY")
    if listen == "lan" and (key is None or not key.value.strip()):
        assign("LLM_ROUTER_GATEWAY_API_KEY", secrets.token_urlsafe(32))
    return "".join(lines).encode("utf-8")


def _replace_environment(
    env_path: Path, original: bytes, original_info: os.stat_result, replacement: bytes,
) -> None:
    """Preserve a private exact backup before atomically changing settings."""

    backup_fd, backup_name = tempfile.mkstemp(prefix=f"{env_path.name}.backup-", dir=env_path.parent)
    with os.fdopen(backup_fd, "wb") as backup_file:
        os.fchmod(backup_file.fileno(), 0o600)
        backup_file.write(original)
    descriptor, staging_name = tempfile.mkstemp(prefix=f".{env_path.name}.", dir=env_path.parent)
    staging_path = Path(staging_name)
    try:
        with os.fdopen(descriptor, "wb") as env_file:
            os.fchmod(env_file.fileno(), 0o600)
            env_file.write(replacement)
        current, current_info = _read_environment(env_path)
        if current != original or (current_info.st_dev, current_info.st_ino) != (
            original_info.st_dev, original_info.st_ino,
        ):
            raise RuntimeError("Settings changed during listener configuration; retry the command")
        staging_path.replace(env_path)
    finally:
        staging_path.unlink(missing_ok=True)
    print(f"Previous settings backed up privately to {backup_name}")


def _print_listener(env_path: Path) -> None:
    try:
        contents, _ = _read_environment(env_path)
        _, settings = _parse_environment(contents)
        host = settings["LLM_ROUTER_HOST"].value
        port = settings["LLM_ROUTER_PORT"].value
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid port")
    except (OSError, RuntimeError, ValueError, UnicodeError, KeyError):
        print(f"Listener settings preserved; check LLM_ROUTER_HOST and LLM_ROUTER_PORT in {env_path}.")
        return
    if host in ("0.0.0.0", "::"):
        interfaces = "all IPv4 interfaces" if host == "0.0.0.0" else "all IPv6 interfaces"
        print(f"Configured listener: {host}:{port} ({interfaces}).")
        print(f"Status page: http://ROUTER_IP:{port}/status (replace ROUTER_IP with this server's LAN/VPN IP).")
        print(f"OpenAI base URL: http://ROUTER_IP:{port}/v1")
        print("Network access: use the router API key; restrict access to trusted LAN/VPN clients.")
        print("No firewall rules were changed. Use TLS or a VPN for untrusted networks; do not expose plain HTTP publicly.")
    elif host in ("127.0.0.1", "localhost", "::1"):
        address = f"[{host}]" if ":" in host else host
        print(f"Configured listener: localhost only ({host}:{port}).")
        print(f"Status page: http://{address}:{port}/status")
        print(f"OpenAI base URL: http://{address}:{port}/v1")
    else:
        print(f"Custom listener settings preserved; check {env_path} for the address and port.")


def _write_unit(unit_path: Path, unit: str) -> None:
    unit_dir = unit_path.parent
    unit_dir.mkdir(parents=True, exist_ok=True)
    if unit_path.exists() and unit_path.read_text(encoding="utf-8") == unit:
        return
    if unit_path.exists():
        backup_fd, backup_name = tempfile.mkstemp(prefix=f"{unit_path.name}.backup-", dir=unit_dir)
        os.close(backup_fd)
        shutil.copy2(unit_path, backup_name)
        print(f"Previous service definition backed up to {backup_name}")
    fd, staging_name = tempfile.mkstemp(prefix=f".{unit_path.name}.", dir=unit_dir)
    staging_path = Path(staging_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as unit_file:
            unit_file.write(unit)
        staging_path.chmod(0o644)
        staging_path.replace(unit_path)
    finally:
        staging_path.unlink(missing_ok=True)


def write_update_units(install_dir: Path, config_home: Path) -> tuple[Path, Path]:
    """Render the opt-in updater without changing credentials or starting jobs."""

    install_dir = install_dir.expanduser().resolve()
    config_home = config_home.expanduser().resolve()
    python = install_dir / "venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"Installed virtual environment not found: {python}")
    unit_dir = config_home / "systemd" / "user"
    # Validate the destination too, even though only the installation appears in
    # these unit contents. Keep path rules consistent with the gateway service.
    _unit_path(unit_dir)
    service_path = unit_dir / UPDATE_SERVICE_NAME
    timer_path = unit_dir / UPDATE_TIMER_NAME
    update_service = (
        "# Managed by source-agnostic-llm-router install.sh --service --auto-update.\n"
        "[Unit]\n"
        "Description=Check and install Source-Agnostic LLM Router updates\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={_unit_path(install_dir)}\n"
        f'ExecStart=:"{_unit_path(python)}" -m llm_router.updater '
        f'--install-dir "{_unit_path(install_dir)}"\n'
        "Environment=PYTHONUNBUFFERED=1\n"
        "Environment=GIT_TERMINAL_PROMPT=0\n"
        "Environment=PIP_NO_INPUT=1\n"
        "TimeoutStartSec=45min\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
    )
    timer = (
        "# Managed by source-agnostic-llm-router install.sh --service --auto-update.\n"
        "[Unit]\n"
        "Description=Daily Source-Agnostic LLM Router update check\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "RandomizedDelaySec=1h\n"
        "Persistent=true\n"
        f"Unit={UPDATE_SERVICE_NAME}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    _write_unit(service_path, update_service)
    _write_unit(timer_path, timer)
    return service_path, timer_path


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


def install_user_service(
    install_dir: Path, config_home: Path, *, auto_update: bool = False, listen: str | None = None,
) -> None:
    if listen not in (None, "lan", "localhost"):
        raise ValueError("Listen mode must be 'lan' or 'localhost'")
    if auto_update:
        from .updater import installed_revision

        # Validate actual installed provenance before enabling lingering, writing
        # units or restarting anything, not merely the installer's environment.
        installed_revision(install_dir)
    preflight_user_service()
    if listen is None:
        unit_path, env_path = write_user_service(install_dir, config_home)
    else:
        unit_path, env_path = write_user_service(install_dir, config_home, listen=listen)
    if auto_update:
        write_update_units(install_dir, config_home)
    for arguments in (
        ["daemon-reload"], ["enable", SERVICE_NAME], ["restart", SERVICE_NAME],
        ["is-active", "--quiet", SERVICE_NAME],
    ):
        subprocess.run(["systemctl", "--user", *arguments], check=True)
    if auto_update:
        subprocess.run(["systemctl", "--user", "enable", "--now", UPDATE_TIMER_NAME], check=True)
        subprocess.run(["systemctl", "--user", "is-active", "--quiet", UPDATE_TIMER_NAME], check=True)
        print("Automatic updates enabled: official main branch, daily with up to one hour of jitter.")
        print("Update logs: journalctl --user -u llm-router-update.service")
    print(f"Service enabled and running: {SERVICE_NAME}")
    print(f"Service definition: {unit_path}")
    print(f"Settings and client API key (preserved on updates): {env_path}")
    _print_listener(env_path)
    print("Use --lan for LAN/VPN access or --localhost for this machine only; omitted flags preserve existing settings.")
    print("Backends can be added to router.env at any time. Unit overrides may override these settings.")
    print("Status: systemctl --user status llm-router.service")
    print("Logs: journalctl --user -u llm-router.service -f")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the router's systemd user service")
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--config-home", type=Path, required=True)
    parser.add_argument("--auto-update", action="store_true", help="Enable daily official-main updates")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--lan", dest="listen", action="store_const", const="lan", help="Listen on all IPv4 interfaces (new service default)")
    access.add_argument("--localhost", dest="listen", action="store_const", const="localhost", help="Restrict access to this machine")
    args = parser.parse_args(argv)
    try:
        options = {}
        if args.auto_update:
            options["auto_update"] = True
        if args.listen is not None:
            options["listen"] = args.listen
        install_user_service(args.install_dir, args.config_home, **options)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Service installation failed: {exc}", file=sys.stderr)
        print("Inspect logs: journalctl --user -u llm-router.service -n 50", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
