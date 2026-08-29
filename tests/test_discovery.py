from __future__ import annotations

import asyncio

import httpx

from llm_router.discovery import DiscoverySettings, ModelDiscovery, merge_router_configs

from conftest import make_config


def test_discovery_isolates_failures_and_enrolls_ollama_and_openai() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ollama.test" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"model": "qwen3:14b"},
                        {"model": "nomic-embed-text"},
                    ]
                },
            )
        if request.url.host == "ollama.test" and request.method == "POST":
            return httpx.Response(
                200,
                json={"capabilities": ["completion", "tools"], "model_info": {"num_ctx": 65536}},
            )
        if request.url.host == "lm.test":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "local-coder-32b", "context_length": 131072},
                        {"id": "text-embedding-model"},
                    ]
                },
            )
        raise httpx.ConnectError("offline", request=request)

    settings = DiscoverySettings(
        include_loopback=False,
        include_cloud=False,
        extra_urls=(
            "ollama=http://ollama.test:11434",
            "openai=http://lm.test:1234/v1",
            "openai=http://dead.test:8000/v1",
        ),
        source_priority=("ollama", "openai-compatible"),
    )
    discovery = ModelDiscovery(settings, transport=httpx.MockTransport(handler))

    report = asyncio.run(discovery.discover())

    assert report.successful_sources == 2
    assert len(report.config.models) == 2
    assert {model.upstream_model for model in report.config.models} == {
        "qwen3:14b",
        "local-coder-32b",
    }
    qwen = next(model for model in report.config.models if model.upstream_model == "qwen3:14b")
    assert qwen.capabilities["tool_use"] >= 0.5
    assert qwen.context_window == 65536
    assert qwen.priority == 2
    assert next(
        model for model in report.config.models if model.upstream_model == "local-coder-32b"
    ).priority == 1
    failed = next(probe for probe in report.probes if "dead" in probe.base_url)
    assert failed.reachable is False
    assert failed.error == "unreachable"


def test_cloud_provider_self_enrolls_only_when_credential_exists(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for name in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "MISTRAL_API_KEY",
        "XAI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5"}, {"id": "text-embedding-3-small"}]},
        )

    settings = DiscoverySettings(include_loopback=False, include_cloud=True)
    report = asyncio.run(
        ModelDiscovery(settings, transport=httpx.MockTransport(handler)).discover()
    )

    assert len(report.probes) == 1
    assert [model.upstream_model for model in report.config.models] == ["gpt-5"]
    assert report.config.models[0].quality >= 0.95
    endpoint = next(iter(report.config.endpoints.values()))
    assert endpoint.auth.key_env == "OPENAI_API_KEY"


def test_explicit_config_wins_over_discovered_duplicate() -> None:
    configured = make_config(
        models=[
            {
                "id": "same-id",
                "endpoint": "source-a",
                "upstream_model": "explicit",
                "quality": 0.99,
            }
        ]
    )
    discovered = make_config(
        models=[
            {
                "id": "same-id",
                "endpoint": "source-b",
                "upstream_model": "discovered",
                "quality": 0.4,
            }
        ]
    )

    merged = merge_router_configs(configured, discovered)

    assert len(merged.models) == 1
    assert merged.models[0].upstream_model == "explicit"
    assert merged.models[0].quality == 0.99


def test_lan_scanning_is_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LLM_ROUTER_SCAN_CIDRS", raising=False)
    assert DiscoverySettings.from_env().scan_cidrs == ()

    monkeypatch.setenv("LLM_ROUTER_SCAN_CIDRS", "192.0.2.0/30,198.51.100.10/32")
    assert DiscoverySettings.from_env().scan_cidrs == (
        "192.0.2.0/30",
        "198.51.100.10/32",
    )


def test_source_priority_is_loaded_from_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_SOURCE_PRIORITY", "local, Anthropic, cloud")

    assert DiscoverySettings.from_env().source_priority == (
        "local",
        "anthropic",
        "cloud",
    )
