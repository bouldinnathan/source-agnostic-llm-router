from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from llm_router.bootstrap import bootstrap_router
from llm_router.discovery import DiscoverySettings, ModelDiscovery, merge_router_configs
from llm_router.errors import ConfigError
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, RouterConfig

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
    assert endpoint.machine_id == "openai"


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


def test_named_discovery_keeps_identity_when_machine_address_changes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        return httpx.Response(200, json={"models": [{"model": "qwen3:14b"}]})

    reports = []
    for address in ("192.0.2.10", "198.51.100.20"):
        reports.append(
            asyncio.run(
                ModelDiscovery(
                    DiscoverySettings(
                        include_loopback=False,
                        include_cloud=False,
                        extra_urls=(f"ollama@golemframe=http://{address}:11434",),
                    ),
                    transport=httpx.MockTransport(handler),
                ).discover()
            )
        )

    before, after = reports
    assert before.config.models[0].id == after.config.models[0].id
    assert before.probes[0].source == after.probes[0].source
    first_endpoint = next(iter(before.config.endpoints.values()))
    second_endpoint = next(iter(after.config.endpoints.values()))
    assert first_endpoint.machine_id == second_endpoint.machine_id == "golemframe"
    assert first_endpoint.name == second_endpoint.name
    assert first_endpoint.base_url != second_endpoint.base_url


@pytest.mark.parametrize(
    ("url", "machine_id"),
    [
        ("http://127.0.0.1:1234/v1", "local"),
        ("http://[::1]:1234/v1", "local"),
        ("http://laptop.example:1234/v1", "laptop.example"),
        ("http://192.0.2.8:1234/v1", "192.0.2.8"),
    ],
)
def test_unnamed_discovery_uses_host_identity(url: str, machine_id: str) -> None:
    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_loopback=False,
                include_cloud=False,
                extra_urls=(f"openai={url}",),
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})
            ),
        ).discover()
    )

    assert next(iter(report.config.endpoints.values())).machine_id == machine_id


def test_configured_dns_endpoint_discovers_models_and_preserves_connection_settings(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LAPTOP_API_KEY", "secret-test-key")
    monkeypatch.setenv("LAPTOP_HEADER", "resolved-header")
    endpoint = EndpointConfig(
        name="laptop",
        machine_id="golemframe",
        adapter="openai-chat",
        base_url="https://laptop.example/v1",
        auth=AuthConfig(key_env="LAPTOP_API_KEY", scheme="bearer"),
        headers={"X-Machine": "${LAPTOP_HEADER}"},
        options={"max_tokens_field": "max_completion_tokens"},
        timeout_seconds=42,
        verify_tls=False,
        discover=True,
    )
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer secret-test-key"
        assert request.headers["X-Machine"] == "resolved-header"
        return httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_loopback=False, include_cloud=False),
            configured=configured,
            transport=httpx.MockTransport(handler),
        ).discover()
    )

    assert seen == ["https://laptop.example/v1/models"]
    assert report.probes[0].source == "laptop"
    assert report.probes[0].endpoint == "laptop"
    assert report.probes[0].to_dict()["endpoint"] == "laptop"
    assert report.config.endpoints["laptop"] is endpoint
    assert report.config.models[0].endpoint == "laptop"


def test_configured_endpoint_takes_precedence_over_automatic_enrollment() -> None:
    endpoint = EndpointConfig(
        name="preferred-identity",
        machine_id="golemframe",
        adapter="ollama-chat",
        base_url="http://127.0.0.1:11434",
        discover=True,
    )
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=())
    get_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port != 11434:
            raise httpx.ConnectError("offline", request=request)
        if request.method == "GET":
            get_requests.append(str(request.url))
            return httpx.Response(200, json={"models": [{"model": "qwen3:14b"}]})
        return httpx.Response(200, json={})

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_cloud=False,
                extra_urls=("ollama@other-name=http://127.0.0.1:11434",),
            ),
            configured=configured,
            transport=httpx.MockTransport(handler),
        ).discover()
    )

    assert get_requests == ["http://127.0.0.1:11434/api/tags"]
    assert len(report.config.models) == 1
    assert report.config.models[0].endpoint == "preferred-identity"


