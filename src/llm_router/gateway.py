"""Resilient Ollama/OpenAI-compatible HTTP gateway for the model router."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import unquote_to_bytes, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .bootstrap import BootstrapResult, bootstrap_router, load_optional_config
from .aliases import ModelAlias, alias_conflicts, build_aliases
from .discovery import DiscoveryReport, DiscoverySettings, ProbeResult, merge_router_configs
from .errors import AllModelsFailed, NoEligibleModel, RequestError, RouterError
from .health import probe_endpoints
from .inference_jobs import InferenceJobError, InferenceJobs, Runner
from .inference_test import run_inference_checks
from .metrics import MetricsStore
from .routing_settings import FIELDS as ROUTING_SETTING_FIELDS, RoutingSettings, RoutingSettingsStore, validate_settings
from .provisioning import OllamaProvisioner, ProvisioningReport, ProvisioningSettings
from .public_status import public_summary
from .router import LLMRouter
from .schema import QueryRequest, RoutedCompletion, RouterConfig, whole_number
from .saved_hosts import SavedHostStore, check_saved_host
from .saved_discovery import is_saved_endpoint, merge_saved_discovery, saved_hosts_report
from .self_test import run_backend_checks
from .status_page import STATUS_CSS, STATUS_JS, render_status_html
from .update_control import UpdateController, UpdateRequestError

VERSION = "0.4.0"
SAVED_HOST_REFRESH_SECONDS = 30.0
SAVED_HOST_CHECK_COOLDOWN_SECONDS = 3.0
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


class RedactStatusQueryKey:
    """Keep optional page URL keys out of the gateway's normal access logs.

    The browser reads its original URL, erases the key, and sends Bearer headers.
    The API itself never authenticates query parameters. Mutate the ASGI scope
    in place so Uvicorn's access logger also sees the redacted query. This cannot
    protect upstream proxy logs or a browser's previously recorded URL; a URL
    fragment or the password field remains preferable to a query parameter.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("query_string"):
            parts = scope["query_string"].split(b"&")
            changed = False
            for index, part in enumerate(parts):
                key = part.partition(b"=")[0]
                if unquote_to_bytes(key) == b"api_key":
                    parts[index] = b"api_key=REDACTED"
                    changed = True
            if changed:
                scope["query_string"] = b"&".join(parts)
        await self.app(scope, receive, send)


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
        metrics_store: MetricsStore | None = None,
        routing_settings_store: RoutingSettingsStore | None = None,
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
        self._base_discovery = DiscoveryReport(RouterConfig(endpoints={}, models=()), ())
        self._saved_discovery = DiscoveryReport(RouterConfig(endpoints={}, models=()), ())
        self._saved_results: Mapping[str, dict[str, Any]] = {}
        self._configured: RouterConfig | None = None
        self._configuration_loaded = False
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._health_lock = asyncio.Lock()
        self._provision_task: asyncio.Task[None] | None = None
        self._provisioning: ProvisioningReport | None = None
        self._last_error: str | None = None
        self._last_refresh: float | None = None
        self._started_at = time.monotonic()
        self.metrics = metrics_store if metrics_store is not None else MetricsStore()
        self.routing_settings_store = routing_settings_store if routing_settings_store is not None else RoutingSettingsStore()
        self._recent_failures: deque[dict[str, Any]] = deque(maxlen=25)
        self.routing_settings = RoutingSettings()
        self._routing_settings_error: str | None = None
        self.reload_routing_settings()

    def reload_routing_settings(self) -> RoutingSettings:
        """Read saved dashboard settings; an unreadable file keeps defaults and is reported."""
        try:
            self.routing_settings = self.routing_settings_store.load()
            self._routing_settings_error = None
        except RuntimeError:
            self.routing_settings = RoutingSettings()
            self._routing_settings_error = "Saved routing settings could not be read; defaults are in effect until the file is repaired."
        router = self._router
        if router is not None:
            router.settings = self.routing_settings
        return self.routing_settings

    def update_routing_settings(self, changes: Mapping[str, Any]) -> RoutingSettings:
        """Validate, persist, then apply to the live router; nothing applies unless saved."""
        settings = validate_settings(changes, base=self.routing_settings)
        saved = self.routing_settings_store.save(settings)
        self._routing_settings_error = None
        self.routing_settings = saved
        router = self._router
        if router is not None:
            router.settings = saved
        return saved

    def record_request_failure(self, *, api: str, model: str, status: int, kind: str, detail: str) -> None:
        """Remember why a client request failed: name, status and diagnosis, never prompt text."""
        self._recent_failures.appendleft({
            "at": _timestamp(), "api": api, "model": model[:128], "status": int(status),
            "kind": kind, "detail": detail[:512],
        })

    def recent_failures(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._recent_failures]

    def routing_status(self) -> dict[str, Any]:
        router = self._router
        races = [] if router is None else [
            {**record, "participants": {name: dict(item) for name, item in record["participants"].items()}}
            for record in router.runtime.last_races
        ]
        return {
            "settings": self.routing_settings.to_dict(),
            "storage": {"available": self._routing_settings_error is None, "error": self._routing_settings_error},
            "races": races,
        }

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
                    saved_discovery=self._saved_discovery,
                )
                result = self._retain_failed_sources(result, previous)
                for probe in result.discovery.probes:
                    name = _probe_endpoint_name(probe, result.router.config.endpoints)
                    if (
                        name in result.router.config.endpoints
                        and result.router.config.endpoints[name].health_path is None
                    ):
                        endpoint = result.router.config.endpoints[name]
                        if not is_saved_endpoint(endpoint):
                            result.router.runtime.record_endpoint_probe(name, probe.reachable, probe.error)
                        elif previous is None or result.router.runtime is not previous.runtime:
                            self._record_saved_probe(result.router, probe, self._saved_results)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = _safe_exception(exc)
                self._last_refresh = time.time()
                return False
            result.router.metrics = self.metrics
            result.router.settings = self.routing_settings
            self._router = result.router
            self._discovery = result.discovery
            self._base_discovery = result.base_discovery or result.discovery
            self._configured = result.configured
            self._configuration_loaded = True
            self._last_error = None
            self._last_refresh = time.time()
            return True

    async def apply_saved_hosts(self, results: Mapping[str, dict[str, Any]]) -> None:
        """Atomically publish metadata-only catalogs without probing other sources."""
        async with self._refresh_lock:
            saved = saved_hosts_report(results, self._saved_discovery)
            if not self._configuration_loaded:
                self._configured = load_optional_config(self.config_path)
                self._configuration_loaded = True
            report = merge_saved_discovery(self._base_discovery, saved, self._configured)
            merged = merge_router_configs(self._configured, report.config)
            previous = self._router
            runtime = previous.runtime if previous and previous.config.policy == merged.policy else None
            router = LLMRouter(merged, runtime=runtime, metrics=self.metrics, settings=self.routing_settings)
            result = self._retain_failed_sources(BootstrapResult(router, report, self._configured), previous)
            router = result.router
            router.metrics = self.metrics
            router.settings = self.routing_settings
            # Cached ordinary discovery must not reset newer health failures.
            for probe in report.probes:
                name = _probe_endpoint_name(probe, router.config.endpoints)
                endpoint = router.config.endpoints.get(name)
                if endpoint is not None and is_saved_endpoint(endpoint):
                    self._record_saved_probe(router, probe, results)
            self._saved_discovery = saved
            self._saved_results = dict(results)
            self._router = router
            self._discovery = report
            self._last_error = None

    @staticmethod
    def _record_saved_probe(router: LLMRouter, probe: ProbeResult, results: Mapping[str, dict[str, Any]]) -> None:
        """Reusing a cached catalog must not pretend another network check ran."""
        endpoint = router.config.endpoints[probe.endpoint]
        timestamps = []
        for identifier in endpoint.options.get("saved_host_ids", ()):
            checked_at = results.get(identifier, {}).get("checked_at")
            if isinstance(checked_at, str):
                try:
                    value = datetime.fromisoformat(checked_at)
                    if value.tzinfo is not None:
                        timestamps.append(value.timestamp())
                except (ValueError, OverflowError):
                    pass
        router.runtime.record_endpoint_probe(
            endpoint.name, probe.reachable, probe.error,
            checked_at=max(timestamps) if timestamps else None,
        )

    def saved_host_routing(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Describe actual enrollment, not merely a successful version response."""
        router = self._router
        checks = entry.get("checks", [])
        addresses = {_dashboard_address(check.get("base_url", "")) for check in checks}
        owned = {
            name for name, endpoint in (router.config.endpoints.items() if router else ())
            if entry["id"] in endpoint.options.get("saved_host_ids", ())
        }
        managed = {
            name for name, endpoint in (router.config.endpoints.items() if router else ())
            if not is_saved_endpoint(endpoint) and _dashboard_address(endpoint.base_url) in addresses
        }
        models = [model for model in router.config.models if model.enabled and model.endpoint in owned | managed] if router else []
        active = [model for model in models if router.runtime.endpoint_available(model.endpoint)]
        if managed:
            state, detail = "managed", "This address is managed by existing configuration/discovery; saved checks do not override it."
        elif active:
            state, detail = "active", "Chat models enrolled in routing; available through this router's model APIs."
        elif models:
            state, detail = "offline", "Last known chat models retained for recovery; this server is currently unavailable."
        elif any(check.get("catalog_status") == "ok" for check in checks):
            state, detail = "empty", "No supported chat models found to enroll. Non-chat models are listed but not routed."
        elif entry.get("checked_at"):
            state, detail = "offline", "No usable model catalog yet; saved address will be checked again automatically."
        else:
            state, detail = "pending", "Saved; automatic metadata check and routing enrollment pending."
        return {"status": state, "model_count": len(models), "detail": detail}

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
            advertised = set(_advertised_aliases(router))
            for name, alias in build_aliases(router.config).items():
                aliases.append({
                    "name": name,
                    "kind": alias.kind,
                    "available": bool(available.intersection(alias.deployment_ids)),
                    "deployments": len(alias.deployment_ids),
                    "advertised": name in advertised,
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
            "alias_conflicts": [] if router is None else list(alias_conflicts(router.config)),
            "routing": self.routing_status(),
            "recent_failures": self.recent_failures(),
        }

    def metrics_status(self) -> dict[str, Any]:
        """Read persisted observations only; never discover or invoke a model."""
        try:
            snapshot = self.metrics.snapshot()
        except Exception:
            snapshot = {
                "available": False, "error": "Performance history could not be read.",
                "updated_at": None, "deployments": [],
            }
        current = set()
        router = self._router
        if router is not None:
            for model in router.config.models:
                endpoint = router.config.endpoints[model.endpoint]
                current.add((endpoint.machine_id or endpoint.name, endpoint.name, model.upstream_model))
        return {
            **snapshot,
            "schema_version": 2,
            "collection": "passive",
            "notice": (
                "Real routed requests only; no benchmarks. Input/output rates and load/setup time "
                "require backend-reported timings. Missing measurements are not zero. "
                "Slow reported loads (at least 1000 ms) suggest, but do not prove, a cold/storage load. "
                "EWMA uses alpha 0.2; each metric retains its own last-observed timestamp."
            ),
            "deployments": [
                {**row, "current": (row["machine"], row["endpoint"], row["model"]) in current}
                for row in snapshot["deployments"]
            ],
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
                # Saved targets have their own bounded catalog checks, including
                # proxy-recursion rejection; do not reactivate them with a less
                # strict generic health response between those checks.
                ordinary = {name: endpoint for name, endpoint in router.config.endpoints.items() if not is_saved_endpoint(endpoint)}
                health_config = replace(router.config, endpoints=ordinary, models=tuple(model for model in router.config.models if model.endpoint in ordinary))
                results = await probe_endpoints(health_config, router.runtime)
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
            if is_saved_endpoint(previous.config.endpoints[name]):
                # Saved-source retention and revocation are handled by the
                # saved catalog registry, never by ordinary discovery fallback.
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
        router = LLMRouter(merged, runtime=result.router.runtime, metrics=result.router.metrics, settings=result.router.settings)
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
    update_controller: UpdateController | None = None,
    inference_test_runner: Runner | None = None,
    routing_settings_store: RoutingSettingsStore | None = None,
) -> Starlette:
    service = gateway or RouterGateway(
        config_path=config_path,
        discovery=discovery,
        routing_settings_store=routing_settings_store,
        settings=settings,
        provisioning_settings=provisioning_settings,
        provisioner=provisioner,
    )
    updates = update_controller if update_controller is not None else UpdateController()
    inference_jobs = InferenceJobs(inference_test_runner if inference_test_runner is not None else run_inference_checks)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        saved_task = None
        try:
            await service.start()
            # Existing saved addresses from older installs are enrolled at startup.
            await automatic_host_check()
            saved_task = asyncio.create_task(saved_host_loop(), name="llm-router-saved-hosts")
            yield
        finally:
            await inference_jobs.close()
            if saved_task is not None:
                saved_task.cancel()
                try:
                    await saved_task
                except asyncio.CancelledError:
                    pass
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
    host_generations: dict[str, int] = {}
    host_scan_requested = asyncio.Event()
    host_enrollment_error: str | None = None
    host_publication_pending = False

    async def publish_hosts() -> None:
        nonlocal host_enrollment_error, host_publication_pending
        host_publication_pending = True
        try:
            await service.apply_saved_hosts(host_results)
        except Exception:
            host_enrollment_error = "Saved, but routing enrollment failed. Check router configuration and retry."
            raise RuntimeError(host_enrollment_error) from None
        host_enrollment_error = None
        host_publication_pending = False

    async def scan_hosts(entries: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
        """Caller serializes scans; store lock serializes route publication/removal."""
        nonlocal host_check_next_allowed
        generations = {entry["id"]: host_generations.get(entry["id"], 0) for entry in entries}
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(check_saved_host(entry) for entry in entries)), timeout=12.0,
            )
            async with host_store_lock:
                current = {entry["id"]: entry for entry in host_store.list()}
                # Also reject a response from before DELETE + re-add of the same
                # address (its stable ID alone cannot distinguish that race).
                results = [row for row in results if current.get(row["id"], {}).get("address") == row["address"] and generations[row["id"]] == host_generations.get(row["id"], 0)]
                for identifier in list(host_results):
                    if identifier not in current:
                        host_results.pop(identifier)
                host_results.update({row["id"]: row for row in results})
                await publish_hosts()
                return [host_snapshot(row) for row in results]
        finally:
            host_check_next_allowed = time.monotonic() + SAVED_HOST_CHECK_COOLDOWN_SECONDS

    async def automatic_host_check() -> None:
        try:
            async with host_check_lock:
                delay = host_check_next_allowed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                async with host_store_lock:
                    entries = host_store.list()
                if entries or host_results or host_publication_pending:
                    await scan_hosts(entries)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Bad storage/unreachable targets must not stop the gateway or
            # terminate all future automatic retries.
            return

    async def saved_host_loop() -> None:
        while True:
            try:
                await asyncio.wait_for(host_scan_requested.wait(), timeout=SAVED_HOST_REFRESH_SECONDS)
            except asyncio.TimeoutError:
                pass
            host_scan_requested.clear()
            await automatic_host_check()

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

    async def hosts_body(request: Request, fields: set[str], *, what: str = "Saved-address") -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Send a JSON object with Content-Type application/json.")

        async def read() -> bytes:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    raise ValueError(f"{what} request is too large.")
                body.extend(chunk)
            return bytes(body)

        try:
            payload = json.loads(await asyncio.wait_for(read(), timeout=3.0))
        except (UnicodeError, json.JSONDecodeError, RecursionError, asyncio.TimeoutError) as exc:
            raise ValueError("Send a valid, small JSON object.") from exc
        if not isinstance(payload, dict) or set(payload) - fields:
            raise ValueError(f"Unexpected {what.lower()} request fields.")
        return payload

    def settings_reply(payload: Mapping[str, Any], code: int = 200) -> Response:
        return JSONResponse(payload, status_code=code, headers=page_headers)

    def settings_authorize(request: Request) -> Response | None:
        # Routing switches change what every client sees, so they need the
        # configured key even on gateways that otherwise allow keyless use.
        if not os.environ.get("LLM_ROUTER_GATEWAY_API_KEY", "").strip():
            return settings_reply({"error": "Set LLM_ROUTER_GATEWAY_API_KEY to change routing settings."}, 403)
        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        if request.url.query:
            return settings_reply({"error": "Routing settings endpoints do not accept query parameters."}, 400)
        if request.method != "GET":
            origin = request.headers.get("origin")
            if (
                request.headers.get("x-llm-router-settings") != "1"
                or origin is not None and origin != str(request.base_url).rstrip("/")
            ):
                return settings_reply({"error": "Use the routing settings controls on this router's status page."}, 403)
        return None

    async def routing_settings(request: Request) -> Response:
        denied = settings_authorize(request)
        if denied is not None:
            return denied
        if request.method == "POST":
            try:
                body = await hosts_body(request, set(ROUTING_SETTING_FIELDS), what="Routing settings")
                service.update_routing_settings(body)
            except ValueError as exc:
                return settings_reply({"error": str(exc)}, 400)
            except RuntimeError:
                return settings_reply({"error": "Routing settings could not be saved. Check the router's storage permissions and routing-settings file; nothing was changed."}, 503)
        return settings_reply(service.routing_status())

    def host_snapshot(entry: Mapping[str, Any]) -> dict[str, Any]:
        cached = host_results.get(entry["id"])
        if cached is not None and cached["address"] == entry["address"]:
            row = dict(cached)
        else:
            row = {**entry, "checked_at": None, "checks": []}
        row["routing"] = (
            {"status": "error", "model_count": 0, "detail": host_enrollment_error}
            if host_enrollment_error else service.saved_host_routing(row)
        )
        return row

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
                    previous_ids = {row["id"] for row in host_store.list()}
                    entry = host_store.add(body["address"])
                    if entry["id"] not in previous_ids:
                        host_generations[entry["id"]] = host_generations.get(entry["id"], 0) + 1
                else:
                    entries = host_store.list()
                    return hosts_reply({"hosts": [host_snapshot(entry) for entry in entries], "limit": 16})
            if host_check_lock.locked() or time.monotonic() < host_check_next_allowed:
                host_scan_requested.set()
            else:
                async with host_check_lock:
                    try:
                        await scan_hosts([entry])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Persistence succeeded; report pending/error and retry
                        # rather than suggesting the address was not saved.
                        host_scan_requested.set()
            return hosts_reply({"host": host_snapshot(entry)}, 201)
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
                host_generations[identifier] = host_generations.get(identifier, 0) + 1
                await publish_hosts()
                return hosts_reply({"removed": True})
        except ValueError:
            return hosts_reply({"error": "Invalid saved-address identifier."}, 400)
        except (OSError, RuntimeError):
            return hosts_reply({"error": "Saved address could not be removed. Check the router's saved-address file."}, 503)

    async def check_saved_hosts(request: Request) -> Response:
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
                results = await scan_hosts(entries)
                return hosts_reply({"hosts": results})
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

    async def status_update(request: Request) -> Response:
        """One authenticated action may install code; passive reads never do."""
        def reply(payload: Mapping[str, Any], code: int = 200) -> Response:
            return JSONResponse(payload, status_code=code, headers=page_headers)

        if not os.environ.get("LLM_ROUTER_GATEWAY_API_KEY", "").strip():
            return reply({"error": "Set LLM_ROUTER_GATEWAY_API_KEY to manage updates."}, 403)
        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        if request.url.query:
            return reply({"error": "Update endpoints do not accept query parameters."}, 400)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if request.headers.get("x-llm-router-update") != "1" or origin is not None and origin != str(request.base_url).rstrip("/"):
                return reply({"error": "Use the update button on this router's status page."}, 403)

            async def empty_body() -> bool:
                async for chunk in request.stream():
                    if chunk:
                        return False
                return True

            try:
                if not await asyncio.wait_for(empty_body(), timeout=3.0):
                    return reply({"error": "Update requests must have an empty body; custom update targets are not accepted."}, 400)
            except (asyncio.TimeoutError, RuntimeError):
                return reply({"error": "Send an empty update request."}, 400)
        try:
            operation = updates.start if request.method == "POST" else updates.status
            payload = await asyncio.to_thread(operation)
            return reply(payload, 202 if request.method == "POST" else 200)
        except UpdateRequestError as exc:
            return reply({"error": str(exc), "message": str(exc)}, exc.status_code)
        except Exception:
            message = "Update status could not be confirmed. Check the updater service before retrying."
            return reply({"error": message, "message": message}, 503)

    async def status_css(request: Request) -> Response:
        return Response(STATUS_CSS, media_type="text/css", headers=page_headers)

    async def status_js(request: Request) -> Response:
        return Response(STATUS_JS, media_type="text/javascript", headers=page_headers)

    async def status_data(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        if denied is not None:
            response = denied
        else:
            response = JSONResponse({
                **service.dashboard_status(), "summary": public_summary(service, host_results),
                "performance": await asyncio.to_thread(service.metrics_status),
            })
        response.headers.update(page_headers)
        return response

    async def performance_metrics(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        payload = await asyncio.to_thread(service.metrics_status)
        return JSONResponse(payload, status_code=200 if payload["available"] else 503, headers=page_headers)

    async def status_inference_test(request: Request) -> Response:
        """Opt-in model use is separate from metadata-only connectivity tests."""
        def reply(payload: Mapping[str, Any], code: int = 200) -> Response:
            return JSONResponse(payload, status_code=code, headers=page_headers)

        if not os.environ.get("LLM_ROUTER_GATEWAY_API_KEY", "").strip():
            return reply({"error": "Set LLM_ROUTER_GATEWAY_API_KEY to run inference tests."}, 403)
        denied = _authorize(request, openai=True)
        if denied is not None:
            denied.headers.update(page_headers)
            return denied
        if request.url.query:
            return reply({"error": "Inference tests do not accept query parameters."}, 400)
        if request.method in {"GET", "HEAD"}:
            return reply(inference_jobs.status())
        origin = request.headers.get("origin")
        if request.headers.get("x-llm-router-inference-test") != "1" or origin is not None and origin != str(request.base_url).rstrip("/"):
            return reply({"error": "Use the inference-test button on this router's status page."}, 403)

        async def empty_body() -> bool:
            async for chunk in request.stream():
                if chunk:
                    return False
            return True

        try:
            if not await asyncio.wait_for(empty_body(), timeout=3.0):
                return reply({"error": "Inference-test requests must have an empty body; custom targets or prompts are not accepted."}, 400)
        except (asyncio.TimeoutError, RuntimeError):
            return reply({"error": "Send an empty inference-test request."}, 400)
        cached = service._router
        try:
            return reply(inference_jobs.start(cached.config if cached is not None else None), 202)
        except InferenceJobError as exc:
            response = reply({"error": str(exc)}, exc.status_code)
            if exc.status_code == 429:
                response.headers["Retry-After"] = "30"
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
        public_status = {
            "status": status["status"], "version": status["version"],
            "summary": public_summary(service, host_results),
        }
        return JSONResponse(
            public_status,
            status_code=200 if status.get("ready", status["status"] == "ready") else 503,
            headers=page_headers,
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
        model = "unknown"
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
            _record_failure(service, "ollama", model, exc, 400)
            return _ollama_error(str(exc), 400)
        except (GatewayUnavailable, AllModelsFailed, NoEligibleModel) as exc:
            _record_failure(service, "ollama", model, exc, 503)
            return _ollama_error(str(exc), 503)
        except RouterError as exc:
            _record_failure(service, "ollama", model, exc, 502)
            return _ollama_error(str(exc), 502)
        except Exception as exc:
            _record_failure(service, "ollama", model, exc, 500)
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
                    for model in (*VIRTUAL_MODELS, *_advertised_aliases(router))
                ],
            }
        )

    async def openai_chat(request: Request) -> Response:
        denied = _authorize(request, openai=True)
        if denied:
            return denied
        model = "unknown"
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
            _record_failure(service, "openai", model, exc, 400)
            return _openai_error(str(exc), 400, "invalid_request_error")
        except (GatewayUnavailable, AllModelsFailed, NoEligibleModel) as exc:
            _record_failure(service, "openai", model, exc, 503)
            return _openai_error(str(exc), 503, "router_unavailable")
        except RouterError as exc:
            _record_failure(service, "openai", model, exc, 502)
            return _openai_error(str(exc), 502, "upstream_error")
        except Exception as exc:
            _record_failure(service, "openai", model, exc, 500)
            return _openai_error(_safe_exception(exc), 500, "internal_error")

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/status", status_page, methods=["GET"]),
        Route("/status/data", status_data, methods=["GET"]),
        Route("/status/update", status_update, methods=["GET", "POST"]),
        Route("/status/self-test", status_self_test, methods=["POST"]),
        Route("/status/inference-test", status_inference_test, methods=["GET", "POST"]),
        Route("/status/settings", routing_settings, methods=["GET", "POST"]),
        Route("/status/hosts", saved_hosts, methods=["GET", "POST"]),
        Route("/status/hosts/check", check_saved_hosts, methods=["POST"]),
        Route("/status/hosts/{host_id}", remove_saved_host, methods=["DELETE"]),
        Route("/status/assets/style.css", status_css, methods=["GET"]),
        Route("/status/assets/app.js", status_js, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/readyz", health, methods=["GET"]),
        Route("/router/status", router_status, methods=["GET"]),
        Route("/router/metrics", performance_metrics, methods=["GET"]),
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
    app.add_middleware(RedactStatusQueryKey)
    app.state.router_gateway = service
    app.state.inference_jobs = inference_jobs
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


def _usable_whole_number(value: Any, *, default: int | None) -> int | None:
    """A positive whole number in any spelling, or ``default`` when unusable."""
    if value is None:
        return default
    try:
        number = whole_number("value", value)
    except RequestError:
        return default
    return number if number is not None and number > 0 else default


_KEEP_ALIVE = re.compile(r"^-?\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)?$")


def _usable_keep_alive(value: Any) -> str | int | None:
    """Ollama's keep_alive as a client sent it: a duration string or seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value if -1 <= value <= 30 * 86400 else None
    if isinstance(value, str):
        text = value.strip()
        return text if 0 < len(text) <= 32 and _KEEP_ALIVE.match(text) else None
    return None


def _usable_temperature(value: Any) -> float | None:
    """A temperature from 0 to 2 in any spelling, or ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 2 else None


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
    # Optional tuning values are corrected, never fatal: a client such as Home
    # Assistant cannot always control how it stores them, and a conversation is
    # worth more than a knob. Unusable values fall back to the defaults below.
    requested_limit = options.get("num_predict") if ollama else body.get("max_completion_tokens", body.get("max_tokens"))
    max_tokens = _usable_whole_number(requested_limit, default=2048)
    # Only a limit the client actually set is forwarded upstream; the default
    # exists for context budgeting, not to cap a backend whose own default is
    # unlimited. A thinking model can spend 2048 tokens reasoning and then have
    # nothing left to answer with.
    max_tokens_specified = _usable_whole_number(requested_limit, default=None) is not None
    temperature = _usable_temperature(options.get("temperature") if ollama else body.get("temperature"))
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
    think = body.get("think") if ollama and isinstance(body.get("think"), bool) else None
    keep_alive = _usable_keep_alive(body.get("keep_alive")) if ollama else None
    if think is True:
        required.append("reasoning")
    if _messages_have_images(messages):
        required.append("vision")
    min_context_window = _usable_whole_number(options.get("num_ctx"), default=None) if ollama else None
    return QueryRequest(
        messages=tuple(dict(message) for message in messages),
        required_capabilities=tuple(required),
        strategy=strategy,
        min_context_window=min_context_window,
        max_tokens=max_tokens,
        max_tokens_specified=max_tokens_specified,
        keep_alive=keep_alive,
        think=think,
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


def _advertised_aliases(router: LLMRouter) -> dict[str, ModelAlias]:
    """Names shown to clients; every generated alias still resolves when requested."""
    aliases = build_aliases(router.config)
    if router.settings.advertise_machine_aliases:
        return aliases
    return {name: alias for name, alias in aliases.items() if alias.kind == "ha"}


def _ollama_models(router: LLMRouter) -> list[dict[str, Any]]:
    now = _timestamp()
    aliases = _advertised_aliases(router)
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


_SCOPE_REASONS = {"outside selected model alias", "explicitly excluded deployment", "explicitly excluded endpoint"}


def _failure_summary(exc: Exception) -> tuple[str, str]:
    """Classify a failed client request and describe it without client content."""
    if isinstance(exc, NoEligibleModel):
        counts: Counter[str] = Counter()
        considered = 0
        for reasons in exc.excluded.values():
            relevant = [reason for reason in reasons if reason not in _SCOPE_REASONS]
            if relevant:
                considered += 1
                counts.update(relevant)
        if not considered:
            return "no_eligible_model", "No deployment matches the requested model name."
        top = ", ".join(f"{reason} ({count})" for reason, count in counts.most_common(4))
        return "no_eligible_model", f"{considered} candidate deployment{'s' if considered != 1 else ''} excluded: {top}."
    if isinstance(exc, AllModelsFailed):
        kinds = ", ".join(f"{kind} ({count})" for kind, count in Counter(item.kind for item in exc.failures).most_common())
        attempts = "; ".join(f"{item.deployment}: {item.reason}" for item in exc.failures[:4])
        return "all_attempts_failed", f"{len(exc.failures)} attempt{'s' if len(exc.failures) != 1 else ''} failed, {kinds}: {attempts}"
    if isinstance(exc, GatewayUnavailable):
        return "router_unavailable", str(exc)
    if isinstance(exc, (ValueError, RequestError)):
        return "rejected", str(exc)
    if isinstance(exc, RouterError):
        return "router_error", str(exc)
    return "internal_error", _safe_exception(exc)


def _record_failure(service: Any, api: str, model: str, exc: Exception, status: int) -> None:
    recorder = getattr(service, "record_request_failure", None)
    if recorder is None:
        return
    kind, detail = _failure_summary(exc)
    recorder(api=api, model=model, status=status, kind=kind, detail=detail)


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
