"""Automatic, failure-isolated discovery of local and hosted LLM deployments."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .schema import AuthConfig, EndpointConfig, ModelConfig, PolicyConfig, RouterConfig


@dataclass(frozen=True, slots=True)
class DiscoverySettings:
    """Controls safe automatic enrollment.

    Loopback probes and credential-backed cloud providers are enabled by default.
    LAN probing only occurs for explicitly supplied CIDRs.
    """

    enabled: bool = True
    include_loopback: bool = True
    include_cloud: bool = True
    timeout_seconds: float = 1.25
    cloud_timeout_seconds: float = 6.0
    refresh_seconds: float = 300.0
    extra_urls: tuple[str, ...] = ()
    scan_cidrs: tuple[str, ...] = ()
    source_priority: tuple[str, ...] = ()
    max_hosts_per_cidr: int = 64
    max_models_per_source: int = 100

    @classmethod
    def from_env(cls) -> "DiscoverySettings":
        return cls(
            enabled=_env_bool("LLM_ROUTER_DISCOVERY", True),
            include_loopback=_env_bool("LLM_ROUTER_DISCOVER_LOCAL", True),
            include_cloud=_env_bool("LLM_ROUTER_DISCOVER_CLOUD", True),
            timeout_seconds=_env_float("LLM_ROUTER_DISCOVERY_TIMEOUT", 1.25, minimum=0.1),
            cloud_timeout_seconds=_env_float(
                "LLM_ROUTER_CLOUD_DISCOVERY_TIMEOUT", 6.0, minimum=0.1
            ),
            refresh_seconds=_env_float(
                "LLM_ROUTER_DISCOVERY_REFRESH", 300.0, minimum=5.0
            ),
            extra_urls=_env_list("LLM_ROUTER_DISCOVERY_URLS"),
            scan_cidrs=_env_list("LLM_ROUTER_SCAN_CIDRS"),
            source_priority=tuple(
                item.lower() for item in _env_list("LLM_ROUTER_SOURCE_PRIORITY")
            ),
            max_hosts_per_cidr=_env_int(
                "LLM_ROUTER_MAX_SCAN_HOSTS", 64, minimum=1, maximum=256
            ),
            max_models_per_source=_env_int(
                "LLM_ROUTER_MAX_MODELS_PER_SOURCE", 100, minimum=1, maximum=1000
            ),
        )


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    name: str
    provider: str
    kind: str
    base_url: str
    list_url: str
    adapter: str
    local: bool
    timeout_seconds: float
    key_env: str | None = None
    auth_scheme: str = "none"
    auth_header: str | None = None
    auth_prefix: str | None = None
    request_headers: Mapping[str, str] = field(default_factory=dict)
    request_query_param: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    source: str
    provider: str
    base_url: str
    reachable: bool
    enrolled_models: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "provider": self.provider,
            "base_url": self.base_url,
            "reachable": self.reachable,
            "enrolled_models": self.enrolled_models,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    config: RouterConfig
    probes: tuple[ProbeResult, ...]

    @property
    def successful_sources(self) -> int:
        return sum(1 for probe in self.probes if probe.reachable and probe.enrolled_models)

    def to_dict(self, *, include_failures: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "successful_sources": self.successful_sources,
            "enrolled_models": len(self.config.models),
            "attempted_sources": len(self.probes),
            "sources": [
                probe.to_dict()
                for probe in self.probes
                if probe.reachable or include_failures
            ],
        }
        return payload


@dataclass(frozen=True, slots=True)
class _EnrolledSource:
    endpoint: EndpointConfig
    models: tuple[ModelConfig, ...]
    result: ProbeResult


class ModelDiscovery:
    """Discover sources concurrently; a failed probe never cancels another."""

    def __init__(
        self,
        settings: DiscoverySettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or DiscoverySettings.from_env()
        self.transport = transport

    async def discover(self) -> DiscoveryReport:
        if not self.settings.enabled:
            return DiscoveryReport(_empty_config(), ())

        probes = self._probes()
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            limits=limits,
        ) as client:
            enrolled = await asyncio.gather(
                *(self._probe_safely(client, probe) for probe in probes)
            )

        endpoints: dict[str, EndpointConfig] = {}
        models: dict[str, ModelConfig] = {}
        results: list[ProbeResult] = []
        for source in enrolled:
            results.append(source.result)
            if not source.models:
                continue
            endpoints[source.endpoint.name] = source.endpoint
            for model in source.models:
                models.setdefault(model.id, model)
        config = RouterConfig(
            endpoints=endpoints,
            models=tuple(models.values()),
            policy=PolicyConfig(),
            source_path="automatic discovery",
        )
        return DiscoveryReport(config, tuple(results))

    async def _probe_safely(
        self, client: httpx.AsyncClient, probe: ProbeSpec
    ) -> _EnrolledSource:
        try:
            return await self._probe(client, probe)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            message = "authentication rejected" if status in (401, 403) else f"HTTP {status}"
        except (httpx.TimeoutException, TimeoutError):
            message = "timed out"
        except (httpx.NetworkError, ConnectionError):
            message = "unreachable"
        except (ValueError, TypeError, KeyError):
            message = "invalid model-list response"
        except httpx.HTTPError as exc:
            message = f"HTTP client error ({type(exc).__name__})"
        except Exception as exc:  # Discovery plugins/endpoints must not stop the gateway.
            message = f"probe failed ({type(exc).__name__})"
        return _EnrolledSource(
            endpoint=_endpoint_for_probe(probe),
            models=(),
            result=ProbeResult(
                source=probe.name,
                provider=probe.provider,
                base_url=probe.base_url,
                reachable=False,
                error=message,
            ),
        )

    async def _probe(
        self, client: httpx.AsyncClient, probe: ProbeSpec
    ) -> _EnrolledSource:
        headers, params = _probe_auth(probe)
        response = await client.get(
            probe.list_url,
            headers=headers,
            params=params,
            timeout=probe.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("model list is not an object")
        entries = _model_entries(payload, probe.kind)
        entries = entries[: self.settings.max_models_per_source]
        if probe.kind == "ollama" and entries:
            entries = await self._ollama_details(client, probe, entries, headers, params)

        endpoint = _endpoint_for_probe(probe)
        models: list[ModelConfig] = []
        seen: set[str] = set()
        for entry in entries:
            model_name = _model_name(entry, probe.kind)
            if not model_name or model_name in seen or not _is_chat_model(model_name, entry):
                continue
            seen.add(model_name)
            models.append(
                _model_config(
                    probe,
                    endpoint.name,
                    model_name,
                    entry,
                    source_priority=self.settings.source_priority,
                )
            )
        result = ProbeResult(
            source=probe.name,
            provider=probe.provider,
            base_url=probe.base_url,
            reachable=True,
            enrolled_models=len(models),
            error=None if models else "reachable but no chat-capable models were listed",
        )
        return _EnrolledSource(endpoint=endpoint, models=tuple(models), result=result)

    async def _ollama_details(
        self,
        client: httpx.AsyncClient,
        probe: ProbeSpec,
        entries: list[Mapping[str, Any]],
        headers: Mapping[str, str],
        params: Mapping[str, str],
    ) -> list[Mapping[str, Any]]:
        semaphore = asyncio.Semaphore(12)

        async def enrich(entry: Mapping[str, Any]) -> Mapping[str, Any]:
            name = _model_name(entry, "ollama")
            if not name:
                return entry
            try:
                async with semaphore:
                    response = await client.post(
                        probe.base_url.rstrip("/") + "/api/show",
                        json={"model": name},
                        headers=headers,
                        params=params,
                        timeout=probe.timeout_seconds,
                    )
                response.raise_for_status()
                details = response.json()
                if isinstance(details, Mapping):
                    return {**entry, "_show": dict(details)}
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            return entry

        return list(await asyncio.gather(*(enrich(entry) for entry in entries)))

    def _probes(self) -> tuple[ProbeSpec, ...]:
        probes: list[ProbeSpec] = []
        if self.settings.include_loopback:
            probes.extend(_loopback_probes(self.settings.timeout_seconds))
        probes.extend(_extra_probes(self.settings.extra_urls, self.settings.timeout_seconds))
        probes.extend(
            _lan_probes(
                self.settings.scan_cidrs,
                min(self.settings.timeout_seconds, 0.5),
                self.settings.max_hosts_per_cidr,
            )
        )
        if self.settings.include_cloud:
            probes.extend(_cloud_probes(self.settings.cloud_timeout_seconds))
        unique: dict[tuple[str, str], ProbeSpec] = {}
        for probe in probes:
            unique.setdefault((probe.kind, probe.list_url), probe)
        return tuple(unique.values())


def merge_router_configs(
    configured: RouterConfig | None, discovered: RouterConfig
) -> RouterConfig:
    """Merge enrollment with explicit config; explicit entries always win."""

    if configured is None:
        return discovered
    endpoints = dict(discovered.endpoints)
    endpoints.update(configured.endpoints)
    models = {model.id: model for model in discovered.models}
    for model in configured.models:
        models[model.id] = model
    source = configured.source_path or "explicit config"
    if discovered.models:
        source += " + automatic discovery"
    return RouterConfig(
        endpoints=endpoints,
        models=tuple(models.values()),
        policy=configured.policy,
        source_path=source,
    )


def infer_model_profile(
    model_name: str,
    *,
    provider: str,
    local: bool,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer conservative routing metadata from provider metadata and model naming."""

    metadata = metadata or {}
    lowered = model_name.lower()
    quality = 0.62 if local else 0.82
    provider_quality = {
        "openai": 0.91,
        "anthropic": 0.91,
        "gemini": 0.89,
        "xai": 0.87,
        "deepseek": 0.88,
        "mistral": 0.84,
        "groq": 0.80,
        "together": 0.81,
        "openrouter": 0.83,
    }
    quality = max(quality, provider_quality.get(provider, quality))

    size_match = re.search(r"(?<![a-z0-9])(\d+(?:\.\d+)?)b(?:\b|[-_:])", lowered)
    if size_match:
        size = float(size_match.group(1))
        quality = max(quality, min(0.92, 0.50 + 0.09 * math.log2(max(1.0, size))))
    if any(token in lowered for token in ("opus", "ultra", "pro", "reasoner")):
        quality = max(quality, 0.94)
    if re.search(r"(?:^|[-_/])(o[134]|gpt-5)(?:[-_/.:]|$)", lowered):
        quality = max(quality, 0.97)
    if any(token in lowered for token in ("sonnet", "large", "max")):
        quality = max(quality, 0.91)
    if any(token in lowered for token in ("mini", "nano", "small", "flash", "haiku", "lite")):
        quality = min(quality, 0.82)

    context = _first_positive_int(
        metadata,
        ("context_length", "context_window", "input_token_limit", "num_ctx"),
    ) or (32_768 if local else 128_000)
    max_output = _first_positive_int(
        metadata,
        ("max_output_tokens", "output_token_limit", "max_tokens"),
    ) or (8_192 if local else 16_384)

    declared = _declared_capabilities(metadata)
    reasoning = quality
    if any(token in lowered for token in ("reason", "thinking", "deepseek-r1")):
        reasoning = max(reasoning, 0.96)
    coding = quality
    if any(token in lowered for token in ("code", "coder", "codestral", "devstral")):
        coding = max(coding, 0.94)
    vision = 0.0
    if "vision" in declared or any(
        token in lowered for token in ("vision", "llava", "-vl", ".vl", "multimodal")
    ):
        vision = 0.9
    tools = 0.0
    if "tools" in declared or "tool" in declared:
        tools = 0.9
    elif not local:
        tools = 0.86
    elif any(
        token in lowered
        for token in (
            "qwen2.5",
            "qwen3",
            "llama3.1",
            "llama3.2",
            "llama3.3",
            "mistral",
            "command-r",
            "tool",
        )
    ):
        tools = 0.72

    capabilities = {
        "general": min(1.0, quality + 0.02),
        "reasoning": min(1.0, reasoning),
        "coding": min(1.0, coding),
        "writing": min(1.0, quality),
        "structured_output": 0.80 if not local else 0.62,
        "long_context": min(1.0, context / 128_000),
    }
    if tools:
        capabilities["tool_use"] = tools
    if vision:
        capabilities["vision"] = vision
    return {
        "quality": round(min(0.99, max(0.1, quality)), 3),
        "context_window": context,
        "max_output_tokens": min(context, max_output),
        "capabilities": capabilities,
    }