def test_enabled_configured_model_enrolls_source_and_explicit_replica_overrides_discovery() -> None:
    endpoint = EndpointConfig(
        name="laptop", adapter="ollama-chat", base_url="http://laptop.example:11434"
    )
    explicit = ModelConfig(
        id="my-custom-id",
        endpoint="laptop",
        upstream_model="qwen3:14b",
        quality=0.99,
        replica_group="qwen",
    )
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=(explicit,))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={})
        return httpx.Response(
            200, json={"models": [{"model": "qwen3:14b"}, {"model": "qwen3:32b"}]}
        )

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_loopback=False, include_cloud=False),
            configured=configured,
            transport=httpx.MockTransport(handler),
        ).discover()
    )
    merged = merge_router_configs(configured, report.config)

    assert len(merged.models) == 2
    assert next(model for model in merged.models if model.id == "my-custom-id") is explicit
    assert [model.upstream_model for model in merged.models].count("qwen3:14b") == 1

    disabled = replace(configured, models=(replace(explicit, enabled=False),))
    merged_disabled = merge_router_configs(disabled, report.config)
    assert not next(model for model in merged_disabled.models if model.id == "my-custom-id").enabled
    assert [model.upstream_model for model in merged_disabled.models].count("qwen3:14b") == 1


def test_bootstrap_keeps_known_discoverable_machine_when_it_starts_offline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    endpoint = EndpointConfig(
        name="laptop",
        adapter="ollama-chat",
        base_url="http://laptop.example:11434",
        discover=True,
    )
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=())
    monkeypatch.setattr("llm_router.bootstrap.load_optional_config", lambda path: configured)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(
        "llm_router.bootstrap.ModelDiscovery",
        lambda settings, *, configured: ModelDiscovery(
            settings, configured=configured, transport=httpx.MockTransport(offline)
        ),
    )
    result = asyncio.run(
        bootstrap_router(settings=DiscoverySettings(include_loopback=False, include_cloud=False))
    )

    assert result.router.config.models == ()
    assert result.router.config.endpoints["laptop"] is endpoint
    assert result.discovery.probes[0].source == "laptop"
    assert not result.discovery.probes[0].reachable


def test_bootstrap_without_models_or_known_discoverable_machines_still_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("llm_router.bootstrap.load_optional_config", lambda path: None)
    with pytest.raises(ConfigError, match="No LLM models"):
        asyncio.run(bootstrap_router(settings=DiscoverySettings(enabled=False)))


@pytest.mark.parametrize(
    "source",
    [
        "ollama@golemframe=http://laptop.example:11434",
        "ollama=http://laptop.example:11434",
        "openai@pantheon=http://pantheon.example:1234/v1",
        "http://laptop.example:11434",
    ],
)
def test_bootstrap_keeps_explicit_extra_sources_that_have_never_connected(
    monkeypatch, source: str,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("llm_router.bootstrap.load_optional_config", lambda path: None)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(
        "llm_router.bootstrap.ModelDiscovery",
        lambda settings, *, configured: ModelDiscovery(
            settings, configured=configured, transport=httpx.MockTransport(offline)
        ),
    )
    settings = DiscoverySettings(
        include_loopback=False, include_cloud=False, extra_urls=(source,)
    )

    result = asyncio.run(bootstrap_router(settings=settings))

    assert result.router.config.models == ()
    assert result.router.config.endpoints
    assert all(endpoint.discover for endpoint in result.router.config.endpoints.values())
    assert all(not probe.reachable for probe in result.discovery.probes)


def test_failed_automatic_scans_do_not_enroll_unknown_machines() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_cloud=False, scan_cidrs=("192.0.2.0/30",)),
            transport=httpx.MockTransport(offline),
        ).discover()
    )

    assert len(report.probes) > 7
    assert report.config.endpoints == {}
    assert report.config.models == ()


def test_offline_named_source_keeps_health_identity_after_address_change() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    reports = [
        asyncio.run(
            ModelDiscovery(
                DiscoverySettings(
                    include_loopback=False,
                    include_cloud=False,
                    extra_urls=(f"ollama@golemframe=http://{address}:11434",),
                ),
                transport=httpx.MockTransport(offline),
            ).discover()
        )
        for address in ("192.0.2.10", "198.51.100.20")
    ]
    before, after = reports
    before_endpoint = next(iter(before.config.endpoints.values()))
    after_endpoint = next(iter(after.config.endpoints.values()))

    assert before_endpoint.name == after_endpoint.name
    assert before_endpoint.machine_id == after_endpoint.machine_id == "golemframe"
    assert before_endpoint.base_url != after_endpoint.base_url
    assert before.probes[0].source == after.probes[0].source
    assert after.probes[0].base_url == after_endpoint.base_url
    assert before.probes[0].endpoint == after.probes[0].endpoint == after_endpoint.name


