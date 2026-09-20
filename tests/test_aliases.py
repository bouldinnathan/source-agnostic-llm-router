from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from llm_router.adapters import AdapterRegistry
from llm_router.aliases import alias_conflicts, build_aliases
from llm_router.errors import AllModelsFailed, NoEligibleModel, UpstreamError
from llm_router.router import LLMRouter
from llm_router.schema import EndpointConfig, ModelConfig, QueryRequest, RouterConfig, UpstreamResult


def fleet() -> RouterConfig:
    return RouterConfig(
        endpoints={
            "golemframe-ollama": EndpointConfig(
                "golemframe-ollama", "ollama-chat", machine_id="golemframe"
            ),
            "pantheon-lmstudio": EndpointConfig(
                "pantheon-lmstudio", "openai-chat", machine_id="pantheon"
            ),
        },
        models=(
            ModelConfig(
                "qwen-golemframe", "golemframe-ollama", "qwen3:14b", replica_group="qwen",
                estimated_latency_ms=10_000, capabilities={"tools": 1.0},
            ),
            ModelConfig(
                "qwen-pantheon", "pantheon-lmstudio", "qwen3-14b-gguf", replica_group="qwen",
                estimated_latency_ms=100, capabilities={"tools": 1.0},
            ),
            ModelConfig(
                "other", "pantheon-lmstudio", "other-model", estimated_latency_ms=1,
            ),
        ),
    )


def test_aliases_expose_ha_preferred_and_strict_for_every_group_and_machine() -> None:
    aliases = build_aliases(fleet())

    assert set(aliases) == {
        "qwen-ha", "qwen-golemframe", "qwen-golemframe-nofailover",
        "qwen-pantheon", "qwen-pantheon-nofailover", "other-model-ha",
        "other-model-pantheon", "other-model-pantheon-nofailover",
    }
    assert aliases["qwen-ha"].deployment_ids == ("qwen-golemframe", "qwen-pantheon")
    assert aliases["qwen-ha"].preferred_endpoints == ()
    assert aliases["qwen-ha"].strategy == "latency"
    assert aliases["qwen-golemframe"].preferred_endpoints == ("golemframe-ollama",)
    assert aliases["qwen-golemframe-nofailover"].deployment_ids == ("qwen-golemframe",)


def test_ha_ranks_replicas_by_latency_and_never_uses_a_different_model() -> None:
    config = fleet()
    query = build_aliases(config)["qwen-ha"].apply(QueryRequest.from_prompt("Hello"))

    decision = LLMRouter(config).route(query)

    assert [candidate.model.id for candidate in decision.candidates] == [
        "qwen-pantheon", "qwen-golemframe",
    ]
    assert "other" in decision.excluded


def test_machine_preference_is_strict_ordering_after_capability_filters() -> None:
    config = fleet()
    alias = build_aliases(config)["qwen-golemframe"]
    query = alias.apply(QueryRequest.from_prompt("Hello", required_capabilities=("tools",)))

    assert LLMRouter(config).route(query).candidates[0].model.id == "qwen-golemframe"

    incapable = replace(config.models[0], capabilities={"tools": 0.0})
    constrained_config = replace(config, models=(incapable, *config.models[1:]))
    assert LLMRouter(constrained_config).route(query).candidates[0].model.id == "qwen-pantheon"


def test_strict_machine_does_not_bypass_request_constraints() -> None:
    config = fleet()
    alias = build_aliases(config)["qwen-golemframe-nofailover"]
    query = alias.apply(QueryRequest.from_prompt("Hello", exclude_endpoints=("golemframe-ollama",)))

    with pytest.raises(NoEligibleModel):
        LLMRouter(config).route(query)


def test_apply_preserves_existing_filters_and_never_broadens_allowed_deployments() -> None:
    alias = build_aliases(fleet())["qwen-golemframe"]
    query = QueryRequest.from_prompt(
        "Hello", strategy="quality", allowed_deployments=("other", "qwen-pantheon"),
        exclude_deployments=("qwen-golemframe",), required_capabilities=("tools",),
    )

    constrained = alias.apply(query)

    assert constrained.allowed_deployments == ("qwen-pantheon",)
    assert constrained.strategy == "quality"
    assert constrained.exclude_deployments == query.exclude_deployments
    assert constrained.required_capabilities == query.required_capabilities
    assert query.allowed_deployments == ("other", "qwen-pantheon")
    assert alias.apply(replace(query, allowed_deployments=())).allowed_deployments == ()
    with pytest.raises(NoEligibleModel):
        LLMRouter(fleet()).route(alias.apply(replace(query, allowed_deployments=("other",))))


def test_default_groups_preserve_model_versions_and_disabled_members_are_omitted() -> None:
    config = fleet()
    config = replace(config, models=(
        replace(config.models[0], replica_group=None),
        replace(config.models[1], upstream_model="qwen3:8b", replica_group=None),
        replace(config.models[2], enabled=False),
    ))

    aliases = build_aliases(config)

    assert aliases["qwen3-14b-ha"].deployment_ids == ("qwen-golemframe",)
    assert aliases["qwen3-8b-ha"].deployment_ids == ("qwen-pantheon",)
    assert "other-model-ha" not in aliases


def test_exact_upstream_names_group_without_operator_configuration() -> None:
    config = fleet()
    config = replace(config, models=tuple(
        replace(model, replica_group=None, upstream_model="qwen3:14b")
        for model in config.models[:2]
    ))

    assert build_aliases(config)["qwen3-14b-ha"].deployment_ids == (
        "qwen-golemframe", "qwen-pantheon",
    )


