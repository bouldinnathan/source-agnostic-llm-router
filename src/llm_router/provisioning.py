"""Resource-aware, failure-isolated provisioning for local Ollama models."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx


GIB = 1024**3


@dataclass(frozen=True, slots=True)
class ProvisioningCandidate:
    """A bounded model choice with conservative host requirements."""

    model: str
    download_bytes: int
    minimum_memory_bytes: int
    minimum_cpu_count: int


# Sizes track the published Ollama artifacts. Resource requirements deliberately
# include headroom for inference and the operating system; they are admission
# controls rather than performance guarantees.
DEFAULT_CANDIDATES: tuple[ProvisioningCandidate, ...] = (
    ProvisioningCandidate("qwen3.5:0.8b", int(1.0 * GIB), 3 * GIB, 2),
    ProvisioningCandidate("qwen3.5:2b", int(2.7 * GIB), 6 * GIB, 4),
    ProvisioningCandidate("qwen3.5:4b", int(3.4 * GIB), 8 * GIB, 4),
    ProvisioningCandidate("qwen3.5:9b", int(6.6 * GIB), 16 * GIB, 8),
    ProvisioningCandidate("qwen3.5:27b", int(17.0 * GIB), 36 * GIB, 12),
)


@dataclass(frozen=True, slots=True)
class HardwareResources:
    cpu_count: int | None
    available_memory_bytes: int | None
    free_disk_bytes: int | None
    disk_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_count": self.cpu_count,
            "available_memory_gib": _gib(self.available_memory_bytes),
            "free_disk_gib": _gib(self.free_disk_bytes),
            "disk_path": self.disk_path,
        }


@dataclass(frozen=True, slots=True)
class ProvisioningSettings:
    enabled: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    allow_remote: bool = False
    priority: str = "balanced"
    preferred_models: tuple[str, ...] = ()
    max_download_bytes: int = 8 * GIB
    reserve_disk_bytes: int = 5 * GIB
    probe_timeout_seconds: float = 2.0
    pull_timeout_seconds: float = 1_800.0

    @classmethod
    def from_env(cls) -> "ProvisioningSettings":
        priority = os.environ.get("LLM_ROUTER_PROVISION_PRIORITY", "balanced").strip().lower()
        if priority not in {"quality", "balanced", "smallest"}:
            priority = "balanced"
        return cls(
            enabled=_env_bool("LLM_ROUTER_AUTO_PROVISION", True),
            ollama_url=_normalized_url(
                os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
            ),
            allow_remote=_env_bool("LLM_ROUTER_PROVISION_REMOTE", False),
            priority=priority,
            preferred_models=_env_list("LLM_ROUTER_PROVISION_MODELS"),
            max_download_bytes=int(
                _env_float("LLM_ROUTER_PROVISION_MAX_GB", 8.0, minimum=0.5) * GIB
            ),
            reserve_disk_bytes=int(
                _env_float("LLM_ROUTER_PROVISION_DISK_RESERVE_GB", 5.0, minimum=0.0)
                * GIB
            ),
            probe_timeout_seconds=_env_float(
                "LLM_ROUTER_PROVISION_PROBE_TIMEOUT", 2.0, minimum=0.1
            ),
            pull_timeout_seconds=_env_float(
                "LLM_ROUTER_PROVISION_PULL_TIMEOUT", 1_800.0, minimum=30.0
            ),
        )


@dataclass(frozen=True, slots=True)
class ProvisioningReport:
    status: str
    endpoint: str
    reason: str
    selected_model: str | None = None
    resources: HardwareResources | None = None
    existing_models: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"installed", "already_available", "planned"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "endpoint": self.endpoint,
            "selected_model": self.selected_model,
            "reason": self.reason,
            "existing_models": list(self.existing_models),
        }
        if self.resources is not None:
            payload["resources"] = self.resources.to_dict()
        return payload


class OllamaProvisioner:
    """Install one suitable Ollama model without making discovery fragile."""

    def __init__(
        self,
        settings: ProvisioningSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resource_provider: Callable[[], HardwareResources] | None = None,
        candidates: tuple[ProvisioningCandidate, ...] = DEFAULT_CANDIDATES,
    ) -> None:
        self.settings = settings or ProvisioningSettings.from_env()
        self.transport = transport
        self.resource_provider = resource_provider or detect_hardware_resources
        self.candidates = candidates
        self._lock = asyncio.Lock()

    async def provision(
        self,
        *,
        dry_run: bool = False,
        requested_model: str | None = None,
        allow_remote: bool | None = None,
    ) -> ProvisioningReport:
        async with self._lock:
            return await self._provision(
                dry_run=dry_run,
                requested_model=requested_model,
                allow_remote=allow_remote,
            )

    async def _provision(
        self,
        *,
        dry_run: bool,
        requested_model: str | None,
        allow_remote: bool | None,
    ) -> ProvisioningReport:
        endpoint = self.settings.ollama_url.rstrip("/")
        if not self.settings.enabled:
            return ProvisioningReport("skipped", endpoint, "automatic provisioning is disabled")
        remote_allowed = self.settings.allow_remote if allow_remote is None else allow_remote
        if not _is_loopback_url(endpoint) and not remote_allowed:
            return ProvisioningReport(
                "skipped",
                endpoint,
                "remote provisioning is disabled because remote hardware cannot be measured",
            )

        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                limits=limits,
            ) as client:
                existing = await self._existing_models(client, endpoint)
                if requested_model and requested_model in existing:
                    return ProvisioningReport(
                        "already_available",
                        endpoint,
                        "the requested model is already installed",
                        selected_model=requested_model,
                        existing_models=existing,
                    )
                if not requested_model and await self._has_tool_model(
                    client, endpoint, existing
                ):
                    return ProvisioningReport(
                        "already_available",
                        endpoint,
                        "a tool-capable local chat model is already installed",
                        existing_models=existing,
                    )

                resources = self.resource_provider()
                candidate, reason = choose_candidate(
                    resources,
                    self.settings,
                    self.candidates,
                    requested_model=requested_model,
                    remote=_is_remote_url(endpoint),
                )
                if candidate is None:
                    return ProvisioningReport(
                        "skipped",
                        endpoint,
                        reason,
                        resources=resources,
                        existing_models=existing,
                    )
                if dry_run:
                    return ProvisioningReport(
                        "planned",
                        endpoint,
                        "resource checks passed; no model was downloaded in dry-run mode",
                        selected_model=candidate.model,
                        resources=resources,
                        existing_models=existing,
                    )

                response = await client.post(
                    endpoint + "/api/pull",
                    json={"model": candidate.model, "stream": False},
                    timeout=self.settings.pull_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("pull response is not an object")
                status = str(payload.get("status", "")).strip().lower()
                if status not in {"success", "complete", "completed"}:
                    raise ValueError("pull did not report success")
                return ProvisioningReport(
                    "installed",
                    endpoint,
                    "model download completed",
                    selected_model=candidate.model,
                    resources=resources,
                    existing_models=existing,
                )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            message = (
                "Ollama rejected authentication"
                if exc.response.status_code in {401, 403}
                else f"Ollama returned HTTP {exc.response.status_code}"
            )
        except (httpx.TimeoutException, TimeoutError):
            message = "Ollama provisioning timed out"
        except (httpx.NetworkError, ConnectionError):
            message = "Ollama is unreachable"
        except (ValueError, TypeError, KeyError):
            message = "Ollama returned an invalid provisioning response"
        except httpx.HTTPError as exc:
            message = f"Ollama client failure ({type(exc).__name__})"
        except Exception as exc:
            message = f"provisioning failed ({type(exc).__name__})"
        return ProvisioningReport("failed", endpoint, message)

    async def _existing_models(
        self, client: httpx.AsyncClient, endpoint: str
    ) -> tuple[str, ...]:
        response = await client.get(
            endpoint + "/api/tags", timeout=self.settings.probe_timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("models", []), list):
            raise ValueError("model list is invalid")
        models: list[str] = []
        for entry in payload.get("models", []):
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("model", entry.get("name"))
            if isinstance(value, str) and value.strip():
                models.append(value.strip())
        return tuple(dict.fromkeys(models))

    async def _has_tool_model(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        models: tuple[str, ...],
    ) -> bool:
        for model in models[:25]:
            if not _looks_like_chat_model(model):
                continue
            try:
                response = await client.post(
                    endpoint + "/api/show",
                    json={"model": model},
                    timeout=self.settings.probe_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, Mapping):
                    capabilities = payload.get("capabilities", [])
                    if isinstance(capabilities, list) and any(
                        str(value).lower() in {"tools", "tool", "tool_use"}
                        for value in capabilities
                    ):
                        return True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if _known_tool_family(model):
                return True
        return False


def choose_candidate(
    resources: HardwareResources,
    settings: ProvisioningSettings,
    candidates: tuple[ProvisioningCandidate, ...] = DEFAULT_CANDIDATES,
    *,
    requested_model: str | None = None,
    remote: bool = False,
) -> tuple[ProvisioningCandidate | None, str]:
    """Choose a model deterministically from explicit priority and host limits."""

    by_name = {candidate.model: candidate for candidate in candidates}
    if requested_model:
        candidate = by_name.get(requested_model)
        if candidate is None:
            choices = ", ".join(by_name)
            return None, f"unknown provisionable model '{requested_model}'; choose one of: {choices}"
        ordered = [candidate]
    elif settings.preferred_models:
        ordered = [
            by_name[name] for name in settings.preferred_models if name in by_name
        ]
        if not ordered:
            return None, "none of LLM_ROUTER_PROVISION_MODELS are known bounded model choices"
    elif settings.priority == "smallest":
        ordered = list(candidates)
    elif settings.priority == "quality":
        ordered = list(reversed(candidates))
    else:
        # Balanced intentionally caps the automatic tier at 9B even when the
        # operator raises the byte limit; quality mode can select the 27B tier.
        ordered = list(reversed(candidates[:-1]))

    if remote:
        # The caller explicitly allowed a remote pull. Local measurements cannot
        # describe that host, so use the smallest requested/allowed choice and
        # let the remote Ollama server enforce its own storage constraints.
        candidate = min(ordered, key=lambda item: item.download_bytes)
        if candidate.download_bytes > settings.max_download_bytes:
            return None, "selected model exceeds LLM_ROUTER_PROVISION_MAX_GB"
        return candidate, "remote provisioning explicitly allowed"

    if resources.cpu_count is None or resources.cpu_count < 1:
        return None, "CPU capacity could not be measured safely"
    if resources.available_memory_bytes is None:
        return None, "available memory could not be measured safely"
    if resources.free_disk_bytes is None:
        return None, "free storage could not be measured safely"

    rejection_reasons: list[str] = []
    for candidate in ordered:
        if candidate.download_bytes > settings.max_download_bytes:
            rejection_reasons.append(f"{candidate.model}: exceeds maximum download size")
            continue
        required_disk = candidate.download_bytes + settings.reserve_disk_bytes
        if resources.free_disk_bytes < required_disk:
            rejection_reasons.append(f"{candidate.model}: insufficient free storage")
            continue
        if resources.available_memory_bytes < candidate.minimum_memory_bytes:
            rejection_reasons.append(f"{candidate.model}: insufficient available memory")
            continue
        if resources.cpu_count < candidate.minimum_cpu_count:
            rejection_reasons.append(f"{candidate.model}: insufficient CPU capacity")
            continue
        return candidate, "resource checks passed"
    reason = rejection_reasons[0] if rejection_reasons else "no provisioning candidate is enabled"
    return None, reason


def detect_hardware_resources() -> HardwareResources:
    """Collect conservative Linux-friendly host resources without extra dependencies."""

    memory = _linux_available_memory()
    if memory is None:
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            available_pages = os.sysconf("SC_AVPHYS_PAGES")
            memory = int(page_size) * int(available_pages)
        except (AttributeError, OSError, TypeError, ValueError):
            memory = None
    cgroup_memory = _cgroup_available_memory()
    if cgroup_memory is not None:
        memory = min(memory, cgroup_memory) if memory is not None else cgroup_memory

    disk_path = _disk_probe_path()
    try:
        free_disk = shutil.disk_usage(disk_path).free
    except OSError:
        free_disk = None
    return HardwareResources(
        cpu_count=_effective_cpu_count(),
        available_memory_bytes=memory,
        free_disk_bytes=free_disk,
        disk_path=str(disk_path),
    )


def _linux_available_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def _effective_cpu_count() -> int | None:
    limits = [value for value in (os.cpu_count(), _cgroup_cpu_quota(), _cpuset_count()) if value]
    return min(limits) if limits else None


def _cgroup_cpu_quota() -> int | None:
    value = _read_text(Path("/sys/fs/cgroup/cpu.max"))
    if value:
        parts = value.split()
        if len(parts) >= 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return max(1, quota // period)
            except ValueError:
                pass
    quota = _read_positive_int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    period = _read_positive_int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    if quota is not None and period is not None:
        return max(1, quota // period)
    return None


def _cpuset_count() -> int | None:
    for path in (
        Path("/sys/fs/cgroup/cpuset.cpus.effective"),
        Path("/sys/fs/cgroup/cpuset/cpuset.cpus"),
    ):
        value = _read_text(path)
        if not value:
            continue
        count = 0
        try:
            for group in value.split(","):
                bounds = group.strip().split("-", 1)
                start = int(bounds[0])
                end = int(bounds[-1])
                count += end - start + 1
        except (ValueError, IndexError):
            continue
        if count > 0:
            return count
    return None


def _cgroup_available_memory() -> int | None:
    for limit_path, usage_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        limit_text = _read_text(limit_path)
        usage = _read_positive_int(usage_path, allow_zero=True)
        if not limit_text or limit_text == "max" or usage is None:
            continue
        try:
            limit = int(limit_text)
        except ValueError:
            continue
        if 0 < limit < 1 << 60:
            return max(0, limit - usage)
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_positive_int(path: Path, *, allow_zero: bool = False) -> int | None:
    value = _read_text(path)
    try:
        parsed = int(value) if value is not None else -1
    except ValueError:
        return None
    if parsed > 0 or (allow_zero and parsed == 0):
        return parsed
    return None


def _disk_probe_path() -> Path:
    configured = os.environ.get("OLLAMA_MODELS")
    candidate = Path(configured).expanduser() if configured else Path("/")
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _looks_like_chat_model(model: str) -> bool:
    lowered = model.lower()
    blocked = ("embed", "rerank", "whisper", "speech", "guard", "classifier")
    return not any(token in lowered for token in blocked)


def _known_tool_family(model: str) -> bool:
    lowered = model.lower()
    return any(
        token in lowered
        for token in (
            "qwen2.5",
            "qwen3",
            "llama3.1",
            "llama3.2",
            "llama3.3",
            "mistral",
            "command-r",
            "functiongemma",
            "tool",
        )
    )


def _is_loopback_url(value: str) -> bool:
    host = urlparse(value).hostname
    return bool(
        host
        and (
            host.lower() == "localhost"
            or host in {"::", "::1", "0.0.0.0"}
            or host.startswith("127.")
        )
    )


def _is_remote_url(value: str) -> bool:
    return not _is_loopback_url(value)


def _normalized_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = "http://" + normalized
    return normalized


def _env_list(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    value = os.environ.get(name)
    try:
        parsed = float(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _gib(value: int | None) -> float | None:
    return round(value / GIB, 2) if value is not None else None


__all__ = [
    "DEFAULT_CANDIDATES",
    "HardwareResources",
    "OllamaProvisioner",
    "ProvisioningCandidate",
    "ProvisioningReport",
    "ProvisioningSettings",
    "choose_candidate",
    "detect_hardware_resources",
]
