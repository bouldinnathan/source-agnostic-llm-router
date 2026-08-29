from __future__ import annotations

import pytest

from llm_router.errors import NoEligibleModel, RequestError
from llm_router.router import LLMRouter
from llm_router.schema import QueryRequest

from conftest import make_config


def test_quality_strategy_selects_most_capable() -> None:
    router = LLMRouter(make_config())
    request = QueryRequest.from_prompt(
        "Analyze this architecture and find the root cause",
        required_capabilities=("reasoning",),
        strategy="quality",
    )

    decision = router.route(request)

    assert decision.candidates[0].model.id == "frontier-a"
    assert "reasoning" in decision.inferred_capabilities


def test_cost_strategy_selects_budget_deployment() -> None:
    router = LLMRouter(make_config())
    request = QueryRequest.from_prompt("Say hello", strategy="cost")

    decision = router.route(request)

    assert decision.candidates[0].model.id == "budget-b"


def test_hard_capability_filters_ineligible_models() -> None:
    router = LLMRouter(make_config())
    request = QueryRequest.from_prompt(
        "Implement this function",
        required_capabilities=("coding",),
    )

    decision = router.route(request)

    assert [candidate.model.id for candidate in decision.candidates] == ["frontier-a"]
    assert "missing required capability 'coding'" in decision.excluded["budget-b"]


def test_hard_price_limit_excludes_unknown_price() -> None:
    config = make_config(
        models=[
            {
                "id": "unknown",
                "endpoint": "source-a",
                "upstream_model": "model",
                "quality": 0.9,
                "capabilities": {"general": 1.0},
            }
        ]
    )
    router = LLMRouter(config)

    with pytest.raises(NoEligibleModel) as captured:
        router.route(
            QueryRequest.from_prompt("Hello", max_input_cost_per_million=1.0)
        )

    assert "input cost is unknown" in captured.value.excluded["unknown"]


def test_context_and_output_constraints_are_both_enforced() -> None:
    router = LLMRouter(make_config())

    with pytest.raises(NoEligibleModel) as captured:
        router.route(
            QueryRequest.from_prompt(
                "Large task",
                min_context_window=300_000,
                max_tokens=20_000,
            )
        )

    assert any("context window" in reason for reason in captured.value.excluded["frontier-a"])
    assert any("max output" in reason for reason in captured.value.excluded["frontier-a"])


def test_fallback_order_diversifies_endpoints() -> None:
    models = [
        {
            "id": "a-best",
            "endpoint": "source-a",
            "upstream_model": "one",
            "quality": 0.99,
            "capabilities": {"general": 1.0},
        },
        {
            "id": "a-second",
            "endpoint": "source-a",
            "upstream_model": "two",
            "quality": 0.95,
            "capabilities": {"general": 1.0},
        },
        {
            "id": "b-third",
            "endpoint": "source-b",
            "upstream_model": "three",
            "quality": 0.80,
            "capabilities": {"general": 1.0},
        },
    ]
    router = LLMRouter(make_config(models=models))

    decision = router.route(QueryRequest.from_prompt("Hello"))

    assert [candidate.model.id for candidate in decision.candidates] == [
        "a-best",
        "b-third",
        "a-second",
    ]


def test_priority_strategy_honors_operator_priority_after_constraints() -> None:
    router = LLMRouter(
        make_config(
            models=[
                {
                    "id": "quality-cloud",
                    "endpoint": "source-a",
                    "upstream_model": "quality",
                    "quality": 0.98,
                    "priority": 0,
                    "capabilities": {"general": 1.0},
                },
                {
                    "id": "preferred-local",
                    "endpoint": "source-b",
                    "upstream_model": "local",
                    "quality": 0.75,
                    "priority": 10,
                    "capabilities": {"general": 1.0},
                },
            ]
        )
    )

    decision = router.route(QueryRequest.from_prompt("Hello", strategy="priority"))

    assert decision.candidates[0].model.id == "preferred-local"


def test_request_validation_rejects_invalid_limits() -> None:
    with pytest.raises(RequestError, match="max_tokens"):
        QueryRequest.from_prompt("Hello", max_tokens=0)

    with pytest.raises(RequestError, match="non-negative"):
        QueryRequest.from_prompt("Hello", max_input_cost_per_million=-1)
