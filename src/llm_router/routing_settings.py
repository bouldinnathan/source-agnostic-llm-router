"""Operator-adjustable routing settings, saved privately on the router.

These switches change what the gateway advertises and how replicas are chosen.
They are edited from the authenticated status page, persist across restarts and
software updates, and apply to the live router without a restart. Nothing here
contacts a backend or reads request content.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # Normal gateway imports must still work off POSIX.
    fcntl = None


MIN_RACE_EVERY = 2
MAX_RACE_EVERY = 1000
# Whole seconds. A first-token limit covers model loading and prompt evaluation;
# idle is the longest silence during generation; the cap bounds the whole answer.
INTEGER_FIELDS = {
    "race_every": (MIN_RACE_EVERY, MAX_RACE_EVERY),
    "first_token_timeout_seconds": (5, 3600),
    "idle_timeout_seconds": (5, 3600),
    "max_request_seconds": (0, 86400),
}
_MAX_STATE_BYTES = 8 * 1024
_ERROR = "Routing settings storage is unavailable or unsafe; saved settings were not changed."


@dataclass(frozen=True, slots=True)
class RoutingSettings:
    """Switches the operator can flip from the dashboard."""

    advertise_machine_aliases: bool = True
    prefer_fastest_replica: bool = False
    prefer_first_token: bool = False
    race_replicas: bool = False
    session_affinity: bool = True
    race_every: int = 20
    first_token_timeout_seconds: int = 300
    idle_timeout_seconds: int = 90
    max_request_seconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "advertise_machine_aliases": self.advertise_machine_aliases,
            "prefer_fastest_replica": self.prefer_fastest_replica,
            "prefer_first_token": self.prefer_first_token,
            "race_replicas": self.race_replicas,
            "session_affinity": self.session_affinity,
            "race_every": self.race_every,
            "first_token_timeout_seconds": self.first_token_timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_request_seconds": self.max_request_seconds,
        }


FIELDS = tuple(RoutingSettings().to_dict())


def validate_settings(data: object, *, base: RoutingSettings | None = None) -> RoutingSettings:
    """Apply a partial mapping of known fields onto ``base`` with strict types."""
    if not isinstance(data, Mapping):
        raise ValueError("Routing settings must be a JSON object.")
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise ValueError("Unknown routing setting: " + ", ".join(sorted(str(name) for name in unknown)))
    changes: dict[str, Any] = {}
    for name in ("advertise_machine_aliases", "prefer_fastest_replica", "prefer_first_token", "race_replicas", "session_affinity"):
        if name in data:
            if type(data[name]) is not bool:
                raise ValueError(f"{name} must be true or false.")
            changes[name] = data[name]
    for name, (low, high) in INTEGER_FIELDS.items():
        if name not in data:
            continue
        value = data[name]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be a whole number from {low} to {high}.")
        changes[name] = value
    return replace(base or RoutingSettings(), **changes)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class RoutingSettingsStore:
    """Small, atomic, owner-only JSON file; construction has no side effects."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            override = os.environ.get("LLM_ROUTER_ROUTING_SETTINGS_FILE")
            config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            path = Path(override) if override else config_home / "llm-router" / "routing-settings.json"
        self.path = Path(os.path.abspath(os.path.expanduser(path)))

    def _directory(self, *, create: bool = False) -> bool:
        parent = self.path.parent
        for directory in (*reversed(parent.parents), parent):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(_ERROR)
        info = parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(_ERROR)
        return True

    def _open(self, path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RuntimeError(_ERROR)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def load(self) -> RoutingSettings:
        """Return saved settings, defaults when nothing is saved, or raise safely."""
        try:
            if not self._directory():
                return RoutingSettings()
            try:
                fd = self._open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                return RoutingSettings()
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(_MAX_STATE_BYTES + 1)
            if len(raw) > _MAX_STATE_BYTES:
                raise ValueError("too large")
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(payload, dict) or set(payload) != {"version", "settings"}
                or type(payload["version"]) is not int or payload["version"] != 1
            ):
                raise ValueError("unexpected layout")
            return validate_settings(payload["settings"])
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise RuntimeError(_ERROR) from None

    @contextmanager
    def _locked(self):
        if fcntl is None:
            raise RuntimeError(_ERROR)
        self._directory(create=True)
        fd = self._open(self.path.with_name(self.path.name + ".lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def save(self, settings: RoutingSettings) -> RoutingSettings:
        """Atomically replace the saved settings; an unsafe existing file is never replaced."""
        if not isinstance(settings, RoutingSettings):
            raise ValueError("Invalid routing settings.")
        settings = validate_settings(settings.to_dict())
        raw = (json.dumps({"version": 1, "settings": settings.to_dict()}, indent=2) + "\n").encode("utf-8")
        try:
            with self._locked():
                self.load()
                fd, name = tempfile.mkstemp(prefix=".routing-settings-", dir=self.path.parent)
                staged = Path(name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staged, self.path)
                    directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    staged.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            raise RuntimeError(_ERROR) from None
        return settings
