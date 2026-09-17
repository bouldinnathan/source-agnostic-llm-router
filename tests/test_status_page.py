from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
import re
import time
from dataclasses import replace

import httpx
import pytest

from llm_router.config import config_from_mapping
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.schema import AuthConfig

from conftest import make_config


async def request(app, path: str, **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as client:
        return await client.get(path, **kwargs)


def gateway_with_router() -> tuple[RouterGateway, LLMRouter]:
    config = make_config(
        models=[
            {
                "id": "qwen-a",
                "endpoint": "source-a",
                "upstream_model": "qwen",
                "replica_group": "qwen",
            },
            {
                "id": "qwen-b",
                "endpoint": "source-b",
                "upstream_model": "qwen",
                "replica_group": "qwen",
            },
        ]
    )
    config = replace(
        config,
        endpoints={
            name: replace(endpoint, machine_id=machine)
            for (name, endpoint), machine in zip(
                config.endpoints.items(), ("golemframe", "pantheon")
            )
        },
    )
    router = LLMRouter(config)
    gateway = RouterGateway(discovery=False)
    gateway._router = router
    return gateway, router


@pytest.fixture(autouse=True)
def no_ambient_gateway_key(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/status", "text/html"),
        ("/status/assets/style.css", "text/css"),
        ("/status/assets/app.js", "javascript"),
    ],
)
def test_status_shell_and_assets_are_public_but_contain_no_private_state(
    monkeypatch, path, content_type  # type: ignore[no-untyped-def]
) -> None:
    gateway, _ = gateway_with_router()
    gateway._last_error = "private-upstream-error-value"
    gateway.config_path = "/private/config/path/router.toml"
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "private-router-key-value")
    response = asyncio.run(request(create_app(gateway=gateway), path))

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for private_value in (
        "private-router-key-value",
        "private-upstream-error-value",
        "/private/config/path/router.toml",
        "source-a.invalid",
        "source-b.invalid",
        "qwen-a",
        "qwen-b",
    ):
        assert private_value not in response.text


def test_status_html_has_strict_same_origin_content_security_policy() -> None:
    response = asyncio.run(request(create_app(gateway=RouterGateway(discovery=False)), "/status"))
    directives = {
        directive.strip()
        for directive in response.headers["content-security-policy"].split(";")
        if directive.strip()
    }
    assert {
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    } <= directives
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "/status/assets/app.js" in response.text
    assert "/status/assets/style.css" in response.text
    assert 'type="password"' in response.text


@pytest.mark.parametrize("api_key_required", [False, True])
def test_status_version_is_unique_and_visible_in_public_topbar(api_key_required: bool) -> None:
    from llm_router.status_page import STATUS_CSS, render_status_html

    class VersionLocator(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[tuple[str, dict[str, str | None]]] = []
            self.locations: list[list[tuple[str, dict[str, str | None]]]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attributes = dict(attrs)
            current = (tag, attributes)
            if attributes.get("id") == "version":
                self.locations.append([*self.stack, current])
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.stack.append(current)

        def handle_endtag(self, tag: str) -> None:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    locator = VersionLocator()
    locator.feed(render_status_html(api_key_required=api_key_required))
    assert len(locator.locations) == 1, "Keep exactly one live version element"
    location = locator.locations[0]
    assert any(tag == "header" and "topbar" in (attrs.get("class") or "").split() for tag, attrs in location)
    assert "version-badge" in (location[-1][1].get("class") or "").split()
    assert all(tag != "footer" and attrs.get("id") != "details" and "hidden" not in attrs for tag, attrs in location)
    classes = {name for _, attrs in location for name in (attrs.get("class") or "").split()}
    assert "readonly" not in classes, "The mobile-hidden tagline must not hide the version badge"
    for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", STATUS_CSS):
        if not re.search(r"(?:^|;)\s*display\s*:\s*none\b", declarations):
            continue
        for selector in selectors.split(","):
            if ":empty" in selector:
                continue  # Hiding an empty placeholder does not hide a known version.
            assert not any(re.search(rf"\.{re.escape(name)}(?![\w-])", selector) for name in classes), selector
            assert not re.search(r"#version(?![\w-])", selector), selector


def test_browser_root_serves_status_but_updater_plain_text_probe_is_unchanged() -> None:
    app = create_app(gateway=RouterGateway(discovery=False))
    probe = asyncio.run(request(app, "/"))
    browser = asyncio.run(request(app, "/", headers={"Accept": "text/html"}))
    status = asyncio.run(request(app, "/status"))

    assert probe.status_code == 200
    assert probe.text == "LLM Router is running"
    assert probe.headers["content-type"].startswith("text/plain")
    assert browser.status_code == 200
    assert browser.headers["content-type"].startswith("text/html")
    assert browser.text == status.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer incorrect"}, {"Authorization": "Basic not-a-key"}],
)
def test_status_data_requires_the_gateway_key(monkeypatch, headers) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "correct-router-key")
    response = asyncio.run(request(create_app(gateway=gateway), "/status/data", headers=headers))

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert "error" in response.json()
    assert "correct-router-key" not in response.text
    assert "source-a" not in response.text