@pytest.mark.parametrize("base_url", ["https://anthropic.example", "https://anthropic.example/v1"])
def test_configured_anthropic_discovery_uses_api_version_and_single_version_path(
    monkeypatch, base_url: str,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "test-key")
    endpoint = EndpointConfig(
        name="anthropic-source",
        adapter="anthropic-messages",
        base_url=base_url,
        discover=True,
        auth=AuthConfig(key_env="ANTHROPIC_TEST_KEY"),
        options={"api_version": "2023-06-01", "path": "/messages"},
    )
    configured = RouterConfig(endpoints={endpoint.name: endpoint}, models=())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://anthropic.example/v1/models"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == endpoint.options["api_version"]
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet"}]})

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_loopback=False, include_cloud=False),
            configured=configured,
            transport=httpx.MockTransport(handler),
        ).discover()
    )

    assert report.successful_sources == 1
    assert report.probes[0].endpoint == endpoint.name


@pytest.mark.parametrize(
    ("first", "second"),
    [("lab.a", "lab-a"), ("Golemframe", "golemframe"), ("a" * 120 + "first", "a" * 120 + "second")],
)
def test_distinct_machine_names_do_not_collapse_to_same_discovery_identity(
    first: str, second: str,
) -> None:
    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_loopback=False,
                include_cloud=False,
                extra_urls=(
                    f"openai@{first}=http://one.example:1234/v1",
                    f"openai@{second}=http://two.example:1234/v1",
                ),
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})
            ),
        ).discover()
    )

    assert len(report.config.endpoints) == 2
    assert {endpoint.machine_id for endpoint in report.config.endpoints.values()} == {first, second}
    assert len({model.id for model in report.config.models}) == 2


def test_reusing_named_source_for_conflicting_urls_fails_before_probing() -> None:
    discoverer = ModelDiscovery(
        DiscoverySettings(
            include_loopback=False,
            include_cloud=False,
            extra_urls=(
                "ollama@golemframe=http://one.example:11434",
                "ollama@golemframe=http://two.example:11434",
            ),
        )
    )

    with pytest.raises(ConfigError, match="multiple URLs or protocols"):
        asyncio.run(discoverer.discover())


def test_configured_and_automatic_sources_cannot_overwrite_conflicting_identity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    endpoint = EndpointConfig(
        name="auto-ollama-loopback",
        adapter="openai-chat",
        base_url="http://different.example:1234/v1",
        discover=True,
    )
    discoverer = ModelDiscovery(
        DiscoverySettings(include_cloud=False),
        configured=RouterConfig(endpoints={endpoint.name: endpoint}, models=()),
    )

    with pytest.raises(ConfigError, match="multiple URLs or protocols"):
        asyncio.run(discoverer.discover())


def test_unnamed_services_with_same_host_and_different_paths_get_distinct_identity() -> None:
    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_loopback=False,
                include_cloud=False,
                extra_urls=(
                    "openai=http://host.example:1234/first/v1",
                    "openai=http://host.example:1234/second/v1",
                ),
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})
            ),
        ).discover()
    )

    assert len(report.config.endpoints) == 2
    assert len({model.id for model in report.config.models}) == 2


def test_disabled_configured_service_cannot_be_reenrolled_automatically(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    endpoint = EndpointConfig(
        name="golemframe",
        adapter="ollama-chat",
        base_url="http://localhost:11434",
    )
    configured = RouterConfig(
        endpoints={endpoint.name: endpoint},
        models=(ModelConfig(id="disabled-qwen", endpoint="golemframe", upstream_model="qwen3:14b", enabled=False),),
    )
    seen: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.port)
        if request.url.port == 11434:
            return httpx.Response(
                200, json={"models": [{"model": "qwen3:14b"}], "data": [{"id": "qwen3:14b"}]}
            )
        raise httpx.ConnectError("offline", request=request)

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_cloud=False,
                extra_urls=("http://127.0.0.1:11434", "ollama@override=http://localhost:11434"),
            ),
            configured=configured,
            transport=httpx.MockTransport(handler),
        ).discover()
    )

    assert 11434 not in seen
    assert report.config.models == ()
    assert all(probe.base_url not in {"http://127.0.0.1:11434", "http://localhost:11434"} for probe in report.probes)
    merged = merge_router_configs(configured, report.config)
    assert len(merged.models) == 1
    assert not merged.models[0].enabled


