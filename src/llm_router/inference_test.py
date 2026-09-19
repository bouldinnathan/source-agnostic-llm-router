"""Explicit, tiny generation tests, isolated from routing and passive checks.

Only the caller's authenticated, deliberate action may schedule this runner.
It can load an installed model into memory; it never downloads models, retries
generation, changes routing health, or records normal-traffic performance data.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .adapters import BUILTIN_ADAPTERS
from .schema import EndpointConfig, ModelConfig, RouterConfig
from .self_test import _display_target, _validated_base_url

MAX_CONCURRENCY = 2
MAX_ENDPOINT_PROBES = 16
METADATA_TIMEOUT_SECONDS = 4.0
INFERENCE_TIMEOUT_SECONDS = 90.0
BACKEND_TIMEOUT_SECONDS = 102.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_INFERENCE_RESPONSE_BYTES = 64 * 1024
MAX_OUTPUT_TOKENS = 16
PROMPT = "Reply with OK."
_OLLAMA = {"ollama", "ollama-chat"}
_OPENAI = {"openai-chat", "openai-compatible", "openai-responses"}
_NON_CHAT = (
    "embed", "rerank", "whisper", "transcri", "moderation", "guard", "classifier",
    "tts", "speech", "audio", "realtime", "dall-e", "gpt-image", "sora", "imagen",
    "veo-", "minilm",
)
_PARAMETERS = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?\s*([bmk])(?:\b|[_:-])", re.I)


class _CheckFailure(Exception):
    """Only fixed, non-sensitive messages are allowed here."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


