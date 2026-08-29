from __future__ import annotations

from typing import Any

from llm_router.config import config_from_mapping
from llm_router.schema import RouterConfig


def make_config(
    *,
    models: list[dict[str, Any]] | None = None,
    router: dict[str, Any] | None = None,
) -> RouterConfig:
    return config_from_mapping(
        {
            "router": router or {},
            "endpoints": {
                "source-a": {
                    "adapter": "ollama-chat",
                    "base_url": "http://source-a.invalid",
                },
                "source-b": {
                    "adapter": "ollama-chat",
                    "base_url": "http://source-b.invalid",
                },
            },
            "models": models
            or [
                {
                    "id": "frontier-a",
                    "endpoint": "source-a",
                    "upstream_model": "frontier",
                    "quality": 0.98,
                    "context_window": 200_000,
                    "max_output_tokens": 16_000,
                    "input_cost_per_million": 10.0,
                    "output_cost_per_million": 30.0,
                    "estimated_latency_ms": 8_000,
                    "capabilities": {
                        "general": 1.0,
                        "reasoning": 1.0,
                        "coding": 0.98,
                    },
                },
                {
                    "id": "budget-b",
                    "endpoint": "source-b",
                    "upstream_model": "budget",
                    "quality": 0.70,
                    "context_window": 64_000,
                    "max_output_tokens": 8_000,
                    "input_cost_per_million": 0.1,
                    "output_cost_per_million": 0.2,
                    "estimated_latency_ms": 400,
                    "capabilities": {
                        "general": 0.78,
                        "reasoning": 0.55,
                        "coding": 0.45,
                    },
                },
            ],
        }
    )