def _loopback_probes(timeout: float) -> list[ProbeSpec]:
    openai_sources = (
        ("lm-studio", 1234),
        ("vllm", 8000),
        ("llama-cpp-localai", 8080),
        ("text-generation-webui", 5000),
        ("koboldcpp", 5001),
        ("jan", 1337),
    )
    ollama_base = _normalized_base_url(
        os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    )
    probes = [
        ProbeSpec(
            name="ollama-loopback",
            provider="ollama",
            kind="ollama",
            base_url=ollama_base,
            list_url=ollama_base + "/api/tags",
            adapter="ollama-chat",
            local=True,
            timeout_seconds=timeout,
        )
    ]
    for name, port in openai_sources:
        base = f"http://127.0.0.1:{port}/v1"
        probes.append(
            ProbeSpec(
                name=f"{name}-loopback",
                provider=name,
                kind="openai",
                base_url=base,
                list_url=base + "/models",
                adapter="openai-chat",
                local=True,
                timeout_seconds=timeout,
            )
        )
    return probes


def _extra_probes(values: tuple[str, ...], timeout: float) -> list[ProbeSpec]:
    probes: list[ProbeSpec] = []
    for index, raw in enumerate(values):
        kind = "auto"
        value = raw.strip()
        for prefix in ("ollama=", "openai="):
            if value.lower().startswith(prefix):
                kind = prefix[:-1]
                value = value[len(prefix) :]
                break
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        base = value.rstrip("/")
        host_label = _slug(parsed.netloc)
        if kind in {"auto", "ollama"} and not base.endswith("/v1"):
            probes.append(
                ProbeSpec(
                    name=f"custom-ollama-{index}-{host_label}",
                    provider="ollama",
                    kind="ollama",
                    base_url=base,
                    list_url=base + "/api/tags",
                    adapter="ollama-chat",
                    local=_is_private_host(parsed.hostname),
                    timeout_seconds=timeout,
                )
            )
        if kind in {"auto", "openai"}:
            api_base = base if base.endswith("/v1") else base + "/v1"
            probes.append(
                ProbeSpec(
                    name=f"custom-openai-{index}-{host_label}",
                    provider="openai-compatible",
                    kind="openai",
                    base_url=api_base,
                    list_url=api_base + "/models",
                    adapter="openai-chat",
                    local=_is_private_host(parsed.hostname),
                    timeout_seconds=timeout,
                )
            )
    return probes