async def run_inference_checks(
    config: RouterConfig | None,
    *,
    on_result: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Try one smallest eligible model per backend, without failover.

    Catalogs only narrow the enabled configured model set. Reported bytes are
    preferred, then explicitly labelled parameter estimates; absent size data
    uses a deterministic model-ID fallback. Returned rows contain no replies,
    prompts, upstream errors, authentication values, or arbitrary metadata.
    """
    if config is None:
        return []
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    scheduled = 0

    async def check(endpoint: EndpointConfig, reason: str | None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": endpoint.name, "target": _display_target(endpoint),
            "status": "skip" if reason else "fail", "model": None,
            "selection": "Not selected.", "detail": reason or "Model test failed.",
            "elapsed_ms": 0, "http_status": None,
        }
        if reason is None:
            async with semaphore:
                started = time.monotonic()
                candidates = {
                    model.upstream_model: model for model in config.models
                    if model.endpoint == endpoint.name and model.enabled and _chat_model(model, {})
                }
                try:
                    await asyncio.wait_for(
                        _check(endpoint, candidates, row, transport), BACKEND_TIMEOUT_SECONDS,
                    )
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    row["detail"] = "Backend test timed out; no retry was attempted. A model load or generation may still be finishing on the backend."
                except _CheckFailure as exc:
                    row["detail"] = str(exc)
                    if exc.http_status is not None:
                        row["http_status"] = exc.http_status
                except httpx.HTTPError:
                    row["detail"] = "Backend connection or TLS check failed; no retry was attempted."
                except Exception:
                    row["detail"] = "Backend authentication, address, or response is invalid; no retry was attempted."
                row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if on_result is not None:
            await on_result(dict(row))
        return row

    pending = []
    for endpoint in config.endpoints.values():
        reason = None
        if endpoint.adapter not in _OLLAMA | _OPENAI:
            reason = "This adapter has no supported bounded inference test; no model was invoked."
        elif not any(model.endpoint == endpoint.name and model.enabled and _chat_model(model, {}) for model in config.models):
            reason = "No enabled chat models are configured on this backend; no model was invoked."
        elif scheduled >= MAX_ENDPOINT_PROBES:
            reason = "Per-run backend limit reached; no model was invoked."
        else:
            scheduled += 1
        pending.append(asyncio.create_task(check(endpoint, reason)))
    try:
        return list(await asyncio.gather(*pending))
    except BaseException:
        # A rejected progress callback or cancellation must not leave other
        # generation tasks running after the owning job reports interruption.
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise


def _chat_model(model: ModelConfig, metadata: Mapping[str, Any]) -> bool:
    name = model.upstream_model
    if not isinstance(name, str) or not name or len(name) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    if any(term in name.lower() for term in _NON_CHAT):
        return False
    if any(tag.lower() in {"embedding", "embeddings", "rerank", "reranker", "non-chat"} for tag in model.tags):
        return False
    kind = metadata.get("type", metadata.get("model_type", ""))
    if isinstance(kind, str) and any(term in kind.lower() for term in _NON_CHAT):
        return False
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        declared = {value for value in capabilities if isinstance(value, str)}
        if declared.intersection({"embedding", "embeddings", "rerank", "speech", "transcription"}) and not declared.intersection({"completion", "chat", "generate", "text-generation"}):
            return False
    if any(model.capabilities.get(key, 0) > 0 for key in ("embedding", "embeddings", "rerank")):
        if not any(model.capabilities.get(key, 0) > 0 for key in ("general", "chat", "coding", "reasoning")):
            return False
    details = metadata.get("details")
    if isinstance(details, Mapping) and details.get("family") in {"bert", "nomic-bert", "jina-bert"}:
        return False
    return True


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value <= 10 ** 18 and math.isfinite(value) else None


def _size(metadata: Mapping[str, Any]) -> float | None:
    for key in ("size_bytes", "size"):
        value = _positive(metadata.get(key))
        if value is not None:
            return value
    return None


def _parameter_estimate(name: str, metadata: Mapping[str, Any]) -> float | None:
    direct = _positive(metadata.get("parameter_count"))
    if direct is not None:
        return direct
    details = metadata.get("details")
    details = details if isinstance(details, Mapping) else {}
    for value in (metadata.get("params_string"), details.get("parameter_size"), name):
        if isinstance(value, str) and len(value) <= 512:
            match = _PARAMETERS.search(value)
            if match:
                count = float(match[1]) * float(match[2] or 1) * {"b": 1e9, "m": 1e6, "k": 1e3}[match[3].lower()]
                if _positive(count) is not None:
                    return count
    return None


def _choose(candidates: Mapping[str, ModelConfig], entries: Mapping[str, Mapping[str, Any]]) -> tuple[str, str] | None:
    eligible = [(name, entries[name]) for name, model in candidates.items() if name in entries and _chat_model(model, entries[name])]
    if not eligible:
        return None
    sizes = [(size, name) for name, meta in eligible if (size := _size(meta)) is not None]
    if sizes:
        size, name = min(sizes)
        suffix = "; other model sizes are unknown" if len(sizes) < len(eligible) else ""
        return name, f"Smallest reported model size: {int(size):,} bytes{suffix}."
    estimates = [(count, name) for name, meta in eligible if (count := _parameter_estimate(name, meta)) is not None]
    if estimates:
        count, name = min(estimates)
        suffix = "; other parameter counts are unknown" if len(estimates) < len(eligible) else ""
        return name, f"Estimated smallest by parameter count/name: {count / 1e9:g}B; file sizes unavailable{suffix}."
    return min(name for name, _ in eligible), "Sizes and parameter counts unknown; deterministic model-ID selection (not a verified smallest model)."


async def _json_request(
    endpoint: EndpointConfig, method: str, url: str, headers: Mapping[str, str],
    params: Mapping[str, str], transport: httpx.AsyncBaseTransport | None,
    *, body: Mapping[str, Any] | None = None,
) -> tuple[int, Mapping[str, Any]]:
    timeout = METADATA_TIMEOUT_SECONDS if method == "GET" else INFERENCE_TIMEOUT_SECONDS
    limit = MAX_RESPONSE_BYTES if method == "GET" else MAX_INFERENCE_RESPONSE_BYTES
    # A fresh client also prevents a catalog response setting cookies for the
    # generation call. No ambient proxy, netrc, or redirect credentials.
    async def request() -> tuple[int, Mapping[str, Any]]:
        async with httpx.AsyncClient(timeout=timeout, verify=endpoint.verify_tls, follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream(method, url, headers=headers, params=params, json=body) as response:
                if not 200 <= response.status_code < 300:
                    return response.status_code, {}
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise _CheckFailure("Backend returned an unsupported compressed response; no retry was attempted.", response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                    if len(content) + len(chunk) > limit:
                        raise _CheckFailure("Backend response exceeds the test size limit; no retry was attempted.", response.status_code)
                    content.extend(chunk)
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeError):
                    raise _CheckFailure("Backend did not return valid JSON; no retry was attempted.", response.status_code) from None
                if not isinstance(payload, Mapping):
                    raise _CheckFailure("Backend response has an unexpected format; no retry was attempted.", response.status_code)
                return response.status_code, payload
    return await asyncio.wait_for(request(), timeout)


def _catalog(payload: Mapping[str, Any], field: str, *, ollama: bool = False) -> dict[str, Mapping[str, Any]]:
    entries = payload.get(field)
    if not isinstance(entries, list) or "error" in payload:
        raise _CheckFailure("Backend model catalog has an unexpected format; no model was invoked.")
    indexed = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("model", entry.get("name")) if ollama else entry.get("id", entry.get("key"))
        if isinstance(name, str) and len(name) <= 512:
            indexed[name] = entry
            if ollama and name.endswith(":latest"):
                indexed.setdefault(name[:-7], entry)
        # Native LM Studio loaded instances may use IDs unlike the model key.
        instances = entry.get("loaded_instances", [])
        if isinstance(instances, list):
            for instance in instances:
                if isinstance(instance, Mapping) and isinstance(instance.get("id"), str) and len(instance["id"]) <= 512:
                    indexed[instance["id"]] = entry
    return indexed


async def _check(endpoint: EndpointConfig, candidates: Mapping[str, ModelConfig], row: dict[str, Any], transport: httpx.AsyncBaseTransport | None) -> None:
    base, _ = _validated_base_url(endpoint.base_url)
    if endpoint.auth.key_env == "LLM_ROUTER_GATEWAY_API_KEY" or any("${LLM_ROUTER_GATEWAY_API_KEY}" in value for value in endpoint.headers.values()):
        raise _CheckFailure("Backend credentials must be configured separately from the router API key; no model was invoked.")
    adapter = BUILTIN_ADAPTERS[endpoint.adapter]()
    headers, params = adapter.connection_metadata(endpoint, {})
    gateway_key = os.environ.get("LLM_ROUTER_GATEWAY_API_KEY", "")
    if gateway_key and any(gateway_key in value for value in (*headers.values(), *params.values())):
        raise _CheckFailure("Backend credentials must be configured separately from the router API key; no model was invoked.")
    headers.update({"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"})
    ollama = endpoint.adapter in _OLLAMA
    status, payload = await _json_request(endpoint, "GET", base + ("/api/tags" if ollama else "/models"), headers, params, transport)
    row["http_status"] = status
    if not ollama and status in {404, 405}:
        entries: dict[str, Mapping[str, Any]] = {name: {} for name in candidates}
        catalog_note = " Catalog unavailable; used enabled configured model IDs."
    elif 200 <= status < 300:
        entries = _catalog(payload, "models" if ollama else "data", ollama=ollama)
        catalog_note = ""
    else:
        raise _CheckFailure(f"Model catalog returned HTTP {status}; no model was invoked.")

    if any(isinstance(meta.get("owned_by"), str) and meta["owned_by"].strip().lower() == "llm-router" for meta in entries.values()):
        row.update(status="skip", detail="This address identifies another LLM router, not a direct model backend; no model was invoked.")
        return

    # Native LM Studio metadata provides true file sizes, unlike its OpenAI
    # catalog. Never remove an arbitrary reverse-proxy prefix from its URL.
    parsed = urlsplit(base)
    lm_studio = parsed.port == 1234 or "lm-studio" in endpoint.name.lower() or "lmstudio" in endpoint.name.lower()
    if not ollama and lm_studio and parsed.path.rstrip("/").endswith("/v1"):
        native_base = base[:-3]
        try:
            status, native = await _json_request(endpoint, "GET", native_base + "/api/v1/models", headers, params, transport)
            if status in {404, 405}:
                status, native = await _json_request(endpoint, "GET", native_base + "/api/v0/models", headers, params, transport)
                native_field = "data"
            else:
                native_field = "models"
            if 200 <= status < 300:
                extras = _catalog(native, native_field)
                entries = {name: {**meta, **extras.get(name, {})} for name, meta in entries.items()}
        except (httpx.HTTPError, asyncio.TimeoutError, _CheckFailure):
            pass  # Optional size metadata is not generation retry/failover.

    selected = _choose(candidates, entries)
    if selected is None:
        row.update(status="skip", detail="No eligible enabled chat model was listed by this backend; no model was invoked.")
        return
    model, selection = selected
    row.update(model=model, selection=selection + catalog_note)
    token_limit = min(MAX_OUTPUT_TOKENS, candidates[model].max_output_tokens)
    if ollama:
        path = "/api/chat"
        body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "stream": False, "think": False, "options": {"num_predict": token_limit, "temperature": 0}}
    elif endpoint.adapter == "openai-responses":
        path = "/responses"
        body = {"model": model, "input": [{"role": "user", "content": PROMPT}], "max_output_tokens": token_limit, "stream": False, "store": False}
    else:
        path = "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": token_limit, "stream": False, "temperature": 0}
    row["http_status"] = None
    status, response = await _json_request(endpoint, "POST", base + path, headers, params, transport, body=body)
    row["http_status"] = status
    if not 200 <= status < 300:
        raise _CheckFailure(f"Generation returned HTTP {status}; no retry or failover was attempted.")
    if not _generated_text(response, endpoint.adapter):
        raise _CheckFailure("Generation returned no valid nonempty text response; no retry or failover was attempted.")
    row.update(status="pass", detail="One tiny generation returned nonempty text from this backend; reply content was discarded. No retry or failover was used.")


def _generated_text(payload: Mapping[str, Any], adapter: str) -> bool:
    if "error" in payload:
        return False
    if adapter in _OLLAMA:
        message = payload.get("message")
        return payload.get("done") is True and isinstance(message, Mapping) and isinstance(message.get("content"), str) and bool(message["content"].strip())
    if adapter == "openai-responses":
        if payload.get("status") in {"failed", "cancelled", "in_progress", "queued"}:
            return False
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return True
        output = payload.get("output")
        return isinstance(output, list) and any(
            isinstance(item, Mapping) and item.get("type") == "message" and isinstance(item.get("content"), list)
            and any(isinstance(part, Mapping) and part.get("type") == "output_text" and isinstance(part.get("text"), str) and part["text"].strip() for part in item["content"])
            for item in output
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return False
    message = choices[0].get("message")
    return isinstance(message, Mapping) and isinstance(message.get("content"), str) and bool(message["content"].strip())
