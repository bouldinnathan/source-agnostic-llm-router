from __future__ import annotations

from dataclasses import replace

import pytest

from llm_router.discovery import DiscoveryReport, ProbeResult, infer_model_profile
from llm_router.saved_discovery import is_saved_endpoint, merge_saved_discovery, saved_hosts_report
from llm_router.schema import AuthConfig, EndpointConfig, ModelConfig, PolicyConfig, RouterConfig


def check(*, kind="ollama", origin="http://192.168.194.1:11434", names=("qwen3:14b",), ok=True, catalog="ok", blocked=False):
    return {
        "provider": "Ollama" if kind == "ollama" else "LM Studio / OpenAI-compatible",
        "base_url": origin if kind == "ollama" else origin + "/v1",
        "status": "pass" if ok else "fail", "catalog_status": catalog,
        "models": [{"id": name, "address": "http://untrusted-payload.invalid"} for name in names],
        "model_count": len(names), "models_truncated": False,
        **({"enrollment_blocked": True} if blocked else {}),
    }


def rows(*checks, host_id="one"):
    return {host_id: {"id": host_id, "address": "192.168.194.1", "checked_at": "2026-09-18T01:00:00+00:00", "checks": list(checks)}}


def report(*, endpoints=(), models=(), probes=(), policy=None):
    return DiscoveryReport(
        RouterConfig(endpoints={endpoint.name: endpoint for endpoint in endpoints}, models=tuple(models), policy=policy or PolicyConfig()),
        tuple(probes),
    )


def one_endpoint(discovery):
    assert len(discovery.config.endpoints) == 1
    return next(iter(discovery.config.endpoints.values()))


