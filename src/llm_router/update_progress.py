"""Private, durable, sanitized updater progress that survives runtime swaps."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid


STATUS_FILE = ".update-status.json"
_MAX_BYTES = 8192
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_FIELDS = {
    "schema_version", "run_id", "state", "stage", "message", "started_at",
    "updated_at", "current_commit", "target_commit",
}
_MESSAGES = {
    ("running", "checking"): "Checking the installed revision and official main branch.",
    ("running", "downloading"): "Downloading and preparing the new router runtime.",
    ("running", "validating"): "Validating the new runtime with isolated, model-free checks.",
    ("running", "draining"): "Waiting for in-flight requests to finish before restarting the router.",
    ("running", "restarting"): "Activating the validated update and checking the router service.",
    ("succeeded", "complete"): "Update installed successfully; the router service state was preserved.",
    ("current", "complete"): "The router already has the current official main revision.",
    ("failed", "failed"): "Update failed; inspect the updater service before retrying.",
}
_ERROR = "Updater progress is unavailable or unsafe."


def _directory(install_dir: Path) -> Path | None:
    directory = Path(os.path.abspath(os.path.expanduser(install_dir)))
    for item in (*reversed(directory.parents), directory):
        try:
            info = item.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(_ERROR)
    info = directory.stat()
    # Existing managed installs normally use 0755, which is fine: the progress
    # file itself is private. Other users must not be able to replace its entry.
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(_ERROR)
    return directory


def _open_private(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_BYTES
        ):
            raise RuntimeError(_ERROR)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate progress field")
        result[key] = value
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Invalid progress timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Invalid progress timestamp")
    if parsed.year < 1970:
        raise ValueError("Invalid progress timestamp")
    return parsed


def _validate(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("Invalid progress structure")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Invalid progress version")
    if not isinstance(payload["run_id"], str) or not _HEX_ID.fullmatch(payload["run_id"]):
        raise ValueError("Invalid progress run identifier")
    state, stage = payload["state"], payload["stage"]
    if not isinstance(state, str) or not isinstance(stage, str):
        raise ValueError("Invalid progress state")
    if (state, stage) not in _MESSAGES or payload["message"] != _MESSAGES[(state, stage)]:
        raise ValueError("Invalid progress message")
    if _timestamp(payload["updated_at"]) < _timestamp(payload["started_at"]):
        raise ValueError("Invalid progress times")
    for key in ("current_commit", "target_commit"):
        value = payload[key]
        if value is not None and (not isinstance(value, str) or not _COMMIT.fullmatch(value)):
            raise ValueError("Invalid progress revision")
    return payload


def read_update_status(install_dir: Path) -> dict | None:
    """Read bounded progress without creating files; reject unsafe/malformed data."""
    try:
        directory = _directory(install_dir)
        if directory is None:
            return None
        try:
            fd = _open_private(directory / STATUS_FILE)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError
        return _validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError, OverflowError):
        raise RuntimeError(_ERROR) from None


def _write_status(install_dir: Path, payload: dict) -> None:
    temporary = None
    try:
        _validate(payload)
        directory = _directory(install_dir)
        if directory is None:
            raise RuntimeError(_ERROR)
        destination = directory / STATUS_FILE
        try:
            fd = _open_private(destination)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
        fd, name = tempfile.mkstemp(prefix=".update-status-", dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError, OverflowError):
        raise RuntimeError(_ERROR) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class UpdateProgress:
    """Updater-only writer; callers must hold the installation's update lock."""

    def __init__(self, install_dir: Path) -> None:
        self.install_dir = install_dir
        invocation = os.environ.get("INVOCATION_ID", "")
        run_id = invocation if _HEX_ID.fullmatch(invocation) else uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self.payload = {
            "schema_version": 1, "run_id": run_id, "state": "running", "stage": "checking",
            "message": _MESSAGES[("running", "checking")], "started_at": now, "updated_at": now,
            "current_commit": None, "target_commit": None,
        }
        _write_status(self.install_dir, self.payload)

    def advance(self, stage: str, *, state: str = "running", **revisions: str | None) -> None:
        if set(revisions) - {"current_commit", "target_commit"} or (state, stage) not in _MESSAGES:
            raise RuntimeError(_ERROR)
        now = max(datetime.now(timezone.utc), _timestamp(self.payload["updated_at"]))
        updated = {
            **self.payload, **revisions, "state": state, "stage": stage,
            "message": _MESSAGES[(state, stage)], "updated_at": now.isoformat(),
        }
        _write_status(self.install_dir, updated)
        self.payload = updated


__all__ = ["STATUS_FILE", "read_update_status", "UpdateProgress"]
