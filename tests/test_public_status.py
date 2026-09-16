from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from llm_router.public_status import public_summary
from llm_router.schema import EndpointConfig, ModelConfig, RouterConfig


PAST = datetime.now(timezone.utc) - timedelta(days=1)
EARLIER = PAST - timedelta(hours=1)


def gateway(*, endpoints=(), models=(), discovery=None, error=None, refreshed=None):
    config = RouterConfig(endpoints={endpoint.name: endpoint for endpoint in endpoints}, models=tuple(models))
    return SimpleNamespace(
        _router=SimpleNamespace(config=config), _discovery=discovery, _last_error=error, _last_refresh=refreshed,
    )


def endpoint(name="worker", url="http://worker:11434"):
    return EndpointConfig(name=name, adapter="ollama", base_url=url)


def model(name="deployment", endpoint_name="worker", upstream="qwen", enabled=True):
    return ModelConfig(id=name, endpoint=endpoint_name, upstream_model=upstream, enabled=enabled)


def check(url="http://worker:11434", *, status="pass", ids=("qwen",), catalog="ok", truncated=False, total=1):
    return {
        "base_url": url, "status": status, "catalog_status": catalog,
        "models": [{"id": identifier, "address": "http://untrusted-returned-address.invalid"} for identifier in ids],
        "models_truncated": truncated, "model_count": total,
    }


def row(*checks, when=PAST.isoformat()):
    return {"checked_at": when, "checks": list(checks)}


def test_empty_cached_state_has_zero_counts_without_making_calls():
    def forbidden(*args, **kwargs):
        pytest.fail("Public summary must not call methods or inspect files/network")

    service = SimpleNamespace(
        _router=None, _discovery=None, _last_error=None, _last_refresh=None,
        router=forbidden, refresh=forbidden, status=forbidden, check_health=forbidden, provision=forbidden,
    )
    assert public_summary(service, {}) == {
        "servers": 0, "models": 0, "last_verified_at": None, "models_truncated": False,
    }
    assert public_summary(None, {}) == public_summary(service, {})


def test_configured_models_and_servers_are_known_even_without_verification():
    service = gateway(
        endpoints=[endpoint(), endpoint("unused", "http://unused:1234/v1")],
        models=[model(), model("disabled", upstream="disabled", enabled=False)],
    )
    assert public_summary(service, {}) == {
        "servers": 2, "models": 1, "last_verified_at": None, "models_truncated": False,
    }


def test_same_server_protocol_endpoints_and_aliases_are_deduplicated():
    service = gateway(
        endpoints=[endpoint("native", "http://WORKER:80"), endpoint("openai", "http://worker/v1")],
        models=[model("alias-1", "native"), model("alias-2", "openai")],
    )
    saved = {
        "one": row(check("http://worker"), check("http://WORKER:80/v1")),
        "two": row(check("http://worker./private/path", ids=("qwen", "embed"))),
    }
    assert public_summary(service, saved) == {
        "servers": 1, "models": 2, "last_verified_at": PAST.isoformat(), "models_truncated": False,
    }


def test_replicas_on_different_origins_are_separate_model_copies():
    service = gateway(endpoints=[endpoint()], models=[model()])
    saved = {
        "one": row(check("http://worker:11434", ids=("qwen", "qwen"))),
        "two": row(check("http://laptop:11434", ids=("qwen",))),
        "three": row(check("http://worker:1234/v1", ids=("qwen",))),
    }
    result = public_summary(service, saved)
    assert result["servers"] == 3
    assert result["models"] == 3


@pytest.mark.parametrize(("first", "second"), [
    ("https://WORKER:443/v1", "https://worker/"),
    ("http://[fd12:0:0:0:0:0:0:1]:80/v1", "http://[fd12::1]"),
    ("http://münchen.invalid/v1", "http://xn--mnchen-3ya.invalid:80"),
])
def test_origin_canonicalization_deduplicates_equivalent_addresses(first, second):
    result = public_summary(gateway(), {"one": row(check(first), check(second))})
    assert result["servers"] == 1
    assert result["models"] == 1


