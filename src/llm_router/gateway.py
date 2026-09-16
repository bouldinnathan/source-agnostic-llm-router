"""Resilient Ollama/OpenAI-compatible HTTP gateway for the model router."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from .bootstrap import BootstrapResult, bootstrap_router
from .aliases import ModelAlias, alias_conflicts, build_aliases
from .discovery import DiscoveryReport, DiscoverySettings, ProbeResult
from .errors import AllModelsFailed, NoEligibleModel, RequestError, RouterError
from .health import probe_endpoints
from .provisioning import OllamaProvisioner, ProvisioningReport, ProvisioningSettings
from .router import LLMRouter
from .schema import QueryRequest, RoutedCompletion, RouterConfig
from .saved_hosts import SavedHostStore, check_saved_host
from .self_test import run_backend_checks
from .status_page import STATUS_CSS, STATUS_JS, render_status_html

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
        self._health_task: asyncio.Task[None] | None = None
        self._health_lock = asyncio.Lock()
        self._provision_task: asyncio.Task[None] | None = None
        self._provisioning: ProvisioningReport | None = None
        self._last_error: str | None = None
        self._last_refresh: float | None = None
        self._started_at = time.monotonic()

    async def start(self) -> None:
        await self.refresh()
        await self.check_health()
        self._health_task = asyncio.create_task(
            self._health_loop(), name="llm-router-endpoint-health"
        )
        self._ensure_provisioning()
        if self.discovery_enabled and self.settings.enabled:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="llm-router-discovery-refresh"
            )

    async def stop(self) -> None:
        for task in (self._refresh_task, self._provision_task, self._health_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None
        self._provision_task = None
        self._health_task = None

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
                for probe in result.discovery.probes:
                    name = _probe_endpoint_name(probe, result.router.config.endpoints)
                    if (
                        name in result.router.config.endpoints
                        and result.router.config.endpoints[name].health_path is None
                    ):
                        result.router.runtime.record_endpoint_probe(name, probe.reachable, probe.error)
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
        ready = self._router is not None and any(
            model.enabled
            and self._router.runtime.is_available(model.id)
            and self._router.runtime.endpoint_available(model.endpoint)
            for model in self._router.config.models
        )
        if self._router is None:
            state = "unavailable"
        elif self._last_error:
            state = "degraded"
        elif not ready:
            state = "degraded"
            availability_error = "No enabled deployment is currently available"
        else:
            state = "ready"
        payload: dict[str, Any] = {
            "status": state,
            "ready": ready,
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
            payload["alias_conflicts"] = list(alias_conflicts(self._router.config))
        if self._discovery is not None:
            payload["discovery"] = self._discovery.to_dict(include_failures=True)
        if self._provisioning is not None:
            payload["provisioning"] = self._provisioning.to_dict()
        return payload

    def dashboard_status(self) -> dict[str, Any]:
        """Read cached fleet state without discovery, inference or secret fields.

        Reachability is explicitly tri-state. An unprobed endpoint is not shown
        as online, even though the routing policy permits trying it.
        """
        status = self.status()
        router = self._router
        endpoints: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        aliases: list[dict[str, Any]] = []
        if router is not None:
            snapshot = router.runtime.snapshot(router.config.models)
            endpoint_states = snapshot["endpoints"]
            for model in router.config.models:
                endpoint = router.config.endpoints[model.endpoint]
                state = router.runtime.state(model.id)
                if not model.enabled:
                    model_state = "disabled"
                elif not router.runtime.endpoint_available(model.endpoint):
                    model_state = "offline"
                elif not router.runtime.is_available(model.id):
                    model_state = "cooldown"
                else:
                    model_state = "available"
                models.append({
                    "name": model.upstream_model,
                    "deployment": model.id,
                    "machine": endpoint.machine_id or endpoint.name,
                    "state": model_state,
                    "active_requests": state.active_requests,
                    "successes": state.successes,
                    "failures": state.failures,
                })
            for endpoint in router.config.endpoints.values():
                health_state = endpoint_states.get(endpoint.name, {})
                reachable = health_state.get("reachable")
                endpoint_models = [model for model in router.config.models if model.endpoint == endpoint.name]
                enabled_models = [model for model in endpoint_models if model.enabled]
                endpoints.append({
                    "name": endpoint.name,
                    "machine": endpoint.machine_id or endpoint.name,
                    "adapter": endpoint.adapter,
                    "address": _dashboard_address(endpoint.base_url),
                    "state": "online" if reachable is True else "offline" if reachable is False else "unchecked",
                    "last_checked": health_state.get("last_checked_at"),
                    "model_count": len(enabled_models),
                    "available_models": sum(
                        router.runtime.endpoint_available(model.endpoint)
                        and router.runtime.is_available(model.id)
                        for model in enabled_models
                    ),
                })
            available = {model["deployment"] for model in models if model["state"] == "available"}
            for name, alias in build_aliases(router.config).items():
                aliases.append({
                    "name": name,
                    "kind": alias.kind,
                    "available": bool(available.intersection(alias.deployment_ids)),
                    "deployments": len(alias.deployment_ids),
                })
        if status["ready"]:
            notice = (
                "Models are available; the last discovery refresh failed, so the last known fleet is retained."
                if status["status"] == "degraded" else
                "The router is running and has eligible models. API reachability does not prove inference will succeed."
            )
        elif models or endpoints:
            notice = "The router is running, but no enabled model is currently available. Check backend servers and their model lists."
        else:
            notice = "The router is running, but no fleet is available yet. Add reachable backend URLs in router.env and ensure a chat model is available."
        return {
            "status": status["status"],
            "ready": status["ready"],
            "version": VERSION,
            "uptime_seconds": round(max(0.0, time.monotonic() - self._started_at), 1),
            "checked_at": _timestamp(),
            "last_discovery": status["last_refresh"],
            "notice": notice,
            "counts": {
                "endpoints": len(endpoints),
                "online": sum(endpoint["state"] == "online" for endpoint in endpoints),
                "models": sum(model["state"] != "disabled" for model in models),
                "available_models": sum(model["state"] == "available" for model in models),
                "aliases": len(aliases),
            },
            "endpoints": sorted(endpoints, key=lambda endpoint: (endpoint["machine"], endpoint["name"])),
            "models": sorted(models, key=lambda model: (model["name"], model["machine"], model["deployment"])),
            "aliases": aliases,
        }

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.refresh_seconds)
            await self.refresh()
            self._ensure_provisioning()

    async def check_health(self) -> None:
        recovered = False
        async with self._health_lock, self._refresh_lock:
            router = self._router
            if router is not None:
                previously_offline = {
                    name for name in router.config.endpoints
                    if not router.runtime.endpoint_available(name)
                }
                results = await probe_endpoints(router.config, router.runtime)
                recovered = any(results.get(name) is True for name in previously_offline)
                # An endpoint-only config can start while every server is asleep.
                # Enroll models promptly once one of those servers becomes usable.
                recovered = recovered or (
                    not router.config.models and any(value is True for value in results.values())
                )
        if recovered and self.discovery_enabled:
            await self.refresh()

    async def _health_loop(self) -> None:
        while True:
            router = self._router
            interval = router.config.policy.health_check_interval_seconds if router else 15.0
            await asyncio.sleep(interval)
            try:
                await self.check_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One health cycle must not terminate future checks or discovery.
                continue

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
        failed_probes = [probe for probe in result.discovery.probes if not probe.reachable]
        if not failed_probes:
            return result
        endpoints = dict(result.router.config.endpoints)
        models = {model.id: model for model in result.router.config.models}
        retained_endpoints: set[str] = set()
        known_endpoints = {**previous.config.endpoints, **endpoints}
        for probe in failed_probes:
            name = _probe_endpoint_name(probe, known_endpoints)
            if name not in previous.config.endpoints:
                continue
            if name not in endpoints:
                endpoint = previous.config.endpoints[name]
                if endpoint.base_url.rstrip("/") != probe.base_url.rstrip("/"):
                    continue
                endpoints[name] = endpoint
            # Preserve the current address for a stable identity after an IP
            # change. Discovery failure must not erase that machine's aliases.
            retained_endpoints.add(name)
        current_pairs = {(model.endpoint, model.upstream_model) for model in models.values()}
        for model in previous.config.models:
            if (
                model.endpoint in retained_endpoints
                and "discovered" in model.tags
                and model.id not in models
                and (model.endpoint, model.upstream_model) not in current_pairs
            ):
                models[model.id] = model
        if not retained_endpoints:
            return result
        merged = RouterConfig(
            endpoints=endpoints,
            models=tuple(models.values()),
            policy=result.router.config.policy,
            source_path=result.router.config.source_path,
        )
        router = LLMRouter(merged, runtime=result.router.runtime)
        return replace(result, router=router)


def _probe_endpoint_name(probe: ProbeResult, endpoints: Mapping[str, Any]) -> str:
    if getattr(probe, "endpoint", None):
        return probe.endpoint
    if probe.source in endpoints:
        return probe.source
    return re.sub(r"[^a-z0-9]+", "-", "auto-" + probe.source.lower()).strip("-")


def _dashboard_address(value: str) -> str:
    """Show only protocol, hostname and port; omit credentials, paths and queries."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host:
            return "Custom endpoint"
        if ":" in host:
            host = f"[{host}]"
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port is not None else "")
    except ValueError:
        return "Custom endpoint"


