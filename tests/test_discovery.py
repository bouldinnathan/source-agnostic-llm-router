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
