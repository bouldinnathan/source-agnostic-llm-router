from __future__ import annotations

import asyncio
import json

import httpx

from llm_router.provisioning import (
    GIB,
    HardwareResources,
    OllamaProvisioner,
    ProvisioningSettings,
    choose_candidate,
)


def resources(*, cpu: int = 12, memory: float = 32, disk: float = 80) -> HardwareResources:
    return HardwareResources(
        cpu_count=cpu,
        available_memory_bytes=int(memory * GIB),
        free_disk_bytes=int(disk * GIB),
        disk_path="/",
    )


def test_candidate_selection_supports_balanced_quality_and_smallest_priority() -> None:
    host = resources(cpu=16, memory=48, disk=100)

    balanced, _ = choose_candidate(host, ProvisioningSettings(priority="balanced"))
    quality, _ = choose_candidate(
        host,
        ProvisioningSettings(priority="quality", max_download_bytes=20 * GIB),
    )
    smallest, _ = choose_candidate(host, ProvisioningSettings(priority="smallest"))

    assert balanced and balanced.model == "qwen3.5:9b"
    assert quality and quality.model == "qwen3.5:27b"
    assert smallest and smallest.model == "qwen3.5:0.8b"


def test_candidate_selection_skips_when_storage_headroom_is_insufficient() -> None:
    candidate, reason = choose_candidate(
        resources(cpu=16, memory=48, disk=5.5),
        ProvisioningSettings(priority="smallest", reserve_disk_bytes=5 * GIB),
    )

    assert candidate is None
    assert "storage" in reason


def test_provisioner_pulls_selected_model_when_ollama_is_empty() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/pull":
            assert json.loads(request.content)["model"] == "qwen3.5:4b"
            return httpx.Response(200, json={"status": "success"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provisioner = OllamaProvisioner(
        ProvisioningSettings(priority="balanced"),
        transport=httpx.MockTransport(handler),
        resource_provider=lambda: resources(cpu=4, memory=10, disk=20),
    )
    report = asyncio.run(provisioner.provision())

    assert report.status == "installed"
    assert report.selected_model == "qwen3.5:4b"
    assert [request.url.path for request in requests] == ["/api/tags", "/api/pull"]


def test_provisioner_does_not_pull_when_tool_model_exists() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": "custom-agent:latest"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        raise AssertionError("model pull should not be attempted")

    provisioner = OllamaProvisioner(
        ProvisioningSettings(),
        transport=httpx.MockTransport(handler),
        resource_provider=resources,
    )
    report = asyncio.run(provisioner.provision())

    assert report.status == "already_available"
    assert report.selected_model is None
    assert [request.url.path for request in requests] == ["/api/tags", "/api/show"]


def test_provisioning_failure_is_sanitized_and_non_throwing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(500, json={"error": "secret filesystem details"})

    provisioner = OllamaProvisioner(
        ProvisioningSettings(priority="smallest"),
        transport=httpx.MockTransport(handler),
        resource_provider=resources,
    )
    report = asyncio.run(provisioner.provision())

    assert report.status == "failed"
    assert report.reason == "Ollama returned HTTP 500"
    assert "secret" not in report.reason


def test_remote_provisioning_requires_explicit_permission() -> None:
    provisioner = OllamaProvisioner(
        ProvisioningSettings(ollama_url="http://192.0.2.10:11434"),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        resource_provider=resources,
    )

    report = asyncio.run(provisioner.provision())

    assert report.status == "skipped"
    assert "remote provisioning is disabled" in report.reason


def test_dry_run_reports_choice_without_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": []})

    provisioner = OllamaProvisioner(
        ProvisioningSettings(priority="smallest"),
        transport=httpx.MockTransport(handler),
        resource_provider=resources,
    )

    report = asyncio.run(provisioner.provision(dry_run=True))

    assert report.status == "planned"
    assert report.selected_model == "qwen3.5:0.8b"