def create_app(
    *,
    config_path: str | None = None,
    discovery: bool = True,
    settings: DiscoverySettings | None = None,
    provisioning_settings: ProvisioningSettings | None = None,
    provisioner: OllamaProvisioner | None = None,
    gateway: RouterGateway | None = None,
    saved_host_store: SavedHostStore | None = None,
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

    page_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        ),
    }
    self_test_lock = asyncio.Lock()
    self_test_next_allowed = 0.0
    host_store = saved_host_store if saved_host_store is not None else SavedHostStore()
    host_store_lock = asyncio.Lock()
    host_check_lock = asyncio.Lock()
    host_check_next_allowed = 0.0
    host_results: dict[str, dict[str, Any]] = {}

    def hosts_reply(payload: Mapping[str, Any], code: int = 200) -> Response:
        return JSONResponse(payload, status_code=code, headers=page_headers)

    def hosts_authorize(request: Request) -> Response | None:
        # These endpoints grant permission to contact and save new network
        # targets, so deliberately keyless inference gateways cannot use them.
        if not os.environ.get("LLM_ROUTER_GATEWAY_API_KEY", "").strip():
            return hosts_reply({"error": "Set LLM_ROUTER_GATEWAY_API_KEY to manage saved addresses."}, 403)
        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        if request.url.query:
            return hosts_reply({"error": "Saved-address endpoints do not accept query parameters."}, 400)
        if request.method != "GET":
            origin = request.headers.get("origin")
            if (
                request.headers.get("x-llm-router-hosts") != "1"
                or origin is not None and origin != str(request.base_url).rstrip("/")
            ):
                return hosts_reply({"error": "Use the saved-address controls on this router's status page."}, 403)
        return None

    async def hosts_body(request: Request, fields: set[str]) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Send a JSON object with Content-Type application/json.")

        async def read() -> bytes:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    raise ValueError("Saved-address request is too large.")
                body.extend(chunk)
            return bytes(body)

        try:
            payload = json.loads(await asyncio.wait_for(read(), timeout=3.0))
        except (UnicodeError, json.JSONDecodeError, RecursionError, asyncio.TimeoutError) as exc:
            raise ValueError("Send a valid, small JSON object.") from exc
        if not isinstance(payload, dict) or set(payload) - fields:
            raise ValueError("Unexpected saved-address request fields.")
        return payload

    def host_snapshot(entry: Mapping[str, Any]) -> dict[str, Any]:
        cached = host_results.get(entry["id"])
        if cached is not None and cached["address"] == entry["address"]:
            return dict(cached)
        return {**entry, "checked_at": None, "checks": []}

    async def saved_hosts(request: Request) -> Response:
        denied = hosts_authorize(request)
        if denied is not None:
            return denied
        try:
            body = await hosts_body(request, {"address"}) if request.method == "POST" else None
            async with host_store_lock:
                if body is not None:
                    if not isinstance(body.get("address"), str):
                        raise ValueError("Enter a single IP address, hostname, or HTTP(S) URL.")
                    entry = host_store.add(body["address"])
                    return hosts_reply({"host": host_snapshot(entry)}, 201)
                entries = host_store.list()
                return hosts_reply({"hosts": [host_snapshot(entry) for entry in entries], "limit": 16})
        except ValueError as exc:
            return hosts_reply({"error": str(exc)}, 400)
        except (OSError, RuntimeError):
            return hosts_reply({"error": "Saved addresses could not be read or saved. Check the router's storage permissions and saved-address file."}, 503)

    async def remove_saved_host(request: Request) -> Response:
        denied = hosts_authorize(request)
        if denied is not None:
            return denied
        try:
            async with host_store_lock:
                identifier = request.path_params["host_id"]
                if not host_store.remove(identifier):
                    return hosts_reply({"error": "Saved address not found."}, 404)
                host_results.pop(identifier, None)
                return hosts_reply({"removed": True})
        except ValueError:
            return hosts_reply({"error": "Invalid saved-address identifier."}, 400)
        except (OSError, RuntimeError):
            return hosts_reply({"error": "Saved address could not be removed. Check the router's saved-address file."}, 503)

    async def check_saved_hosts(request: Request) -> Response:
        nonlocal host_check_next_allowed
        denied = hosts_authorize(request)
        if denied is not None:
            return denied
        try:
            body = await hosts_body(request, {"id"})
            if "id" in body and (not isinstance(body["id"], str) or not body["id"] or len(body["id"]) > 64):
                raise ValueError("Choose a saved address to check.")
            async with host_store_lock:
                entries = host_store.list()
                if "id" in body:
                    entries = [entry for entry in entries if entry["id"] == body["id"]]
                    if not entries:
                        return hosts_reply({"error": "Saved address not found."}, 404)
            if host_check_lock.locked() or time.monotonic() < host_check_next_allowed:
                response = hosts_reply({"error": "Address checks are running or just finished; retry in a few seconds."}, 429)
                response.headers["Retry-After"] = "3"
                return response
            async with host_check_lock:
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*(check_saved_host(entry) for entry in entries)), timeout=12.0,
                    )
                    async with host_store_lock:
                        current = {entry["id"]: entry for entry in host_store.list()}
                        # Removing an address while a probe is in flight must
                        # not resurrect it in cached status or in the browser.
                        results = [row for row in results if current.get(row["id"], {}).get("address") == row["address"]]
                        host_results.update({row["id"]: row for row in results})
                    return hosts_reply({"hosts": results})
                finally:
                    host_check_next_allowed = time.monotonic() + 3.0
        except ValueError as exc:
            return hosts_reply({"error": str(exc)}, 400)
        except asyncio.TimeoutError:
            return hosts_reply({"error": "Address checks timed out; try checking one saved address."}, 504)
        except (OSError, RuntimeError):
            return hosts_reply({"error": "Saved-address checks could not complete. Check the saved-address file and retry."}, 503)

    async def status_page(request: Request) -> Response:
        return HTMLResponse(
            render_status_html(api_key_required=bool(os.environ.get("LLM_ROUTER_GATEWAY_API_KEY"))),
            headers=page_headers,
        )

    async def status_css(request: Request) -> Response:
        return Response(STATUS_CSS, media_type="text/css", headers=page_headers)

    async def status_js(request: Request) -> Response:
        return Response(STATUS_JS, media_type="text/javascript", headers=page_headers)

    async def status_data(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        response = denied or JSONResponse(service.dashboard_status())
        response.headers.update(page_headers)
        return response

    async def status_self_test(request: Request) -> Response:
        """Explicit, bounded API checks; never discover, provision or infer."""
        nonlocal self_test_next_allowed

        def reply(payload: Mapping[str, Any], code: int = 200) -> Response:
            return JSONResponse(payload, status_code=code, headers=page_headers)

        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        # This header cannot be set by cross-origin HTML forms. No permissive
        # CORS handler exists here, including when gateway auth is disabled.
        origin = request.headers.get("origin")
        if (
            request.headers.get("x-llm-router-self-test") != "1"
            or origin is not None and origin != str(request.base_url).rstrip("/")
        ):
            return reply({"error": "Use the self-test button on this router's status page."}, 403)
        if request.url.query:
            return reply({"error": "Self-test only checks configured targets; parameters are not accepted."}, 400)
        if self_test_lock.locked() or time.monotonic() < self_test_next_allowed:
            response = reply({"error": "A self-test is running or just finished; retry in a few seconds."}, 429)
            response.headers["Retry-After"] = "5"
            return response
        async with self_test_lock:
            try:
                checks = await _gateway_self_test_checks(app, service, request)
                cached = service._router
                checks.extend(await run_backend_checks(cached.config if cached is not None else None))
                statuses = {check["status"] for check in checks}
                result = "fail" if "fail" in statuses else "partial" if "skip" in statuses else "pass"
                return reply({
                    "status": result,
                    "checked_at": _timestamp(),
                    "checks": checks,
                    "notice": (
                        "API and connectivity checks only. No prompts, inference, model loading, "
                        "downloads, discovery or routing-health changes are performed. "
                        "Backend requests originate from the router, not your browser."
                    ),
                })
            except Exception:
                return reply({"error": "Self-test could not be completed; try again."}, 500)
            finally:
                self_test_next_allowed = time.monotonic() + 5.0

    async def root(request: Request) -> Response:
        if "text/html" in request.headers.get("accept", "").lower():
            response = await status_page(request)
            response.headers["Vary"] = "Accept"
            return response
        return PlainTextResponse("LLM Router is running", headers={"Vary": "Accept"})

    async def health(request: Request) -> Response:
        status = service.status()
        public_status = {"status": status["status"], "version": status["version"]}
        return JSONResponse(
            public_status,
            status_code=200 if status.get("ready", status["status"] == "ready") else 503,
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
            router = await service.router()
            _, alias = _resolve_model(router, model)
        except (ValueError, GatewayUnavailable) as exc:
            return _ollama_error(str(exc), 503 if isinstance(exc, GatewayUnavailable) else 404)
        return JSONResponse(
            {
                "license": "",
                "modelfile": "# Virtual model routed by source-agnostic-llm-router",
                "parameters": "",
                "template": "",
                "details": {"family": "llm-router", "families": ["llm-router"]},
                "capabilities": _model_capabilities(router, alias),
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
            if model in VIRTUAL_MODELS:
                alias = None
            else:
                router = await service.router()
                _, alias = _resolve_model(router, model)
        except (ValueError, GatewayUnavailable) as exc:
            return _ollama_error(str(exc), 503 if isinstance(exc, GatewayUnavailable) else 404)
        report: ProvisioningReport | None = None
        if alias is None and hasattr(service, "provision"):
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
            router = await service.router()
            strategy, alias = _resolve_model(router, model)
            preferred_tags = VIRTUAL_PREFERRED_TAGS.get(model, ())
            messages = body.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages must be an array")
            if not messages:
                return JSONResponse(_ollama_empty(model, "load"))
            query = _query_request(
                body, messages, strategy, ollama=True, preferred_tags=preferred_tags
            )
            if alias is not None:
                query = alias.apply(query)
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
            router = await service.router()
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
                    for model in (*VIRTUAL_MODELS, *build_aliases(router.config))
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
            router = await service.router()
            strategy, alias = _resolve_model(router, model)
            preferred_tags = VIRTUAL_PREFERRED_TAGS.get(model, ())
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a non-empty array")
            query = _query_request(
                body, messages, strategy, ollama=False, preferred_tags=preferred_tags
            )
            if alias is not None:
                query = alias.apply(query)
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
        Route("/status", status_page, methods=["GET"]),
        Route("/status/data", status_data, methods=["GET"]),
        Route("/status/self-test", status_self_test, methods=["POST"]),
        Route("/status/hosts", saved_hosts, methods=["GET", "POST"]),
        Route("/status/hosts/check", check_saved_hosts, methods=["POST"]),
        Route("/status/hosts/{host_id}", remove_saved_host, methods=["DELETE"]),
        Route("/status/assets/style.css", status_css, methods=["GET"]),
        Route("/status/assets/app.js", status_js, methods=["GET"]),
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


async def _gateway_self_test_checks(
    app: Starlette, service: RouterGateway, request: Request,
) -> list[dict[str, Any]]:
    """Exercise actual read-only routes in-process, never an arbitrary host/port."""
    checks: list[dict[str, Any]] = []
    headers = {"Accept": "application/json"}
    if request.headers.get("authorization"):
        headers["Authorization"] = request.headers["authorization"]
    paths = [
        ("Gateway HTTP", "/", "text"),
        ("Public health API", "/healthz", "health"),
        ("Readiness API", "/readyz", "health"),
        ("Dashboard API", "/status/data", "dashboard"),
        ("Ollama-compatible version API", "/api/version", "version"),
        ("OpenAI-compatible catalog", "/v1/models", "data"),
        ("Ollama-compatible catalog", "/api/tags", "models"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://self-test.invalid", follow_redirects=False, trust_env=False,
    ) as client:
        for name, path, kind in paths:
            row: dict[str, Any] = {
                "name": name, "target": path, "status": "fail", "detail": "",
                "elapsed_ms": 0.0, "http_status": None,
            }
            if kind in {"data", "models"} and service._router is None:
                row.update(status="skip", detail="No cached fleet; skipped to avoid triggering discovery.")
                checks.append(row)
                continue
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(client.get(path, headers=headers), timeout=2.0)
                row["http_status"] = response.status_code
                if kind == "text":
                    valid = response.status_code == 200 and response.text == "LLM Router is running"
                else:
                    payload = response.json()
                    valid = isinstance(payload, dict)
                    if kind == "health":
                        valid = valid and response.status_code in {200, 503} and payload.get("status") in {
                            "ready", "degraded", "unavailable",
                        }
                    elif kind == "dashboard":
                        valid = valid and response.status_code == 200 and isinstance(payload.get("ready"), bool)
                    elif kind == "version":
                        valid = valid and response.status_code == 200 and isinstance(payload.get("version"), str)
                    else:
                        valid = valid and response.status_code == 200 and isinstance(payload.get(kind), list)
                if valid and kind == "health" and response.status_code == 503:
                    row.update(status="skip", detail="API responds correctly; cached fleet is not ready (HTTP 503). No model was tested.")
                elif valid:
                    row.update(status="pass", detail="Expected HTTP response received; metadata only.")
                else:
                    row["detail"] = "Unexpected HTTP status or response format."
            except Exception:
                # Do not echo response bodies, URLs or exception text.
                row["detail"] = "API check failed or timed out."
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            checks.append(row)

        row = {
            "name": "Gateway authentication", "target": "/status/data",
            "status": "skip", "detail": "Gateway API-key authentication is disabled.",
            "elapsed_ms": 0.0, "http_status": None,
        }
        if os.environ.get("LLM_ROUTER_GATEWAY_API_KEY"):
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(client.get("/status/data"), timeout=2.0)
                row.update(
                    status="pass" if response.status_code == 401 else "fail",
                    http_status=response.status_code,
                    detail="Request without a key rejected." if response.status_code == 401 else "Unauthenticated request was not rejected as expected.",
                )
            except Exception:
                row.update(status="fail", detail="Authentication check failed or timed out.")
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        checks.append(row)
    return checks


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
    aliases = build_aliases(router.config)
    def largest_context(name: str) -> int:
        members = aliases[name].models if name in aliases else router.config.models
        return max((model.context_window for model in members if model.enabled), default=8192)
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
                "context_length": largest_context(name),
            },
        }
        for name in (*VIRTUAL_MODELS, *aliases)
    ]


def _resolve_model(router: LLMRouter, name: str) -> tuple[str, ModelAlias | None]:
    if name in VIRTUAL_MODELS:
        return VIRTUAL_MODELS[name], None
    alias = build_aliases(router.config).get(name)
    if alias is None:
        raise ValueError(f"unknown virtual model '{name}'; query the model list for available aliases")
    return alias.strategy, alias


def _model_capabilities(router: LLMRouter, alias: ModelAlias | None) -> list[str]:
    members = alias.models if alias else router.config.models
    capabilities = ["completion"]
    for public, internal in (("tools", "tool_use"), ("vision", "vision")):
        if any(
            model.enabled
            and model.capabilities.get(internal, 0) >= router.config.policy.capability_threshold
            for model in members
        ):
            capabilities.append(public)
    return capabilities


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
    if supplied and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
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