def test_reachable_implicit_server_without_models_remains_discoverable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 11434:
            return httpx.Response(200, json={"models": []})
        raise httpx.ConnectError("offline", request=request)

    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_cloud=False),
            transport=httpx.MockTransport(handler),
        ).discover()
    )

    assert report.config.models == ()
    assert len(report.config.endpoints) == 1
    endpoint = next(iter(report.config.endpoints.values()))
    assert endpoint.base_url == "http://127.0.0.1:11434"
    assert endpoint.discover
    assert next(probe for probe in report.probes if probe.endpoint == endpoint.name).reachable


def test_unnamed_source_identifiers_do_not_expose_url_credentials_or_private_paths() -> None:
    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(
                include_loopback=False,
                include_cloud=False,
                extra_urls=(
                    "openai=https://user-secret:password-secret@host.example:1234/path-secret/v1",
                ),
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})
            ),
        ).discover()
    )

    identifiers = [report.config.models[0].id, report.probes[0].source, report.probes[0].endpoint]
    for identifier in identifiers:
        assert identifier is not None
        assert "host-example" in identifier
        assert all(secret not in identifier for secret in ("user-secret", "password-secret", "path-secret"))


def test_backend_metadata_gives_real_context_windows_and_model_types() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ollama.test" and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": "qwen3:1.7b"}]})
        if request.url.host == "ollama.test" and request.url.path == "/api/show":
            return httpx.Response(200, json={
                "capabilities": ["completion", "tools", "thinking"],
                "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960, "qwen3.embedding_length": 2048},
                "parameters": "top_k 20",
            })
        if request.url.host == "lm.test" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "nemotron-3.5-lightning-30b"}, {"id": "nomic-thing"}, {"id": "gemma-3-12b"}, {"id": "embeddinggemma-latest"}, {"id": "plain-server-model"}]})
        if request.url.host == "lm.test" and request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"data": [
                {"id": "nemotron-3.5-lightning-30b", "type": "llm", "state": "loaded", "max_context_length": 1048576, "loaded_context_length": 8192, "capabilities": ["tool_use"]},
                {"id": "nomic-thing", "type": "embeddings", "state": "not-loaded", "max_context_length": 2048},
                {"id": "gemma-3-12b", "type": "vlm", "state": "not-loaded", "max_context_length": 131072},
                {"id": "embeddinggemma-latest", "type": "llm", "state": "not-loaded", "max_context_length": 2048},
            ]})
        if request.url.host == "plain.test" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "some-model"}]})
        if request.url.host == "plain.test":
            return httpx.Response(404, json={"error": "no such API"})
        raise httpx.ConnectError("offline", request=request)

    settings = DiscoverySettings(
        include_loopback=False, include_cloud=False,
        extra_urls=("ollama=http://ollama.test:11434", "openai=http://lm.test:1234/v1", "openai=http://plain.test:8000/v1"),
    )
    report = asyncio.run(ModelDiscovery(settings, transport=httpx.MockTransport(handler)).discover())
    models = {model.upstream_model: model for model in report.config.models}
    assert models["qwen3:1.7b"].context_window == 40960, "Ollama's architecture-prefixed context length is read"
    assert models["nemotron-3.5-lightning-30b"].context_window == 8192, "LM Studio serves the context a model was loaded with"
    assert models["nemotron-3.5-lightning-30b"].capabilities["tool_use"] >= 0.9, "LM Studio's declared tool_use capability is trusted"
    assert "nomic-thing" not in models, "LM Studio says it is an embedding model, whatever its name"
    assert "embeddinggemma-latest" not in models, "An embedding model LM Studio mislabels as an llm is still caught by its name"
    assert models["gemma-3-12b"].context_window == 131072 and models["gemma-3-12b"].capabilities.get("vision", 0) >= 0.9
    from llm_router.discovery import infer_model_profile
    assert models["some-model"].context_window == infer_model_profile("some-model", provider="openai-compatible", local=False)["context_window"], "A server without LM Studio's API keeps the inferred default"


