from __future__ import annotations

import builtins
import copy
import json
import socket
import subprocess

import pytest

from llm_router.schema import UpstreamResult
from llm_router.telemetry import extract_observation


FIELDS = {
    "input_tokens", "output_tokens", "input_tokens_per_second",
    "output_tokens_per_second", "load_duration_ms", "request_duration_ms",
}
INVALID_NUMBERS = [None, True, False, "12", [], {}, -1, float("nan"), float("inf"), -float("inf"), 10**1000]


def observe(adapter: str = "openai-compatible", *, usage=None, raw=None, latency_ms=5000.0):
    return extract_observation(adapter, UpstreamResult(text="private reply", usage=usage or {}, raw=raw), latency_ms)


@pytest.mark.parametrize("adapter", [
    "ollama", "ollama-chat", "openai-chat", "openai-compatible", "openai-responses",
    "anthropic", "anthropic-messages", "gemini", "gemini-generate", "generic-json",
])
def test_missing_measurements_remain_unknown_with_exact_numeric_only_schema(adapter):
    actual = observe(adapter)
    assert actual == {name: 5000.0 if name == "request_duration_ms" else None for name in FIELDS}
    assert set(actual) == FIELDS


@pytest.mark.parametrize("adapter", ["ollama", "ollama-chat"])
def test_ollama_uses_separate_nanosecond_phase_durations(adapter):
    actual = observe(adapter, raw={
        "prompt_eval_count": 150,
        "prompt_eval_cached_count": 50,
        "prompt_eval_duration": 250_000_000,
        "eval_count": 80,
        "eval_duration": 2_000_000_000,
        "load_duration": 1_750_000_000,
        "total_duration": 9_000_000_000,
    }, latency_ms=11000)
    assert actual == {
        "input_tokens": 150,
        "output_tokens": 80,
        "input_tokens_per_second": 400.0,
        "output_tokens_per_second": 40.0,
        "load_duration_ms": 1750.0,
        "request_duration_ms": 11000.0,
    }


def test_ollama_without_cache_count_treats_prompt_work_as_uncached():
    actual = observe("ollama", raw={"prompt_eval_count": 20, "prompt_eval_duration": 100_000_000})
    assert actual["input_tokens"] == 20
    assert actual["input_tokens_per_second"] == 200.0


@pytest.mark.parametrize("cached", [20, 21, *INVALID_NUMBERS])
def test_ollama_all_cached_or_invalid_cache_count_has_no_prefill_rate(cached):
    actual = observe("ollama", raw={
        "prompt_eval_count": 20, "prompt_eval_cached_count": cached,
        "prompt_eval_duration": 100_000_000,
    })
    assert actual["input_tokens"] == 20
    assert actual["input_tokens_per_second"] is None


@pytest.mark.parametrize("duration", [0, *INVALID_NUMBERS, 86_400_000_000_001])
def test_ollama_invalid_or_zero_phase_duration_does_not_invent_rate(duration):
    actual = observe("ollama", raw={
        "prompt_eval_count": 20, "prompt_eval_duration": duration,
        "eval_count": 10, "eval_duration": duration,
    })
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None


def test_ollama_usage_counts_fallback_but_not_overall_duration():
    actual = observe("ollama", usage={"prompt_eval_count": 20, "eval_count": 10, "total_duration": 1000})
    assert actual["input_tokens"] == 20
    assert actual["output_tokens"] == 10
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


def test_ollama_raw_counts_take_precedence_and_explicit_bad_values_are_not_replaced():
    actual = observe("ollama", usage={"prompt_eval_count": 100, "eval_count": 200},
                     raw={"prompt_eval_count": "100", "eval_count": 3})
    assert actual["input_tokens"] is None
    assert actual["output_tokens"] == 3


@pytest.mark.parametrize("adapter", ["openai-chat", "openai-compatible", "openai-responses"])
@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 300, "completion_tokens": 50},
    {"input_tokens": 300, "output_tokens": 50},
])
def test_openai_usage_aliases_are_counts_not_phase_throughputs(adapter, usage):
    actual = observe(adapter, usage=usage, raw={
        "total_duration": 1000, "load_duration": 1500,
        "stats": {"time_to_first_token": 0.1, "generation_time": 1, "time_to_first_token_seconds": 0.1},
    })
    assert actual["input_tokens"] == 300
    assert actual["output_tokens"] == 50
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


