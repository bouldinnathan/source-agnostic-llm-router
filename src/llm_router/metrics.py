"""Private persistent aggregates from real routed requests, never active tests.

Only allowlisted numbers and deployment identity are stored. Reported model-load
durations of at least one second are counted as slow loads; that is a timing
heuristic, not confirmation that a disk read occurred.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Mapping
import unicodedata
from urllib.parse import quote, urlsplit

from .schema import EndpointConfig, ModelConfig

try:
    import fcntl
except ImportError:
    fcntl = None


LOCK_TIMEOUT_SECONDS = 0.25
MAX_DEPLOYMENTS = 4096
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_OBSERVATION_VALUE = 1e15
SLOW_LOAD_THRESHOLD_MS = 1000
_MAX_TOTAL = 1e18
_MAX_COUNT = 2**63 - 1
_APPLICATION_ID = 0x4C4C4D52
_VERSION = 1
_METRICS = (
    "input_tokens_per_second", "output_tokens_per_second", "load_duration_ms", "request_duration_ms",
)
_COLUMNS = (
    "id", "machine", "endpoint", "model", "address", "adapter", "successes", "failures",
    "input_tokens_total", "output_tokens_total", "last_seen_at", "metrics_json", "slow_load_count",
)
_SCHEMA = """
CREATE TABLE deployments (
    id TEXT PRIMARY KEY NOT NULL,
    machine TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    address TEXT NOT NULL,
    adapter TEXT NOT NULL,
    successes INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    input_tokens_total REAL NOT NULL,
    output_tokens_total REAL NOT NULL,
    last_seen_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    slow_load_count INTEGER NOT NULL,
    UNIQUE(machine, endpoint, model)
)
"""
_ERROR = "Passive metrics storage is unavailable or unsafe; existing data was not reset."


def _number(value: object) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= MAX_OBSERVATION_VALUE else None


def _label(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 512
        or any(unicodedata.category(char)[0] == "C" or char in "\u2028\u2029" for char in value)
    ):
        raise ValueError("Invalid passive metrics deployment identity.")
    return value


def _address(value: object) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.lower()
        try:
            parsed_ip = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").removesuffix(".")
            if not host or any(char.isspace() for char in host):
                return ""
        else:
            host = f"[{parsed_ip.compressed}]" if parsed_ip.version == 6 else parsed_ip.compressed
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return ""
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except (ValueError, UnicodeError):
        return ""


def _identity(machine: str, endpoint: str, model: str) -> str:
    return hashlib.sha256(json.dumps([machine, endpoint, model], separators=(",", ":")).encode()).hexdigest()


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid metrics timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.year < 1970:
        raise ValueError("Invalid metrics timestamp")
    return value


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid metrics data")
        result[key] = value
    return result


def _decode(row: sqlite3.Row) -> dict:
    deployment = dict(row)
    for name in ("machine", "endpoint", "model", "adapter"):
        _label(deployment[name])
    if deployment["id"] != _identity(deployment["machine"], deployment["endpoint"], deployment["model"]):
        raise ValueError("Invalid metrics identity")
    address = deployment["address"]
    if not isinstance(address, str) or address != _address(address):
        raise ValueError("Invalid metrics address")
    for name in ("successes", "failures", "slow_load_count"):
        if type(deployment[name]) is not int or not 0 <= deployment[name] <= _MAX_COUNT:
            raise ValueError("Invalid metrics counter")
    for name in ("input_tokens_total", "output_tokens_total"):
        value = deployment[name]
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= _MAX_TOTAL:
            raise ValueError("Invalid metrics total")
        if float(value).is_integer():
            deployment[name] = int(value)
    _timestamp(deployment["last_seen_at"])
    raw = deployment.pop("metrics_json")
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("Invalid metrics aggregates")
    metrics = json.loads(raw, object_pairs_hook=_unique_json)
    if not isinstance(metrics, dict) or set(metrics) != set(_METRICS):
        raise ValueError("Invalid metrics aggregates")
    for sample in metrics.values():
        if sample is None:
            continue
        if not isinstance(sample, dict) or set(sample) != {"latest", "ewma", "samples", "updated_at"}:
            raise ValueError("Invalid metrics aggregate")
        if _number(sample["latest"]) is None or _number(sample["ewma"]) is None:
            raise ValueError("Invalid metrics sample")
        if type(sample["samples"]) is not int or not 1 <= sample["samples"] <= _MAX_COUNT:
            raise ValueError("Invalid metrics sample count")
        _timestamp(sample["updated_at"])
    deployment["metrics"] = metrics
    deployment["slow_load_threshold_ms"] = SLOW_LOAD_THRESHOLD_MS
    return deployment


class MetricsStore:
    """SQLite metrics outside the managed venv, with fail-closed storage checks."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            override = os.environ.get("LLM_ROUTER_METRICS_FILE")
            state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
            path = Path(override) if override else state_home / "llm-router" / "metrics.sqlite3"
        self.path = Path(os.path.abspath(os.path.expanduser(path)))
        self._last_write_failed = False

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

    def _open_private(self, path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_DATABASE_BYTES
            ):
                raise RuntimeError(_ERROR)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _check_sidecars(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                fd = self._open_private(self.path.with_name(self.path.name + suffix), os.O_RDONLY)
            except FileNotFoundError:
                continue
            else:
                os.close(fd)

    @contextmanager
    def _lock(self):
        if fcntl is None:
            raise RuntimeError(_ERROR)
        self._directory(create=True)
        fd = self._open_private(self.path.with_name(self.path.name + ".lock"), os.O_CREAT | os.O_RDWR)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(_ERROR) from None
                    time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                else:
                    break
            yield
        finally:
            os.close(fd)

    def _connect(self, *, create: bool = False) -> tuple[sqlite3.Connection | None, bool]:
        if not self._directory(create=create):
            return None, False
        created = False
        try:
            fd = self._open_private(self.path, os.O_RDONLY)
        except FileNotFoundError:
            if not create:
                return None, False
            fd = self._open_private(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created = True
        os.close(fd)
        self._check_sidecars()
        mode = "rw" if create else "ro"
        connection = sqlite3.connect(
            f"file:{quote(str(self.path), safe='/')}?mode={mode}", uri=True,
            timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA mmap_size=0")
            if not create:
                connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return connection, created

    def _schema(self, connection: sqlite3.Connection, *, created: bool = False) -> None:
        if created:
            connection.execute(_SCHEMA)
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_VERSION}")
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
            raise RuntimeError(_ERROR)
        if connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION:
            raise RuntimeError(_ERROR)
        objects = connection.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if [tuple(item) for item in objects] != [("table", "deployments")]:
            raise RuntimeError(_ERROR)
        columns = connection.execute("PRAGMA table_info(deployments)").fetchall()
        if tuple(column["name"] for column in columns) != _COLUMNS:
            raise RuntimeError(_ERROR)
        stored_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='deployments'").fetchone()[0]
        if " ".join(stored_sql.split()).strip() != " ".join(_SCHEMA.split()).strip():
            raise RuntimeError(_ERROR)

    def record(
        self, endpoint: EndpointConfig, model: ModelConfig,
        observation: Mapping[str, float | int | None], *, success: bool,
    ) -> None:
        """Aggregate one actual upstream attempt; raises only safe storage errors."""
        try:
            if type(success) is not bool or not isinstance(observation, Mapping):
                raise ValueError("Invalid passive metrics observation.")
            machine = _label(endpoint.machine_id or endpoint.name)
            endpoint_name = _label(endpoint.name)
            upstream = _label(model.upstream_model)
            adapter = _label(endpoint.adapter)
            deployment_id = _identity(machine, endpoint_name, upstream)
            address = _address(endpoint.base_url)
            now = datetime.now(timezone.utc).isoformat()
            fields = {name: _number(observation.get(name)) for name in (*_METRICS, "input_tokens", "output_tokens")}
            with self._lock():
                connection, created = self._connect(create=True)
                assert connection is not None
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._schema(connection, created=created)
                    raw = connection.execute(
                        "SELECT * FROM deployments WHERE id=? OR (machine=? AND endpoint=? AND model=?)",
                        (deployment_id, machine, endpoint_name, upstream),
                    ).fetchone()
                    if raw is None:
                        if connection.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] >= MAX_DEPLOYMENTS:
                            raise RuntimeError(_ERROR)
                        deployment = {
                            "successes": 0, "failures": 0, "input_tokens_total": 0, "output_tokens_total": 0,
                            "slow_load_count": 0, "metrics": {name: None for name in _METRICS},
                        }
                    else:
                        deployment = _decode(raw)
                    counter = "successes" if success else "failures"
                    deployment[counter] = min(_MAX_COUNT, deployment[counter] + 1)
                    if success:
                        for name in ("input_tokens", "output_tokens"):
                            if fields[name] is not None:
                                target = name + "_total"
                                deployment[target] = min(_MAX_TOTAL, deployment[target] + fields[name])
                        if fields["load_duration_ms"] is not None and fields["load_duration_ms"] >= SLOW_LOAD_THRESHOLD_MS:
                            deployment["slow_load_count"] = min(_MAX_COUNT, deployment["slow_load_count"] + 1)
                    for name in _METRICS:
                        value = fields[name]
                        if value is None or (not success and name != "request_duration_ms"):
                            continue
                        previous = deployment["metrics"][name]
                        deployment["metrics"][name] = {
                            "latest": value,
                            "ewma": value if previous is None else previous["ewma"] + 0.2 * (value - previous["ewma"]),
                            "samples": 1 if previous is None else min(_MAX_COUNT, previous["samples"] + 1),
                            "updated_at": now,
                        }
                    connection.execute(
                        "INSERT OR REPLACE INTO deployments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            deployment_id, machine, endpoint_name, upstream, address, adapter,
                            deployment["successes"], deployment["failures"], deployment["input_tokens_total"],
                            deployment["output_tokens_total"], now,
                            json.dumps(deployment["metrics"], separators=(",", ":"), allow_nan=False),
                            deployment["slow_load_count"],
                        ),
                    )
                    connection.execute("COMMIT")
                    self._last_write_failed = False
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            self._last_write_failed = True
            raise RuntimeError(_ERROR) from None

    def snapshot(self) -> dict:
        """Read private persisted aggregates; an absent store creates no files."""
        empty = {
            "available": not self._last_write_failed,
            "error": _ERROR if self._last_write_failed else None,
            "updated_at": None, "deployments": [],
        }
        try:
            connection, _ = self._connect()
            if connection is None:
                return empty
            try:
                connection.execute("BEGIN")
                self._schema(connection)
                rows = connection.execute("SELECT * FROM deployments ORDER BY machine, endpoint, model LIMIT ?", (MAX_DEPLOYMENTS + 1,)).fetchall()
                if len(rows) > MAX_DEPLOYMENTS:
                    raise RuntimeError(_ERROR)
                deployments = [_decode(row) for row in rows]
            finally:
                connection.close()
            return {
                **empty, "deployments": deployments,
                "updated_at": max((row["last_seen_at"] for row in deployments), default=None),
            }
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            return {**empty, "available": False, "error": _ERROR}
