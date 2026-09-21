"""Authenticated-dashboard control of the one installed systemd updater.

This module never accepts an install path, unit, repository or revision from an
HTTP client. Installation itself belongs to the separate updater service, which
survives a gateway restart and writes durable, sanitized progress.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from . import __version__
from .update_progress import read_update_status

UPDATE_UNIT = "llm-router-update.service"
GATEWAY_UNIT = "llm-router.service"
_INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
_BUSY = {"activating", "active", "reloading", "deactivating"}
_TERMINAL = {"current", "succeeded", "failed"}
_PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "Result", "MainPID",
    "WorkingDirectory", "InvocationID",
)


def installed_revision(install_dir: Path, *, timeout: float) -> str:
    # The updater itself uses POSIX locking. Import it only after the platform
    # preflight so ordinary gateways remain importable on Windows/macOS.
    from .updater import installed_revision as validate

    return validate(install_dir, timeout=timeout)


class UpdateRequestError(RuntimeError):
    """Fixed safe message suitable for an authenticated API response."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def _systemctl(*arguments: str) -> str:
    env = {name: value for name, value in os.environ.items() if name in {
        "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    }}
    env.update(SYSTEMD_PAGER="", SYSTEMD_COLORS="0")
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments], check=True, capture_output=True,
            text=True, timeout=5, env=env,
        )
        if len(result.stdout) > 16384:
            raise RuntimeError("Oversized manager response")
        return result.stdout
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        raise UpdateRequestError("The user service manager could not confirm update status. Check the router's service logs.") from None


def _properties(unit: str) -> dict[str, str]:
    output = _systemctl("show", unit, *(f"--property={name}" for name in _PROPERTIES))
    result = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in _PROPERTIES:
            if name in result:
                raise UpdateRequestError("The user service manager returned an invalid update status.")
            result[name] = value
    return result


def _invocation(properties: dict[str, str]) -> str | None:
    value = properties.get("InvocationID", "")
    return value if _INVOCATION.fullmatch(value) and value != "0" * 32 else None