# --- local port sweep ---------------------------------------------------------


def _listening(*addresses: tuple[str, int]):  # type: ignore[no-untyped-def]
    async def enumerate_addresses() -> tuple[tuple[str, int], ...]:
        return addresses

    return enumerate_addresses


def test_local_port_sweep_finds_ollama_and_lm_studio_on_any_port() -> None:
    asked: set[int] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        port, path = request.url.port, request.url.path
        asked.add(port)
        if port == 11500:
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen3:8b", "details": {"family": "qwen3"}}]})
            if path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["completion", "tools"], "model_info": {"qwen3.context_length": 40960}})
        if port == 1235:
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gemma-3-12b"}]})
            if path == "/api/v0/models":
                return httpx.Response(200, json={"data": [{"id": "gemma-3-12b", "type": "llm", "max_context_length": 131072}]})
        if port == 8081 and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "served-model"}]})
        if port == 8088 and path == "/api/tags":  # another llm-router, unkeyed
            return httpx.Response(200, json={"models": [{"model": "auto", "details": {"family": "llm-router"}}]})
        if port == 8089:  # another llm-router, keyed
            return httpx.Response(401, json={"error": "Invalid or missing gateway API key"})
        if port == 9999:  # some other HTTP service
            return httpx.Response(404, text="not here")
        if port == 7000:  # JSON, but not a model list
            return httpx.Response(200, json={"status": "ok"})
        if port == 7001:  # HTML for every path
            return httpx.Response(200, text="<html>hello</html>")
        raise httpx.ConnectError("offline", request=request)

    discovery = ModelDiscovery(
        DiscoverySettings(include_cloud=False),
        transport=httpx.MockTransport(handler),
        listening_addresses=_listening(
            ("127.0.0.1", 11500), ("[::1]", 11500), ("127.0.0.1", 1235), ("127.0.0.1", 8081),
            ("127.0.0.1", 8088), ("127.0.0.1", 8089), ("127.0.0.1", 9999), ("127.0.0.1", 7000),
            ("127.0.0.1", 7001), ("127.0.0.1", 631), ("127.0.0.1", 22),
        ),
    )
    report = asyncio.run(discovery.discover())

    assert {model.upstream_model for model in report.config.models} == {"qwen3:8b", "gemma-3-12b", "served-model"}
    swept = {probe.source: probe for probe in report.probes if "-local-" in probe.source}
    assert set(swept) == {"ollama-local-11500", "lm-studio-local-1235", "openai-compatible-local-8081"}
    assert all(probe.reachable for probe in swept.values())
    assert swept["ollama-local-11500"].base_url == "http://127.0.0.1:11500"
    assert swept["lm-studio-local-1235"].base_url == "http://127.0.0.1:1235/v1"
    assert all(endpoint.machine_id == "local" for endpoint in report.config.endpoints.values())
    assert {endpoint.adapter for endpoint in report.config.endpoints.values()} == {"ollama-chat", "openai-chat"}
    qwen = next(model for model in report.config.models if model.upstream_model == "qwen3:8b")
    assert qwen.context_window == 40960 and qwen.capabilities["tool_use"] >= 0.5
    gemma = next(model for model in report.config.models if model.upstream_model == "gemma-3-12b")
    assert gemma.context_window == 131072
    assert "lm-studio" in gemma.tags
    assert not {8088, 8089} & {httpx.URL(endpoint.base_url).port for endpoint in report.config.endpoints.values()}
    assert not {22, 631} & asked, "privileged ports are never asked"


