"""Whitelisted timing observations from responses to ordinary routed requests.

This module performs no I/O and never requests a benchmark, model load, or a
different API endpoint. Missing phase timings remain unknown: total request
latency and time to first token are not prompt/decode durations.

Documented wire formats used here:
* https://docs.ollama.com/api/chat (nanoseconds; prompt duration excludes cache)
* https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
  (OpenAI-compatible ``timings``: milliseconds and tokens/second)
* https://lmstudio.ai/docs/developer/rest/endpoints (``stats.tokens_per_second``)
* https://lmstudio.ai/docs/developer/rest/chat (``model_load_time_seconds``)
* https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* https://ai.google.dev/api/generate-content#UsageMetadata

LM Studio documents enhanced stats for its native APIs, not as a guarantee for
its OpenAI-compatible API. We only consume those documented fields if a routed
response already includes them; we never switch APIs to obtain them. Reported
model load time is not a measurement of disk I/O or proof of a cold load.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .schema import UpstreamResult


# Deliberately generous sanity limits, not admission limits or benchmarks.
MAX_TOKENS = 1_000_000_000
MAX_TOKENS_PER_SECOND = 1_000_000_000
MAX_DURATION_MS = 86_400_000  # One day for one observation.
_OPENAI = {"openai-chat", "openai-compatible", "openai-responses"}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object, maximum: float) -> float | None:
    # Exact types avoid booleans, numeric strings, arbitrary coercion methods,
    # and overflow when a malicious JSON integer has thousands of digits.
    if type(value) not in (int, float) or not 0 <= value <= maximum:
        return None
    return float(value) if math.isfinite(value) else None


def _count(value: object) -> int | None:
    parsed = _number(value, MAX_TOKENS)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def _lookup(sources: Sequence[Mapping[str, Any]], *names: str) -> tuple[bool, object]:
    """Use the first reported field, not a replacement for a malformed value."""
    for source in sources:
        for name in names:
            if name in source:
                return True, source[name]
    return False, None


def _sum_counts(sources: Sequence[Mapping[str, Any]], required: str, *optional: str) -> int | None:
    _, value = _lookup(sources, required)
    total = _count(value)
    if total is None:
        return None
    for name in optional:
        present, value = _lookup(sources, name)
        count = _count(value) if present else 0
        if count is None:
            return None
        total += count
    return total if total <= MAX_TOKENS else None


def _duration(value: object, milliseconds_per_unit: float = 1.0) -> float | None:
    parsed = _number(value, MAX_DURATION_MS / milliseconds_per_unit)
    if parsed is None:
        return None
    return _number(parsed * milliseconds_per_unit, MAX_DURATION_MS)


def _rate(tokens: int | None, duration_ms: float | None) -> float | None:
    if tokens is None or tokens <= 0 or duration_ms is None or duration_ms <= 0:
        return None
    # The numerator counts only the work associated with this phase duration.
    return _number(tokens / duration_ms * 1000.0, MAX_TOKENS_PER_SECOND)


def _llama_rate(timings: Mapping[str, Any], phase: str) -> float | None:
    tokens = _count(timings.get(f"{phase}_n"))
    duration = _duration(timings.get(f"{phase}_ms"))
    if tokens == 0 or duration == 0:
        return None
    rate_name = f"{phase}_per_second"
    if rate_name in timings:
        return _number(timings[rate_name], MAX_TOKENS_PER_SECOND)
    return _rate(tokens, duration)


def extract_observation(adapter: str, result: UpstreamResult, latency_ms: float) -> dict[str, int | float | None]:
    """Extract passive numeric-only per-request telemetry without side effects.

    ``request_duration_ms`` is caller-measured HTTP/request latency, independent
    of reported model phase timings. Input counts include prompt-cache tokens;
    input throughput uses uncached work only where the provider identifies it.
    Output counts include reported reasoning tokens where separately reported.
    """
    raw = _mapping(result.raw)
    usage = (_mapping(result.usage), _mapping(raw.get("usage")))
    observation: dict[str, int | float | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "input_tokens_per_second": None,
        "output_tokens_per_second": None,
        "load_duration_ms": None,
        "request_duration_ms": _duration(latency_ms),
    }

    if adapter in {"ollama", "ollama-chat"}:
        sources = (raw, *usage)
        _, value = _lookup(sources, "prompt_eval_count")
        prompt_tokens = _count(value)
        _, value = _lookup(sources, "eval_count")
        output_tokens = _count(value)
        cached_present, value = _lookup(sources, "prompt_eval_cached_count")
        cached_tokens = _count(value) if cached_present else 0
        uncached = (
            prompt_tokens - cached_tokens
            if prompt_tokens is not None and cached_tokens is not None and cached_tokens <= prompt_tokens
            else None
        )
        observation.update({
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "input_tokens_per_second": _rate(uncached, _duration(raw.get("prompt_eval_duration"), 1e-6)),
            "output_tokens_per_second": _rate(output_tokens, _duration(raw.get("eval_duration"), 1e-6)),
            "load_duration_ms": _duration(raw.get("load_duration"), 1e-6),
        })
        return observation

    if adapter in {"anthropic", "anthropic-messages"}:
        observation["input_tokens"] = _sum_counts(
            usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        )
        _, value = _lookup(usage, "output_tokens")
        observation["output_tokens"] = _count(value)
        return observation

    if adapter in {"gemini", "gemini-generate"}:
        sources = (_mapping(result.usage), _mapping(raw.get("usageMetadata")))
        _, value = _lookup(sources, "promptTokenCount")
        observation["input_tokens"] = _count(value)
        observation["output_tokens"] = _sum_counts(sources, "candidatesTokenCount", "thoughtsTokenCount")
        return observation

    input_names = ("input_tokens", "prompt_tokens") if adapter == "openai-responses" else ("prompt_tokens", "input_tokens")
    output_names = ("output_tokens", "completion_tokens") if adapter == "openai-responses" else ("completion_tokens", "output_tokens")
    input_present, value = _lookup(usage, *input_names)
    observation["input_tokens"] = _count(value)
    output_present, value = _lookup(usage, *output_names)
    observation["output_tokens"] = _count(value)

    if adapter not in _OPENAI:
        # Unknown adapters can share standard usage counts, but cannot opt into
        # another provider's timing units merely by using a similarly named key.
        return observation

    timings = _mapping(raw.get("timings"))
    stats = _mapping(raw.get("stats"))
    if not input_present:
        present, value = _lookup((stats,), "input_tokens")
        observation["input_tokens"] = _count(value) if present else _sum_counts((timings,), "prompt_n", "cache_n")
    if not output_present:
        present, value = _lookup((stats,), "total_output_tokens")
        observation["output_tokens"] = _count(value) if present else _count(timings.get("predicted_n"))

    observation["input_tokens_per_second"] = _llama_rate(timings, "prompt")
    observation["output_tokens_per_second"] = _llama_rate(timings, "predicted")
    if "predicted_per_second" not in timings and "predicted_ms" not in timings:
        # Documented LM Studio generation speed; never divide by TTFT or its
        # generic generation_time field to invent a missing prompt speed.
        observation["output_tokens_per_second"] = _number(stats.get("tokens_per_second"), MAX_TOKENS_PER_SECOND)
    observation["load_duration_ms"] = _duration(stats.get("model_load_time_seconds"), 1000.0)
    return observation


__all__ = ["extract_observation"]
