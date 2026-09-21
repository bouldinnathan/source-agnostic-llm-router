"""Private persistent aggregates from real routed requests, never active tests.

Only allowlisted numbers and deployment identity are stored. Reported model-load
durations of at least one second are counted as slow loads; that is a timing
heuristic, not confirmation that a disk read occurred.

Client traffic is counted per request, not per upstream attempt: one request that
fails over to a second server is one request, one reroute, and one failed attempt
of a classified kind. Hourly buckets are kept for 30 days plus all-time totals.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Iterable, Mapping
import unicodedata
from urllib.parse import quote, urlsplit

from .errors import FAILURE_KINDS
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
_VERSION = 2
TRAFFIC_RETENTION_HOURS = 24 * 30
_TRAFFIC_WINDOWS = {"24h": 24, "7d": 24 * 7}
_TRAFFIC_HOURLY_ROWS = 24 * 7
_MAX_FAILURE_KINDS_PER_REQUEST = 64
_TRAFFIC_COUNTERS = ("requests_ok", "requests_failed", "reroutes_ok", "reroutes_failed")
_TRAFFIC_TOKENS = ("input_tokens", "output_tokens")
_METRICS = (
    "input_tokens_per_second", "output_tokens_per_second", "load_duration_ms", "request_duration_ms",
    "first_token_ms",
)
# Rows written before time-to-first-token existed lack that key; every other key is required.
_OPTIONAL_METRICS = {"first_token_ms"}
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
_TRAFFIC_HOURLY_COLUMNS = ("hour", *_TRAFFIC_COUNTERS, *_TRAFFIC_TOKENS, "failures_json")
_TRAFFIC_HOURLY_SCHEMA = """
CREATE TABLE traffic_hourly (
    hour TEXT PRIMARY KEY NOT NULL,
    requests_ok INTEGER NOT NULL,
    requests_failed INTEGER NOT NULL,
    reroutes_ok INTEGER NOT NULL,
    reroutes_failed INTEGER NOT NULL,
    input_tokens REAL NOT NULL,
    output_tokens REAL NOT NULL,
    failures_json TEXT NOT NULL
)
"""
_TRAFFIC_TOTALS_COLUMNS = ("id", "since", *_TRAFFIC_COUNTERS, *_TRAFFIC_TOKENS, "failures_json")
_TRAFFIC_TOTALS_SCHEMA = """
CREATE TABLE traffic_totals (
    id INTEGER PRIMARY KEY NOT NULL,
    since TEXT NOT NULL,
    requests_ok INTEGER NOT NULL,
    requests_failed INTEGER NOT NULL,
    reroutes_ok INTEGER NOT NULL,
    reroutes_failed INTEGER NOT NULL,
    input_tokens REAL NOT NULL,
    output_tokens REAL NOT NULL,
    failures_json TEXT NOT NULL
)
"""
_TABLES = {
    "deployments": (_COLUMNS, _SCHEMA),
    "traffic_hourly": (_TRAFFIC_HOURLY_COLUMNS, _TRAFFIC_HOURLY_SCHEMA),
    "traffic_totals": (_TRAFFIC_TOTALS_COLUMNS, _TRAFFIC_TOTALS_SCHEMA),
}
_LEGACY_TABLES = ("deployments",)
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
    if not isinstance(metrics, dict) or not (set(_METRICS) - _OPTIONAL_METRICS <= set(metrics) <= set(_METRICS)):
        raise ValueError("Invalid metrics aggregates")
    for name in _OPTIONAL_METRICS:
        metrics.setdefault(name, None)
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


def _hour_start(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def _hour(value: object) -> str:
    text = _timestamp(value)
    parsed = datetime.fromisoformat(text)
    if parsed.utcoffset() != timedelta(0) or parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("Invalid traffic hour")
    return text


def _kind(value: object) -> str:
    return value if isinstance(value, str) and value in FAILURE_KINDS else "other"


def _empty_bucket() -> dict:
    return {**{name: 0 for name in _TRAFFIC_COUNTERS}, **{name: 0 for name in _TRAFFIC_TOKENS}, "failures": {}}


def _decode_bucket(row: sqlite3.Row) -> dict:
    bucket = dict(row)
    for name in _TRAFFIC_COUNTERS:
        if type(bucket[name]) is not int or not 0 <= bucket[name] <= _MAX_COUNT:
            raise ValueError("Invalid traffic counter")
    for name in _TRAFFIC_TOKENS:
        value = bucket[name]
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= _MAX_TOTAL:
            raise ValueError("Invalid traffic total")
        if float(value).is_integer():
            bucket[name] = int(value)
    raw = bucket.pop("failures_json")
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("Invalid traffic failures")
    failures = json.loads(raw, object_pairs_hook=_unique_json)
    if not isinstance(failures, dict) or not set(failures) <= set(FAILURE_KINDS):
        raise ValueError("Invalid traffic failures")
    for count in failures.values():
        if type(count) is not int or not 1 <= count <= _MAX_COUNT:
            raise ValueError("Invalid traffic failure count")
    bucket["failures"] = failures
    return bucket


def _decode_hourly(row: sqlite3.Row) -> dict:
    bucket = _decode_bucket(row)
    bucket["hour"] = _hour(bucket["hour"])
    return bucket


def _decode_totals(row: sqlite3.Row) -> dict:
    bucket = _decode_bucket(row)
    if bucket.pop("id") != 1:
        raise ValueError("Invalid traffic totals row")
    _timestamp(bucket["since"])
    return bucket


def _sum_buckets(buckets: Iterable[dict]) -> dict:
    total = _empty_bucket()
    for bucket in buckets:
        for name in _TRAFFIC_COUNTERS:
            total[name] = min(_MAX_COUNT, total[name] + bucket[name])
        for name in _TRAFFIC_TOKENS:
            total[name] = min(_MAX_TOTAL, total[name] + bucket[name])
        for kind, count in bucket["failures"].items():
            total["failures"][kind] = min(_MAX_COUNT, total["failures"].get(kind, 0) + count)
    return total


def _bucket_values(bucket: dict) -> tuple:
    return (
        *(bucket[name] for name in _TRAFFIC_COUNTERS), *(bucket[name] for name in _TRAFFIC_TOKENS),
        json.dumps(bucket["failures"], separators=(",", ":"), sort_keys=True, allow_nan=False),
    )


def _empty_traffic(*, available: bool = True) -> dict:
    return {
        "available": available,
        "retention_hours": TRAFFIC_RETENTION_HOURS,
        "since": None,
        "totals": _empty_bucket(),
        "windows": {label: _empty_bucket() for label in _TRAFFIC_WINDOWS},
        "hourly": [],
    }


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

    def _schema(self, connection: sqlite3.Connection, *, created: bool = False, writable: bool = False) -> int:
        """Validate the exact expected layout; only a known older layout is upgraded.

        A version-1 database (deployments only) is read as-is and gains the traffic
        tables inside the caller's write transaction. Anything else is refused
        without modification. Returns the layout version now in effect.
        """
        if created:
            for _, schema in _TABLES.values():
                connection.execute(schema)
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_VERSION}")
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
            raise RuntimeError(_ERROR)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            self._check_tables(connection, _LEGACY_TABLES)
            if not writable:
                return 1
            for name, (_, schema) in _TABLES.items():
                if name not in _LEGACY_TABLES:
                    connection.execute(schema)
            connection.execute(f"PRAGMA user_version={_VERSION}")
            version = _VERSION
        if version != _VERSION:
            raise RuntimeError(_ERROR)
        self._check_tables(connection, tuple(_TABLES))
        return version

    @staticmethod
    def _check_tables(connection: sqlite3.Connection, names: tuple[str, ...]) -> None:
        objects = connection.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if sorted(tuple(item) for item in objects) != sorted(("table", name) for name in names):
            raise RuntimeError(_ERROR)
        for name in names:
            columns, schema = _TABLES[name]
            info = connection.execute(f"PRAGMA table_info({name})").fetchall()
            if tuple(column["name"] for column in info) != columns:
                raise RuntimeError(_ERROR)
            stored_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            if " ".join(stored_sql.split()).strip() != " ".join(schema.split()).strip():
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
                    self._schema(connection, created=created, writable=True)
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

    def record_request(
        self, *, success: bool, rerouted: bool,
        input_tokens: float | int | None = None, output_tokens: float | int | None = None,
        failure_kinds: Iterable[str] = (),
    ) -> None:
        """Count one finished client request in the current hour and all-time totals.

        Tokens are added only for successful requests. Each failed upstream attempt
        contributes one failure of its kind, so a rescued request still records the
        attempt that made the reroute necessary. Buckets older than the retention
        window are pruned here; reads never modify the database.
        """
        try:
            if type(success) is not bool or type(rerouted) is not bool:
                raise ValueError("Invalid traffic observation.")
            kinds = [_kind(kind) for kind in list(failure_kinds)[:_MAX_FAILURE_KINDS_PER_REQUEST]]
            tokens = {"input_tokens": _number(input_tokens), "output_tokens": _number(output_tokens)}
            now = datetime.now(timezone.utc)
            stamp = now.isoformat()
            hour = _hour_start(now)
            cutoff = _hour_start(now - timedelta(hours=TRAFFIC_RETENTION_HOURS))
            with self._lock():
                connection, created = self._connect(create=True)
                assert connection is not None
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._schema(connection, created=created, writable=True)
                    raw_hour = connection.execute("SELECT * FROM traffic_hourly WHERE hour=?", (hour,)).fetchone()
                    bucket = _empty_bucket() if raw_hour is None else _decode_hourly(raw_hour)
                    raw_totals = self._totals_row(connection)
                    totals = {**_empty_bucket(), "since": stamp} if raw_totals is None else _decode_totals(raw_totals)
                    for target in (bucket, totals):
                        counter = "requests_ok" if success else "requests_failed"
                        target[counter] = min(_MAX_COUNT, target[counter] + 1)
                        if rerouted:
                            counter = "reroutes_ok" if success else "reroutes_failed"
                            target[counter] = min(_MAX_COUNT, target[counter] + 1)
                        if success:
                            for name, value in tokens.items():
                                if value is not None:
                                    target[name] = min(_MAX_TOTAL, target[name] + value)
                        for kind in kinds:
                            target["failures"][kind] = min(_MAX_COUNT, target["failures"].get(kind, 0) + 1)
                    connection.execute(
                        "INSERT OR REPLACE INTO traffic_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (hour, *_bucket_values(bucket)),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO traffic_totals VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (totals["since"], *_bucket_values(totals)),
                    )
                    connection.execute("DELETE FROM traffic_hourly WHERE hour < ?", (cutoff,))
                    connection.execute("COMMIT")
                    self._last_write_failed = False
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            self._last_write_failed = True
            raise RuntimeError(_ERROR) from None

    @staticmethod
    def _totals_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        rows = connection.execute("SELECT * FROM traffic_totals LIMIT 2").fetchall()
        if len(rows) > 1:
            raise RuntimeError(_ERROR)
        return rows[0] if rows else None

    @staticmethod
    def _read_traffic(connection: sqlite3.Connection, now: datetime) -> dict:
        cutoff = _hour_start(now - timedelta(hours=_TRAFFIC_HOURLY_ROWS - 1))
        rows = connection.execute(
            "SELECT * FROM traffic_hourly WHERE hour >= ? ORDER BY hour LIMIT ?", (cutoff, _TRAFFIC_HOURLY_ROWS + 1),
        ).fetchall()
        if len(rows) > _TRAFFIC_HOURLY_ROWS:
            raise RuntimeError(_ERROR)
        hourly = [_decode_hourly(row) for row in rows]
        raw_totals = MetricsStore._totals_row(connection)
        totals = None if raw_totals is None else _decode_totals(raw_totals)
        windows = {}
        for label, hours in _TRAFFIC_WINDOWS.items():
            start = _hour_start(now - timedelta(hours=hours - 1))
            windows[label] = _sum_buckets(bucket for bucket in hourly if bucket["hour"] >= start)
        traffic = _empty_traffic()
        traffic["since"] = None if totals is None else totals.pop("since")
        traffic["totals"] = _empty_bucket() if totals is None else totals
        traffic["windows"] = windows
        traffic["hourly"] = hourly
        return traffic

    def snapshot(self) -> dict:
        """Read private persisted aggregates; an absent store creates no files."""
        empty = {
            "available": not self._last_write_failed,
            "error": _ERROR if self._last_write_failed else None,
            "updated_at": None, "deployments": [],
            "traffic": _empty_traffic(available=not self._last_write_failed),
        }
        try:
            connection, _ = self._connect()
            if connection is None:
                return empty
            try:
                connection.execute("BEGIN")
                version = self._schema(connection)
                rows = connection.execute("SELECT * FROM deployments ORDER BY machine, endpoint, model LIMIT ?", (MAX_DEPLOYMENTS + 1,)).fetchall()
                if len(rows) > MAX_DEPLOYMENTS:
                    raise RuntimeError(_ERROR)
                deployments = [_decode(row) for row in rows]
                # A not-yet-upgraded database simply has no traffic history yet.
                traffic = self._read_traffic(connection, datetime.now(timezone.utc)) if version == _VERSION else empty["traffic"]
            finally:
                connection.close()
            return {
                **empty, "deployments": deployments, "traffic": traffic,
                "updated_at": max((row["last_seen_at"] for row in deployments), default=None),
            }
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            return {**empty, "available": False, "error": _ERROR, "traffic": _empty_traffic(available=False)}
