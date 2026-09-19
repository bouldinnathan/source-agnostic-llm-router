"""Exercise the HTTP/controller/updater progress contract without external work."""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from llm_router import update_control as control, updater
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter
from llm_router.update_progress import UpdateProgress, read_update_status

# Reuse the explicit fake installation/service manager, not any real host state.
from test_update_control import managed, OLD, NEW, OLD_RUN, NEW_RUN


KEY = "workflow-private-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
MUTATE = {**AUTH, "X-LLM-Router-Update": "1", "Origin": "http://router.test"}


async def request(app, method="GET"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
        return await client.request(method, "/status/update", headers=MUTATE if method == "POST" else AUTH)


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", KEY)

    def forbidden_process(*args, **kwargs):
        pytest.fail("Workflow test must not invoke real systemd, git, pip, or installation commands")

    async def forbidden_model_or_network(*args, **kwargs):
        pytest.fail("Workflow test must not use real networking, models, discovery, or provisioning")

    monkeypatch.setattr(subprocess, "run", forbidden_process)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_model_or_network)
    monkeypatch.setattr(LLMRouter, "complete", forbidden_model_or_network)
    for name in ("start", "refresh", "check_health", "provision"):
        monkeypatch.setattr(RouterGateway, name, forbidden_model_or_network)


@pytest.mark.parametrize("up_to_date", [False, True])
def test_authenticated_update_reaches_real_durable_terminal_state(managed, monkeypatch, up_to_date):
    monkeypatch.setenv("INVOCATION_ID", OLD_RUN)
    old_progress = UpdateProgress(managed.install)
    old_progress.advance("complete", state="current", current_commit=OLD, target_commit=OLD)
    app = create_app(gateway=RouterGateway(discovery=False), update_controller=managed.controller)
    stages = []

    def start_unit(*arguments):
        assert arguments == ("start", "--no-block", control.UPDATE_UNIT)
        managed.calls.append(arguments)
        managed.update.update(ActiveState="activating", SubState="start", InvocationID=NEW_RUN)
        return ""

    monkeypatch.setattr(control, "_systemctl", start_unit)
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: OLD if up_to_date else NEW)

    def stage(directory, commit):
        assert not up_to_date and directory == managed.install and commit == NEW
        response = asyncio.run(request(app))
        assert response.status_code == 200
        payload = response.json()
        assert payload["busy"] and payload["state"] == "running"
        assert payload["stage"] == "downloading"
        assert payload["run_id"] == NEW_RUN
        assert payload["current_commit"] == OLD and payload["target_commit"] == NEW
        assert KEY not in response.text
        assert asyncio.run(request(app, "POST")).status_code == 409
        stages.append(payload["stage"])
        return directory / "fake-staged-runtime"

    def activate(directory, runtime, was_active):
        assert not up_to_date and was_active
        assert runtime == directory / "fake-staged-runtime"
        # A fresh gateway/controller can recover progress during its restart.
        restarted = create_app(gateway=RouterGateway(discovery=False), update_controller=control.UpdateController())
        response = asyncio.run(request(restarted))
        assert response.status_code == 200
        payload = response.json()
        assert payload["busy"] and payload["stage"] == "restarting"
        assert payload["run_id"] == NEW_RUN
        stages.append(payload["stage"])

    monkeypatch.setattr(updater, "_stage", stage)
    monkeypatch.setattr(updater, "_service_active", lambda: True)
    monkeypatch.setattr(updater, "_activate", activate)
    accepted = asyncio.run(request(app, "POST"))
    assert accepted.status_code == 202
    assert accepted.json()["state"] == "queued"
    pending = asyncio.run(request(app)).json()
    assert pending["busy"] and pending["state"] == "queued"
    assert pending["run_id"] == NEW_RUN, "Old terminal progress is not the newly requested job"
    assert read_update_status(managed.install)["run_id"] == OLD_RUN

    # run_update owns a SIGTERM guard, so execute it on the test's main thread.
    monkeypatch.setenv("INVOCATION_ID", NEW_RUN)
    updater.run_update(managed.install)
    managed.update.update(ActiveState="inactive", SubState="dead", Result="success")
    result = asyncio.run(request(app))
    assert result.status_code == 200
    payload = result.json()
    assert payload["available"] and not payload["busy"]
    assert payload["state"] == ("current" if up_to_date else "succeeded")
    assert payload["stage"] == "complete" and payload["run_id"] == NEW_RUN
    assert payload["current_commit"] == payload["target_commit"] == (OLD if up_to_date else NEW)
    assert stages == ([] if up_to_date else ["downloading", "restarting"])
    assert managed.calls == [("start", "--no-block", control.UPDATE_UNIT)]
    assert result.headers["cache-control"] == "no-store"
    assert KEY not in result.text


def test_real_updater_failure_reaches_api_as_sanitized_terminal_state(managed, monkeypatch):
    app = create_app(gateway=RouterGateway(discovery=False), update_controller=managed.controller)

    def start_unit(*arguments):
        managed.calls.append(arguments)
        managed.update.update(ActiveState="activating", InvocationID=NEW_RUN)
        return ""

    def fail_download(*args):
        raise RuntimeError("private-pip-error https://user:secret@private-index.invalid")

    monkeypatch.setattr(control, "_systemctl", start_unit)
    monkeypatch.setattr(updater, "installed_revision", lambda _: OLD)
    monkeypatch.setattr(updater, "_remote_revision", lambda: NEW)
    monkeypatch.setattr(updater, "_stage", fail_download)
    assert asyncio.run(request(app, "POST")).status_code == 202
    monkeypatch.setenv("INVOCATION_ID", NEW_RUN)
    with pytest.raises(RuntimeError):
        updater.run_update(managed.install)
    managed.update.update(ActiveState="failed", SubState="failed", Result="exit-code")
    response = asyncio.run(request(app))
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "failed" and not payload["busy"]
    assert payload["run_id"] == NEW_RUN
    assert payload["current_commit"] == OLD and payload["target_commit"] == NEW
    for secret in ("private-pip-error", "private-index", "secret", KEY):
        assert secret not in response.text
