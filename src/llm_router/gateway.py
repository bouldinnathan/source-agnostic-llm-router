"""Resilient Ollama/OpenAI-compatible HTTP gateway for the model router."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from .bootstrap import BootstrapResult, bootstrap_router
from .discovery import DiscoveryReport, DiscoverySettings
from .errors import AllModelsFailed, NoEligibleModel, RequestError, RouterError
from .provisioning import OllamaProvisioner, ProvisioningReport, ProvisioningSettings
from .router import LLMRouter
from .schema import QueryRequest, RoutedCompletion, RouterConfig

VERSION = "0.3.0"
VIRTUAL_MODELS: dict[str, str] = {
    "auto": "quality",
    "auto:quality": "quality",
    "auto:balanced": "balanced",
    "auto:cost": "cost",
    "auto:latency": "latency",
    "auto:priority": "priority",
    "auto:local": "quality",
    "auto:cloud": "quality",
}
VIRTUAL_PREFERRED_TAGS: dict[str, tuple[str, ...]] = {
    "auto:local": ("local",),
    "auto:cloud": ("cloud",),
}


class GatewayUnavailable(RuntimeError):
    pass


class RouterGateway:
    """Owns an atomically refreshed router and retains the last good state."""

    def __init__(
        self,
        *,
        config_path: str | None = None,
        discovery: bool = True,
        settings: DiscoverySettings | None = None,
        provisioning_settings: ProvisioningSettings | None = None,
        provisioner: OllamaProvisioner | None = None,
    ) -> None:
        self.config_path = config_path
        self.discovery_enabled = discovery
        self.settings = settings or DiscoverySettings.from_env()
        self.provisioning_settings = (
            provisioning_settings or ProvisioningSettings.from_env()
        )
        self._provisioner = provisioner or OllamaProvisioner(self.provisioning_settings)
        self._router: LLMRouter | None = None
        self._discovery: DiscoveryReport | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._provision_task: asyncio.Task[None] | None = None
        self._provisioning: ProvisioningReport | None = None
        self._last_error: str | None = None
        self._last_refresh: float | None = None

    async def start(self) -> None:
        await self.refresh()
        self._ensure_provisioning()
        if self.discovery_enabled and self.settings.enabled:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="llm-router-discovery-refresh"
            )

    async def stop(self) -> None:
        for task in (self._refresh_task, self._provision_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None
        self._provision_task = None

    async def refresh(self) -> bool:
        async with self._refresh_lock:
            previous = self._router
            try:
                result = await bootstrap_router(
                    self.config_path,
                    discovery=self.discovery_enabled,
                    settings=self.settings,
                    previous=previous,
                )
                result = self._retain_failed_sources(result, previous)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = _safe_exception(exc)
                self._last_refresh = time.time()
                return False
            self._router = result.router
            self._discovery = result.discovery
            self._last_error = None
            self._last_refresh = time.time()
            return True

    async def router(self) -> LLMRouter:
        if self._router is None:
            await self.refresh()
        if self._router is None:
            raise GatewayUnavailable(self._last_error or "No router is available")
        return self._router

    async def provision(
        self,
        *,
        dry_run: bool = False,
        requested_model: str | None = None,
        allow_remote: bool | None = None,
    ) -> ProvisioningReport:
        report = await self._provisioner.provision(
            dry_run=dry_run,
            requested_model=requested_model,
            allow_remote=allow_remote,
        )
        self._provisioning = report
        if report.status == "installed":
            await self.refresh()
        return report

    def status(self) -> dict[str, Any]:
        availability_error: str | None = None
        if self._router is None:
            state = "unavailable"
        elif self._last_error:
            state = "degraded"
        elif not any(
            model.enabled and self._router.runtime.is_available(model.id)
            for model in self._router.config.models
        ):
            state = "degraded"
            availability_error = "No enabled deployment is currently available"
        else:
            state = "ready"
        payload: dict[str, Any] = {
            "status": state,
            "version": VERSION,
            "last_refresh": (
                datetime.fromtimestamp(self._last_refresh, timezone.utc).isoformat()
                if self._last_refresh is not None
                else None
            ),
            "last_error": self._last_error or availability_error,
        }
        if self._router is not None:
            payload["router"] = self._router.status()
        if self._discovery is not None:
            payload["discovery"] = self._discovery.to_dict(include_failures=True)
        if self._provisioning is not None:
            payload["provisioning"] = self._provisioning.to_dict()
        return payload

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.refresh_seconds)
            await self.refresh()
            self._ensure_provisioning()

    def _ensure_provisioning(self) -> None:
        if (
            not self.discovery_enabled
            or not self.provisioning_settings.enabled
            or (self._provision_task is not None and not self._provision_task.done())
        ):
            return
        self._provision_task = asyncio.create_task(
            self._provision_and_refresh(), name="llm-router-model-provisioning"
        )

    async def _provision_and_refresh(self) -> None:
        try:
            await self.provision()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._provisioning = ProvisioningReport(
                "failed",
                self.provisioning_settings.ollama_url,
                _safe_exception(exc),
            )

    @staticmethod
    def _retain_failed_sources(
        result: BootstrapResult, previous: LLMRouter | None
    ) -> BootstrapResult:
        if previous is None:
            return result
        failed_urls = {
            probe.base_url.rstrip("/")
            for probe in result.discovery.probes
            if not probe.reachable
        }
        if not failed_urls:
            return result
        endpoints = dict(result.router.config.endpoints)
        models = {model.id: model for model in result.router.config.models}
        retained_endpoints: set[str] = set()
        for name, endpoint in previous.config.endpoints.items():
            if endpoint.base_url.rstrip("/") in failed_urls and name not in endpoints:
                endpoints[name] = endpoint
                retained_endpoints.add(name)
        for model in previous.config.models:
            if model.endpoint in retained_endpoints and model.id not in models:
                models[model.id] = model
        if not retained_endpoints:
            return result
        merged = RouterConfig(
            endpoints=endpoints,
            models=tuple(models.values()),
            policy=result.router.config.policy,
            source_path=result.router.config.source_path,
        )
        router = LLMRouter(merged, runtime=previous.runtime)
        return replace(result, router=router)


def create_app(
    *,
    config_path: str | None = None,
    discovery: bool = True,
    settings: DiscoverySettings | None = None,
    provisioning_settings: ProvisioningSettings | None = None,
    provisioner: OllamaProvisioner | None = None,
    gateway: RouterGateway | None = None,
) -> Starlette:
    service = gateway or RouterGateway(
        config_path=config_path,
        discovery=discovery,
        settings=settings,
        provisioning_settings=provisioning_settings,
        provisioner=provisioner,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    async def root(request: Request) -> Response:
        return PlainTextResponse("LLM Router is running")

    async def health(request: Request) -> Response:
        status = service.status()
        public_status = {"status": status["status"], "version": status["version"]}
        return JSONResponse(
            public_status, status_code=200 if status["status"] != "unavailable" else 503
        )

    async def router_status(request: Request) -> Response:
        denied = _authorize(request)
        return denied or JSONResponse(service.status())

    async def refresh(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        refreshed = await service.refresh()
        return JSONResponse(
            {"ok": refreshed, **service.status()}, status_code=200 if refreshed else 503
        )

    async def provision(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        if not hasattr(service, "provision"):
            return _ollama_error("provisioning is unavailable", 503)
        try:
            body = await _optional_json_body(request)
            dry_run = body.get("dry_run", False)
            model = body.get("model")
            allow_remote = body.get("allow_remote")
            if not isinstance(dry_run, bool):
                raise ValueError("dry_run must be true or false")
            if model is not None and not isinstance(model, str):
                raise ValueError("model must be a string")
            if allow_remote is not None and not isinstance(allow_remote, bool):
                raise ValueError("allow_remote must be true or false")
            report = await service.provision(
                dry_run=dry_run,
                requested_model=model,
                allow_remote=allow_remote,
            )
            return JSONResponse(report.to_dict(), status_code=200 if report.ok else 409)
        except ValueError as exc:
            return _ollama_error(str(exc), 400)
        except Exception as exc:
            return _ollama_error(_safe_exception(exc), 500)

    async def ollama_version(request: Request) -> Response:
        denied = _authorize(request)
        return denied or JSONResponse({"version": f"llm-router-{VERSION}"})

    async def ollama_tags(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        try:
            router = await service.router()
        except GatewayUnavailable as exc:
            return _ollama_error(str(exc), 503)
        return JSONResponse({"models": _ollama_models(router)})

    async def ollama_show(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        try:
            body = await _json_body(request)
            model = str(body.get("model", body.get("name", "auto")))
            await service.router()
            _strategy(model)
        except (ValueError, GatewayUnavailable) as exc:
            return _ollama_error(str(exc), 503 if isinstance(exc, GatewayUnavailable) else 404)
        return JSONResponse(
            {
                "license": "",
                "modelfile": "# Virtual model routed by source-agnostic-llm-router",
                "parameters": "",
                "template": "",
                "details": {"family": "llm-router", "families": ["llm-router"]},
                "capabilities": ["completion", "tools", "vision"],
                "model_info": {"general.architecture": "llm-router"},
            }
        )

    async def ollama_pull(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        try:
            body = await _json_body(request)
            model = str(body.get("model", body.get("name", "")))
            _strategy(model)
        except ValueError as exc:
            return _ollama_error(str(exc), 404)
        report: ProvisioningReport | None = None
        if hasattr(service, "provision"):
            try:
                report = await service.provision()
            except Exception as exc:
                return _ollama_error(_safe_exception(exc), 500)
            if not report.ok:
                return _ollama_error(report.reason, 503 if report.status == "failed" else 409)
        payload: dict[str, Any] = {"status": "success"}
        if report is not None:
            payload["router_provisioning"] = report.to_dict()
        if body.get("stream", True):
            return StreamingResponse(_ndjson([payload]), media_type="application/x-ndjson")
        return JSONResponse(payload)

    async def ollama_ps(request: Request) -> Response:
        denied = _authorize(request)
        return denied or JSONResponse({"models": []})

    async def ollama_chat(request: Request) -> Response:
        denied = _authorize(request)
        if denied:
            return denied
        try:
            body = await _json_body(request)
            model = str(body.get("model", "auto"))
            strategy = _strategy(model)
            preferred_tags = VIRTUAL_PREFERRED_TAGS.get(model, ())
            messages = body.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages must be an array")
            if not messages:
                return JSONResponse(_ollama_empty(model, "load"))
            router = await service.router()
            query = _query_request(
                body, messages, strategy, ollama=True, preferred_tags=preferred_tags
            )
            completion = await router.complete(query)
            first, final = _ollama_completion(model, completion)
            if body.get("stream", True):
                return StreamingResponse(
                    _ndjson([first, final]), media_type="application/x-ndjson"
                )
            combined = dict(final)
            combined["message"] = first["message"]
            return JSONResponse(combined)
        except (ValueError, RequestError) as exc:
            return _ollama_error(str(exc), 400)
        except (GatewayUnavailable, AllModelsFailed, NoEligibleModel) as exc:
            return _ollama_error(str(exc), 503)
        except RouterError as exc:
            return _ollama_error(str(exc), 502)
        except Exception as exc:
            return _ollama_error(_safe_exception(exc), 500)

    async def openai_models(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        if denied:
            return denied
        try:
            await service.router()
        except GatewayUnavailable as exc:
            return _openai_error(str(exc), 503, "router_unavailable")
        now = int(time.time())
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": now,
                        "owned_by": "llm-router",
                    }
                    for model in VIRTUAL_MODELS
                ],
            }
        )

    async def openai_chat(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        if denied:
            return denied
        try:
            body = await _json_body(request)
            model = str(body.get("model", "auto"))
            strategy = _strategy(model)
            preferred_tags = VIRTUAL_PREFERRED_TAGS.get(model, ())
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a non-empty array")
            router = await service.router()
            query = _query_request(
                body, messages, strategy, ollama=False, preferred_tags=preferred_tags
            )
            completion = await router.complete(query)
            payload = _openai_completion(model, completion)
            if body.get("stream", False):
                return StreamingResponse(
                    _openai_sse(payload), media_type="text/event-stream"
                )
            return JSONResponse(payload)
        except (ValueError, RequestError) as exc:
            return _openai_error(str(exc), 400, "invalid_request_error")
        except (GatewayUnavailable, AllModelsFailed, NoEligibleModel) as exc:
            return _openai_error(str(exc), 503, "router_unavailable")
        except RouterError as exc:
            return _openai_error(str(exc), 502, "upstream_error")
        except Exception as exc:
            return _openai_error(_safe_exception(exc), 500, "internal_error")

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/readyz", health, methods=["GET"]),
        Route("/router/status", router_status, methods=["GET"]),
        Route("/router/discover", refresh, methods=["POST"]),
        Route("/router/provision", provision, methods=["POST"]),
        Route("/api/version", ollama_version, methods=["GET"]),
        Route("/api/tags", ollama_tags, methods=["GET"]),
        Route("/api/show", ollama_show, methods=["POST"]),
        Route("/api/pull", ollama_pull, methods=["POST"]),
        Route("/api/ps", ollama_ps, methods=["GET"]),
        Route("/api/chat", ollama_chat, methods=["POST"]),
        Route("/v1/models", openai_models, methods=["GET"]),
        Route("/v1/chat/completions", openai_chat, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.router_gateway = service
    return app


def _query_request(
    body: Mapping[str, Any],
    messages: list[Any],
    strategy: str,
    *,
    ollama: bool,
    preferred_tags: tuple[str, ...] = (),
) -> QueryRequest:
    if not all(isinstance(message, Mapping) for message in messages):
        raise ValueError("every message must be an object")
    tools = body.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or not all(isinstance(tool, Mapping) for tool in tools):
        raise ValueError("tools must be an array of objects")
    options = body.get("options", {}) if ollama else {}
    if not isinstance(options, Mapping):
        raise ValueError("options must be an object")
    max_tokens = (
        options.get("num_predict", 2048)
        if ollama
        else body.get("max_completion_tokens", body.get("max_tokens", 2048))
    )
    temperature = options.get("temperature") if ollama else body.get("temperature")
    response_format: Mapping[str, Any] | None = None
    format_value = body.get("format") if ollama else body.get("response_format")
    if format_value == "json":
        response_format = {"type": "json_object"}
    elif isinstance(format_value, Mapping):
        if format_value.get("type") in {"json_object", "json_schema"}:
            response_format = dict(format_value)
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "home_assistant_response",
                    "schema": dict(format_value),
                    "strict": True,
                },
            }
    required: list[str] = []
    if tools:
        required.append("tool_use")
    if response_format is not None:
        required.append("structured_output")
    if ollama and body.get("think"):
        required.append("reasoning")
    if _messages_have_images(messages):
        required.append("vision")
    min_context_window = options.get("num_ctx") if ollama else None
    return QueryRequest(
        messages=tuple(dict(message) for message in messages),
        required_capabilities=tuple(required),
        strategy=strategy,
        min_context_window=min_context_window,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tuple(dict(tool) for tool in tools),
        response_format=response_format,
        preferred_tags=preferred_tags,
    )


def _messages_have_images(messages: list[Any]) -> bool:
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        images = message.get("images")
        if isinstance(images, list) and images:
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, Mapping)
            and part.get("type") in {"image", "image_url", "input_image"}
            for part in content
        ):
            return True
    return False


def _ollama_models(router: LLMRouter) -> list[dict[str, Any]]:
    now = _timestamp()
    largest_context = max(
        (model.context_window for model in router.config.models if model.enabled), default=8192
    )
    return [
        {
            "name": name,
            "model": name,
            "modified_at": now,
            "size": 0,
            "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
            "details": {
                "format": "router",
                "family": "llm-router",
                "families": ["llm-router"],
                "parameter_size": "dynamic",
                "quantization_level": "dynamic",
                "context_length": largest_context,
            },
        }
        for name in VIRTUAL_MODELS
    ]


def _ollama_completion(
    model: str, completion: RoutedCompletion
) -> tuple[dict[str, Any], dict[str, Any]]:
    created = _timestamp()
    message: dict[str, Any] = {"role": "assistant", "content": completion.text}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "function": {
                    "name": str(call.get("function", {}).get("name", "tool")),
                    "arguments": dict(call.get("function", {}).get("arguments", {})),
                }
            }
            for call in completion.tool_calls
            if isinstance(call.get("function"), Mapping)
        ]
    first = {
        "model": model,
        "created_at": created,
        "message": message,
        "done": False,
    }
    usage = _usage(completion.usage)
    latency_ms = next(
        (
            float(attempt.get("latency_ms", 0))
            for attempt in reversed(completion.attempts)
            if attempt.get("success")
        ),
        0.0,
    )
    final = {
        "model": model,
        "created_at": created,
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": completion.finish_reason or (
            "tool_calls" if completion.tool_calls else "stop"
        ),
        "total_duration": int(latency_ms * 1_000_000),
        "prompt_eval_count": usage["prompt_tokens"],
        "eval_count": usage["completion_tokens"],
        "router": {
            "deployment": completion.deployment,
            "endpoint": completion.endpoint,
            "upstream_model": completion.upstream_model,
        },
    }
    return first, final


def _ollama_empty(model: str, reason: str) -> dict[str, Any]:
    return {
        "model": model,
        "created_at": _timestamp(),
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": reason,
    }


def _openai_completion(model: str, completion: RoutedCompletion) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.text or None}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": str(call.get("id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(call.get("function", {}).get("name", "tool")),
                    "arguments": json.dumps(
                        call.get("function", {}).get("arguments", {}), separators=(",", ":")
                    ),
                },
            }
            for index, call in enumerate(completion.tool_calls)
            if isinstance(call.get("function"), Mapping)
        ]
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if completion.tool_calls else "stop",
            }
        ],
        "usage": _usage(completion.usage),
        "router": {
            "deployment": completion.deployment,
            "endpoint": completion.endpoint,
            "upstream_model": completion.upstream_model,
        },
    }


async def _openai_sse(payload: Mapping[str, Any]) -> AsyncIterator[bytes]:
    choice = payload["choices"][0]
    message = choice["message"]
    delta = dict(message)
    delta.pop("role", None)
    chunk = {
        "id": payload["id"],
        "object": "chat.completion.chunk",
        "created": payload["created"],
        "model": payload["model"],
        "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}],
    }
    final = {
        "id": payload["id"],
        "object": "chat.completion.chunk",
        "created": payload["created"],
        "model": payload["model"],
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
    }
    yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
    yield f"data: {json.dumps(final, separators=(',', ':'))}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def _ndjson(items: list[Mapping[str, Any]]) -> AsyncIterator[bytes]:
    for item in items:
        yield (json.dumps(item, separators=(",", ":")) + "\n").encode()


async def _json_body(request: Request) -> Mapping[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("request body must be a JSON object")
    return value


async def _optional_json_body(request: Request) -> Mapping[str, Any]:
    body = await request.body()
    if not body:
        return {}
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("request body must be a JSON object")
    return value


def _strategy(model: str) -> str:
    try:
        return VIRTUAL_MODELS[model]
    except KeyError as exc:
        choices = ", ".join(VIRTUAL_MODELS)
        raise ValueError(f"unknown virtual model '{model}'; choose one of: {choices}") from exc


def _usage(usage: Mapping[str, Any]) -> dict[str, int]:
    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return 0

    prompt = number("prompt_tokens", "input_tokens", "prompt_eval_count")
    completion = number("completion_tokens", "output_tokens", "eval_count")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": number("total_tokens") or prompt + completion,
    }


def _authorize(request: Request, *, openai: bool = False) -> Response | None:
    expected = os.environ.get("LLM_ROUTER_GATEWAY_API_KEY")
    if not expected:
        return None
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if supplied and hmac.compare_digest(supplied, expected):
        return None
    if openai:
        return _openai_error("Invalid or missing gateway API key", 401, "authentication_error")
    return _ollama_error("Invalid or missing gateway API key", 401)


def _ollama_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _openai_error(message: str, status: int, error_type: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "param": None, "code": error_type}},
        status_code=status,
    )


def _safe_exception(exc: Exception) -> str:
    if isinstance(exc, RouterError):
        return str(exc)
    return f"Internal router failure ({type(exc).__name__})"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-router-gateway",
        description="Serve one auto-discovered Ollama/OpenAI-compatible LLM endpoint.",
    )
    parser.add_argument("--config", help="Optional TOML/JSON config merged over discovery")
    parser.add_argument("--host", default=os.environ.get("LLM_ROUTER_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LLM_ROUTER_PORT", "8088"))
    )
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("--no-provision", action="store_true")
    parser.add_argument("--refresh-seconds", type=float)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    settings = DiscoverySettings.from_env()
    provisioning_settings = ProvisioningSettings.from_env()
    if args.refresh_seconds is not None:
        if args.refresh_seconds < 5:
            parser.error("--refresh-seconds must be at least 5")
        settings = replace(settings, refresh_seconds=args.refresh_seconds)
    if args.no_provision:
        provisioning_settings = replace(provisioning_settings, enabled=False)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency error in broken installs.
        raise RuntimeError("uvicorn is required to run llm-router-gateway") from exc
    app = create_app(
        config_path=args.config,
        discovery=not args.no_discovery,
        settings=settings,
        provisioning_settings=provisioning_settings,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RouterGateway", "VIRTUAL_MODELS", "create_app", "main"]