@pytest.mark.parametrize(("adapter", "counts"), [
    ("openai-chat", (10, 20)), ("openai-compatible", (10, 20)), ("openai-responses", (30, 40)),
])
def test_openai_prefers_endpoint_specific_count_names(adapter, counts):
    actual = observe(adapter, usage={"prompt_tokens": 10, "completion_tokens": 20, "input_tokens": 30, "output_tokens": 40})
    assert (actual["input_tokens"], actual["output_tokens"]) == counts


def test_openai_raw_usage_is_a_fallback_but_malformed_primary_is_not_hidden():
    actual = observe(usage={"prompt_tokens": True}, raw={
        "usage": {"prompt_tokens": 20, "completion_tokens": 30},
        "stats": {"input_tokens": 40}, "timings": {"prompt_n": 50},
    })
    assert actual["input_tokens"] is None
    assert actual["output_tokens"] == 30


def test_llama_documented_timings_preserve_reported_phase_speeds():
    actual = observe(raw={
        "usage": {"prompt_tokens": 237, "completion_tokens": 35},
        "timings": {
            "cache_n": 236, "prompt_n": 1, "prompt_ms": 30.958,
            "prompt_per_second": 32.301828283480845,
            "predicted_n": 35, "predicted_ms": 661.064,
            "predicted_per_second": 52.94494935437416,
        },
    })
    assert actual["input_tokens"] == 237
    assert actual["output_tokens"] == 35
    assert actual["input_tokens_per_second"] == pytest.approx(32.301828283480845)
    assert actual["output_tokens_per_second"] == pytest.approx(52.94494935437416)
    assert actual["load_duration_ms"] is None


def test_llama_phase_count_duration_fallback_excludes_cached_tokens_from_rate():
    actual = observe(raw={"timings": {
        "prompt_n": 40, "cache_n": 60, "prompt_ms": 200,
        "predicted_n": 20, "predicted_ms": 500,
    }})
    assert actual["input_tokens"] == 100
    assert actual["output_tokens"] == 20
    assert actual["input_tokens_per_second"] == 200.0
    assert actual["output_tokens_per_second"] == 40.0


@pytest.mark.parametrize("timings", [
    {"prompt_n": 0, "cache_n": 20, "prompt_ms": 1, "prompt_per_second": 123},
    {"prompt_n": 20, "prompt_ms": 0, "prompt_per_second": 123},
    {"prompt_n": 20, "prompt_ms": 0},
    {"prompt_n": 20},
    {"prompt_ms": 100},
])
def test_llama_all_cached_zero_duration_or_missing_phase_data_has_no_derived_rate(timings):
    actual = observe(raw={"timings": timings})
    assert actual["input_tokens_per_second"] is None


@pytest.mark.parametrize("rate", INVALID_NUMBERS + [1_000_000_001])
def test_llama_invalid_explicit_rates_remain_unknown_without_silent_fallback(rate):
    actual = observe(raw={"timings": {
        "prompt_n": 10, "prompt_ms": 100, "prompt_per_second": rate,
        "predicted_n": 10, "predicted_ms": 100, "predicted_per_second": rate,
    }, "stats": {"tokens_per_second": 10}})
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None


@pytest.mark.parametrize("timings", [
    {"predicted_n": 0, "predicted_ms": 1, "predicted_per_second": 123},
    {"predicted_n": 20, "predicted_ms": 0, "predicted_per_second": 123},
])
def test_llama_no_decode_work_or_duration_is_unknown_not_infinite(timings):
    assert observe(raw={"timings": timings})["output_tokens_per_second"] is None


def test_llama_input_total_is_unknown_if_cache_component_is_malformed():
    actual = observe(raw={"timings": {"prompt_n": 5, "cache_n": "10", "prompt_ms": 100}})
    assert actual["input_tokens"] is None
    assert actual["input_tokens_per_second"] == 50.0


def test_lmstudio_optional_stats_use_documented_seconds_and_output_rate():
    actual = observe(raw={"stats": {
        "input_tokens": 150, "total_output_tokens": 25, "reasoning_output_tokens": 10,
        "tokens_per_second": 12.5, "model_load_time_seconds": 1.25,
        "time_to_first_token_seconds": 2, "generation_time": 4,
    }})
    assert actual == {
        "input_tokens": 150, "output_tokens": 25,
        "input_tokens_per_second": None, "output_tokens_per_second": 12.5,
        "load_duration_ms": 1250.0, "request_duration_ms": 5000.0,
    }