def _lan_probes(
    cidrs: tuple[str, ...], timeout: float, max_hosts_per_cidr: int
) -> list[ProbeSpec]:
    probes: list[ProbeSpec] = []
    for raw in cidrs[:8]:
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        for index, address in enumerate(network.hosts()):
            if index >= max_hosts_per_cidr:
                break
            host = f"[{address}]" if address.version == 6 else str(address)
            ollama_base = f"http://{host}:11434"
            probes.append(
                ProbeSpec(
                    name=f"lan-ollama-{address}",
                    provider="ollama",
                    kind="ollama",
                    base_url=ollama_base,
                    list_url=ollama_base + "/api/tags",
                    adapter="ollama-chat",
                    local=True,
                    timeout_seconds=timeout,
                )
            )
            for port in (1234, 8000, 8080):
                base = f"http://{host}:{port}/v1"
                probes.append(
                    ProbeSpec(
                        name=f"lan-openai-{address}-{port}",
                        provider="openai-compatible",
                        kind="openai",
                        base_url=base,
                        list_url=base + "/models",
                        adapter="openai-chat",
                        local=True,
                        timeout_seconds=timeout,
                    )
                )
    return probes


def _cloud_probes(timeout: float) -> list[ProbeSpec]:
    gemini_key_env = (
        "GEMINI_API_KEY" if os.environ.get("GEMINI_API_KEY") else "GOOGLE_API_KEY"
    )
    openai_base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip(
        "/"
    )
    definitions = (
        ("openai", "OPENAI_API_KEY", "openai", openai_base, "openai-chat"),
        (
            "anthropic",
            "ANTHROPIC_API_KEY",
            "anthropic",
            "https://api.anthropic.com",
            "anthropic-messages",
        ),
        (
            "gemini",
            gemini_key_env,
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-generate",
        ),
        (
            "openrouter",
            "OPENROUTER_API_KEY",
            "openai",
            "https://openrouter.ai/api/v1",
            "openai-chat",
        ),
        ("groq", "GROQ_API_KEY", "openai", "https://api.groq.com/openai/v1", "openai-chat"),
        (
            "together",
            "TOGETHER_API_KEY",
            "openai",
            "https://api.together.xyz/v1",
            "openai-chat",
        ),
        (
            "mistral",
            "MISTRAL_API_KEY",
            "openai",
            "https://api.mistral.ai/v1",
            "openai-chat",
        ),
        ("xai", "XAI_API_KEY", "openai", "https://api.x.ai/v1", "openai-chat"),
        (
            "deepseek",
            "DEEPSEEK_API_KEY",
            "openai",
            "https://api.deepseek.com/v1",
            "openai-chat",
        ),
    )
    probes: list[ProbeSpec] = []
    for provider, key_env, kind, base, adapter in definitions:
        if not os.environ.get(key_env):
            continue
        if kind == "anthropic":
            probes.append(
                ProbeSpec(
                    name=f"cloud-{provider}",
                    provider=provider,
                    kind=kind,
                    base_url=base,
                    list_url=base + "/v1/models",
                    adapter=adapter,
                    local=False,
                    timeout_seconds=timeout,
                    key_env=key_env,
                    auth_scheme="header",
                    auth_header="x-api-key",
                    auth_prefix="",
                    request_headers={"anthropic-version": "2023-06-01"},
                )
            )
        elif kind == "gemini":
            probes.append(
                ProbeSpec(
                    name=f"cloud-{provider}",
                    provider=provider,
                    kind=kind,
                    base_url=base,
                    list_url=base + "/models",
                    adapter=adapter,
                    local=False,
                    timeout_seconds=timeout,
                    key_env=key_env,
                    auth_scheme="query",
                    request_query_param="key",
                )
            )
        else:
            probes.append(
                ProbeSpec(
                    name=f"cloud-{provider}",
                    provider=provider,
                    kind=kind,
                    base_url=base,
                    list_url=base + "/models",
                    adapter=adapter,
                    local=False,
                    timeout_seconds=timeout,
                    key_env=key_env,
                    auth_scheme="bearer",
                    auth_header="Authorization",
                    auth_prefix="Bearer ",
                )
            )
    return probes


