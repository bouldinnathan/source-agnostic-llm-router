from __future__ import annotations

import asyncio

import pytest

from llm_router.adapters import AdapterRegistry
from llm_router.errors import AllModelsFailed, UpstreamError
from llm_router.router import LLMRouter
from llm_router.schema import QueryRequest, UpstreamResult

from conftest import make_config


class FakeAdapter:
    def __init__(self, failures: set[str]) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        self.calls.append(endpoint.name)
        if endpoint.name in self.failures:
            raise UpstreamError("simulated outage", status_code=503)
        return UpstreamResult(
            text=f"answer from {model.id}",
            usage={"input_tokens": 5, "output_tokens": 3},
            raw={"prompt_eval_count": 5, "eval_count": 3},
            finish_reason="stop",
        )


def _router_with_fake(failures: set[str], *, breaker_failures: int = 1, metrics=None):  # type: ignore[no-untyped-def]
    config = make_config(
        models=[
            {
                "id": "primary-a",
                "endpoint": "source-a",
                "upstream_model": "primary",
                "quality": 0.99,
                "capabilities": {"general": 1.0},
            },
            {
                "id": "fallback-b",
                "endpoint": "source-b",
                "upstream_model": "fallback",
                "quality": 0.80,
                "capabilities": {"general": 1.0},
            },
        ],
        router={
            "max_attempts": 2,
            "circuit_breaker_failures": breaker_failures,
            "circuit_breaker_cooldown_seconds": 60,
        },
    )
    adapter = FakeAdapter(failures)
    registry = AdapterRegistry()
    registry.register("ollama-chat", adapter)
    return LLMRouter(config, adapters=registry, metrics=metrics), adapter


def test_completion_fails_over_to_independent_source() -> None:
    router, adapter = _router_with_fake({"source-a"})

    result = asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))

    assert result.deployment == "fallback-b"
    assert adapter.calls == ["source-a", "source-b"]
    assert [attempt["success"] for attempt in result.attempts] == [False, True]
    assert not router.runtime.is_available("primary-a")


def test_open_circuit_is_skipped_on_next_request() -> None:
    router, adapter = _router_with_fake({"source-a"})
    request = QueryRequest.from_prompt("Hello")

    asyncio.run(router.complete(request))
    adapter.calls.clear()
    result = asyncio.run(router.complete(request))

    assert result.deployment == "fallback-b"
    assert adapter.calls == ["source-b"]


def test_all_failures_are_structured() -> None:
    router, _ = _router_with_fake({"source-a", "source-b"})

    with pytest.raises(AllModelsFailed) as captured:
        asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))

    assert [failure.endpoint for failure in captured.value.failures] == [
        "source-a",
        "source-b",
    ]
    assert all(failure.reason == "simulated outage" for failure in captured.value.failures)


def test_success_updates_health_and_latency() -> None:
    router, _ = _router_with_fake(set())

    asyncio.run(router.complete(QueryRequest.from_prompt("Hello")))
    state = router.runtime.state("primary-a")

    assert state.successes == 1
    assert state.failures == 0
    assert state.latency_ewma_ms is not None


@pytest.mark.parametrize("field,value", [("max_tokens", 16.0), ("min_context_window", 8192.0)])
def test_integral_floats_normalize_to_integers(field: str, value: float) -> None:
    from llm_router.errors import RequestError

    query = QueryRequest.from_prompt("hello", **{field: value})
    assert getattr(query, field) == int(value) and type(getattr(query, field)) is int
    for spelled in (str(int(value)), f"{int(value)}.0", f" {int(value)} "):
        assert getattr(QueryRequest.from_prompt("hello", **{field: spelled}), field) == int(value)
    for bad in (value + 0.5, float("nan"), float("inf"), True, "eight", "", "1e3", "0x10", [8192]):
        with pytest.raises(RequestError, match="must be a whole number"):
            QueryRequest.from_prompt("hello", **{field: bad})


def _traffic(router: LLMRouter) -> dict:
    snapshot = router.metrics.snapshot()
    assert snapshot["available"] is True, snapshot
    return snapshot["traffic"]["totals"]


def test_client_request_counters_follow_the_request_not_the_attempts() -> None:
    router, adapter = _router_with_fake({"source-a"})
    result = asyncio.run(router.complete(QueryRequest.from_prompt("hello")))
    assert result.deployment == "fallback-b" and adapter.calls == ["source-a", "source-b"]
    assert [attempt["kind"] for attempt in result.attempts if not attempt["success"]] == ["http_5xx"]
    totals = _traffic(router)
    assert totals["requests_ok"] == 1 and totals["requests_failed"] == 0, "One rescued request is one success"
    assert totals["reroutes_ok"] == 1 and totals["reroutes_failed"] == 0
    assert totals["failures"] == {"http_5xx": 1}, "The failed attempt still counts as a failure of its kind"
    assert totals["input_tokens"] == 5 and totals["output_tokens"] == 3


