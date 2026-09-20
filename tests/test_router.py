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
    for bad in (value + 0.5, float("nan"), float("inf"), True, "8192"):
        with pytest.raises(RequestError):
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
