"""High-level routing and multi-source failover service."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
import re
import time
from typing import Any, AsyncIterator, Sequence

from .adapters import AdapterRegistry
from .adapters.base import STREAM_TIMEOUTS, StreamDelta, StreamTimeouts, adapter_stream
from .errors import AllModelsFailed, NoEligibleModel, StreamInterrupted, UpstreamError, UpstreamFailure
from .metrics import MetricsStore
from .ranking import Ranker, replica_group
from .routing_settings import RoutingSettings
from .runtime import RuntimeRegistry
from .schema import (
    EndpointConfig, ModelConfig, QueryRequest, RouteCandidate, RoutedCompletion, RouterConfig,
    RoutingDecision, UpstreamResult,
)
from .telemetry import extract_observation

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(slots=True)
class _AttemptOutcome:
    candidate: RouteCandidate
    result: UpstreamResult | None
    observation: dict[str, Any]
    failure: UpstreamFailure | None
    attempt: dict[str, Any]


class LLMRouter:
    """Routes provider-neutral requests across configured deployments."""

    def __init__(
        self,
        config: RouterConfig,
        *,
        adapters: AdapterRegistry | None = None,
        runtime: RuntimeRegistry | None = None,
        metrics: MetricsStore | None = None,
        settings: RoutingSettings | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters or AdapterRegistry()
        self.runtime = runtime or RuntimeRegistry(config.policy)
        self.metrics = metrics if metrics is not None else MetricsStore()
        self.ranker = Ranker(config, self.runtime)
        self.settings = settings if settings is not None else RoutingSettings()

    @property
    def settings(self) -> RoutingSettings:
        return self._settings

    @settings.setter
    def settings(self, value: RoutingSettings) -> None:
        self._settings = value
        self.ranker.prefer_fastest = value.prefer_fastest_replica
        self.ranker.prefer_first_token = value.prefer_first_token

    def _stream_timeouts(self) -> StreamTimeouts:
        settings = self._settings
        return StreamTimeouts(
            first_token=float(settings.first_token_timeout_seconds),
            idle=float(settings.idle_timeout_seconds),
            total=float(settings.max_request_seconds) if settings.max_request_seconds > 0 else None,
        )

    def route(self, request: QueryRequest) -> RoutingDecision:
        """Rank deployments without contacting an upstream model."""

        return self.ranker.rank(request)

    async def complete(self, request: QueryRequest) -> RoutedCompletion:
        """Call the highest-ranked deployment and fail over across sources.

        The answer is read from the backend as a stream but returned whole, so
        a deployment that fails part-way through is simply retried elsewhere.
        See :meth:`complete_stream` for replica races.
        """

        async with aclosing(self.complete_stream(request, relay=False)) as events:
            async for event in events:
                if isinstance(event, RoutedCompletion):
                    return event
        raise AllModelsFailed([])  # unreachable: complete_stream always ends in a completion or raises

    async def complete_stream(
        self, request: QueryRequest, *, relay: bool = True,
    ) -> AsyncIterator[StreamDelta | RoutedCompletion]:
        """Yield an answer's fragments as they are generated, then the completion.

        Failures before any fragment has been yielded fail over to the next
        candidate silently. With ``relay`` the caller is passing fragments on to
        a client as they arrive, so a failure after the first fragment cannot be
        retried without repeating text the client already has; it raises
        :class:`StreamInterrupted` instead. With ``relay=False`` nothing is ever
        committed and every failure fails over.

        With replica racing enabled, every Nth request whose best candidate has
        other available replicas is sent to all of them at once. The first
        replica to produce a token is relayed; each other replica is stopped as
        soon as its own first token has been timed, so a race refreshes every
        replica's time to first token for the cost of one prompt evaluation
        each, without generating the whole answer twice.
        """

        try:
            decision = self.route(request)
        except NoEligibleModel:
            await self._record_request(success=False, rerouted=False, failure_kinds=("no_eligible_model",))
            raise
        limit = min(self.config.policy.max_attempts, len(decision.candidates))
        failures: list[UpstreamFailure] = []
        attempts: list[dict[str, Any]] = []
        failure_kinds: list[str] = []
        remaining = list(decision.candidates[:limit])

        sources: list[AsyncIterator[StreamDelta | _AttemptOutcome]] = []
        participants = self._race_participants(request, decision)
        if participants:
            sources.append(self._race_stream(request, participants, failures, attempts, failure_kinds))
            raced = {candidate.model.id for candidate in participants}
            remaining = [candidate for candidate in decision.candidates if candidate.model.id not in raced][:limit]
        sources.extend(self._attempt_stream(candidate, request) for candidate in remaining)

        sent = 0
        for source in sources:
            outcome: _AttemptOutcome | None = None
            async with aclosing(source) as events:
                async for event in events:
                    if isinstance(event, _AttemptOutcome):
                        outcome = event
                        break
                    if event.text or event.thinking:
                        sent += len(event.text)
                        yield event
            if outcome is None:
                continue  # every raced replica failed before its first token; try what is left
            if outcome.failure is None:
                yield await self._finish(outcome, decision, failures, attempts, failure_kinds)
                return
            failures.append(outcome.failure)
            attempts.append(outcome.attempt)
            failure_kinds.append(outcome.failure.kind)
            if relay and sent:
                await self._record_request(success=False, rerouted=len(failures) > 1, failure_kinds=failure_kinds)
                raise StreamInterrupted(outcome.failure, sent)

        await self._record_request(success=False, rerouted=len(failures) > 1, failure_kinds=failure_kinds)
        raise AllModelsFailed(failures)

    async def _finish(
        self, outcome: _AttemptOutcome, decision: RoutingDecision,
        failures: list[UpstreamFailure], attempts: list[dict[str, Any]], failure_kinds: list[str],
    ) -> RoutedCompletion:
        assert outcome.result is not None
        attempts.append(outcome.attempt)
        await self._record_request(
            success=True, rerouted=bool(failures), failure_kinds=failure_kinds,
            input_tokens=outcome.observation.get("input_tokens"), output_tokens=outcome.observation.get("output_tokens"),
        )
        candidate = outcome.candidate
        return RoutedCompletion(
            text=outcome.result.text,
            deployment=candidate.model.id,
            endpoint=candidate.model.endpoint,
            upstream_model=candidate.model.upstream_model,
            score=candidate.score,
            usage=outcome.result.usage,
            finish_reason=outcome.result.finish_reason,
            attempts=tuple(attempts),
            inferred_capabilities=decision.inferred_capabilities,
            tool_calls=outcome.result.tool_calls,
        )

    async def _attempt_stream(
        self, candidate: RouteCandidate, request: QueryRequest, *, race: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta | _AttemptOutcome]:
        """Run one upstream attempt: yield its fragments, then its recorded outcome.

        Never raises except on cancellation or when closed early, in which case
        the backend connection is closed and nothing is recorded for the
        deployment beyond the end of its in-flight request.
        """
        model = candidate.model
        endpoint = self.config.endpoints[model.endpoint]
        self.runtime.begin(model.id)
        started = time.perf_counter()
        timeouts_token = STREAM_TIMEOUTS.set(self._stream_timeouts())
        recorded = False
        result: UpstreamResult | None = None
        failure: UpstreamFailure | None = None
        first_token_ms: float | None = None
        try:
            try:
                adapter = self.adapters.get(endpoint.adapter)
                async with aclosing(adapter_stream(adapter, endpoint, model, request)) as items:
                    async for item in items:
                        if isinstance(item, UpstreamResult):
                            result = item
                            break
                        if first_token_ms is None:
                            first_token_ms = (
                                item.first_token_ms if item.first_token_ms is not None
                                else (time.perf_counter() - started) * 1_000
                            )
                        yield item
                if result is None:
                    raise UpstreamError("Adapter stream ended without a result", retryable=False, kind="adapter")
            except UpstreamError as exc:
                failure = UpstreamFailure(
                    deployment=model.id, endpoint=model.endpoint, reason=exc.reason,
                    retryable=exc.retryable, status_code=exc.status_code, kind=exc.kind,
                )
            except Exception as exc:  # Custom adapters must not crash the MCP process.
                failure = UpstreamFailure(
                    deployment=model.id, endpoint=model.endpoint,
                    reason=f"Adapter failure: {type(exc).__name__}", retryable=False, kind="adapter",
                )
            latency_ms = (time.perf_counter() - started) * 1_000
            if failure is None:
                assert result is not None
                if result.first_token_ms is None and first_token_ms is not None:
                    result = replace(result, first_token_ms=first_token_ms)
                self.runtime.record_success(model.id, latency_ms, first_token_ms=result.first_token_ms)
                recorded = True
                observation = await self._record_metrics(endpoint, model, latency_ms, success=True, result=result)
                attempt = {
                    "deployment": model.id, "endpoint": model.endpoint,
                    "latency_ms": round(latency_ms, 2), "success": True,
                }
                if result.first_token_ms is not None:
                    attempt["first_token_ms"] = round(result.first_token_ms, 2)
                if race is not None:
                    attempt["race"] = True
                    race["participants"][model.id] = {
                        "endpoint": model.endpoint, "success": True, "latency_ms": round(latency_ms, 2), "kind": None,
                        "first_token_ms": None if result.first_token_ms is None else round(result.first_token_ms, 2),
                    }
                yield _AttemptOutcome(candidate, result, observation, None, attempt)
                return
            self.runtime.record_failure(model.id, failure.reason)
            recorded = True
            await self._record_metrics(endpoint, model, latency_ms, success=False)
            attempt = {**failure.to_dict(), "latency_ms": round(latency_ms, 2), "success": False}
            if race is not None:
                attempt["race"] = True
                race["participants"][model.id] = {
                    "endpoint": model.endpoint, "success": False, "latency_ms": round(latency_ms, 2), "kind": failure.kind,
                }
            yield _AttemptOutcome(candidate, None, {"request_duration_ms": latency_ms}, failure, attempt)
        finally:
            if not recorded:
                self.runtime.end_without_result(model.id)
            try:
                STREAM_TIMEOUTS.reset(timeouts_token)
            except ValueError:
                # The first fragment was pulled by another task (a race head), so
                # the variable was set in that task's context, which is gone.
                pass

    def _race_participants(self, request: QueryRequest, decision: RoutingDecision) -> tuple[RouteCandidate, ...]:
        """Decide whether this request is one of the periodic all-replica races."""
        settings = self._settings
        if not settings.race_replicas or request.preferred_endpoints:
            return ()
        group = replica_group(decision.candidates[0].model)
        members = tuple(
            candidate for candidate in decision.candidates if replica_group(candidate.model) == group
        )
        if len(members) < 2:
            return ()
        count = self.runtime.race_counters.get(group, 0) + 1
        self.runtime.race_counters[group] = count
        if count % settings.race_every != 0:
            return ()
        return members

    async def _race_stream(
        self, request: QueryRequest, participants: tuple[RouteCandidate, ...],
        failures: list[UpstreamFailure], attempts: list[dict[str, Any]], failure_kinds: list[str],
    ) -> AsyncIterator[StreamDelta | _AttemptOutcome]:
        """Send the request to every replica; relay the first to produce a token.

        Replicas that fail before any token are recorded as failures here.
        Yields nothing when every replica failed. The other replicas are closed
        in the background once their own first token has been timed.
        """
        record: dict[str, Any] = {
            "group": replica_group(participants[0].model),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "winner": None,
            "participants": {},
        }
        self.runtime.last_races.appendleft(record)
        heads: dict[asyncio.Future[Any], tuple[RouteCandidate, Any]] = {}
        for candidate in participants:
            stream = self._attempt_stream(candidate, request, race=record)
            heads[asyncio.ensure_future(stream.__anext__())] = (candidate, stream)
        winner: tuple[RouteCandidate, Any, StreamDelta | _AttemptOutcome] | None = None
        try:
            while heads and winner is None:
                done, _ = await asyncio.wait(set(heads), return_when=asyncio.FIRST_COMPLETED)
                for head in done:
                    candidate, stream = heads.pop(head)
                    first = head.result()
                    if isinstance(first, _AttemptOutcome) and first.failure is not None:
                        failures.append(first.failure)
                        attempts.append(first.attempt)
                        failure_kinds.append(first.failure.kind)
                        await stream.aclose()
                        continue
                    winner = (candidate, stream, first)
                    break
        except BaseException:
            for head in heads:
                head.cancel()
            raise
        if winner is None:
            return
        candidate, stream, first = winner
        record["winner"] = candidate.model.id
        for head, (loser, loser_stream) in heads.items():
            task = asyncio.create_task(self._stop_loser(head, loser, loser_stream, record))
            self.runtime.race_tasks.add(task)
            task.add_done_callback(self.runtime.race_tasks.discard)
        async with aclosing(stream) as events:
            yield first
            if isinstance(first, _AttemptOutcome):
                return
            async for event in events:
                yield event

    async def _stop_loser(
        self, head: asyncio.Future[Any], candidate: RouteCandidate, stream: Any, record: dict[str, Any],
    ) -> None:
        """Let a race loser reach its first token, time it, then close it."""
        try:
            first = await head
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        try:
            if isinstance(first, StreamDelta):
                # Losers keep the first-token figure current at the cost of one
                # prompt evaluation; they are neither successes nor failures.
                first_token_ms = first.first_token_ms
                if first_token_ms is not None:
                    self.runtime.record_first_token(candidate.model.id, first_token_ms)
                record["participants"][candidate.model.id] = {
                    "endpoint": candidate.model.endpoint, "success": True, "stopped": True,
                    "latency_ms": None, "kind": None,
                    "first_token_ms": None if first_token_ms is None else round(first_token_ms, 2),
                }
        finally:
            await stream.aclose()

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
            observation["first_token_ms"] = result.first_token_ms
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