def test_partial_ollama_version_success_counts_server_not_unknown_catalog_models():
    partial = check(catalog="error", ids=("stale-qwen",), total=None)
    result = public_summary(gateway(), {"one": row(partial)})
    assert result == {"servers": 1, "models": 0, "last_verified_at": PAST.isoformat(), "models_truncated": False}


def test_failed_checks_do_not_count_stale_catalogs_or_advance_verification():
    saved = {"one": row(check(status="fail", ids=("stale-qwen",), truncated=True))}
    assert public_summary(gateway(), saved) == {
        "servers": 0, "models": 0, "last_verified_at": None, "models_truncated": False,
    }
    service = gateway(endpoints=[endpoint()], models=[model()])
    result = public_summary(service, saved)
    assert result["servers"] == 1
    assert result["models"] == 1  # Still configured/known, not claimed online.
    assert result["last_verified_at"] is None


def test_truncation_counts_displayed_unique_models_not_untrusted_total():
    saved = {
        "one": row(check(ids=("qwen", "embed", "qwen"), truncated=True, total=5000)),
        "two": row(check("http://worker:11434/v1", ids=("qwen",), total=9999)),
    }
    result = public_summary(gateway(), saved)
    assert result["models"] == 2
    assert result["models_truncated"] is True


def test_latest_successful_cached_scan_wins_not_failed_newer_scan():
    saved = {
        "older-success": row(check(), when=EARLIER.isoformat()),
        "newer-failure": row(check("http://offline:1234", status="fail"), when=PAST.isoformat()),
    }
    assert public_summary(gateway(), saved)["last_verified_at"] == EARLIER.isoformat()
    saved["newer-success"] = row(check("http://online:1234"), when=PAST.isoformat())
    assert public_summary(gateway(), saved)["last_verified_at"] == PAST.isoformat()


def test_repeated_views_never_change_last_verified_timestamp():
    service = gateway()
    saved = {"one": row(check())}
    first = public_summary(service, saved)
    assert public_summary(service, saved) == first
    assert first["last_verified_at"] == PAST.isoformat()


@pytest.mark.parametrize("value", [
    None, "", "private-secret-error", 123, True, [], {}, "2026-09-16", "2026-09-16T12:00:00",
    "9999-12-31T00:00:00+00:00", "1900-01-01T00:00:00+00:00", "x" * 65,
])
def test_bad_or_future_cached_timestamps_do_not_claim_verification(value):
    result = public_summary(gateway(), {"one": row(check(), when=value)})
    assert result["servers"] == 1
    assert result["last_verified_at"] is None
    assert "private-secret" not in json.dumps(result)


def test_saved_timestamps_normalize_z_and_offsets_to_utc():
    offset = timezone(timedelta(hours=-5))
    saved = {
        "one": row(check(), when=EARLIER.isoformat().replace("+00:00", "Z")),
        "two": row(check("http://another"), when=PAST.astimezone(offset).isoformat()),
    }
    assert public_summary(gateway(), saved)["last_verified_at"] == PAST.isoformat()


def test_successful_cached_discovery_is_verification_even_with_no_models():
    discovery = SimpleNamespace(probes=(SimpleNamespace(reachable=True),))
    service = gateway(discovery=discovery, refreshed=PAST.timestamp())
    assert public_summary(service, {})["last_verified_at"] == PAST.isoformat()
    saved = {"one": row(check(), when=EARLIER.isoformat())}
    assert public_summary(service, saved)["last_verified_at"] == PAST.isoformat()