class UpdateController:
    """Small local-only status reader and fixed-unit launcher; thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: tuple[str | None, str | None, float] | None = None
        self._last_request = float("-inf")
        self._validation: tuple[tuple[str, int, int], float, str] | None = None

    @staticmethod
    def _snapshot(**fields: Any) -> dict[str, Any]:
        return {
            "available": True, "busy": False, "state": "idle", "stage": "idle",
            "message": "Check the official main branch and install a newer update, if available. The router restarts once the answers it is producing have finished.",
            "run_id": None, "updated_at": None, "current_version": __version__,
            "current_commit": None, "target_commit": None, **fields,
        }

    def _installation(self, update: dict[str, str], *, force: bool) -> tuple[Path, str]:
        if update.get("LoadState") != "loaded":
            raise UpdateRequestError("The updater service is not installed. As the service user, run install.sh --service --auto-update from official main.")
        service = _properties(GATEWAY_UNIT)
        if (service.get("LoadState") != "loaded" or service.get("ActiveState") not in {"active", "reloading"}
                or service.get("MainPID") != str(os.getpid())):
            raise UpdateRequestError("Web updates require this gateway to run as the managed llm-router.service user service.")
        directory = update.get("WorkingDirectory", "")
        if not directory or not Path(directory).is_absolute() or any(ord(char) < 32 for char in directory):
            raise UpdateRequestError("The updater service does not identify a managed installation.")
        try:
            install = Path(directory).resolve(strict=True)
            info = install.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("Unsafe installation")
            runtime = (install / "venv").resolve(strict=True)
            if Path(sys.prefix).resolve(strict=True) != runtime:
                raise ValueError("Unrelated installation")
            # Reject source-tree/PYTHONPATH and editable instances even if their
            # Python happens to belong to the managed virtual environment.
            Path(__file__).resolve(strict=True).relative_to(runtime)
            runtime_info = runtime.stat()
            identity = (str(runtime), runtime_info.st_ino, runtime_info.st_mtime_ns)
            if not force and self._validation and self._validation[0] == identity and self._validation[1] > time.monotonic():
                return install, self._validation[2]
            commit = installed_revision(install, timeout=5)
            self._validation = (identity, time.monotonic() + 30, commit)
            return install, commit
        except (OSError, ValueError, RuntimeError):
            raise UpdateRequestError("Web updates support only the managed official-main installation. Pinned, fork, editable and unmanaged installs must be updated manually.") from None

    def _inspect(self, *, force: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
        if sys.platform != "linux" or os.geteuid() == 0 or shutil.which("systemctl") is None:
            raise UpdateRequestError("Web updates require a non-root Linux systemd user-service installation.")
        update = _properties(UPDATE_UNIT)
        install, commit = self._installation(update, force=force)
        try:
            progress = read_update_status(install)
        except (OSError, ValueError, RuntimeError):
            raise UpdateRequestError("Saved update progress is unavailable or unsafe. Inspect the updater service and its private status file before retrying.") from None
        active = update.get("ActiveState")
        if active not in _BUSY | {"inactive", "failed"}:
            raise UpdateRequestError("The updater service state is unavailable. Check the service before retrying.")
        busy = active in _BUSY
        invocation = _invocation(update)
        run_id = progress.get("run_id") if progress else None
        if self._pending:
            old_run, old_invocation, requested = self._pending
            observed_new = (run_id is not None and run_id != old_run) or (invocation is not None and invocation != old_invocation)
            if observed_new:
                self._pending = None
            elif time.monotonic() - requested < 30:
                return self._snapshot(busy=True, state="queued", stage="queued", current_commit=commit,
                                      message="Update requested; waiting for the updater service to confirm this check."), update
            else:
                self._pending = None
                return self._snapshot(state="interrupted", stage="failed", current_commit=commit,
                                      message="The update request could not be confirmed. Inspect service status before trying again."), update
        if busy:
            # A durable result from yesterday is not proof that today's queued
            # check completed. systemd InvocationID matches the writer's run ID.
            if not progress or invocation is None or invocation != run_id:
                return self._snapshot(busy=True, state="queued", stage="queued", run_id=invocation, current_commit=commit,
                                      message="Updater service is starting; waiting for this run's progress."), update
        if progress:
            fields = {key: progress[key] for key in (
                "state", "stage", "message", "run_id", "updated_at", "current_commit", "target_commit",
            )}
            if not busy and invocation is not None and invocation != run_id:
                fields.update(state="interrupted", stage="failed", run_id=invocation,
                              message="This updater run ended without saved confirmation. A previous run's result is not confirmation; inspect updater logs.")
            elif not busy and (progress["state"] not in _TERMINAL or active == "failed" and progress["state"] != "failed"):
                fields.update(state="interrupted", stage="failed", message="The update stopped before completion was confirmed. Check updater logs before retrying.")
            return self._snapshot(**fields, busy=busy), update
        if active == "failed":
            return self._snapshot(state="failed", stage="failed", run_id=invocation, current_commit=commit,
                                  message="The updater service failed without saved progress. Check its service logs before retrying."), update
        if invocation is not None:
            return self._snapshot(state="interrupted", stage="failed", run_id=invocation, current_commit=commit,
                                  message="The updater run ended without a saved result. It may have used an older updater; inspect service logs to confirm its outcome."), update
        return self._snapshot(current_commit=commit), update

    def status(self) -> dict[str, Any]:
        """Read local state only; never contact GitHub, install, or start a unit."""
        with self._lock:
            try:
                return self._inspect()[0]
            except UpdateRequestError as exc:
                return self._snapshot(available=False, state="unavailable", message=str(exc))

    def start(self) -> dict[str, Any]:
        """Check and install via the already-installed, independent updater unit."""
        with self._lock:
            snapshot, properties = self._inspect(force=True)
            if snapshot["busy"] or time.monotonic() - self._last_request < 30:
                raise UpdateRequestError("An update is running or was just requested. Check its progress before starting another.", 409)
            self._last_request = time.monotonic()
            self._pending = (snapshot["run_id"], _invocation(properties), self._last_request)
            # A timeout/lost reply is not proof the service did not start. Keep
            # the pending marker, expose status, and never retry this mutation.
            try:
                _systemctl("start", "--no-block", UPDATE_UNIT)
            except UpdateRequestError:
                raise UpdateRequestError("The update request could not be confirmed. Refresh update status before retrying.") from None
            return self._snapshot(busy=True, state="queued", stage="queued", current_commit=snapshot["current_commit"],
                                  message="Update requested. Checking official main and installing a newer commit if available.")