def test_local_port_sweep_leaves_fixed_and_configured_ports_to_their_own_probes() -> None:
    asked: list[tuple[int, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.port, request.url.path))
        if request.url.port in {11434, 5005} and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": f"model-{request.url.port}"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        raise httpx.ConnectError("offline", request=request)

    configured = RouterConfig(
        endpoints={"studio": EndpointConfig(name="studio", adapter="ollama-chat", base_url="http://localhost:5005", discover=True)},
        models=(), policy=make_config().policy, source_path=None,
    )
    report = asyncio.run(
        ModelDiscovery(
            DiscoverySettings(include_cloud=False),
            transport=httpx.MockTransport(handler),
            configured=configured,
            listening_addresses=_listening(("127.0.0.1", 11434), ("[::1]", 11434), ("127.0.0.1", 5005), ("0.0.0.0", 1234)),
        ).discover()
    )

    assert set(report.config.endpoints) == {"auto-ollama-loopback", "studio"}
    assert {model.upstream_model for model in report.config.models} == {"model-11434", "model-5005"}
    assert not [probe for probe in report.probes if "-local-" in probe.source]
    # The claimed ports were probed by their owners exactly once each, not by the sweep as well.
    assert asked.count((11434, "/api/tags")) == 1 and asked.count((5005, "/api/tags")) == 1


def test_local_port_sweep_can_be_turned_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    async def enumerate_addresses() -> tuple[tuple[str, int], ...]:
        nonlocal calls
        calls += 1
        return (("127.0.0.1", 11500),)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    for settings in (
        DiscoverySettings(include_cloud=False, sweep_local_ports=False),
        DiscoverySettings(include_cloud=False, include_loopback=False),
    ):
        asyncio.run(ModelDiscovery(settings, transport=httpx.MockTransport(offline), listening_addresses=enumerate_addresses).discover())
    assert calls == 0
    asyncio.run(ModelDiscovery(DiscoverySettings(include_cloud=False), transport=httpx.MockTransport(offline), listening_addresses=enumerate_addresses).discover())
    assert calls == 1

    monkeypatch.delenv("LLM_ROUTER_DISCOVER_LOCAL_PORTS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_MAX_LOCAL_PORTS", raising=False)
    assert DiscoverySettings.from_env().sweep_local_ports is True
    assert DiscoverySettings.from_env().max_local_ports == 512
    monkeypatch.setenv("LLM_ROUTER_DISCOVER_LOCAL_PORTS", "off")
    monkeypatch.setenv("LLM_ROUTER_MAX_LOCAL_PORTS", "99999")
    assert DiscoverySettings.from_env().sweep_local_ports is False
    assert DiscoverySettings.from_env().max_local_ports == 4096


def test_local_port_sweep_remembers_ports_that_are_not_model_servers() -> None:
    from llm_router import discovery as module

    serve_ollama = False
    tags_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tags_requests
        if request.url.port == 7000 and request.url.path == "/api/tags":
            tags_requests += 1
            if serve_ollama:
                return httpx.Response(200, json={"models": [{"model": "late:1b"}]})
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        raise httpx.ConnectError("offline", request=request)

    def run() -> set[str]:
        report = asyncio.run(
            ModelDiscovery(
                DiscoverySettings(include_cloud=False), transport=httpx.MockTransport(handler),
                listening_addresses=_listening(("127.0.0.1", 7000)),
            ).discover()
        )
        return {model.upstream_model for model in report.config.models}

    assert run() == set() and tags_requests == 1
    serve_ollama = True
    assert run() == set() and tags_requests == 1, "a rejected port is not asked again straight away"
    module._sweep_rejections[("127.0.0.1", 7000)] -= module._SWEEP_NEGATIVE_TTL_SECONDS
    assert run() == {"late:1b"}
    assert ("127.0.0.1", 7000) not in module._sweep_rejections


def test_proc_net_tcp_tables_yield_connectable_listening_addresses() -> None:
    from llm_router.discovery import _parse_proc_net_tcp

    tcp = "\n".join([
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
        "   0: 00000000:2CAA 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1 0 100 0 0 10 0",  # 0.0.0.0:11434
        "   1: 0100007F:04D2 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1 0 100 0 0 10 0",  # 127.0.0.1:1234
        "   2: 0501A8C0:2CAB 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 3 1 0 100 0 0 10 0",  # 192.168.1.5:11435
        "   3: 0100007F:9E69 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 4 1 0 100 0 0 10 0",  # established, not listening
        "   4: garbage line",
    ])
    tcp6 = "\n".join([
        "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
        "   0: 00000000000000000000000001000000:04D3 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5 1 0 100 0 0 10 0",  # [::1]:1235
        "   1: 00000000000000000000000000000000:1F40 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 6 1 0 100 0 0 10 0",  # [::]:8000
        "   2: 000080FE000000000000000001000000:1F41 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 7 1 0 100 0 0 10 0",  # fe80::1, skipped
        "   3: 0000000000000000FFFF00000100007F:1F42 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 8 1 0 100 0 0 10 0",  # ::ffff:127.0.0.1:8002
    ])
    assert _parse_proc_net_tcp(tcp) == [("127.0.0.1", 11434), ("127.0.0.1", 1234), ("192.168.1.5", 11435)]
    assert _parse_proc_net_tcp(tcp6) == [("[::1]", 1235), ("127.0.0.1", 8000), ("[::1]", 8000), ("127.0.0.1", 8002)]


