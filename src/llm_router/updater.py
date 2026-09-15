"""Opt-in, locked updates of the official main branch with runtime rollback.

Downloads and smoke tests happen in a separate virtual environment. Configuration,
credentials, service definitions and backend model files are never changed here.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Iterator, Sequence

REPOSITORY = "https://github.com/bouldinnathan/source-agnostic-llm-router.git"
BRANCH = "main"
SERVICE_NAME = "llm-router.service"
PROVENANCE_FILE = ".llm-router-update.json"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_METADATA_CODE = (
    "import importlib.metadata; "
    "print(importlib.metadata.distribution('source-agnostic-llm-router')"
    ".read_text('direct_url.json') or 'null')"
)
_SMOKE_CODE = """
import asyncio
import httpx
from llm_router import bootstrap
from llm_router.discovery import DiscoverySettings
from llm_router.gateway import create_app
from llm_router.provisioning import ProvisioningSettings

async def smoke():
    # A user's default ~/.config router file must not enter an isolated test.
    bootstrap.DEFAULT_CONFIG_LOCATIONS = ()
    app = create_app(discovery=False,
        settings=DiscoverySettings(enabled=False, include_loopback=False, include_cloud=False),
        provisioning_settings=ProvisioningSettings(enabled=False))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='http://router.invalid') as client:
            response = await client.get('/')
            if response.status_code != 200 or response.text != 'LLM Router is running':
                raise RuntimeError('Router liveness smoke test failed')