@pytest.mark.parametrize("discovery", [
    None, SimpleNamespace(probes=()), SimpleNamespace(probes=(SimpleNamespace(reachable=False),)),
    SimpleNamespace(probes=(SimpleNamespace(reachable="true"),)), SimpleNamespace(probes="bad"),
])
def test_disabled_empty_or_failed_discovery_does_not_claim_verification(discovery):
    assert public_summary(gateway(discovery=discovery, refreshed=PAST.timestamp()), {})["last_verified_at"] is None


def test_failed_refresh_does_not_retimestamp_previous_good_discovery():
    discovery = SimpleNamespace(probes=(SimpleNamespace(reachable=True),))
    service = gateway(discovery=discovery, refreshed=PAST.timestamp(), error="private-secret-error")
    assert public_summary(service, {})["last_verified_at"] is None
    saved = {"one": row(check(), when=EARLIER.isoformat())}
    assert public_summary(service, saved)["last_verified_at"] == EARLIER.isoformat()


@pytest.mark.parametrize("timestamp", [None, True, "1234", [], {}, float("nan"), float("inf"), -1, 10**1000, 999999999999])
def test_invalid_discovery_epoch_does_not_throw_or_leak(timestamp):
    discovery = SimpleNamespace(probes=(SimpleNamespace(reachable=True),))
    service = gateway(discovery=discovery, refreshed=timestamp)
    assert public_summary(service, {})["last_verified_at"] is None


@pytest.mark.parametrize("url", [None, 1, "", "file:///private-secret", "worker", "http://", "http://[broken", "http://worker:99999", "http://worker:0", "http://worker\n"])
def test_invalid_origins_are_skipped(url):
    service = gateway(endpoints=[endpoint(url=url)], models=[model()])
    result = public_summary(service, {"one": row(check(url))})
    assert result == {"servers": 0, "models": 0, "last_verified_at": None, "models_truncated": False}


def test_anonymous_output_cannot_reveal_backend_fields_or_errors():
    service = gateway(
        endpoints=[endpoint("private-machine-name", "https://private-user:private-key@private-machine:1234/v1?key=private-query")],
        models=[model("private-alias", "private-machine-name", "private-model-id")],
        error="private-error",
    )
    saved = {"private-row": row(check("https://private-machine:1234/v1", ids=("private-model-id",)))}
    result = public_summary(service, saved)
    assert result["servers"] == result["models"] == 1
    assert set(result) == {"servers", "models", "last_verified_at", "models_truncated"}
    assert "private" not in json.dumps(result)
    assert "http" not in json.dumps(result)


@pytest.mark.parametrize("saved", [None, [], {"bad": None}, {"bad": "bad"}, {"bad": {}}, {"bad": {"checks": "bad"}}])
def test_malformed_saved_cache_is_ignored(saved):
    assert public_summary(gateway(), saved) == {"servers": 0, "models": 0, "last_verified_at": None, "models_truncated": False}


def test_malformed_cached_items_are_skipped_without_counting_unknown_models():
    saved = {"one": row(
        None, "bad", {},
        {"status": "pass", "base_url": "http://worker", "catalog_status": "ok", "models": "bad"},
        {"status": "pass", "base_url": "http://worker", "catalog_status": "ok", "models": [None, {}, {"id": []}, {"id": "\nprivate"}, {"id": "qwen"}]},
    )}
    result = public_summary(gateway(), saved)
    assert result["servers"] == 1
    assert result["models"] == 1


def test_malformed_config_does_not_call_router_or_raise():
    service = SimpleNamespace(_router=SimpleNamespace(config=SimpleNamespace(endpoints=[], models="bad")))
    assert public_summary(service, {})["models"] == 0
    service = gateway(endpoints=[endpoint()], models=[
        SimpleNamespace(endpoint=[], upstream_model="qwen", enabled=True),
        SimpleNamespace(endpoint="worker", upstream_model=[], enabled=True),
        SimpleNamespace(endpoint="missing", upstream_model="qwen", enabled=True),
        SimpleNamespace(endpoint="worker", upstream_model="qwen", enabled="true"),
    ])
    assert public_summary(service, {})["models"] == 0