def test_multiple_services_on_one_machine_share_the_same_machine_alias() -> None:
    config = fleet()
    extra_endpoint = EndpointConfig("golemframe-lmstudio", "openai-chat", machine_id="golemframe")
    extra_model = replace(config.models[0], id="qwen-golemframe-2", endpoint=extra_endpoint.name)
    config = replace(
        config, endpoints={**config.endpoints, extra_endpoint.name: extra_endpoint},
        models=(*config.models, extra_model),
    )

    aliases = build_aliases(config)

    assert aliases["qwen-golemframe"].preferred_endpoints == (
        "golemframe-lmstudio", "golemframe-ollama",
    )
    assert aliases["qwen-golemframe-nofailover"].deployment_ids == (
        "qwen-golemframe", "qwen-golemframe-2",
    )
    assert {model.id for model in aliases["qwen-golemframe-nofailover"].models} == {
        "qwen-golemframe", "qwen-golemframe-2",
    }


def test_machine_identity_falls_back_to_endpoint_name_and_ignores_ip_changes() -> None:
    config = fleet()
    config = replace(config, endpoints={
        key: replace(endpoint, machine_id=None, base_url="http://192.0.2.1:11434")
        for key, endpoint in config.endpoints.items()
    })
    initial = build_aliases(config)
    moved = replace(config, endpoints={
        key: replace(endpoint, base_url="http://198.51.100.1:11434")
        for key, endpoint in config.endpoints.items()
    })

    assert "qwen-golemframe-ollama" in initial
    assert build_aliases(moved) == initial


def test_names_differing_only_in_punctuation_form_one_replica_group() -> None:
    # Ollama publishes qwen3:14b, LM Studio publishes qwen3-14b: the same model.
    config = fleet()
    original = replace(config.models[0], replica_group="qwen:14b")
    newcomer = replace(config.models[1], replica_group="qwen-14b")
    config = replace(config, models=(original, config.models[2]))
    before = build_aliases(config)
    expanded = replace(config, models=(*config.models, newcomer))
    after = build_aliases(expanded)

    assert before["qwen-14b-ha"].deployment_ids == ("qwen-golemframe",)
    assert after["qwen-14b-ha"].deployment_ids == ("qwen-golemframe", "qwen-pantheon"), "Both spellings share the HA name"
    assert alias_conflicts(expanded) == ()
    assert after["qwen-14b-golemframe"].deployment_ids == ("qwen-golemframe", "qwen-pantheon")
    assert after["qwen-14b-pantheon-nofailover"].deployment_ids == ("qwen-pantheon",)
    assert after["other-model-ha"] == before["other-model-ha"]
    distinct = replace(expanded, models=(*expanded.models[:2], replace(newcomer, id="qwen-quant", replica_group="qwen-14b-q4_K_M")))
    assert "qwen-14b-q4-k-m-ha" in build_aliases(distinct), "A quantization tag is a different name and keeps its own group"


def test_machine_slug_collision_hides_preference_and_nofailover_but_keeps_ha() -> None:
    config = fleet()
    config = replace(config, endpoints={
        "golemframe-ollama": replace(config.endpoints["golemframe-ollama"], machine_id="work.station"),
        "pantheon-lmstudio": replace(config.endpoints["pantheon-lmstudio"], machine_id="work-station"),
    })

    aliases = build_aliases(config)

    assert "qwen-ha" in aliases
    assert "qwen-work-station" not in aliases
    assert "qwen-work-station-nofailover" not in aliases
    assert alias_conflicts(config) == ("qwen-work-station", "qwen-work-station-nofailover")


def test_reserved_suffix_collision_never_turns_ha_into_machine_preference() -> None:
    config = fleet()
    config = replace(config, endpoints={
        **config.endpoints,
        "golemframe-ollama": replace(config.endpoints["golemframe-ollama"], machine_id="ha"),
    })

    aliases = build_aliases(config)

    assert "qwen-ha" not in aliases
    assert aliases["qwen-ha-nofailover"].deployment_ids == ("qwen-golemframe",)
    assert "qwen-ha" in alias_conflicts(config)


class ReplicaAdapter:
    default_auth_scheme = "none"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, endpoint, model, request) -> UpstreamResult:
        self.calls.append(model.id)
        if model.id == "qwen-golemframe":
            raise UpstreamError("machine offline", retryable=True)
        return UpstreamResult(text="fallback response")


def test_preferred_machine_fails_over_to_matching_replica() -> None:
    config = fleet()
    adapter = ReplicaAdapter()
    registry = AdapterRegistry()
    registry.register("ollama-chat", adapter)
    registry.register("openai-chat", adapter)
    router = LLMRouter(config, adapters=registry)
    query = build_aliases(config)["qwen-golemframe"].apply(QueryRequest.from_prompt("Hello"))

    result = asyncio.run(router.complete(query))

    assert result.deployment == "qwen-pantheon"
    assert adapter.calls == ["qwen-golemframe", "qwen-pantheon"]


def test_nofailover_never_contacts_another_machine_after_failure() -> None:
    config = fleet()
    adapter = ReplicaAdapter()
    registry = AdapterRegistry()
    registry.register("ollama-chat", adapter)
    registry.register("openai-chat", adapter)
    router = LLMRouter(config, adapters=registry)
    query = build_aliases(config)["qwen-golemframe-nofailover"].apply(QueryRequest.from_prompt("Hello"))

    with pytest.raises(AllModelsFailed):
        asyncio.run(router.complete(query))

    assert adapter.calls == ["qwen-golemframe"]
