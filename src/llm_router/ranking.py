"""Capability-aware deployment scoring and constraint filtering."""

from __future__ import annotations

from collections.abc import Iterable

from .analyze import infer_capabilities, normalize_capabilities
from .errors import ConfigError, NoEligibleModel
from .runtime import RuntimeRegistry
from .schema import (
    ModelConfig,
    QueryRequest,
    RouteCandidate,
    RouterConfig,
    RoutingDecision,
    ScoreBreakdown,
)


class Ranker:
    def __init__(self, config: RouterConfig, runtime: RuntimeRegistry) -> None:
        self.config = config
        self.runtime = runtime
        # Dashboard switch: order replicas of the chosen model by observed latency.
        self.prefer_fastest = False

    def rank(self, request: QueryRequest) -> RoutingDecision:
        strategy = request.strategy or self.config.policy.default_strategy
        if strategy not in self.config.policy.strategies:
            choices = ", ".join(sorted(self.config.policy.strategies))
            raise ConfigError(f"Unknown routing strategy '{strategy}'. Choose one of: {choices}")

        required = normalize_capabilities(request.required_capabilities)
        inferred = infer_capabilities(request.prompt_text, tools_present=bool(request.tools))
        desired = tuple(dict.fromkeys((*required, *inferred)))
        excluded: dict[str, tuple[str, ...]] = {}
        eligible: list[ModelConfig] = []

        for model in self.config.models:
            reasons = self._exclusion_reasons(model, request, required)
            if reasons:
                excluded[model.id] = tuple(reasons)
            else:
                eligible.append(model)

        if not eligible:
            raise NoEligibleModel(
                "No configured deployment satisfies the request constraints",
                {key: list(value) for key, value in excluded.items()},
            )

        costs = {model.id: self._estimated_cost(model, request) for model in eligible}
        latencies = {model.id: self.runtime.observed_latency(model) for model in eligible}
        priorities = {model.id: float(model.priority) for model in eligible}
        cost_utility = _inverse_utilities(costs)
        latency_utility = _inverse_utilities(latencies)
        priority_utility = _utilities(priorities, neutral_when_equal=0.5)
        weights = self.config.policy.strategies[strategy]
        total_weight = sum(weights.values())

        candidates: list[RouteCandidate] = []
        for model in eligible:
            capability = self._capability_score(model, desired)
            health = self.runtime.health_score(model)
            load = 1.0 / (1.0 + self.runtime.state(model.id).active_requests)
            breakdown = ScoreBreakdown(
                quality=model.quality,
                capability=capability,
                reliability=health,
                latency=latency_utility[model.id],
                cost=cost_utility[model.id],
                load=load,
                priority=priority_utility[model.id],
            )
            values = breakdown.to_dict()
            score = sum(weights.get(name, 0.0) * values[name] for name in weights) / total_weight
            tag_matches = len(set(request.preferred_tags).intersection(model.tags))
            if request.preferred_tags:
                score += 0.03 * tag_matches / len(set(request.preferred_tags))
            score *= min(1.5, max(0.5, model.routing_weight))
            candidates.append(
                RouteCandidate(
                    model=model,
                    score=score,
                    breakdown=breakdown,
                    estimated_request_cost=costs[model.id],
                    observed_latency_ms=latencies[model.id],
                    health_score=health,
                )
            )

        candidates.sort(key=lambda item: (-item.score, -item.model.quality, item.model.id))
        if self.config.policy.diversify_fallbacks:
            candidates = _diversify_endpoints(candidates)
        if self.prefer_fastest:
            candidates = _prefer_fastest_replicas(candidates)
        if request.preferred_endpoints:
            # A named machine is the first choice after hard constraints, even
            # when another replica has a better score. Preserve scoring within
            # each tier and independent-endpoint fallback order.
            preferred = set(request.preferred_endpoints)
            candidates.sort(key=lambda item: item.model.endpoint not in preferred)
        return RoutingDecision(
            strategy=strategy,
            inferred_capabilities=inferred,
            required_capabilities=required,
            candidates=tuple(candidates),
            excluded=excluded,
        )

    def _exclusion_reasons(
        self,
        model: ModelConfig,
        request: QueryRequest,
        required: tuple[str, ...],
    ) -> list[str]:
        reasons: list[str] = []
        if not model.enabled:
            reasons.append("disabled")
        if request.allowed_deployments is not None and model.id not in request.allowed_deployments:
            reasons.append("outside selected model alias")
        if model.id in request.exclude_deployments:
            reasons.append("explicitly excluded deployment")
        if model.endpoint in request.exclude_endpoints:
            reasons.append("explicitly excluded endpoint")
        if not self.runtime.is_available(model.id):
            reasons.append("circuit breaker open")
        if not self.runtime.endpoint_available(model.endpoint):
            reasons.append("endpoint health check failed")
        required_context = max(
            request.estimated_input_tokens + request.max_tokens,
            request.min_context_window or 0,
        )
        if model.context_window < required_context:
            reasons.append(
                f"context window {model.context_window} is below required {required_context}"
            )
        if model.max_output_tokens < request.max_tokens:
            reasons.append(
                f"max output {model.max_output_tokens} is below requested {request.max_tokens}"
            )
        threshold = self.config.policy.capability_threshold
        for capability in required:
            if self._capability_value(model, capability) < threshold:
                reasons.append(f"missing required capability '{capability}'")
        if request.max_input_cost_per_million is not None:
            if model.input_cost_per_million is None:
                reasons.append("input cost is unknown")
            elif model.input_cost_per_million > request.max_input_cost_per_million:
                reasons.append("input cost exceeds limit")
        if request.max_output_cost_per_million is not None:
            if model.output_cost_per_million is None:
                reasons.append("output cost is unknown")
            elif model.output_cost_per_million > request.max_output_cost_per_million:
                reasons.append("output cost exceeds limit")
        return reasons

    @staticmethod
    def _capability_value(model: ModelConfig, capability: str) -> float:
        if capability == "general":
            return float(model.capabilities.get("general", model.quality))
        return float(model.capabilities.get(capability, 0.0))

    def _capability_score(self, model: ModelConfig, desired: Iterable[str]) -> float:
        values = [self._capability_value(model, capability) for capability in desired]
        return sum(values) / len(values) if values else model.quality

    @staticmethod
    def _estimated_cost(model: ModelConfig, request: QueryRequest) -> float | None:
        if model.input_cost_per_million is None or model.output_cost_per_million is None:
            return None
        return (
            request.estimated_input_tokens * model.input_cost_per_million
            + request.max_tokens * model.output_cost_per_million
        ) / 1_000_000