asyncio.run(smoke())
"""


def _environment(*, smoke: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    # Neither package build subprocesses nor smoke tests need provider secrets.
    for key in tuple(env):
        if key.startswith("LLM_ROUTER_") or key.endswith(("_API_KEY", "_TOKEN")):
            env.pop(key)
    if smoke:
        # Do not probe providers, read user configuration, or provision models.
        env.update(LLM_ROUTER_DISCOVERY="0", LLM_ROUTER_AUTO_PROVISION="0")
    return env


def _run(arguments: list[str], *, timeout: float = 60, cwd: Path | None = None,
         smoke: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, check=True, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=_environment(smoke=smoke))
    except (OSError, subprocess.SubprocessError) as exc:
        # pip/git errors may contain private index credentials. Keep their output
        # out of the systemd journal; communicate only the failed operation.
        raise RuntimeError(f"Update command failed ({Path(arguments[0]).name}; {type(exc).__name__})") from exc


def _metadata(runtime: Path) -> dict[str, object]:
    python = runtime / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("Installed router virtual environment was not found")
    result = _run([str(python), "-I", "-c", _METADATA_CODE])
    try:
        metadata = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Installed router source metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("Automatic updates require an official Git main-branch install")
    return metadata


def _revision(metadata: dict[str, object]) -> tuple[str, str]:
    vcs = metadata.get("vcs_info")
    if (metadata.get("url") != REPOSITORY or not isinstance(vcs, dict)
            or vcs.get("vcs") != "git" or "dir_info" in metadata):
        raise RuntimeError("Automatic updates support only the official Git main branch")
    commit = vcs.get("commit_id")
    revision = vcs.get("requested_revision")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit) or not isinstance(revision, str):
        raise RuntimeError("Installed router commit metadata is missing or invalid")
    return commit, revision


def installed_revision(install_dir: Path) -> str:
    """Validate the official moving-main source and return its installed commit.

    Exact commits installed by this updater retain a matching provenance marker
    inside their runtime. Arbitrary pinned, editable, local or fork installs are
    deliberately rejected instead of silently switching the user's source.
    """
    runtime = install_dir.expanduser().resolve() / "venv"
    commit, requested = _revision(_metadata(runtime))
    if requested == BRANCH:
        return commit
    try:
        provenance = json.loads((runtime / PROVENANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        provenance = None
    if requested == commit and provenance == {
        "repository": REPOSITORY, "branch": BRANCH, "commit": commit,
    }:
        return commit
    raise RuntimeError("Pinned installs do not automatically track main; reinstall official main to opt in")


def _remote_revision() -> str:
    result = _run(["git", "ls-remote", "--exit-code", REPOSITORY, "refs/heads/main"], timeout=60)
    fields = result.stdout.strip().split()
    if len(fields) != 2 or not _COMMIT.fullmatch(fields[0]) or fields[1] != "refs/heads/main":
        raise RuntimeError("GitHub did not return one valid main-branch commit")
    return fields[0]


def _stage(install_dir: Path, commit: str) -> Path:
    releases = install_dir / "releases"
    if releases.is_symlink():
        raise RuntimeError("The managed releases directory must not be a symlink")
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=f"{commit[:12]}-", dir=releases))
    try:
        return _populate_release(install_dir, release, commit)
    except BaseException:
        # This directory was just created exclusively for this failed attempt;
        # never delete an active or previous runtime, or accumulate daily failures.
        shutil.rmtree(release)
        raise


def _populate_release(install_dir: Path, release: Path, commit: str) -> Path:
    runtime = release / "venv"
    _run([str(install_dir / "venv/bin/python"), "-I", "-m", "venv", "--copies", str(runtime)],
         timeout=180)
    python = str(runtime / "bin/python")
    _run([python, "-I", "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
          f"git+{REPOSITORY}@{commit}"], timeout=1800)
    staged_commit, staged_requested = _revision(_metadata(runtime))
    if (staged_commit, staged_requested) != (commit, commit):
        raise RuntimeError("Downloaded runtime does not match the requested official commit")
    for module in ("cli", "gateway", "service", "updater"):
        _run([python, "-I", "-m", f"llm_router.{module}", "--help"],
             cwd=release, smoke=True)
    _run([python, "-I", "-c", _SMOKE_CODE], cwd=release, smoke=True)
    # The marker follows this exact runtime through activation and rollback.
    with (runtime / PROVENANCE_FILE).open("x", encoding="utf-8") as provenance:
        json.dump({"repository": REPOSITORY, "branch": BRANCH, "commit": commit}, provenance)
    (runtime / PROVENANCE_FILE).chmod(0o600)
    return runtime


def _service_active() -> bool:
    result = _run(["systemctl", "--user", "show", SERVICE_NAME, "--property=ActiveState", "--value"])
    state = result.stdout.strip()
    if state in {"active", "reloading"}:
        return True
    if state in {"inactive", "failed"}:
        return False
    raise RuntimeError("Router service is transitioning or unavailable; retry the update later")


def _restart_and_verify() -> None:
    _run(["systemctl", "--user", "restart", SERVICE_NAME], timeout=90)
    identity: tuple[str, str] | None = None
    for attempt in range(5):
        if attempt:
            time.sleep(1)
        result = _run(["systemctl", "--user", "show", SERVICE_NAME,
                       "--property=ActiveState", "--property=SubState",
                       "--property=MainPID", "--property=NRestarts"])
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        current = (properties.get("MainPID", ""), properties.get("NRestarts", ""))
        if (properties.get("ActiveState") != "active" or properties.get("SubState") != "running"
                or not current[0].isdigit() or int(current[0]) <= 0 or not current[1].isdigit()
                or identity is not None and identity != current):
            raise RuntimeError("Router did not remain running after restart")
        identity = current


def _point_to(active: Path, target: Path) -> None:
    # Create alongside the active entry so replacing an existing symlink is
    # atomic and cannot expose a partially written destination.
    fd, name = tempfile.mkstemp(prefix=".venv-link-", dir=active.parent)
    os.close(fd)
    link = Path(name)
    try:
        link.unlink()
        link.symlink_to(target, target_is_directory=True)
        link.replace(active)
    finally:
        link.unlink(missing_ok=True)


def _activate(install_dir: Path, runtime: Path, was_active: bool) -> None:
    active = install_dir / "venv"
    previous_target = active.resolve(strict=True)
    previous_link = active.is_symlink()
    backup: Path | None = None
    changed = False
    stopped = False
    try:
        if not previous_link:
            # Only the first migration needs a brief directory-to-link gap.
            # Stop a running process before moving its import paths.
            if was_active:
                stopped = True
                _run(["systemctl", "--user", "stop", SERVICE_NAME], timeout=90)
            backup_dir = Path(tempfile.mkdtemp(prefix="pre-update-", dir=install_dir / "releases"))
            backup = backup_dir / "venv"
            # Mark intent before filesystem operations: SIGTERM can arrive after
            # the rename succeeds but before Python executes its next statement.
            changed = True
            active.rename(backup)
        changed = True
        _point_to(active, runtime)
        if was_active:
            _restart_and_verify()
    except BaseException as exc:
        try:
            if changed:
                if previous_link:
                    _point_to(active, previous_target)
                elif backup is not None and backup.exists():
                    # Only remove the symlink we just installed, never contents.
                    if active.is_symlink():
                        active.unlink()
                    backup.rename(active)
            if was_active and (changed or stopped):
                _restart_and_verify()
        except BaseException as rollback_error:
            raise RuntimeError("Update activation and rollback failed; prior runtime is retained on disk; inspect the service") from rollback_error
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError("Update activation failed; previous runtime was restored") from exc


@contextmanager
def _termination_guard() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)

    def cancel(signum: int, frame: object) -> None:
        # A second cancellation must not interrupt restoring the old runtime.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise RuntimeError("Router update cancelled; preserving the previous runtime")

    signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_update(install_dir: Path) -> None:
    """Check for a new official commit, validate separately, activate or roll back."""
    install_dir = install_dir.expanduser().resolve()
    if not install_dir.is_dir():
        raise RuntimeError("Router installation directory does not exist")
    with _termination_guard(), (install_dir / ".update.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another router installation or update is running; skipping this check.")
            return
        current = installed_revision(install_dir)
        commit = _remote_revision()
        if current == commit:
            print(f"Router is already current ({current[:12]}).")
            return
        print(f"Preparing router update {current[:12]} -> {commit[:12]}.")
        runtime = _stage(install_dir, commit)
        # Read the service state after staging so a deliberate stop made during
        # the download is honored. Never turn a stopped service on automatically.
        was_active = _service_active()
        _activate(install_dir, runtime, was_active)
        print(f"Router updated to {commit[:12]}; previous runtime retained for rollback.")
        if not was_active:
            print("Router service was stopped and remains stopped.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely update the official router main-branch installation")
    parser.add_argument("--install-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_update(args.install_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Router update failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
