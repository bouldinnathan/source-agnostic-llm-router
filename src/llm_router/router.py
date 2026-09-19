"""High-level routing and multi-source failover service."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Sequence

from .adapters import AdapterRegistry
from .errors import AllModelsFailed, NoEligibleModel, UpstreamError, UpstreamFailure
from .metrics import MetricsStore
from .ranking import Ranker
from .runtime import RuntimeRegistry
from .schema import EndpointConfig, ModelConfig, QueryRequest, RoutedCompletion, RouterConfig, RoutingDecision, UpstreamResult
from .telemetry import extract_observation

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LLMRouter:
    """Routes provider-neutral requests across configured deployments."""

    def __init__(
        self,
        config: RouterConfig,
        *,
        adapters: AdapterRegistry | None = None,
        runtime: RuntimeRegistry | None = None,
        metrics: MetricsStore | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters or AdapterRegistry()
        self.runtime = runtime or RuntimeRegistry(config.policy)
        self.metrics = metrics if metrics is not None else MetricsStore()
        self.ranker = Ranker(config, self.runtime)

    def route(self, request: QueryRequest) -> RoutingDecision:
        """Rank deployments without contacting an upstream model."""

        return self.ranker.rank(request)

    async def complete(self, request: QueryRequest) -> RoutedCompletion:
        """Call the highest-ranked deployment and fail over across sources."""

        try:
            decision = self.route(request)
        except NoEligibleModel:
            await self._record_request(success=False, rerouted=False, failure_kinds=("no_eligible_model",))
            raise
        limit = min(self.config.policy.max_attempts, len(decision.candidates))
        failures: list[UpstreamFailure] = []
        attempts: list[dict[str, Any]] = []
        failure_kinds: list[str] = []

        for candidate in decision.candidates[:limit]:
            model = candidate.model
            endpoint = self.config.endpoints[model.endpoint]
            self.runtime.begin(model.id)
            started = time.perf_counter()
            try:
                adapter = self.adapters.get(endpoint.adapter)
                result = await adapter.complete(endpoint, model, request)
            except asyncio.CancelledError:
                self.runtime.end_without_result(model.id)
                raise
            except UpstreamError as exc:
                latency_ms = (time.perf_counter() - started) * 1_000
                self.runtime.record_failure(model.id, exc.reason)
                failure = UpstreamFailure(
                    deployment=model.id,
                    endpoint=model.endpoint,
                    reason=exc.reason,
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                    kind=exc.kind,
                )
                failures.append(failure)
                failure_kinds.append(exc.kind)
                attempts.append(
                    {
                        **failure.to_dict(),
                        "latency_ms": round(latency_ms, 2),
                        "success": False,
                    }
                )
                await self._record_metrics(endpoint, model, latency_ms, success=False)
                continue
            except Exception as exc:  # Custom adapters must not crash the MCP process.
                latency_ms = (time.perf_counter() - started) * 1_000
                reason = f"Adapter failure: {type(exc).__name__}"
                self.runtime.record_failure(model.id, reason)
                failure = UpstreamFailure(
                    deployment=model.id,
                    endpoint=model.endpoint,
                    reason=reason,
                    retryable=False,
                    kind="adapter",
                )
                failures.append(failure)
                failure_kinds.append("adapter")
                attempts.append(
                    {
                        **failure.to_dict(),
                        "latency_ms": round(latency_ms, 2),
                        "success": False,
                    }
                )
                await self._record_metrics(endpoint, model, latency_ms, success=False)
                continue

            latency_ms = (time.perf_counter() - started) * 1_000
            self.runtime.record_success(model.id, latency_ms)
            observation = await self._record_metrics(endpoint, model, latency_ms, success=True, result=result)
            await self._record_request(
                success=True, rerouted=bool(failures), failure_kinds=failure_kinds,
                input_tokens=observation.get("input_tokens"), output_tokens=observation.get("output_tokens"),
            )
            attempts.append(
                {
                    "deployment": model.id,
                    "endpoint": model.endpoint,
                    "latency_ms": round(latency_ms, 2),
                    "success": True,
                }
            )
            return RoutedCompletion(
                text=result.text,
                deployment=model.id,
                endpoint=model.endpoint,
                upstream_model=model.upstream_model,
                score=candidate.score,
                usage=result.usage,
                finish_reason=result.finish_reason,
                attempts=tuple(attempts),
                inferred_capabilities=decision.inferred_capabilities,
                tool_calls=result.tool_calls,
            )

        await self._record_request(success=False, rerouted=len(failures) > 1, failure_kinds=failure_kinds)
        raise AllModelsFailed(failures)

    async def _record_request(
        self, *, success: bool, rerouted: bool, failure_kinds: Sequence[str] = (),
        input_tokens: float | int | None = None, output_tokens: float | int | None = None,
    ) -> None:
        """Count one client request outcome; a storage problem never affects the answer."""
        try:
            await asyncio.to_thread(
                self.metrics.record_request, success=success, rerouted=rerouted,
                input_tokens=input_tokens, output_tokens=output_tokens, failure_kinds=tuple(failure_kinds),
            )
        except Exception:
            pass

    async def _record_metrics(
        self, endpoint: EndpointConfig, model: ModelConfig, latency_ms: float,
        *, success: bool, result: UpstreamResult | None = None,
    ) -> dict[str, Any]:
        """Observe only completed real attempts; telemetry must never cause a retry.

        The upstream timer has already stopped. SQLite work runs off the event
        loop and persists no prompts, responses, tool arguments, or credentials.
        Returns the sanitized observation so request-level totals reuse it.
        """
        observation: dict[str, Any] = {"request_duration_ms": latency_ms}
        if result is not None:
            try:
                observation = extract_observation(endpoint.adapter, result, latency_ms)
            except Exception:
                # Optional provider statistics cannot erase the attempt itself.
                pass
        try:
            await asyncio.to_thread(
                self.metrics.record, endpoint, model, observation, success=success,
            )
        except Exception:
            # A full disk, locked database, or malformed optional statistics
            # must not discard a valid answer or repeat inference on a fallback.
            pass
        return observation

    def list_models(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        for model in self.config.models:
            if not include_disabled and not model.enabled:
                continue
            state = self.runtime.state(model.id)
            models.append(
                {
                    "deployment": model.id,
                    "endpoint": model.endpoint,
                    "adapter": self.config.endpoints[model.endpoint].adapter,
                    "upstream_model": model.upstream_model,
                    "enabled": model.enabled,
                    "available": self.runtime.is_available(model.id)
                    and self.runtime.endpoint_available(model.endpoint),
                    "machine_id": self.config.endpoints[model.endpoint].machine_id or model.endpoint,
                    "replica_group": model.replica_group or model.upstream_model,
                    "quality": model.quality,
                    "priority": model.priority,
                    "routing_weight": model.routing_weight,
                    "context_window": model.context_window,
                    "max_output_tokens": model.max_output_tokens,
                    "input_cost_per_million": model.input_cost_per_million,
                    "output_cost_per_million": model.output_cost_per_million,
                    "capabilities": dict(model.capabilities),
                    "tags": list(model.tags),
                    "active_requests": state.active_requests,
                    "health_score": round(self.runtime.health_score(model), 6),
                }
            )
        return models

    def status(self) -> dict[str, Any]:
        return {
            "config": self.config.source_path,
            "default_strategy": self.config.policy.default_strategy,
            "endpoint_count": len(self.config.endpoints),
            "deployment_count": len(self.config.models),
            **self.runtime.snapshot(self.config.models),
        }

    def diagnostics(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        endpoint_results: list[dict[str, Any]] = []
        for endpoint in self.config.endpoints.values():
            endpoint_errors: list[str] = []
            endpoint_warnings: list[str] = []
            try:
                adapter = self.adapters.get(endpoint.adapter)
            except Exception as exc:
                endpoint_errors.append(str(exc))
                adapter = None

            default_scheme = getattr(adapter, "default_auth_scheme", "bearer")
            scheme = (endpoint.auth.scheme or default_scheme).lower()
            if scheme != "none":
                if not endpoint.auth.key_env:
                    endpoint_errors.append("auth.key_env is required for the selected auth scheme")
                elif not os.environ.get(endpoint.auth.key_env):
                    endpoint_warnings.append(
                        f"environment variable {endpoint.auth.key_env} is not set"
                    )
            for value in endpoint.headers.values():
                for env_name in _ENV_REFERENCE.findall(value):
                    if not os.environ.get(env_name):
                        endpoint_warnings.append(f"environment variable {env_name} is not set")
            errors.extend(f"{endpoint.name}: {item}" for item in endpoint_errors)
            warnings.extend(f"{endpoint.name}: {item}" for item in endpoint_warnings)
            endpoint_results.append(
                {
                    "endpoint": endpoint.name,
                    "adapter": endpoint.adapter,
                    "valid": not endpoint_errors,
                    "errors": endpoint_errors,
                    "warnings": list(dict.fromkeys(endpoint_warnings)),
                }
            )
        return {
            "ok": not errors,
            "config": self.config.source_path,
            "errors": errors,
            "warnings": list(dict.fromkeys(warnings)),
            "endpoints": endpoint_results,
            "deployment_count": len(self.config.models),
        }