def test_status_data_accepts_bearer_key_but_never_key_in_query(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", "correct-router-key")
    app = create_app(gateway=gateway)
    response = asyncio.run(
        request(app, "/status/data", headers={"Authorization": "Bearer correct-router-key"})
    )
    queried = asyncio.run(request(app, "/status/data?api_key=correct-router-key"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["ready"] is True
    assert queried.status_code == 401
    assert "correct-router-key" not in response.text


def test_dashboard_snapshot_is_cached_and_does_not_trigger_network_or_provisioning(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gateway, _ = gateway_with_router()

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Viewing status must not start backend work")

    for name in ("router", "refresh", "check_health", "provision"):
        monkeypatch.setattr(gateway, name, forbidden)
    monkeypatch.setattr("llm_router.gateway.bootstrap_router", forbidden)
    monkeypatch.setattr("llm_router.gateway.probe_endpoints", forbidden)
    response = asyncio.run(request(create_app(gateway=gateway), "/status/data"))

    assert response.status_code == 200
    assert response.json()["counts"]["models"] == 2


def test_empty_gateway_still_serves_status_without_attempting_bootstrap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gateway = RouterGateway(discovery=False)

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Empty status must not try discovery")

    monkeypatch.setattr(gateway, "router", forbidden)
    monkeypatch.setattr(gateway, "refresh", forbidden)
    response = asyncio.run(request(create_app(gateway=gateway), "/status/data"))
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "unavailable"
    assert payload["ready"] is False
    assert payload["counts"] == {
        "endpoints": 0,
        "online": 0,
        "models": 0,
        "available_models": 0,
        "aliases": 0,
    }
    assert payload["endpoints"] == []
    assert payload["models"] == []
    assert payload["aliases"] == []
    assert payload["last_discovery"] is None
    assert payload["version"]
    assert payload["uptime_seconds"] >= 0
    assert payload["checked_at"]


def test_never_probed_backends_are_labelled_unchecked_not_online() -> None:
    gateway, _ = gateway_with_router()
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["counts"]["endpoints"] == 2
    assert payload["counts"]["online"] == 0
    assert payload["counts"]["available_models"] == 2
    assert {endpoint["state"] for endpoint in payload["endpoints"]} == {"unchecked"}
    assert all(endpoint["last_checked"] is None for endpoint in payload["endpoints"])


def test_known_backend_without_discovered_models_is_visible_but_not_ready() -> None:
    config = config_from_mapping({
        "endpoints": {"empty-studio": {
            "adapter": "openai-compatible",
            "base_url": "http://studio.invalid:1234/v1",
            "discover": True,
        }},
        "models": [],
    })
    gateway = RouterGateway(discovery=False)
    gateway._router = LLMRouter(config)
    gateway._router.runtime.record_endpoint_probe("empty-studio", True)
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["ready"] is False
    assert payload["counts"]["endpoints"] == 1
    assert payload["counts"]["online"] == 1
    assert payload["counts"]["models"] == 0
    assert payload["counts"]["available_models"] == 0
    assert payload["models"] == []
    assert payload["endpoints"][0]["model_count"] == 0
    assert payload["endpoints"][0]["state"] == "online"


def test_healthy_snapshot_has_machine_and_alias_availability() -> None:
    gateway, router = gateway_with_router()
    router.runtime.record_endpoint_probe("source-a", True)
    router.runtime.record_endpoint_probe("source-b", True)
    router.runtime.begin("qwen-a")
    router.runtime.record_success("qwen-a", 100)
    router.runtime.begin("qwen-a")
    gateway._last_refresh = time.time()
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["counts"] == {
        "endpoints": 2,
        "online": 2,
        "models": 2,
        "available_models": 2,
        "aliases": 5,
    }
    assert payload["last_discovery"]
    assert {endpoint["machine"] for endpoint in payload["endpoints"]} == {"golemframe", "pantheon"}
    assert all(endpoint["last_checked"] is not None for endpoint in payload["endpoints"])
    assert all(endpoint["model_count"] == 1 for endpoint in payload["endpoints"])
    assert all(endpoint["available_models"] == 1 for endpoint in payload["endpoints"])
    models = {model["deployment"]: model for model in payload["models"]}
    assert models["qwen-a"]["state"] == "available"
    assert models["qwen-a"]["active_requests"] == 1
    assert models["qwen-a"]["successes"] == 1
    assert models["qwen-a"]["failures"] == 0
    aliases = {alias["name"]: alias for alias in payload["aliases"]}
    assert aliases["qwen-ha"]["kind"] == "ha"
    assert aliases["qwen-ha"]["deployments"] == 2
    assert aliases["qwen-golemframe"]["kind"] == "preferred"
    assert aliases["qwen-golemframe-nofailover"]["kind"] == "pinned"
    assert aliases["qwen-golemframe-nofailover"]["deployments"] == 1
    assert all(alias["available"] for alias in aliases.values())


@pytest.mark.parametrize("machine", ["workstation-ha", "workstation-nofailover"])
def test_alias_kind_does_not_confuse_machine_name_suffixes(machine: str) -> None:
    gateway, router = gateway_with_router()
    config = replace(router.config, endpoints={
        name: replace(endpoint, machine_id=machine if name == "source-a" else endpoint.machine_id)
        for name, endpoint in router.config.endpoints.items()
    })
    gateway._router = LLMRouter(config)
    aliases = {alias["name"]: alias for alias in gateway.dashboard_status()["aliases"]}

    assert aliases["qwen-ha"]["kind"] == "ha"
    assert aliases[f"qwen-{machine}"]["kind"] == "preferred"
    assert aliases[f"qwen-{machine}-nofailover"]["kind"] == "pinned"


def test_offline_machine_preserves_ha_and_preferred_alias_but_not_pinned_availability() -> None:
    gateway, router = gateway_with_router()
    router.runtime.record_endpoint_probe("source-a", False, "private outage diagnostics")
    router.runtime.record_endpoint_probe("source-b", True)
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["ready"] is True
    assert payload["counts"]["online"] == 1
    assert payload["counts"]["available_models"] == 1
    models = {model["deployment"]: model for model in payload["models"]}
    assert models["qwen-a"]["state"] == "offline"
    assert models["qwen-b"]["state"] == "available"
    aliases = {alias["name"]: alias for alias in payload["aliases"]}
    assert aliases["qwen-ha"]["available"] is True
    assert aliases["qwen-golemframe"]["available"] is True
    assert aliases["qwen-golemframe-nofailover"]["available"] is False
    assert "private outage diagnostics" not in json.dumps(payload)


def test_all_offline_dashboard_data_is_available_while_health_check_still_fails() -> None:
    gateway, router = gateway_with_router()
    for name in router.config.endpoints:
        router.runtime.record_endpoint_probe(name, False)
    app = create_app(gateway=gateway)
    response = asyncio.run(request(app, "/status/data"))
    health = asyncio.run(request(app, "/healthz"))

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["status"] in {"degraded", "unavailable"}
    assert response.json()["counts"]["available_models"] == 0
    assert all(not alias["available"] for alias in response.json()["aliases"])
    assert health.status_code == 503


def test_cooldown_and_disabled_models_are_not_counted_available() -> None:
    gateway, router = gateway_with_router()
    router.config = replace(
        router.config,
        models=(router.config.models[0], replace(router.config.models[1], enabled=False)),
    )
    for name in router.config.endpoints:
        router.runtime.record_endpoint_probe(name, True)
    for _ in range(router.config.policy.circuit_breaker_failures):
        router.runtime.record_failure("qwen-a", "private model-error-token")
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["ready"] is False
    assert payload["counts"]["online"] == 2
    assert payload["counts"]["models"] == 1
    assert payload["counts"]["available_models"] == 0
    models = {model["deployment"]: model for model in payload["models"]}
    assert models["qwen-a"]["state"] == "cooldown"
    assert models["qwen-a"]["failures"] == router.config.policy.circuit_breaker_failures
    assert models["qwen-b"]["state"] == "disabled"
    assert all(not alias["available"] for alias in payload["aliases"])
    assert not any(alias["name"].startswith("qwen-pantheon") for alias in payload["aliases"])
    assert "private model-error-token" not in json.dumps(payload)


def test_discovery_error_is_generic_but_preserves_usable_model_readiness() -> None:
    gateway, _ = gateway_with_router()
    gateway._last_error = "Bearer upstream-private-token, URL=https://private.test/secret"
    payload = asyncio.run(request(create_app(gateway=gateway), "/status/data")).json()

    assert payload["status"] == "degraded"
    assert payload["ready"] is True
    assert payload["notice"]
    assert "upstream-private-token" not in json.dumps(payload)
    assert "private.test" not in json.dumps(payload)
    assert "last_error" not in payload


@pytest.mark.parametrize(
    "base_url,public_address",
    [
        ("https://url-user:private-password@studio.test:1234/private-path?key=private-query#private-fragment", "https://studio.test:1234"),
        ("http://url-user:private-password@[fd00::1234]:11434/private-path?key=private-query", "http://[fd00::1234]:11434"),
    ],
)
def test_dashboard_sanitizes_backend_url_and_omits_provider_secrets(
    base_url, public_address  # type: ignore[no-untyped-def]
) -> None:
    gateway, router = gateway_with_router()
    endpoint = replace(
        router.config.endpoints["source-a"],
        base_url=base_url,
        auth=AuthConfig(key_env="PRIVATE_PROVIDER_KEY_ENV", scheme="bearer"),
        headers={"Authorization": "Bearer private-header-value"},
        options={"private_option": "private-option-value"},
    )
    router.config = replace(
        router.config,
        endpoints={**router.config.endpoints, "source-a": endpoint},
        source_path="/private/config-file-location.toml",
    )
    response = asyncio.run(request(create_app(gateway=gateway), "/status/data"))
    endpoints = {row["name"]: row for row in response.json()["endpoints"]}

    assert response.status_code == 200
    assert endpoints["source-a"]["address"] == public_address
    for private_value in (
        "url-user", "private-password", "private-path", "private-query", "private-fragment",
        "PRIVATE_PROVIDER_KEY_ENV", "private-header-value", "private-option-value", "config-file-location",
    ):
        assert private_value not in response.text
    assert not {"auth", "headers", "options", "last_error"} & endpoints["source-a"].keys()


def test_backend_controlled_names_remain_json_data_not_embedded_html() -> None:
    attack = '<img src=x onerror="alert(1)">'
    config = config_from_mapping({
        "endpoints": {attack: {"adapter": "ollama-chat", "base_url": "http://studio.invalid", "machine_id": attack}},
        "models": [{"id": "untrusted-model", "endpoint": attack, "upstream_model": attack}],
    })
    gateway = RouterGateway(discovery=False)
    gateway._router = LLMRouter(config)
    app = create_app(gateway=gateway)
    page = asyncio.run(request(app, "/status"))
    data = asyncio.run(request(app, "/status/data"))
    script = asyncio.run(request(app, "/status/assets/app.js"))

    assert attack not in page.text
    assert data.headers["content-type"].startswith("application/json")
    assert data.json()["endpoints"][0]["name"] == attack
    assert data.json()["models"][0]["name"] == attack
    assert "textContent" in script.text
    for unsafe_rendering in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_rendering not in script.text


@pytest.mark.parametrize(
    "base_url", ["https://host.invalid:bad-port/private-token", "http://[broken-ipv6", "command://private-command"]
)
def test_custom_or_invalid_backend_urls_do_not_break_status_or_leak_full_value(base_url) -> None:  # type: ignore[no-untyped-def]
    gateway, router = gateway_with_router()
    endpoint = replace(router.config.endpoints["source-a"], base_url=base_url)
    router.config = replace(router.config, endpoints={**router.config.endpoints, "source-a": endpoint})
    response = asyncio.run(request(create_app(gateway=gateway), "/status/data"))

    assert response.status_code == 200
    assert base_url not in response.text
    assert "private-token" not in response.text
    assert "private-command" not in response.text


def test_browser_never_persists_api_keys_and_bootstraps_url_keys_into_headers() -> None:
    script = asyncio.run(request(create_app(gateway=RouterGateway(discovery=False)), "/status/assets/app.js"))
    assert script.status_code == 200
    assert "Authorization" in script.text
    assert "/status/data" in script.text
    assert "replaceState" in script.text
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "?token="):
        assert forbidden not in script.text