def test_sweep_candidates_prefer_loopback_and_skip_claimed_and_privileged_ports() -> None:
    from llm_router.discovery import _sweep_candidates, _loopback_key

    addresses = [
        ("[::1]", 1235), ("127.0.0.1", 1235), ("192.168.1.5", 1235),
        ("127.0.0.1", 11434), ("127.0.0.1", 631), ("127.0.0.1", 80), ("0.0.0.0", 9000),
    ]
    claimed = {_loopback_key("localhost", 11434)}
    assert _sweep_candidates(addresses, claimed, 10) == [
        (80, ["127.0.0.1"]), (1235, ["127.0.0.1", "[::1]", "192.168.1.5"]), (9000, ["0.0.0.0"]),
    ]
    assert _sweep_candidates(addresses, claimed, 1) == [(80, ["127.0.0.1"])]


def test_loopback_connect_sweep_reports_only_open_ports() -> None:
    import socket

    from llm_router.discovery import _loopback_connect_sweep

    async def scenario() -> tuple[tuple[str, int], ...]:
        server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
        open_port = server.sockets[0].getsockname()[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        try:
            return await _loopback_connect_sweep([open_port, closed_port], concurrency=2)
        finally:
            server.close()
            await server.wait_closed()

    found = asyncio.run(scenario())
    assert len(found) == 1 and found[0][0] == "127.0.0.1"


def test_local_port_sweep_never_breaks_discovery() -> None:
    async def broken() -> tuple[tuple[str, int], ...]:
        raise RuntimeError("no socket table today")

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    report = asyncio.run(
        ModelDiscovery(DiscoverySettings(include_cloud=False), transport=httpx.MockTransport(offline), listening_addresses=broken).discover()
    )
    assert report.config.models == ()
    assert any(probe.source == "ollama-loopback" for probe in report.probes)


# --- peer gateways --------------------------------------------------------------


def _fleet_listing(instance: str = "router-b") -> dict:  # type: ignore[type-arg]
    return {"router": "llm-router", "version": "x", "instance": instance, "deployments": [
        {"id": "qwen-a", "upstream_model": "qwen3:14b", "replica_group": "qwen3:14b", "endpoint": "ollama-a",
         "machine_id": "golemframe", "adapter": "ollama-chat", "backend": "http://golemframe:11434", "quality": 0.84,
         "context_window": 40960, "max_output_tokens": 8192, "estimated_latency_ms": 1500, "reliability": 0.9,
         "capabilities": {"general": 0.9, "tool_use": 0.9}, "tags": ["discovered", "local", "ollama"],
         "max_concurrent_requests": 2, "available": True, "origin": instance, "via": [instance]},
    ]}


def _peer_transport(*, secret: str | None = "peer-secret", instance: str = "router-b", host: str = "router-b.test", port: int = 8088):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == host and request.url.port == port and request.url.path == "/router/fleet":
            if secret is not None and request.headers.get("authorization") != f"Bearer {secret}":
                return httpx.Response(401, json={"error": "Invalid or missing gateway API key"})
            return httpx.Response(200, json=_fleet_listing(instance))
        raise httpx.ConnectError("offline", request=request)

    return httpx.MockTransport(handler)


def test_peer_gateways_are_enrolled_as_flattened_machines(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_PEER_KEY", "peer-secret")
    report = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, peers=("b@router-b.test",)),
        transport=_peer_transport(),
    ).discover())

    assert set(report.config.endpoints) == {"peer-b-golemframe"}
    endpoint = report.config.endpoints["peer-b-golemframe"]
    assert endpoint.base_url == "http://router-b.test:8088" and endpoint.auth.key_env == "LLM_ROUTER_PEER_KEY"
    assert endpoint.health_path == "/router/machines/golemframe/healthz" and endpoint.machine_id == "golemframe"
    model = report.config.models[0]
    assert model.upstream_model == "deployment:qwen-a" and model.context_window == 40960 and model.replica_group == "qwen3:14b"
    probe = report.probes[0]
    assert probe.source == "peer-b" and probe.reachable and probe.enrolled_models == 1 and probe.endpoint == "peer-b-golemframe"