def test_lmstudio_native_v0_stats_only_supply_output_speed_not_load_or_input_rate():
    actual = observe(raw={"stats": {
        "tokens_per_second": 22.25, "time_to_first_token": 0.5,
        "generation_time": 2, "load_time": 1, "model_load_time": 1,
    }}, usage={"prompt_tokens": 500, "completion_tokens": 40})
    assert actual["input_tokens"] == 500
    assert actual["output_tokens"] == 40
    assert actual["output_tokens_per_second"] == 22.25
    assert actual["input_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


def test_standard_usage_counts_win_over_enhanced_stats_and_reasoning_is_not_double_counted():
    actual = observe(usage={
        "prompt_tokens": 40, "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 15},
    }, raw={"stats": {"input_tokens": 70, "total_output_tokens": 90, "reasoning_output_tokens": 20}})
    assert actual["input_tokens"] == 40
    assert actual["output_tokens"] == 30


@pytest.mark.parametrize("adapter", ["anthropic", "anthropic-messages"])
def test_anthropic_input_includes_cache_creation_and_reads_once(adapter):
    actual = observe(adapter, usage={
        "input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 60,
        "output_tokens": 20, "cache_creation": {"ephemeral_5m_input_tokens": 30},
    }, raw={"stats": {"tokens_per_second": 10, "model_load_time_seconds": 1}})
    assert actual["input_tokens"] == 100
    assert actual["output_tokens"] == 20
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


def test_anthropic_optional_cache_counts_default_to_zero_and_raw_usage_falls_back():
    actual = observe("anthropic", raw={"usage": {"input_tokens": 12, "output_tokens": 3}})
    assert actual["input_tokens"] == 12
    assert actual["output_tokens"] == 3


@pytest.mark.parametrize("cache_field", ["cache_creation_input_tokens", "cache_read_input_tokens"])
@pytest.mark.parametrize("value", INVALID_NUMBERS + [0.5])
def test_anthropic_invalid_cache_component_does_not_understate_total(cache_field, value):
    actual = observe("anthropic", usage={"input_tokens": 10, "output_tokens": 2, cache_field: value})
    assert actual["input_tokens"] is None
    assert actual["output_tokens"] == 2


@pytest.mark.parametrize("adapter", ["gemini", "gemini-generate"])
def test_gemini_cached_prompt_is_already_included_and_thought_output_is_separate(adapter):
    actual = observe(adapter, raw={"usageMetadata": {
        "promptTokenCount": 100, "cachedContentTokenCount": 80,
        "candidatesTokenCount": 20, "thoughtsTokenCount": 30, "totalTokenCount": 150,
    }, "stats": {"tokens_per_second": 10, "model_load_time_seconds": 1}})
    assert actual["input_tokens"] == 100
    assert actual["output_tokens"] == 50
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


def test_gemini_missing_thought_count_is_zero_but_missing_candidate_count_is_unknown():
    assert observe("gemini", usage={"candidatesTokenCount": 4})["output_tokens"] == 4
    assert observe("gemini", usage={"thoughtsTokenCount": 4, "totalTokenCount": 10})["output_tokens"] is None


@pytest.mark.parametrize("thoughts", INVALID_NUMBERS + [0.5])
def test_gemini_invalid_thought_count_keeps_total_output_unknown(thoughts):
    assert observe("gemini", usage={"candidatesTokenCount": 4, "thoughtsTokenCount": thoughts})["output_tokens"] is None


@pytest.mark.parametrize(("adapter", "usage", "raw", "field"), [
    ("anthropic", {"input_tokens": 1_000_000_000, "cache_read_input_tokens": 1}, None, "input_tokens"),
    ("gemini", {"candidatesTokenCount": 1_000_000_000, "thoughtsTokenCount": 1}, None, "output_tokens"),
    ("openai-compatible", {}, {"timings": {"prompt_n": 1_000_000_000, "cache_n": 1}}, "input_tokens"),
])
def test_provider_count_sums_are_bounded(adapter, usage, raw, field):
    assert observe(adapter, usage=usage, raw=raw)[field] is None


def test_unknown_adapter_only_uses_common_usage_counts_not_foreign_timing_keys():
    actual = observe("custom-plugin", usage={"input_tokens": 10, "output_tokens": 20}, raw={
        "stats": {"tokens_per_second": 5, "model_load_time_seconds": 2},
        "timings": {"prompt_per_second": 20, "predicted_per_second": 10},
        "prompt_eval_count": 999, "eval_count": 999, "load_duration": 1000,
    })
    assert actual["input_tokens"] == 10
    assert actual["output_tokens"] == 20
    assert actual["input_tokens_per_second"] is None
    assert actual["output_tokens_per_second"] is None
    assert actual["load_duration_ms"] is None


@pytest.mark.parametrize("value", INVALID_NUMBERS + [0.5, 1_000_000_001])
def test_token_fields_require_bounded_nonnegative_integral_numbers(value):
    actual = observe(usage={"prompt_tokens": value, "completion_tokens": value})
    assert actual["input_tokens"] is None
    assert actual["output_tokens"] is None


@pytest.mark.parametrize("value", [0, 1, 12.0, 1_000_000_000])
def test_valid_token_counts_include_reported_zero_and_integral_floats(value):
    actual = observe(usage={"prompt_tokens": value, "completion_tokens": value})
    assert actual["input_tokens"] == value
    assert type(actual["input_tokens"]) is int
    assert actual["output_tokens"] == value


@pytest.mark.parametrize("value", INVALID_NUMBERS + [1_000_000_001])
def test_lmstudio_reported_rate_must_be_a_sane_number(value):
    assert observe(raw={"stats": {"tokens_per_second": value}})["output_tokens_per_second"] is None


@pytest.mark.parametrize("value", INVALID_NUMBERS + [86400.001])
def test_lmstudio_load_seconds_must_be_a_sane_number(value):
    assert observe(raw={"stats": {"model_load_time_seconds": value}})["load_duration_ms"] is None


@pytest.mark.parametrize("value", INVALID_NUMBERS + [86_400_001])
def test_request_duration_must_be_a_sane_number(value):
    assert observe(latency_ms=value)["request_duration_ms"] is None


def test_load_zero_is_reported_zero_not_missing_and_duration_bound_is_inclusive():
    assert observe("ollama", raw={"load_duration": 0})["load_duration_ms"] == 0.0
    assert observe(raw={"stats": {"model_load_time_seconds": 0}})["load_duration_ms"] == 0.0
    assert observe(raw={"stats": {"model_load_time_seconds": 86400}})["load_duration_ms"] == 86_400_000.0
    assert observe(latency_ms=86_400_000)["request_duration_ms"] == 86_400_000.0
    assert observe(latency_ms=0)["request_duration_ms"] == 0.0


def test_implausible_or_overflowing_derived_rate_is_unknown_not_an_exception():
    assert observe("ollama", raw={"eval_count": 1_000_000_000, "eval_duration": 1})["output_tokens_per_second"] is None
    assert observe(raw={"timings": {"prompt_n": 1, "prompt_ms": 5e-324}})["input_tokens_per_second"] is None


@pytest.mark.parametrize("malformed", [None, [], "not an object", 42, True])
def test_non_object_raw_usage_or_extension_blocks_do_not_crash(malformed):
    result = UpstreamResult(text="private", usage=malformed, raw=malformed)
    actual = extract_observation("openai-compatible", result, 5)
    assert actual["input_tokens"] is None
    assert actual["output_tokens"] is None
    assert observe(raw={"stats": malformed, "timings": malformed})["load_duration_ms"] is None


def test_extraction_has_no_io_does_not_mutate_input_and_never_returns_payload_content(monkeypatch):
    raw = {
        "api_key": "secret-key", "model": "private-model", "host": "192.0.2.10",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "secret": "usage-secret"},
        "stats": {"model_load_time_seconds": 1.5, "tokens_per_second": 20, "filename": "/private/path"},
        "messages": [{"content": "secret prompt"}],
    }
    before = copy.deepcopy(raw)
    result = UpstreamResult(text="secret response", usage=raw["usage"], raw=raw,
                            tool_calls=({"function": {"arguments": "secret tool arguments"}},))

    def forbidden(*args, **kwargs):
        raise AssertionError("telemetry must not make requests, read files, or launch processes")

    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", forbidden)
        isolated.setattr(socket, "socket", forbidden)
        isolated.setattr(subprocess, "run", forbidden)
        actual = extract_observation("openai-compatible", result, 20)
    assert raw == before
    assert set(actual) == FIELDS
    assert all(value is None or type(value) in (int, float) for value in actual.values())
    serialized = json.dumps(actual, allow_nan=False)
    for secret in ("secret", "private-model", "192.0.2.10", "/private/path", "messages", "tool"):
        assert secret not in serialized
