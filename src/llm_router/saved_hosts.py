"""Private saved addresses and short, metadata-only server checks.

An address is an explicit administrator-supplied destination, not a subnet or
scan range. These checks never enroll a backend or invoke/load a model.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Mapping
from urllib.parse import urlsplit
import weakref

import httpx

try:
    import fcntl
except ImportError:  # Normal gateway imports must still work off POSIX.
    fcntl = None


MAX_HOSTS = 16
MAX_RESPONSE_BYTES = 1024 * 1024
PROBE_TIMEOUT_SECONDS = 1.5
_MAX_STATE_BYTES = 32 * 1024
_SEMAPHORES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _ip_authority(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    effective = getattr(address, "ipv4_mapped", None) or address
    if effective.is_unspecified or effective.is_multicast or effective.is_link_local or str(effective) == "255.255.255.255":
        raise ValueError("Use a unicast LAN/VPN address, not an unspecified, multicast, broadcast, or link-local address.")
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def normalize_address(address: str) -> str:
    """Return one canonical host or explicit HTTP origin without credentials."""
    if not isinstance(address, str) or not address or len(address) > 256:
        raise ValueError("Enter a single IP address or hostname (at most 256 characters).")
    if any(ord(char) < 32 or ord(char) == 127 for char in address):
        raise ValueError("Addresses cannot contain control characters.")
    address = address.strip()
    if not address or any(char.isspace() for char in address):
        raise ValueError("Enter one address without spaces.")
    if any(char in address for char in "\\?#@%"):
        raise ValueError("Credentials, query strings, fragments, and escaped addresses are not supported.")

    explicit = "://" in address
    if not explicit:
        try:
            bare_ip = ipaddress.ip_address(address)
        except ValueError:
            pass
        else:
            return _ip_authority(bare_ip)
    try:
        parsed = urlsplit(address if explicit else "//" + address)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("Enter a valid IP address, hostname, and optional port.") from None
    if explicit and parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http:// and https:// addresses are supported.")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Enter an address without credentials.")
    if parsed.path not in {"", "/", "/v1", "/v1/"} or (not explicit and parsed.path):
        raise ValueError("Use a single host, host:port, or HTTP URL with an optional /v1 path; not a subnet or range.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Ports must be between 1 and 65535.")
    if parsed.netloc.endswith(":"):
        raise ValueError("A port number must follow the colon.")
    try:
        host_ip = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname.lower()
        if hostname.endswith("."):
            hostname = hostname[:-1]
        labels = hostname.split(".")
        if (
            len(hostname) > 253
            or all(label.isdigit() for label in labels)
            or re.fullmatch(r"[0-9.-]+", hostname)
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        ):
            raise ValueError("Enter a valid IP address or DNS hostname.") from None
        authority = hostname
    else:
        authority = _ip_authority(host_ip)
    if not explicit and port is None:
        return authority
    scheme = parsed.scheme.lower() if explicit else "http"
    if port is not None and port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return f"{scheme}://{authority}"


def _entry(address: str) -> dict[str, str]:
    return {"id": hashlib.sha256(address.encode("utf-8")).hexdigest()[:24], "address": address}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class SavedHostStore:
    """Small, atomic, owner-only address list; construction has no side effects."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            override = os.environ.get("LLM_ROUTER_SAVED_HOSTS_FILE")
            config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            path = Path(override) if override else config_home / "llm-router" / "saved-hosts.json"
        self.path = Path(os.path.abspath(os.path.expanduser(path)))

    def _directory(self, *, create: bool = False) -> bool:
        parent = self.path.parent
        # Refuse symlinked ancestors as well as a symlinked state file. Do not
        # resolve() away the evidence before this check.
        for directory in (*reversed(parent.parents), parent):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("Saved-address storage requires real directories, not symlinks.")
        info = parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Saved-address storage directory must be owned by this user and private (0700).")
        return True

    def _open(self, path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RuntimeError("Saved-address storage must be a private, owner-held, single-link regular file.")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def list(self) -> list[dict[str, str]]:
        if not self._directory():
            return []
        try:
            fd = self._open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
        try:
            if len(raw) > _MAX_STATE_BYTES:
                raise ValueError
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "hosts"}
                or type(payload["version"]) is not int
                or payload["version"] != 1
            ):
                raise ValueError
            hosts = payload["hosts"]
            if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS:
                raise ValueError
            seen = set()
            for host in hosts:
                if not isinstance(host, dict) or set(host) != {"id", "address"}:
                    raise ValueError
                canonical = normalize_address(host["address"])
                if host != _entry(canonical) or canonical in seen:
                    raise ValueError
                seen.add(canonical)
            return hosts
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise RuntimeError("Saved-address storage is invalid; it was not changed. Repair or restore the file first.") from None

    @contextmanager
    def _locked(self):
        if fcntl is None:
            raise RuntimeError("Saved-address storage requires POSIX file locking.")
        self._directory(create=True)
        fd = self._open(self.path.with_name(self.path.name + ".lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _write(self, hosts: list[dict[str, str]]) -> None:
        raw = (json.dumps({"version": 1, "hosts": hosts}, indent=2) + "\n").encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=".saved-hosts-", dir=self.path.parent)
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Validate existing state again before replacing it. Never replace an
            # unsafe link or corrupt file, even if an external writer changed it.
            self.list()
            os.replace(staged, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            staged.unlink(missing_ok=True)

    def add(self, address: str) -> dict[str, str]:
        host = _entry(normalize_address(address))
        with self._locked():
            hosts = self.list()
            if host in hosts:
                return host
            if len(hosts) >= MAX_HOSTS:
                raise ValueError(f"Save at most {MAX_HOSTS} addresses; remove an address before adding another.")
            hosts.append(host)
            self._write(hosts)
        return host

    def remove(self, host_id: str) -> bool:
        if not isinstance(host_id, str) or not re.fullmatch(r"[0-9a-f]{24}", host_id):
            raise ValueError("Invalid saved-address identifier.")
        if not self._directory():
            return False
        with self._locked():
            hosts = self.list()
            kept = [host for host in hosts if host["id"] != host_id]
            if len(hosts) == len(kept):
                return False
            self._write(kept)
            return True


def _probe_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(8)
        _SEMAPHORES[loop] = semaphore
    return semaphore


async def _check(provider: str, origin: str, path: str, *, transport: httpx.AsyncBaseTransport | None) -> dict:
    result = {
        "provider": provider,
        "base_url": origin if provider == "Ollama" else origin + "/v1",
        "status": "fail",
        "detail": "Server check failed.",
        "http_status": None,
        "elapsed_ms": 0,
    }

    async def fetch() -> None:
        # Separate clients prevent response cookies from being reused, even when
        # the two APIs share a host. No configured router/backend key is used.
        async with httpx.AsyncClient(
            transport=transport,
            timeout=PROBE_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as client:
            async with client.stream("GET", origin + path) as response:
                result["http_status"] = response.status_code
                if response.status_code != 200:
                    result["detail"] = (
                        "Authentication required; this checker does not send credentials."
                        if response.status_code in {401, 403}
                        else f"Metadata endpoint returned HTTP {response.status_code}; redirects are not followed."
                    )
                    return
                # Reject compressed responses before reading: httpx decodes a
                # compressed chunk before yielding it, so a decoded-size check
                # alone cannot prevent a highly compressed allocation bomb.
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise ValueError
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise ValueError
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
                if provider == "Ollama":
                    version = payload.get("version")
                    if not isinstance(version, str) or not version.strip():
                        raise ValueError
                    result["detail"] = "Ollama version endpoint responded; no model was invoked."
                else:
                    if not isinstance(payload.get("data"), list):
                        raise ValueError
                    result["detail"] = "OpenAI-compatible model list responded; this does not uniquely identify LM Studio."
                result["status"] = "pass"

    async with _probe_semaphore():
        started = time.monotonic()
        try:
            await asyncio.wait_for(fetch(), timeout=PROBE_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            result["detail"] = "No complete response within the short check timeout."
        except httpx.HTTPError:
            result["detail"] = "Could not connect securely or complete the metadata request."
        except (ValueError, UnicodeError, RecursionError):
            result["detail"] = "Response was not a valid, bounded metadata response."
        except Exception:
            # Transport exception text can contain private URLs, keys, or bodies.
            result["detail"] = "Metadata request failed."
        finally:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


async def check_saved_host(entry: Mapping, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Probe two fixed metadata URLs concurrently; never send inference requests."""
    address = normalize_address(entry["address"])
    if "://" in address:
        ollama_origin = openai_origin = address
    else:
        ollama_origin = f"http://{address}:11434"
        openai_origin = f"http://{address}:1234"
    checks = await asyncio.gather(
        _check("Ollama", ollama_origin, "/api/version", transport=transport),
        _check("LM Studio / OpenAI-compatible", openai_origin, "/v1/models", transport=transport),
    )
    return {
        "id": entry["id"],
        "address": address,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
