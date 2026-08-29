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
            finish_reason="stop",
        )


def _router_with_fake(failures: set[str], *, breaker_failures: int = 1):
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
    return LLMRouter(config, adapters=registry), adapter


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