def test_the_gateway_key_is_used_for_peers_when_no_peer_key_is_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "shared")
    report = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, peers=("router-b.test",)),
        transport=_peer_transport(secret="shared"),
    ).discover())
    assert set(report.config.endpoints) == {"peer-router-b-test-8088-golemframe"}
    assert report.config.endpoints["peer-router-b-test-8088-golemframe"].auth.key_env == "LLM_ROUTER_GATEWAY_API_KEY"

    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "wrong")
    report = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, peers=("router-b.test",)),
        transport=_peer_transport(secret="shared"),
    ).discover())
    assert report.config.endpoints == {} and report.probes[0].error == "authentication rejected"


def test_a_peer_that_is_this_router_contributes_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_INSTANCE_ID", "me")
    report = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, peers=("router-b.test",)),
        transport=_peer_transport(secret=None, instance="me"),
    ).discover())
    assert report.config.endpoints == {} and report.config.models == ()
    assert report.probes[0].reachable is True and "no deployments" in (report.probes[0].error or "")


def test_an_unreachable_peer_keeps_its_machines_enrolled_offline() -> None:
    from llm_router.bootstrap import BootstrapResult
    from llm_router.gateway import RouterGateway
    from llm_router.router import LLMRouter

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    settings = DiscoverySettings(include_loopback=False, include_cloud=False, peers=("b@router-b.test",))
    known = asyncio.run(ModelDiscovery(settings, transport=_peer_transport(secret=None)).discover()).config
    previous = LLMRouter(known)

    report = asyncio.run(ModelDiscovery(settings, transport=httpx.MockTransport(offline), previous=known).discover())

    probe = report.probes[0]
    assert probe.reachable is False and probe.error == "unreachable" and probe.endpoint == "peer-b-golemframe"
    assert set(report.config.endpoints) == {"peer-b-golemframe"} and report.config.models == ()
    retained = RouterGateway._retain_failed_sources(
        BootstrapResult(router=LLMRouter(report.config), discovery=report, configured=None), previous,
    )
    assert [model.id for model in retained.router.config.models] == ["peer-b-golemframe:qwen-a"]

    nothing_known = asyncio.run(ModelDiscovery(settings, transport=httpx.MockTransport(offline)).discover())
    assert nothing_known.config.endpoints == {} and nothing_known.probes[0].reachable is False


def test_lan_scan_finds_gateways_on_the_fleet_port() -> None:
    report = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, scan_cidrs=("192.0.2.0/30",)),
        transport=_peer_transport(secret=None, host="192.0.2.2"),
    ).discover())
    assert set(report.config.endpoints) == {"peer-192-0-2-2-8088-golemframe"}
    sources = {probe.source for probe in report.probes}
    assert {"lan-peer-192-0-2-1-8088", "lan-peer-192-0-2-2-8088", "lan-ollama-192.0.2.1"} <= sources

    quiet = asyncio.run(ModelDiscovery(
        DiscoverySettings(include_loopback=False, include_cloud=False, scan_cidrs=("192.0.2.0/30",), scan_peers=False),
        transport=_peer_transport(secret=None, host="192.0.2.2"),
    ).discover())
    assert quiet.config.endpoints == {}


def test_peer_settings_come_from_the_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert DiscoverySettings.from_env().peers == () and DiscoverySettings.from_env().scan_peers is True
    monkeypatch.setenv("LLM_ROUTER_PEERS", "b@router-b.test, 10.0.0.5:9000")
    monkeypatch.setenv("LLM_ROUTER_SCAN_PEERS", "no")
    monkeypatch.setenv("LLM_ROUTER_PEER_PORT", "18088")
    settings = DiscoverySettings.from_env()
    assert settings.peers == ("b@router-b.test", "10.0.0.5:9000") and settings.scan_peers is False and settings.peer_port == 18088