def test_clean_success_and_total_failure_are_counted_separately(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from llm_router.metrics import MetricsStore

    clean, _ = _router_with_fake(set(), metrics=MetricsStore(tmp_path / "clean.sqlite3"))
    asyncio.run(clean.complete(QueryRequest.from_prompt("hello")))
    assert _traffic(clean) == {
        "requests_ok": 1, "requests_failed": 0, "reroutes_ok": 0, "reroutes_failed": 0,
        "input_tokens": 5, "output_tokens": 3, "failures": {},
    }
    broken, adapter = _router_with_fake({"source-a", "source-b"}, metrics=MetricsStore(tmp_path / "broken.sqlite3"))
    with pytest.raises(AllModelsFailed):
        asyncio.run(broken.complete(QueryRequest.from_prompt("hello")))
    assert adapter.calls == ["source-a", "source-b"]
    assert _traffic(broken) == {
        "requests_ok": 0, "requests_failed": 1, "reroutes_ok": 0, "reroutes_failed": 1,
        "input_tokens": 0, "output_tokens": 0, "failures": {"http_5xx": 2},
    }


def test_requests_with_no_eligible_model_are_failed_requests_without_attempts() -> None:
    from llm_router.errors import NoEligibleModel

    router, adapter = _router_with_fake(set())
    with pytest.raises(NoEligibleModel):
        asyncio.run(router.complete(QueryRequest.from_prompt("hello", min_context_window=10**9)))
    assert adapter.calls == []
    assert _traffic(router) == {
        "requests_ok": 0, "requests_failed": 1, "reroutes_ok": 0, "reroutes_failed": 0,
        "input_tokens": 0, "output_tokens": 0, "failures": {"no_eligible_model": 1},
    }
    router.route(QueryRequest.from_prompt("hello"))
    assert _traffic(router)["requests_ok"] == 0, "Dry routing is not client traffic"


def test_adapter_crash_is_an_adapter_failure_and_traffic_storage_errors_never_lose_answers(monkeypatch) -> None:
    class CrashingAdapter(FakeAdapter):
        async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
            if endpoint.name == "source-a":
                raise RuntimeError("private adapter crash")
            return await super().complete(endpoint, model, request)

    router, _ = _router_with_fake(set())
    router.adapters.register("ollama-chat", CrashingAdapter(set()))
    result = asyncio.run(router.complete(QueryRequest.from_prompt("hello")))
    assert result.deployment == "fallback-b"
    assert _traffic(router)["failures"] == {"adapter": 1}

    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("private disk failure")

    monkeypatch.setattr(router.metrics, "record_request", broken)
    result = asyncio.run(router.complete(QueryRequest.from_prompt("hello")))
    assert result.text.startswith("answer from")


class TimedAdapter:
    """Replica fake with per-endpoint delays and optional failures."""

    def __init__(self, delays: dict[str, float], failures: set[str] = frozenset()) -> None:
        self.delays = delays
        self.failures = failures
        self.calls: list[str] = []
        self.cancelled: list[str] = []

    async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        self.calls.append(endpoint.name)
        try:
            await asyncio.sleep(self.delays.get(endpoint.name, 0.0))
        except asyncio.CancelledError:
            self.cancelled.append(endpoint.name)
            raise
        if endpoint.name in self.failures:
            raise UpstreamError("simulated outage", status_code=503)
        return UpstreamResult(text=f"answer from {model.id}", usage={}, raw={"prompt_eval_count": 4, "eval_count": 2}, finish_reason="stop")


def _replica_router(adapter, settings=None, *, runtime=None):  # type: ignore[no-untyped-def]
    from llm_router.metrics import MetricsStore
    from llm_router.routing_settings import RoutingSettings

    config = make_config(
        models=[
            {"id": "qwen-a", "endpoint": "source-a", "upstream_model": "qwen", "quality": 0.9, "capabilities": {"general": 1.0}},
            {"id": "qwen-b", "endpoint": "source-b", "upstream_model": "qwen", "quality": 0.9, "capabilities": {"general": 1.0}},
        ],
        router={"max_attempts": 2, "circuit_breaker_failures": 5},
    )
    registry = AdapterRegistry()
    registry.register("ollama-chat", adapter)
    metrics = MetricsStore(_replica_router.tmp / f"{id(adapter)}.sqlite3")
    return LLMRouter(config, adapters=registry, runtime=runtime, metrics=metrics,
                     settings=settings or RoutingSettings(race_replicas=True, race_every=2))


@pytest.fixture(autouse=True)
def _replica_tmp(tmp_path):  # type: ignore[no-untyped-def]
    _replica_router.tmp = tmp_path


async def _settle(router: LLMRouter) -> None:
    while router.runtime.race_tasks:
        await asyncio.sleep(0.005)


def test_every_nth_request_races_all_replicas_and_measures_the_losers() -> None:
    adapter = TimedAdapter({"source-a": 0.08, "source-b": 0.01})
    router = _replica_router(adapter)
    prompt = QueryRequest.from_prompt("hello")

    async def scenario():
        first = await router.complete(prompt)
        assert adapter.calls == ["source-a"] or adapter.calls == ["source-b"]
        assert all("race" not in attempt for attempt in first.attempts)
        second = await router.complete(prompt)
        assert second.deployment == "qwen-b", "The fastest replica answers the raced request"
        assert sorted(adapter.calls[1:]) == ["source-a", "source-b"]
        assert second.attempts[-1]["race"] is True and second.attempts[-1]["success"] is True
        race = router.runtime.last_races[0]
        assert race["group"] == "qwen" and race["winner"] == "qwen-b"
        assert set(race["participants"]) == {"qwen-b"}, "The loser is still running when the answer returns"
        await _settle(router)
        assert set(race["participants"]) == {"qwen-a", "qwen-b"}
        assert race["participants"]["qwen-a"]["success"] is True
        assert race["participants"]["qwen-a"]["latency_ms"] > race["participants"]["qwen-b"]["latency_ms"]
        assert router.runtime.state("qwen-a").successes >= 1 and router.runtime.state("qwen-b").successes == 1
        assert router.runtime.state("qwen-a").active_requests == 0
        traffic = router.metrics.snapshot()["traffic"]["totals"]
        assert traffic["requests_ok"] == 2 and traffic["reroutes_ok"] == 0, "A race is one client request, not a reroute"
        third = await router.complete(prompt)
        assert all("race" not in attempt for attempt in third.attempts), "Races happen every Nth request only"

    asyncio.run(scenario())


def test_race_survives_a_failing_replica_and_all_failures_fall_through() -> None:
    adapter = TimedAdapter({"source-a": 0.05, "source-b": 0.0}, failures={"source-b"})
    router = _replica_router(adapter)
    prompt = QueryRequest.from_prompt("hello")

    async def scenario():
        await router.complete(prompt)
        result = await router.complete(prompt)
        assert result.deployment == "qwen-a"
        assert [attempt["success"] for attempt in result.attempts if attempt.get("race")] == [False, True]
        race = router.runtime.last_races[0]
        assert race["winner"] == "qwen-a" and race["participants"]["qwen-b"]["kind"] == "http_5xx"
        totals = router.metrics.snapshot()["traffic"]["totals"]
        assert totals["failures"] == {"http_5xx": 1} and totals["reroutes_ok"] == 1
        broken = TimedAdapter({}, failures={"source-a", "source-b"})
        bad = _replica_router(broken)
        with pytest.raises(AllModelsFailed):
            await bad.complete(prompt)  # sequential: a fails, b fails
        with pytest.raises(AllModelsFailed) as raced:
            await bad.complete(prompt)  # raced: both fail, nothing left to try
        assert len(raced.value.failures) == 2
        assert bad.runtime.last_races[0]["winner"] is None

    asyncio.run(scenario())


def test_races_skip_pinned_requests_single_replicas_and_disabled_setting() -> None:
    from llm_router.routing_settings import RoutingSettings

    adapter = TimedAdapter({})
    router = _replica_router(adapter)
    pinned = QueryRequest.from_prompt("hello", preferred_endpoints=("source-a",))

    async def scenario():
        for _ in range(4):
            await router.complete(pinned)
        assert adapter.calls == ["source-a"] * 4, "A machine preference is never raced"
        router.settings = RoutingSettings(race_replicas=False)
        for _ in range(4):
            await router.complete(QueryRequest.from_prompt("hello"))
        assert len(adapter.calls) == 8
        router.settings = RoutingSettings(race_replicas=True, race_every=2)
        solo = QueryRequest.from_prompt("hello", allowed_deployments=("qwen-a",))
        for _ in range(4):
            await router.complete(solo)
        assert len(adapter.calls) == 12, "One eligible replica cannot race"

    asyncio.run(scenario())


def test_race_counters_and_history_survive_router_replacement() -> None:
    adapter = TimedAdapter({"source-a": 0.03, "source-b": 0.0})
    first = _replica_router(adapter)
    prompt = QueryRequest.from_prompt("hello")

    async def scenario():
        await first.complete(prompt)
        replacement = _replica_router(adapter, runtime=first.runtime)
        result = await replacement.complete(prompt)
        assert result.attempts[-1].get("race") is True, "Discovery refreshes must not reset the race schedule"
        await _settle(replacement)
        assert replacement.runtime.last_races[0]["winner"] == "qwen-b"

    asyncio.run(scenario())


def test_cancelling_a_raced_request_cancels_every_participant() -> None:
    adapter = TimedAdapter({"source-a": 0.5, "source-b": 0.5})
    router = _replica_router(adapter)
    prompt = QueryRequest.from_prompt("hello")

    async def scenario():
        await router.complete(prompt)  # hmm, this takes 0.5s sequentially; keep delays but accept
        task = asyncio.create_task(router.complete(prompt))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
        assert sorted(adapter.cancelled) == ["source-a", "source-b"]
        assert router.runtime.state("qwen-a").active_requests == 0
        assert router.runtime.state("qwen-b").active_requests == 0
        assert not router.runtime.race_tasks

    asyncio.run(scenario())


def test_attempts_carry_the_configured_stream_timeouts_and_record_first_token(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from llm_router.adapters.base import STREAM_TIMEOUTS
    from llm_router.metrics import MetricsStore
    from llm_router.routing_settings import RoutingSettings

    seen = []

    class TimedFirstToken(TimedAdapter):
        async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
            seen.append(STREAM_TIMEOUTS.get())
            result = await super().complete(endpoint, model, request)
            return UpstreamResult(text=result.text, usage=result.usage, raw=result.raw, finish_reason=result.finish_reason, first_token_ms=42.5)

    adapter = TimedFirstToken({})
    settings = RoutingSettings(first_token_timeout_seconds=600, idle_timeout_seconds=30, max_request_seconds=0)
    router = _replica_router(adapter, settings)
    result = asyncio.run(router.complete(QueryRequest.from_prompt("hello")))
    assert seen[-1].first_token == 600 and seen[-1].idle == 30 and seen[-1].total is None
    assert result.attempts[-1]["first_token_ms"] == 42.5
    assert router.runtime.state(result.deployment).first_token_ewma_ms == 42.5
    router.settings = RoutingSettings(max_request_seconds=120)
    asyncio.run(router.complete(QueryRequest.from_prompt("hello")))
    assert seen[-1].total == 120
    assert STREAM_TIMEOUTS.get().first_token == 300, "The context variable is reset after each attempt"
    row = router.metrics.snapshot()["deployments"][0]
    assert row["metrics"]["first_token_ms"]["latest"] == 42.5


class StreamingFake:
    """Replica fake whose answers arrive as fragments, with per-endpoint timing and failures."""

    def __init__(self, plans: dict[str, dict]) -> None:
        self.plans = plans
        self.calls: list[str] = []
        self.closed: list[str] = []

    async def complete(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        from llm_router.adapters.base import final_result
        return await final_result(self.stream(endpoint, model, request))

    async def stream(self, endpoint, model, request):  # type: ignore[no-untyped-def]
        from llm_router.adapters.base import StreamDelta

        plan = self.plans.get(endpoint.name, {})
        self.calls.append(endpoint.name)
        pieces = plan.get("pieces", ("Hel", "lo"))
        try:
            await asyncio.sleep(plan.get("first_token_delay", 0.0))
            for index, piece in enumerate(pieces):
                if plan.get("fail_after") == index:
                    raise UpstreamError("backend went away", kind="connection")
                yield StreamDelta(text=piece, first_token_ms=1.0 if index == 0 else None)
                await asyncio.sleep(plan.get("piece_delay", 0.0))
            yield UpstreamResult(text="".join(pieces), usage={"prompt_eval_count": 4, "eval_count": len(pieces)}, finish_reason="stop")
        finally:
            self.closed.append(endpoint.name)


def test_complete_stream_relays_fragments_then_the_completion() -> None:
    from llm_router.routing_settings import RoutingSettings
    from llm_router.schema import RoutedCompletion

    adapter = StreamingFake({"source-a": {"pieces": ("Hel", "lo")}})
    router = _replica_router(adapter, RoutingSettings())

    async def scenario():
        events = []
        async for event in router.complete_stream(QueryRequest.from_prompt("hi")):
            events.append(event)
        return events

    events = asyncio.run(scenario())
    assert [event.text for event in events[:-1]] == ["Hel", "lo"]
    completion = events[-1]
    assert isinstance(completion, RoutedCompletion) and completion.text == "Hello"
    assert completion.attempts[-1]["first_token_ms"] == 1.0
    assert router.runtime.state(completion.deployment).first_token_ewma_ms == 1.0
    assert _traffic(router)["requests_ok"] == 1


def test_failure_after_a_relayed_fragment_interrupts_instead_of_retrying() -> None:
    from llm_router.errors import StreamInterrupted
    from llm_router.routing_settings import RoutingSettings

    plans = {"source-a": {"pieces": ("Hel", "lo"), "fail_after": 1}, "source-b": {"pieces": ("Bon", "jour")}}
    prompt = QueryRequest.from_prompt("hi")

    async def relayed():
        adapter = StreamingFake(plans)
        router = _replica_router(adapter, RoutingSettings())
        seen = []
        with pytest.raises(StreamInterrupted) as failure:
            async for event in router.complete_stream(prompt):
                seen.append(event.text)
        assert seen == ["Hel"], "The fragment already sent cannot be taken back, so no retry follows"
        assert failure.value.sent == 3 and failure.value.failure.kind == "connection"
        assert "3 characters" in str(failure.value)
        assert adapter.calls == ["source-a"]
        traffic = _traffic(router)
        assert traffic["requests_failed"] == 1 and traffic["failures"] == {"connection": 1}

    asyncio.run(relayed())

    async def buffered():
        adapter = StreamingFake(plans)
        router = _replica_router(adapter, RoutingSettings())
        completion = await router.complete(prompt)
        assert completion.text == "Bonjour", "Nothing had reached the client, so the request failed over"
        assert adapter.calls == ["source-a", "source-b"]
        assert [attempt["success"] for attempt in completion.attempts] == [False, True]
        assert _traffic(router)["reroutes_ok"] == 1

    asyncio.run(buffered())


def test_races_are_decided_by_first_token_and_losers_stop_after_theirs() -> None:
    plans = {
        "source-a": {"first_token_delay": 0.06, "pieces": ("fast", " finish")},
        "source-b": {"first_token_delay": 0.01, "pieces": ("slow", " but", " first"), "piece_delay": 0.04},
    }
    adapter = StreamingFake(plans)
    router = _replica_router(adapter)
    prompt = QueryRequest.from_prompt("hello")

    async def scenario():
        await router.complete(prompt)
        adapter.calls.clear()
        adapter.closed.clear()
        before = router.runtime.state("qwen-a").successes
        seen = []
        async for event in router.complete_stream(prompt):
            seen.append(event)
        completion = seen[-1]
        assert completion.deployment == "qwen-b", "The replica whose first token came first is relayed"
        assert [event.text for event in seen[:-1]] == ["slow", " but", " first"]
        assert sorted(adapter.calls) == ["source-a", "source-b"]
        await _settle(router)
        assert sorted(adapter.closed) == ["source-a", "source-b"]
        race = router.runtime.last_races[0]
        assert race["winner"] == "qwen-b"
        loser = race["participants"]["qwen-a"]
        assert loser["stopped"] is True and loser["first_token_ms"] == 1.0 and loser["latency_ms"] is None
        assert router.runtime.state("qwen-a").first_token_ewma_ms == 1.0
        assert router.runtime.state("qwen-a").successes == before and router.runtime.state("qwen-a").failures == 0, "A stopped loser is neither a success nor a failure"
        assert router.runtime.state("qwen-a").active_requests == 0
        assert _traffic(router)["requests_ok"] == 2

    asyncio.run(scenario())


def test_closing_the_stream_early_stops_the_backend_and_records_nothing() -> None:
    from llm_router.routing_settings import RoutingSettings

    adapter = StreamingFake({"source-a": {"pieces": ("Hel", "lo"), "piece_delay": 0.5}})
    router = _replica_router(adapter, RoutingSettings())

    async def scenario():
        stream = router.complete_stream(QueryRequest.from_prompt("hi"))
        first = await stream.__anext__()
        assert first.text == "Hel"
        await stream.aclose()
        assert adapter.closed == ["source-a"], "Hanging up closes the backend stream"
        assert router.runtime.state("qwen-a").active_requests == 0
        assert router.runtime.state("qwen-a").successes == 0 and router.runtime.state("qwen-a").failures == 0
        assert _traffic(router)["requests_ok"] == 0 and _traffic(router)["requests_failed"] == 0

    asyncio.run(scenario())