def test_empty_saved_report_has_no_network_or_configuration_side_effects(monkeypatch):
    import httpx
    from llm_router.discovery import ModelDiscovery
    from llm_router.router import LLMRouter

    def forbidden(*args, **kwargs):
        pytest.fail("Cached enrollment must not call networking, discovery, model details, or inference")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(ModelDiscovery, "discover", forbidden)
    monkeypatch.setattr(ModelDiscovery, "_ollama_details", forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    empty = saved_hosts_report({})
    assert empty.config.endpoints == {}
    assert empty.config.models == ()
    assert empty.probes == ()
    populated = saved_hosts_report(rows(check()))
    assert len(populated.config.models) == 1


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_native_and_openai_catalogs_make_stable_owned_routes_with_inferred_profiles(kind):
    source = rows(check(kind=kind, names=("qwen3:14b", "coder-32b")))
    initial = saved_hosts_report(source)
    repeated = saved_hosts_report(source, initial)
    assert repeated == initial
    endpoint = one_endpoint(initial)
    assert endpoint.adapter == ("ollama-chat" if kind == "ollama" else "openai-chat")
    assert endpoint.base_url == "http://192.168.194.1:11434" + ("/v1" if kind == "openai" else "")
    assert endpoint.machine_id == "192.168.194.1"
    assert endpoint.discover is True
    assert endpoint.auth.scheme == "none"
    assert endpoint.options["saved_host_ids"] == ("one",)
    assert is_saved_endpoint(endpoint)
    assert all("saved-host" in model.tags and "discovered" in model.tags for model in initial.config.models)
    qwen = next(model for model in initial.config.models if model.upstream_model == "qwen3:14b")
    profile = infer_model_profile("qwen3:14b", provider="ollama" if kind == "ollama" else "openai-compatible", local=True)
    assert qwen.capabilities == profile["capabilities"]
    assert qwen.context_window == profile["context_window"]
    assert initial.probes[0].endpoint == endpoint.name
    assert initial.probes[0].reachable is True
    assert initial.probes[0].enrolled_models == 2


def test_chat_filter_excludes_the_same_nonchat_names_as_normal_discovery():
    source = rows(check(names=(
        "qwen3:14b", "nomic-embed-text", "bge-embedding", "reranker", "whisper", "tts-fast",
        "moderation", "guard-model", "gpt-image-1", "qwen3:14b", "llava-vision",
    )))
    discovery = saved_hosts_report(source)
    assert {model.upstream_model for model in discovery.config.models} == {"qwen3:14b", "llava-vision"}


def test_same_origin_prefers_native_ollama_and_never_uses_payload_model_address():
    discovery = saved_hosts_report(rows(check(kind="openai", names=("compat-name",)), check(names=("native-name",))))
    endpoint = one_endpoint(discovery)
    assert endpoint.adapter == "ollama-chat"
    assert endpoint.base_url == "http://192.168.194.1:11434"
    assert len(discovery.probes) == 1
    assert [model.upstream_model for model in discovery.config.models] == ["native-name"]
    assert "untrusted-payload" not in str(discovery)


def test_separate_ports_remain_separate_services_on_one_machine():
    discovery = saved_hosts_report(rows(check(), check(kind="openai", origin="http://192.168.194.1:1234")))
    assert len(discovery.config.endpoints) == len(discovery.probes) == len(discovery.config.models) == 2
    assert {endpoint.machine_id for endpoint in discovery.config.endpoints.values()} == {"192.168.194.1"}
    assert len({model.id for model in discovery.config.models}) == 2


def test_same_model_on_different_hosts_is_a_separate_replica():
    source = {**rows(check(), host_id="one"), **rows(check(origin="http://192.168.194.2:11434"), host_id="two")}
    discovery = saved_hosts_report(source)
    assert len(discovery.config.models) == 2
    assert len({model.endpoint for model in discovery.config.models}) == 2
    assert len({endpoint.machine_id for endpoint in discovery.config.endpoints.values()}) == 2


def test_healthy_openai_is_used_when_native_catalog_fails():
    discovery = saved_hosts_report(rows(check(catalog="error"), check(kind="openai", names=("qwen3:14b",))))
    assert one_endpoint(discovery).adapter == "openai-chat"
    assert discovery.probes[0].reachable is True


def test_successful_empty_native_catalog_is_authoritative_and_clears_old_models():
    previous = saved_hosts_report(rows(check()))
    current = saved_hosts_report(rows(check(names=()), check(kind="openai", names=("old-compatible",))), previous)
    assert one_endpoint(current).adapter == "ollama-chat"
    assert current.config.models == ()
    assert current.probes[0].reachable is True
    assert current.probes[0].enrolled_models == 0


@pytest.mark.parametrize("partial", [False, True])
def test_failed_or_partial_check_retains_previous_catalog_offline(partial):
    previous = saved_hosts_report(rows(check()))
    current = saved_hosts_report(rows(check(ok=partial, catalog="error", names=())), previous)
    assert current.config.models == previous.config.models
    assert one_endpoint(current) == one_endpoint(previous)
    assert current.probes[0].reachable is False
    assert current.probes[0].enrolled_models == 1
    assert "retaining" in current.probes[0].error


def test_failed_different_protocol_does_not_change_retained_source_identity():
    previous = saved_hosts_report(rows(check()))
    current = saved_hosts_report(rows(check(kind="openai", ok=False, catalog="error")), previous)
    assert current.config.models == previous.config.models
    assert one_endpoint(current) == one_endpoint(previous)
    assert current.probes[0].source == previous.probes[0].source
    assert current.probes[0].provider == "ollama"


def test_never_successful_unreachable_port_is_probe_only_without_fabricated_model():
    current = saved_hosts_report(rows(check(ok=False, catalog="error")))
    assert current.config.models == ()
    assert current.config.endpoints == {}
    assert len(current.probes) == 1
    assert current.probes[0].reachable is False


def test_version_only_server_is_known_but_has_no_routable_models():
    current = saved_hosts_report(rows(check(catalog="error")))
    assert current.config.models == ()
    assert len(current.config.endpoints) == 1
    assert current.probes[0].reachable is False


def test_duplicate_saved_addresses_share_one_route_and_removing_one_owner_keeps_it():
    source = {**rows(check(), host_id="one"), **rows(check(), host_id="two")}
    previous = saved_hosts_report(source)
    assert one_endpoint(previous).options["saved_host_ids"] == ("one", "two")
    current = saved_hosts_report(rows(check(), host_id="two"), previous)
    assert current.config.models == previous.config.models
    assert one_endpoint(current).options["saved_host_ids"] == ("two",)
    cleared = saved_hosts_report({}, current)
    assert cleared.config.endpoints == {}
    assert cleared.config.models == ()
    assert cleared.probes == ()


def test_still_saved_but_unchecked_previous_source_stays_offline_not_deleted():
    previous = saved_hosts_report(rows(check()))
    current = saved_hosts_report({"one": {"id": "one", "checks": []}}, previous)
    assert current.config.models == previous.config.models
    assert current.probes[0].reachable is False


@pytest.mark.parametrize("with_duplicate_success", [False, True])
def test_blocked_router_proxy_prunes_prior_routes_instead_of_retaining_them(with_duplicate_success):
    previous = saved_hosts_report(rows(check()))
    checks = [check(ok=False, catalog="error", blocked=True)]
    if with_duplicate_success:
        checks.append(check(kind="openai"))
    current = saved_hosts_report(rows(*checks), previous)
    assert current.config.models == ()
    assert current.config.endpoints == {}
    assert len(current.probes) == 1
    assert current.probes[0].reachable is False
    assert "Router proxy" in current.probes[0].error


def test_blocked_port_does_not_prune_independent_other_port():
    previous = saved_hosts_report(rows(check(), check(kind="openai", origin="http://192.168.194.1:1234")))
    current = saved_hosts_report(rows(check(blocked=True), check(kind="openai", origin="http://192.168.194.1:1234")), previous)
    assert one_endpoint(current).base_url == "http://192.168.194.1:1234/v1"
    assert len(current.config.models) == 1


@pytest.mark.parametrize("base_url", [
    "http://user:secret@worker", "http://worker/private", "http://worker?secret", "file:///secret",
    "http://worker\n", "http://worker:99999", "worker", None,
])
def test_malformed_cached_check_addresses_are_not_enrolled(base_url):
    item = check()
    item["base_url"] = base_url
    discovery = saved_hosts_report(rows(item))
    assert discovery.config.endpoints == {}
    assert discovery.config.models == ()
    assert "secret" not in str(discovery)


@pytest.mark.parametrize("models", [None, "bad", [{}], [{"id": "bad\nname"}], [{"id": 1}], [{"id": "x" * 513}]])
def test_invalid_catalog_is_failure_not_an_authoritative_empty_catalog(models):
    previous = saved_hosts_report(rows(check()))
    item = check()
    item["models"] = models
    current = saved_hosts_report(rows(item), previous)
    assert current.config.models == previous.config.models
    assert current.probes[0].reachable is False


def test_bounded_host_and_catalog_counts():
    source = {str(index): rows(check(origin=f"http://worker-{index}:11434"), host_id=str(index))[str(index)] for index in range(100)}
    assert len(saved_hosts_report(source).config.endpoints) == 16
    too_many = check(names=tuple(f"model-{index}" for index in range(201)))
    discovery = saved_hosts_report(rows(too_many))
    assert discovery.config.models == ()
    assert discovery.probes[0].reachable is False


def test_configured_origin_owns_both_api_surfaces_even_if_disabled_and_no_discovery():
    explicit = EndpointConfig(name="manual", adapter="openai-chat", base_url="http://192.168.194.1:11434/private/v1", auth=AuthConfig(key_env="PRIVATE_KEY"), discover=False)
    disabled = ModelConfig(id="manual-model", endpoint=explicit.name, upstream_model="qwen3:14b", enabled=False)
    configured = RouterConfig(endpoints={explicit.name: explicit}, models=(disabled,))
    saved = saved_hosts_report(rows(check()))
    merged = merge_saved_discovery(report(), saved, configured)
    assert merged.config.endpoints == {}
    assert merged.config.models == ()
    assert merged.probes == ()
    assert configured.endpoints["manual"] == explicit
    assert configured.models[0].enabled is False


def test_base_discovery_source_wins_saved_same_origin_and_probes_are_not_duplicated():
    base_endpoint = EndpointConfig(name="auto-existing", adapter="openai-chat", base_url="http://192.168.194.1:11434/v1")
    base_model = ModelConfig(id="existing", endpoint=base_endpoint.name, upstream_model="qwen3:14b")
    base_probe = ProbeResult("existing", "openai-compatible", base_endpoint.base_url, True, 1, endpoint=base_endpoint.name)
    base = report(endpoints=[base_endpoint], models=[base_model, base_model], probes=[base_probe, base_probe])
    merged = merge_saved_discovery(base, saved_hosts_report(rows(check())), None)
    assert merged.config.endpoints == {base_endpoint.name: base_endpoint}
    assert merged.config.models == (base_model,)
    assert merged.probes == (base_probe,)


def test_distinct_base_and_saved_sources_are_combined_with_base_policy():
    base_endpoint = EndpointConfig(name="existing", adapter="openai-chat", base_url="http://other:1234/v1")
    base_model = ModelConfig(id="existing", endpoint="existing", upstream_model="qwen")
    policy = PolicyConfig(max_attempts=7)
    base = report(endpoints=[base_endpoint], models=[base_model], policy=policy)
    merged = merge_saved_discovery(base, saved_hosts_report(rows(check())), None)
    assert len(merged.config.endpoints) == len(merged.config.models) == 2
    assert merged.config.policy == policy


def test_previous_saved_base_routes_are_pruned_on_delete_or_successful_empty_catalog():
    old_saved = saved_hosts_report(rows(check()))
    normal_endpoint = EndpointConfig(name="existing", adapter="openai-chat", base_url="http://other:1234/v1")
    normal_model = ModelConfig(id="existing", endpoint="existing", upstream_model="qwen")
    mixed = merge_saved_discovery(report(endpoints=[normal_endpoint], models=[normal_model]), old_saved, None)
    deleted = merge_saved_discovery(mixed, saved_hosts_report({}, old_saved), None)
    assert deleted.config.endpoints == {"existing": normal_endpoint}
    assert deleted.config.models == (normal_model,)
    empty = merge_saved_discovery(mixed, saved_hosts_report(rows(check(names=())), old_saved), None)
    assert len(empty.config.endpoints) == 2
    assert empty.config.models == (normal_model,)


def test_configured_identity_does_not_get_mistaken_for_saved_ownership_by_name():
    explicit = EndpointConfig(name="auto-saved-human-choice", adapter="ollama-chat", base_url="http://manual:11434")
    assert not is_saved_endpoint(explicit)
    explicit_model = ModelConfig(id="manual", endpoint=explicit.name, upstream_model="qwen")
    merged = merge_saved_discovery(report(endpoints=[explicit], models=[explicit_model]), saved_hosts_report({}), None)
    assert merged.config.models == (explicit_model,)


def test_ordinary_discovery_cannot_bypass_configured_origin_or_replace_auth():
    explicit = EndpointConfig(name="manual", adapter="ollama-chat", base_url="http://WORKER:80", auth=AuthConfig(key_env="PRIVATE_KEY"), discover=False)
    configured = RouterConfig(endpoints={explicit.name: explicit}, models=())
    bypass = EndpointConfig(name="auto-other", adapter="openai-chat", base_url="http://worker/v1")
    bypass_model = ModelConfig(id="bypass", endpoint=bypass.name, upstream_model="qwen")
    bypass_probe = ProbeResult("bypass", "openai-compatible", bypass.base_url, True, 1, endpoint=bypass.name)
    merged = merge_saved_discovery(report(endpoints=[bypass], models=[bypass_model], probes=[bypass_probe]), saved_hosts_report({}), configured)
    assert merged.config.endpoints == {}
    assert merged.config.models == ()
    assert merged.probes == ()
    same_name = replace(explicit, auth=AuthConfig(scheme="none"), discover=True)
    retained = merge_saved_discovery(report(endpoints=[same_name]), saved_hosts_report({}), configured)
    assert retained.config.endpoints["manual"] == explicit


def test_intentional_distinct_configured_endpoints_on_one_origin_are_preserved():
    first = EndpointConfig(name="first", adapter="openai-chat", base_url="http://worker/v1", auth=AuthConfig(key_env="FIRST_KEY"))
    second = replace(first, name="second", auth=AuthConfig(key_env="SECOND_KEY"))
    configured = RouterConfig(endpoints={"first": first, "second": second}, models=())
    base = report(endpoints=[first, second])
    assert merge_saved_discovery(base, saved_hosts_report({}), configured).config.endpoints == configured.endpoints


def test_failed_base_probe_without_endpoint_does_not_suppress_saved_success():
    saved = saved_hosts_report(rows(check()))
    failed = ProbeResult("ordinary", "ollama", "http://192.168.194.1:11434", False, endpoint="auto-ordinary")
    merged = merge_saved_discovery(report(probes=[failed]), saved, None)
    assert merged.config.endpoints == saved.config.endpoints
    assert merged.config.models == saved.config.models
    assert merged.probes == saved.probes


def test_existing_base_endpoint_still_wins_when_its_probe_fails():
    saved = saved_hosts_report(rows(check()))
    endpoint = EndpointConfig(name="auto-ordinary", adapter="ollama-chat", base_url="http://192.168.194.1:11434")
    failed = ProbeResult("ordinary", "ollama", endpoint.base_url, False, endpoint=endpoint.name)
    merged = merge_saved_discovery(report(endpoints=[endpoint], probes=[failed]), saved, None)
    assert merged.config.endpoints == {endpoint.name: endpoint}
    assert merged.config.models == ()
    assert merged.probes == (failed,)


@pytest.mark.parametrize("newer_check", [check(names=()), check(ok=False, catalog="error"), check(catalog="error")])
@pytest.mark.parametrize("newer_first", [False, True])
def test_newest_duplicate_origin_snapshot_wins_over_stale_success(newer_check, newer_first):
    old = rows(check(), host_id="older")
    new = rows(newer_check, host_id="newer")
    old["older"]["checked_at"] = "2026-09-18T01:00:00+00:00"
    new["newer"]["checked_at"] = "2026-09-18T02:00:00+00:00"
    previous = saved_hosts_report(old)
    results = {**new, **old} if newer_first else {**old, **new}
    current = saved_hosts_report(results, previous)
    if newer_check["catalog_status"] == "ok":
        assert current.config.models == ()
        assert current.probes[0].reachable is True
    else:
        assert current.config.models == previous.config.models
        assert current.probes[0].reachable is False
    assert one_endpoint(current).options["saved_host_ids"] == ("newer", "older")


def test_newest_snapshot_comparison_normalizes_timezone_offsets():
    old = rows(check(), host_id="older")
    newer = rows(check(names=()), host_id="newer")
    old["older"]["checked_at"] = "2026-09-18T02:00:00Z"
    newer["newer"]["checked_at"] = "2026-09-17T22:00:00-05:00"
    assert saved_hosts_report({**old, **newer}).config.models == ()