def _utilities(values: dict[str, float], *, neutral_when_equal: float = 1.0) -> dict[str, float]:
    minimum = min(values.values())
    maximum = max(values.values())
    if maximum == minimum:
        return {key: neutral_when_equal for key in values}
    return {key: (value - minimum) / (maximum - minimum) for key, value in values.items()}


def _inverse_utilities(values: dict[str, float | None]) -> dict[str, float]:
    known = {key: value for key, value in values.items() if value is not None}
    if not known:
        return {key: 0.5 for key in values}
    numeric = {key: float(value) for key, value in known.items()}
    if max(numeric.values()) == min(numeric.values()):
        result = {key: 1.0 for key in numeric}
    else:
        utilities = _utilities(numeric)
        result = {key: 1.0 - value for key, value in utilities.items()}
    for key, value in values.items():
        if value is None:
            result[key] = 0.5
    return result


def replica_group(model: ModelConfig) -> str:
    return model.replica_group or model.upstream_model


def _prefer_fastest_replicas(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
    """Keep the model choice, but try that model's replicas fastest-first.

    Groups stay in the order their best-scored member earned; only the order
    inside each group changes, so quality, capability, and cost decisions between
    different models are untouched.
    """
    order: list[str] = []
    groups: dict[str, list[RouteCandidate]] = {}
    for item in candidates:
        key = replica_group(item.model)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(item)
    ordered: list[RouteCandidate] = []
    for key in order:
        ordered.extend(sorted(groups[key], key=lambda item: (item.observed_latency_ms, -item.score, item.model.id)))
    return ordered


def _diversify_endpoints(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
    if len(candidates) < 3:
        return candidates
    ordered: list[RouteCandidate] = [candidates[0]]
    remaining = candidates[1:]
    used = {candidates[0].model.endpoint}
    while remaining:
        next_index = next(
            (index for index, item in enumerate(remaining) if item.model.endpoint not in used),
            0,
        )
        item = remaining.pop(next_index)
        ordered.append(item)
        used.add(item.model.endpoint)
    return ordered