def _endpoint_for_probe(probe: ProbeSpec) -> EndpointConfig:
    options: dict[str, Any] = {}
    if probe.provider == "openai":
        options["max_tokens_field"] = "max_completion_tokens"
    return EndpointConfig(
        name=_slug("auto-" + probe.name),
        adapter=probe.adapter,
        base_url=probe.base_url.rstrip("/"),
        auth=AuthConfig(
            key_env=probe.key_env,
            scheme=probe.auth_scheme,
            header=probe.auth_header,
            prefix=probe.auth_prefix,
            query_param=probe.request_query_param,
        ),
        options=options,
        timeout_seconds=120.0 if probe.local else 90.0,
    )


def _probe_auth(probe: ProbeSpec) -> tuple[dict[str, str], dict[str, str]]:
    headers = dict(probe.request_headers)
    params: dict[str, str] = {}
    if not probe.key_env:
        return headers, params
    secret = os.environ.get(probe.key_env)
    if not secret:
        return headers, params
    if probe.auth_scheme == "query":
        params[probe.request_query_param or "key"] = secret
    else:
        headers[probe.auth_header or "Authorization"] = (probe.auth_prefix or "") + secret
    return headers, params


def _model_entries(payload: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    if kind == "ollama":
        value = payload.get("models", [])
    elif kind == "gemini":
        value = payload.get("models", [])
    else:
        value = payload.get("data", payload.get("models", []))
    if not isinstance(value, list):
        raise ValueError("models is not an array")
    return [entry for entry in value if isinstance(entry, Mapping)]


def _model_name(entry: Mapping[str, Any], kind: str) -> str | None:
    if kind == "ollama":
        value = entry.get("model", entry.get("name"))
    else:
        value = entry.get("id", entry.get("name", entry.get("model")))
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value.removeprefix("models/") if kind == "gemini" else value


def _is_chat_model(name: str, metadata: Mapping[str, Any]) -> bool:
    lowered = name.lower()
    blocked = (
        "embedding",
        "embed-",
        "-embed",
        "rerank",
        "whisper",
        "transcri",
        "moderation",
        "guard",
        "safety",
        "classifier",
        "tts",
        "speech",
        "audio",
        "realtime",
        "dall-e",
        "gpt-image",
        "sora",
        "imagen",
        "veo-",
    )
    if any(token in lowered for token in blocked):
        return False
    methods = metadata.get("supportedGenerationMethods")
    if isinstance(methods, list) and methods:
        return "generateContent" in methods
    return True


def _model_config(
    probe: ProbeSpec,
    endpoint_name: str,
    model_name: str,
    metadata: Mapping[str, Any],
    *,
    source_priority: tuple[str, ...] = (),
) -> ModelConfig:
    detail = metadata.get("_show")
    merged_metadata = {**metadata, **detail} if isinstance(detail, Mapping) else metadata
    profile = infer_model_profile(
        model_name,
        provider=probe.provider,
        local=probe.local,
        metadata=merged_metadata,
    )
    return ModelConfig(
        id=f"{endpoint_name}:{model_name}",
        endpoint=endpoint_name,
        upstream_model=model_name,
        capabilities=profile["capabilities"],
        quality=profile["quality"],
        context_window=profile["context_window"],
        max_output_tokens=profile["max_output_tokens"],
        estimated_latency_ms=1_500.0 if probe.local else 2_500.0,
        reliability=0.90 if probe.local else 0.96,
        enabled=True,
        priority=_source_priority(probe, source_priority),
        tags=("discovered", "local" if probe.local else "cloud", probe.provider),
    )


def _source_priority(probe: ProbeSpec, ordered: tuple[str, ...]) -> int:
    if not ordered:
        return 0
    labels = {
        probe.name.lower(),
        probe.provider.lower(),
        "local" if probe.local else "cloud",
    }
    for index, label in enumerate(ordered):
        if label in labels:
            return len(ordered) - index
    return 0


def _declared_capabilities(metadata: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    for key in ("capabilities", "supported_features"):
        value = metadata.get(key)
        if isinstance(value, list):
            values.extend(value)
    return {str(value).lower() for value in values}


def _first_positive_int(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    stack: list[Mapping[str, Any]] = [metadata]
    visited = 0
    while stack and visited < 50:
        current = stack.pop()
        visited += 1
        for key in keys:
            value = current.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)
        for value in current.values():
            if isinstance(value, Mapping):
                stack.append(value)
    return None


def _empty_config() -> RouterConfig:
    return RouterConfig(
        endpoints={}, models=(), policy=PolicyConfig(), source_path="automatic discovery"
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:96] or "auto-source"


def _is_private_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in {"localhost", "host.docker.internal"} or host.lower().endswith(
        (".local", ".lan", ".home.arpa")
    ):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _normalized_base_url(value: str) -> str:
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


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return min(maximum, max(minimum, parsed))


__all__ = [
    "DiscoveryReport",
    "DiscoverySettings",
    "ModelDiscovery",
    "infer_model_profile",
    "merge_router_configs",
]
