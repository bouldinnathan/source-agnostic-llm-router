"""In-process health, latency, load, and circuit-breaker state."""

from __future__ import annotations

import asyncio
from collections import deque

import time
from dataclasses import dataclass
from typing import Any

from .schema import ModelConfig, PolicyConfig


@dataclass(slots=True)
class DeploymentState:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    active_requests: int = 0
    latency_ewma_ms: float | None = None
    first_token_ewma_ms: float | None = None
    circuit_open_until: float = 0.0
    last_error: str | None = None


@dataclass(slots=True)
class EndpointState:
    """API reachability is independent of model inference circuit breakers."""

    reachable: bool | None = None
    last_checked_at: float | None = None
    last_error: str | None = None


class RuntimeRegistry:
    """Tracks ephemeral deployment health for one router process."""

    def __init__(self, policy: PolicyConfig) -> None:
        self.policy = policy
        self._states: dict[str, DeploymentState] = {}
        self._endpoint_states: dict[str, EndpointState] = {}
        # Replica-race bookkeeping lives here so discovery refreshes, which
        # replace the router object, do not reset the schedule or the history.
        self.race_counters: dict[str, int] = {}
        self.last_races: deque[dict[str, Any]] = deque(maxlen=5)
        self.race_tasks: set[asyncio.Task[Any]] = set()

    def state(self, deployment: str) -> DeploymentState:
        return self._states.setdefault(deployment, DeploymentState())

    def is_available(self, deployment: str) -> bool:
        return self.state(deployment).circuit_open_until <= time.monotonic()

    def endpoint_available(self, endpoint: str) -> bool:
        state = self._endpoint_states.get(endpoint)
        return state is None or state.reachable is not False

    def record_endpoint_probe(
        self, endpoint: str, reachable: bool | None, error: str | None = None,
        *, checked_at: float | None = None,
    ) -> None:
        state = self._endpoint_states.setdefault(endpoint, EndpointState())
        state.reachable = reachable
        state.last_checked_at = (time.time() if checked_at is None else checked_at) if reachable is not None else None
        state.last_error = error

    def begin(self, deployment: str) -> None:
        self.state(deployment).active_requests += 1

    def record_success(self, deployment: str, latency_ms: float, *, first_token_ms: float | None = None) -> None:
        state = self.state(deployment)
        state.active_requests = max(0, state.active_requests - 1)
        state.successes += 1
        state.consecutive_failures = 0
        state.circuit_open_until = 0.0
        state.last_error = None
        alpha = self.policy.latency_ewma_alpha
        if state.latency_ewma_ms is None:
            state.latency_ewma_ms = latency_ms
        else:
            state.latency_ewma_ms = alpha * latency_ms + (1 - alpha) * state.latency_ewma_ms
        if first_token_ms is not None:
            if state.first_token_ewma_ms is None:
                state.first_token_ewma_ms = first_token_ms
            else:
                state.first_token_ewma_ms = alpha * first_token_ms + (1 - alpha) * state.first_token_ewma_ms

    def record_first_token(self, deployment: str, first_token_ms: float) -> None:
        """Note a first-token time for an attempt that was stopped on purpose.

        A race loser is closed once it has produced its first token, so it has
        no total latency and counts as neither a success nor a failure.
        """
        state = self.state(deployment)
        alpha = self.policy.latency_ewma_alpha
        if state.first_token_ewma_ms is None:
            state.first_token_ewma_ms = first_token_ms
        else:
            state.first_token_ewma_ms = alpha * first_token_ms + (1 - alpha) * state.first_token_ewma_ms

    def record_failure(self, deployment: str, reason: str) -> None:
        state = self.state(deployment)
        state.active_requests = max(0, state.active_requests - 1)
        state.failures += 1
        state.consecutive_failures += 1
        state.last_error = reason
        if state.consecutive_failures >= self.policy.circuit_breaker_failures:
            state.circuit_open_until = (
                time.monotonic() + self.policy.circuit_breaker_cooldown_seconds
            )

    def end_without_result(self, deployment: str) -> None:
        state = self.state(deployment)
        state.active_requests = max(0, state.active_requests - 1)

    def observed_latency(self, model: ModelConfig) -> float:
        return self.state(model.id).latency_ewma_ms or model.estimated_latency_ms

    def observed_first_token(self, model: ModelConfig) -> float | None:
        """Smoothed time to first token, or None when never measured."""
        return self.state(model.id).first_token_ewma_ms

    def health_score(self, model: ModelConfig) -> float:
        state = self.state(model.id)
        # Start with the configured reliability as a ten-observation prior.
        prior_strength = 10.0
        return (
            model.reliability * prior_strength + state.successes
        ) / (prior_strength + state.successes + state.failures)

    def snapshot(self, models: tuple[ModelConfig, ...]) -> dict[str, Any]:
        now = time.monotonic()
        deployments: list[dict[str, Any]] = []
        for model in models:
            state = self.state(model.id)
            deployments.append(
                {
                    "deployment": model.id,
                    "endpoint": model.endpoint,
                    "endpoint_available": self.endpoint_available(model.endpoint),
                    "enabled": model.enabled,
                    "circuit_open": state.circuit_open_until > now,
                    "circuit_open_for_seconds": round(max(0.0, state.circuit_open_until - now), 3),
                    "active_requests": state.active_requests,
                    "successes": state.successes,
                    "failures": state.failures,
                    "consecutive_failures": state.consecutive_failures,
                    "latency_ewma_ms": (
                        round(state.latency_ewma_ms, 2)
                        if state.latency_ewma_ms is not None
                        else None
                    ),
                    "health_score": round(self.health_score(model), 6),
                    "last_error": state.last_error,
                }
            )
        endpoint_names = set(self._endpoint_states) | {model.endpoint for model in models}
        endpoints: dict[str, dict[str, Any]] = {}
        for name in sorted(endpoint_names):
            endpoint_state = self._endpoint_states.get(name, EndpointState())
            endpoints[name] = {
                "reachable": endpoint_state.reachable,
                "probed": endpoint_state.reachable is not None,
                "last_checked_at": endpoint_state.last_checked_at,
                "last_error": endpoint_state.last_error,
            }
        return {"deployments": deployments, "endpoints": endpoints}
