"""Configuration and public result types."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence

from .errors import RequestError


DEFAULT_STRATEGIES: dict[str, dict[str, float]] = {
    "quality": {
        "quality": 0.52,
        "capability": 0.30,
        "reliability": 0.10,
        "latency": 0.04,
        "cost": 0.02,
        "load": 0.01,
        "priority": 0.01,
    },
    "balanced": {
        "quality": 0.32,
        "capability": 0.24,
        "reliability": 0.14,
        "latency": 0.12,
        "cost": 0.12,
        "load": 0.04,
        "priority": 0.02,
    },
    "cost": {
        "quality": 0.16,
        "capability": 0.18,
        "reliability": 0.10,
        "latency": 0.06,
        "cost": 0.44,
        "load": 0.04,
        "priority": 0.02,
    },
    "latency": {
        "quality": 0.16,
        "capability": 0.16,
        "reliability": 0.12,
        "latency": 0.42,
        "cost": 0.06,
        "load": 0.06,
        "priority": 0.02,
    },
    "priority": {
        "quality": 0.18,
        "capability": 0.18,
        "reliability": 0.08,
        "latency": 0.03,
        "cost": 0.02,
        "load": 0.01,
        "priority": 0.50,
    },
}


@dataclass(frozen=True, slots=True)
class AuthConfig:
    key_env: str | None = None
    scheme: str | None = None
    header: str | None = None
    prefix: str | None = None
    query_param: str | None = None


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    name: str
    adapter: str
    base_url: str = ""
    auth: AuthConfig = field(default_factory=AuthConfig)
    headers: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 90.0
    verify_tls: bool = True
    machine_id: str | None = None
    discover: bool = False
    health_path: str | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    id: str
    endpoint: str
    upstream_model: str
    capabilities: Mapping[str, float] = field(default_factory=dict)
    quality: float = 0.5
    context_window: int = 8_192
    max_output_tokens: int = 2_048
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    estimated_latency_ms: float = 2_000.0
    reliability: float = 0.95
    enabled: bool = True
    priority: int = 0
    routing_weight: float = 1.0
    tags: tuple[str, ...] = ()
    replica_group: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    default_strategy: str = "quality"
    strategies: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: {name: dict(weights) for name, weights in DEFAULT_STRATEGIES.items()}
    )
    max_attempts: int = 3
    capability_threshold: float = 0.5
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: float = 30.0
    latency_ewma_alpha: float = 0.25
    diversify_fallbacks: bool = True
    health_check_interval_seconds: float = 15.0
    health_check_timeout_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class RouterConfig:
    endpoints: Mapping[str, EndpointConfig]
    models: tuple[ModelConfig, ...]
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """A provider-neutral chat request plus routing constraints."""

    messages: tuple[Mapping[str, Any], ...]
    required_capabilities: tuple[str, ...] = ()
    strategy: str | None = None
    min_context_window: int | None = None
    max_input_cost_per_million: float | None = None
    max_output_cost_per_million: float | None = None
    max_tokens: int = 1_024
    temperature: float | None = None
    tools: tuple[Mapping[str, Any], ...] = ()
    response_format: Mapping[str, Any] | None = None
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    exclude_deployments: tuple[str, ...] = ()
    exclude_endpoints: tuple[str, ...] = ()
    preferred_tags: tuple[str, ...] = ()
    allowed_deployments: tuple[str, ...] | None = None
    preferred_endpoints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.messages:
            raise RequestError("messages must not be empty")
        if not all(isinstance(message, Mapping) for message in self.messages):
            raise RequestError("every message must be an object")
        # JSON clients such as Home Assistant's number selector send whole
        # numbers as floats (8192.0). Treat an integral float as the integer it
        # denotes; anything fractional or non-finite still fails below.
        for name in ("max_tokens", "min_context_window"):
            value = getattr(self, name)
            if isinstance(value, float) and math.isfinite(value) and value.is_integer():
                object.__setattr__(self, name, int(value))
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise RequestError("max_tokens must be an integer")
        if self.max_tokens <= 0:
            raise RequestError("max_tokens must be greater than zero")
        if self.min_context_window is not None:
            if isinstance(self.min_context_window, bool) or not isinstance(
                self.min_context_window, int
            ):
                raise RequestError("min_context_window must be an integer")
            if self.min_context_window <= 0:
                raise RequestError("min_context_window must be greater than zero")
        for name, value in (
            ("max_input_cost_per_million", self.max_input_cost_per_million),
            ("max_output_cost_per_million", self.max_output_cost_per_million),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            ):
                raise RequestError(f"{name} must be a non-negative number")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise RequestError("temperature must be a number")
            if not 0 <= self.temperature <= 2:
                raise RequestError("temperature must be between 0 and 2")
        if not all(isinstance(tool, Mapping) for tool in self.tools):
            raise RequestError("every tool must be an object")
        if self.response_format is not None and not isinstance(self.response_format, Mapping):
            raise RequestError("response_format must be an object")
        if not isinstance(self.extra_body, Mapping):
            raise RequestError("extra_body must be an object")

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        system: str | None = None,
        required_capabilities: Sequence[str] = (),
        **kwargs: Any,
    ) -> "QueryRequest":
        messages: list[Mapping[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return cls(
            messages=tuple(messages),
            required_capabilities=tuple(required_capabilities),
            **kwargs,
        )

    @property
    def prompt_text(self) -> str:
        chunks: list[str] = []
        for message in self.messages:
            content = message.get("content", "")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, Sequence):
                for part in content:
                    if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                        chunks.append(str(part["text"]))
        return "\n".join(chunks)

    @property
    def estimated_input_tokens(self) -> int:
        # A conservative dependency-free estimate. Callers can force a larger
        # context requirement with min_context_window when exact counts matter.
        return max(1, (len(self.prompt_text) + 2) // 3)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    quality: float
    capability: float
    reliability: float
    latency: float
    cost: float
    load: float
    priority: float

    def to_dict(self) -> dict[str, float]:
        return {
            "quality": round(self.quality, 6),
            "capability": round(self.capability, 6),
            "reliability": round(self.reliability, 6),
            "latency": round(self.latency, 6),
            "cost": round(self.cost, 6),
            "load": round(self.load, 6),
            "priority": round(self.priority, 6),
        }


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    model: ModelConfig
    score: float
    breakdown: ScoreBreakdown
    estimated_request_cost: float | None
    observed_latency_ms: float
    health_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment": self.model.id,
            "endpoint": self.model.endpoint,
            "upstream_model": self.model.upstream_model,
            "score": round(self.score, 6),
            "score_breakdown": self.breakdown.to_dict(),
            "estimated_request_cost": (
                round(self.estimated_request_cost, 8)
                if self.estimated_request_cost is not None
                else None
            ),
            "observed_or_estimated_latency_ms": round(self.observed_latency_ms, 2),
            "health_score": round(self.health_score, 6),
            "capabilities": dict(self.model.capabilities),
            "tags": list(self.model.tags),
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    strategy: str
    inferred_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    candidates: tuple[RouteCandidate, ...]
    excluded: Mapping[str, tuple[str, ...]]

    def to_dict(self, *, top_k: int | None = None) -> dict[str, Any]:
        candidates = self.candidates if top_k is None else self.candidates[:top_k]
        return {
            "strategy": self.strategy,
            "inferred_capabilities": list(self.inferred_capabilities),
            "required_capabilities": list(self.required_capabilities),
            "selected": candidates[0].to_dict() if candidates else None,
            "candidates": [candidate.to_dict() for candidate in candidates],
            "excluded": {key: list(value) for key, value in self.excluded.items()},
        }


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    text: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    raw: Mapping[str, Any] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RoutedCompletion:
    text: str
    deployment: str
    endpoint: str
    upstream_model: str
    score: float
    usage: Mapping[str, Any]
    finish_reason: str | None
    attempts: tuple[Mapping[str, Any], ...]
    inferred_capabilities: tuple[str, ...]
    tool_calls: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "deployment": self.deployment,
            "endpoint": self.endpoint,
            "upstream_model": self.upstream_model,
            "routing_score": round(self.score, 6),
            "usage": dict(self.usage),
            "finish_reason": self.finish_reason,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "attempts": [dict(item) for item in self.attempts],
            "inferred_capabilities": list(self.inferred_capabilities),
        }
