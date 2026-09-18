from __future__ import annotations

from typing import Any
import selectors

import pytest

from llm_router.config import config_from_mapping
from llm_router.schema import RouterConfig


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--poll-thread-wakeups", action="store_true", default=False,
        help="Test-only polling for restricted environments that block event-loop thread wakeups",
    )


@pytest.fixture(autouse=True)
def restricted_environment_thread_wakeups(request, monkeypatch):  # type: ignore[no-untyped-def]
    if request.config.getoption("--poll-thread-wakeups"):
        # Keep actual asyncio executors/SQLite threads. Some network-restricted
        # sandboxes suppress the loop's local self-pipe notification. Periodic
        # polling lets completed callbacks run without replacing or faking I/O.
        # Normal test/CI runs do not install this workaround.
        select = selectors.DefaultSelector.select

        def bounded_select(self, timeout=None):
            return select(self, min(timeout, 0.01) if timeout is not None else 0.01)

        monkeypatch.setattr(selectors.DefaultSelector, "select", bounded_select)


@pytest.fixture(autouse=True)
def isolated_passive_metrics(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """Tests must never read or append to an operator's real metrics database."""
    monkeypatch.setenv("LLM_ROUTER_METRICS_FILE", str(tmp_path / "performance" / "metrics.sqlite3"))
    monkeypatch.setenv("LLM_ROUTER_SAVED_HOSTS_FILE", str(tmp_path / "saved-hosts" / "saved-hosts.json"))
    monkeypatch.delenv("LLM_ROUTER_CONFIG", raising=False)
    monkeypatch.setattr("llm_router.bootstrap.DEFAULT_CONFIG_LOCATIONS", ())


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
