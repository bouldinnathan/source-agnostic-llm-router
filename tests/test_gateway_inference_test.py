"""Only the explicitly authorized action may start diagnostic generation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

import llm_router.gateway as gateway_module
from llm_router.gateway import RouterGateway, create_app
from llm_router.router import LLMRouter

from conftest import make_config


KEY = "private-inference-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
MUTATE = {**AUTH, "X-LLM-Router-Inference-Test": "1"}
PATH = "/status/inference-test"
PRIVATE_ERROR = "private-provider-key model-completion-secret /private/router.toml"


def result_row(name="source-a", status="pass"):
    return {
        "name": name, "target": f"http://{name}.invalid", "status": status,
        "model": "tiny-model", "selection": "Smallest suitable model.",
        "detail": "Tiny inference completed.", "elapsed_ms": 4, "http_status": 200,
    }


@dataclass
class FakeRunner:
    rows: list[dict] = field(default_factory=list)
    calls: list = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None
    callback_release: asyncio.Event | None = None
    error: Exception | None = None
    cancelled: bool = False

    async def __call__(self, config, *, on_result=None):  # type: ignore[no-untyped-def]
        self.calls.append(config)
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            for row in self.rows:
                if on_result is not None:
                    await on_result(dict(row))
                if self.callback_release is not None:
                    await self.callback_release.wait()
            if self.error is not None:
                raise self.error
            return [dict(row) for row in self.rows]
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def request(app, method="GET", path=PATH, **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        response = await client.request(method, path, **kwargs)
        # Let an accidentally scheduled job become observable even for requests
        # expected to be rejected; asyncio.run cleanup must not hide that bug.
        await asyncio.sleep(0)
        return response


def safe_response(response):  # type: ignore[no-untyped-def]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-type"].startswith("application/json")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    public = response.text + str(dict(response.headers))
    for secret in (KEY, "private-provider-key", "model-completion-secret", "/private/router.toml"):
        assert secret not in public


def snapshot(payload):  # type: ignore[no-untyped-def]
    assert set(payload) == {
        "state", "run_id", "started_at", "finished_at", "total", "completed", "checks", "notice",
    }
    assert payload["state"] in {"idle", "running", "complete", "interrupted"}
    assert type(payload["total"]) is int and payload["total"] >= 0
    assert type(payload["completed"]) is int and 0 <= payload["completed"] <= payload["total"]
    assert isinstance(payload["checks"], list)
    assert isinstance(payload["notice"], str)


@pytest.fixture(autouse=True)
def no_real_generation_or_discovery(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", KEY)

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Gateway API regressions must not perform real inference, discovery, or networking")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(LLMRouter, "complete", forbidden)
    for name in ("refresh", "router", "check_health", "provision"):
        monkeypatch.setattr(RouterGateway, name, forbidden)
    for name in ("bootstrap_router", "probe_endpoints", "check_saved_host"):
        monkeypatch.setattr(gateway_module, name, forbidden)


@pytest.fixture
def setup_inference():  # type: ignore[no-untyped-def]
    runner = FakeRunner()
    service = RouterGateway(discovery=False)
    app = create_app(gateway=service, inference_test_runner=runner)
    return app, service, runner


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("key", [None, "", "   "])
def test_configured_nonblank_key_is_required_even_for_keyless_gateways(setup_inference, monkeypatch, method, key):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    if key is None:
        monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_GATEWAY_API_KEY", key)
    response = asyncio.run(request(app, method, headers=MUTATE))
    assert response.status_code == 403
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic " + KEY, "Bearer"])
def test_bad_auth_never_starts_or_reads_job(setup_inference, monkeypatch, method, authorization):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference

    def forbidden_status():
        raise AssertionError("Authentication must precede job-state access")

    monkeypatch.setattr(app.state.inference_jobs, "status", forbidden_status)
    headers = {"X-LLM-Router-Inference-Test": "1"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = asyncio.run(request(app, method, headers=headers))
    assert response.status_code == 401
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_invalid_nonascii_key_is_rejected(setup_inference, method):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, method, headers={
        b"Authorization": "Bearer \u2603".encode(), b"X-LLM-Router-Inference-Test": b"1",
    }))
    assert response.status_code == 401
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("query", [
    f"api_key={KEY}", f"%61pi_key={KEY}", "model=expensive", "target=http://attacker.invalid",
    "prompt=custom", "max_tokens=1000000", "refresh=true", "unknown",
])
def test_queries_never_authorize_or_customize_inference(setup_inference, method, query):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, method, PATH + "?" + query, headers=MUTATE))
    assert response.status_code == 400
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_url_api_key_is_not_authentication(setup_inference, method):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, method, PATH + f"?api_key={KEY}", headers={
        "X-LLM-Router-Inference-Test": "1",
    }))
    assert response.status_code == 401
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("marker", [None, "0", "true", "2", "1 "])
def test_post_requires_own_explicit_action_header(setup_inference, marker):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    headers = {**AUTH, "X-LLM-Router-Self-Test": "1", "X-LLM-Router-Update": "1"}
    if marker is not None:
        headers["X-LLM-Router-Inference-Test"] = marker
    response = asyncio.run(request(app, "POST", headers=headers))
    assert response.status_code == 403
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("origin", [
    "null", "http://attacker.invalid", "https://router.test", "http://router.test:9999",
    "http://router.test.attacker.invalid", "http://router.test/",
])
def test_cross_origin_post_is_rejected(setup_inference, origin):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, "POST", headers={**MUTATE, "Origin": origin}))
    assert response.status_code == 403
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("body", [
    b"{}", b" ", b"null", b"[]", b"\x00", b'{"model":"expensive"}',
    b'{"prompt":"custom","max_tokens":1000000}', b"x" * 65536,
])
def test_post_body_must_be_empty_not_even_json(setup_inference, body):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, "POST", headers={**MUTATE, "Content-Type": "application/json"}, content=body))
    assert response.status_code == 400
    assert runner.calls == []
    safe_response(response)


def test_nonempty_chunked_body_is_rejected(setup_inference):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference

    async def chunks():
        yield b""
        yield b"{}"

    response = asyncio.run(request(app, "POST", headers=MUTATE, content=chunks()))
    assert response.status_code == 400
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("headers,path,expected", [
    ({"X-LLM-Router-Inference-Test": "1"}, PATH, 401),
    (AUTH, PATH, 403),
    ({**MUTATE, "Origin": "http://attacker.invalid"}, PATH, 403),
    (MUTATE, PATH + "?prompt=private", 400),
])
def test_invalid_requests_do_not_read_body(setup_inference, headers, path, expected):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference

    async def unreadable():
        raise AssertionError("Rejected action must not read request body")
        yield b""  # pragma: no cover

    response = asyncio.run(request(app, "POST", path, headers=headers, content=unreadable()))
    assert response.status_code == expected
    assert runner.calls == []
    safe_response(response)


def test_body_read_times_out_without_starting_job(setup_inference, monkeypatch):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    original_wait_for = asyncio.wait_for
    deadlines = []

    async def bounded(awaitable, timeout):  # type: ignore[no-untyped-def]
        deadlines.append(timeout)
        return await original_wait_for(awaitable, 0.01)

    async def stalled():
        await asyncio.Event().wait()
        yield b""  # pragma: no cover

    monkeypatch.setattr(gateway_module.asyncio, "wait_for", bounded)
    response = asyncio.run(request(app, "POST", headers=MUTATE, content=stalled()))
    assert deadlines == [3.0]
    assert response.status_code == 400
    assert runner.calls == []
    safe_response(response)


def test_get_is_passive_and_starts_idle(setup_inference):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, headers=AUTH))
    assert response.status_code == 200
    payload = response.json()
    snapshot(payload)
    assert payload["state"] == "idle"
    assert payload["checks"] == [] and payload["total"] == payload["completed"] == 0
    assert payload["run_id"] is payload["started_at"] is payload["finished_at"] is None
    assert runner.calls == []
    safe_response(response)


@pytest.mark.parametrize("headers", [AUTH, MUTATE])
def test_head_is_passive_even_with_action_header(setup_inference, headers):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    response = asyncio.run(request(app, "HEAD", headers=headers))
    assert response.status_code == 200
    assert response.content == b""
    assert runner.calls == []
    assert app.state.inference_jobs.status()["state"] == "idle"
    safe_response(response)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS"])
def test_unsupported_methods_never_launch_diagnostic(setup_inference, method):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    response = asyncio.run(request(app, method, headers=MUTATE))
    assert response.status_code == 405
    assert runner.calls == []
    assert app.state.inference_jobs.status()["state"] == "idle"


@pytest.mark.parametrize("origin", [None, "http://router.test"])
def test_explicit_post_without_cached_fleet_completes_without_discovery(setup_inference, origin):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference

    async def scenario():
        headers = dict(MUTATE)
        if origin is not None:
            headers["Origin"] = origin
        response = await request(app, "POST", headers=headers)
        assert response.status_code == 202
        snapshot(response.json())
        await asyncio.sleep(0)
        final = await request(app, headers=AUTH)
        assert final.json()["state"] == "complete"
        assert final.json()["total"] == final.json()["completed"] == 0
        assert final.json()["checks"] == []
        assert len(runner.calls) <= 1 and all(config is None for config in runner.calls)
        safe_response(response)
        safe_response(final)

    asyncio.run(scenario())


def test_one_job_captures_cached_config_reports_partial_results_and_rejects_busy(setup_inference):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    original = make_config()
    service._router = LLMRouter(original)

    async def scenario():
        runner.release = asyncio.Event()
        runner.callback_release = asyncio.Event()
        runner.rows = [result_row(), result_row("source-b")]
        accepted = await request(app, "POST", headers=MUTATE)
        assert accepted.status_code == 202
        await runner.started.wait()
        run_id = accepted.json()["run_id"]
        assert isinstance(run_id, str) and run_id
        busy = await request(app, "POST", headers=MUTATE)
        assert busy.status_code == 409
        service._router = LLMRouter(make_config(router={"max_attempts": 1}))
        running = await request(app, headers=AUTH)
        assert running.json()["state"] == "running"
        assert running.json()["run_id"] == run_id
        assert running.json()["completed"] == 0
        runner.release.set()
        await asyncio.sleep(0)
        partial = await request(app, headers=AUTH)
        assert partial.json()["state"] == "running"
        assert partial.json()["completed"] == 1
        assert partial.json()["checks"] == runner.rows[:1]
        runner.callback_release.set()
        await asyncio.sleep(0)
        finished = await request(app, headers=AUTH)
        snapshot(finished.json())
        assert finished.json()["state"] == "complete"
        assert finished.json()["run_id"] == run_id
        assert finished.json()["checks"] == runner.rows
        assert finished.json()["finished_at"] is not None
        assert runner.calls == [original]
        for response in (accepted, busy, running, partial, finished):
            safe_response(response)

    asyncio.run(scenario())


def test_concurrent_posts_create_only_one_diagnostic_task(setup_inference):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())

    async def scenario():
        runner.release = asyncio.Event()
        responses = await asyncio.gather(*(request(app, "POST", headers=MUTATE) for _ in range(5)))
        assert sorted(response.status_code for response in responses) == [202, 409, 409, 409, 409]
        await runner.started.wait()
        assert len(runner.calls) == 1
        await app.state.inference_jobs.close()
        assert runner.cancelled

    asyncio.run(scenario())


def test_job_copies_cached_endpoint_mapping_before_background_execution(setup_inference):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    config = make_config()
    service._router = LLMRouter(config)

    async def scenario():
        runner.release = asyncio.Event()
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202
        await runner.started.wait()
        captured = runner.calls[0]
        assert captured is not config
        assert captured.endpoints is not config.endpoints
        config.endpoints.clear()
        assert set(captured.endpoints) == {"source-a", "source-b"}
        await app.state.inference_jobs.close()

    asyncio.run(scenario())


def test_cooldown_begins_when_job_finishes_not_when_it_starts(setup_inference, monkeypatch):  # type: ignore[no-untyped-def]
    import llm_router.inference_jobs as jobs_module

    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    runner.rows = [result_row(), result_row("source-b")]
    now = [100.0]
    monkeypatch.setattr(jobs_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def scenario():
        runner.release = asyncio.Event()
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202
        await runner.started.wait()
        now[0] = 200.0
        runner.release.set()
        await asyncio.sleep(0)
        assert (await request(app, headers=AUTH)).json()["state"] == "complete"
        cooling = await request(app, "POST", headers=MUTATE)
        assert cooling.status_code == 429
        now[0] += jobs_module.COOLDOWN_SECONDS - 0.1
        assert (await request(app, "POST", headers=MUTATE)).status_code == 429
        now[0] += 0.2
        restarted = await request(app, "POST", headers=MUTATE)
        assert restarted.status_code == 202
        await asyncio.sleep(0)
        assert len(runner.calls) == 2
        safe_response(cooling)
        safe_response(restarted)

    asyncio.run(scenario())


@pytest.mark.parametrize("exception", [RuntimeError, ValueError, TypeError])
def test_runner_failure_keeps_safe_partial_results_without_exception_leak(setup_inference, exception):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    runner.rows = [result_row()]
    runner.error = exception(PRIVATE_ERROR)

    async def scenario():
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202
        await asyncio.sleep(0)
        response = await request(app, headers=AUTH)
        assert response.status_code == 200
        payload = response.json()
        snapshot(payload)
        assert payload["state"] == "interrupted"
        assert payload["checks"] == runner.rows
        assert payload["completed"] == 1
        safe_response(response)
        # Reading an interrupted result must never implicitly retry generation.
        again = await request(app, headers=AUTH)
        assert again.json() == payload
        assert len(runner.calls) == 1

    asyncio.run(scenario())


def test_runner_extra_raw_fields_are_not_forwarded_to_status(setup_inference):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    clean_rows = [result_row(), result_row("source-b")]
    runner.rows = [{**row, "raw_response": PRIVATE_ERROR, "authorization": KEY} for row in clean_rows]

    async def scenario():
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202
        await asyncio.sleep(0)
        response = await request(app, headers=AUTH)
        assert response.json()["state"] == "complete"
        assert response.json()["checks"] == clean_rows
        safe_response(response)

    asyncio.run(scenario())


def test_whole_job_deadline_interrupts_without_retrying(setup_inference, monkeypatch):  # type: ignore[no-untyped-def]
    import llm_router.inference_jobs as jobs_module

    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    monkeypatch.setattr(jobs_module, "JOB_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        runner.release = asyncio.Event()
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202
        await runner.started.wait()

        async def finished():
            while app.state.inference_jobs.status()["state"] == "running":
                await asyncio.sleep(0.002)

        await asyncio.wait_for(finished(), timeout=1.0)
        response = await request(app, headers=AUTH)
        assert response.json()["state"] == "interrupted"
        assert runner.cancelled and len(runner.calls) == 1
        assert (await request(app, "POST", headers=MUTATE)).status_code == 429
        safe_response(response)

    asyncio.run(scenario())


def test_lifespan_exit_cancels_and_joins_running_diagnostic(setup_inference, monkeypatch):  # type: ignore[no-untyped-def]
    app, service, runner = setup_inference
    service._router = LLMRouter(make_config())
    stopped = []

    async def start():
        return None

    async def stop():
        stopped.append(True)

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "stop", stop)

    async def scenario():
        runner.release = asyncio.Event()
        async with app.router.lifespan_context(app):
            response = await request(app, "POST", headers=MUTATE)
            assert response.status_code == 202
            await runner.started.wait()
            assert not runner.cancelled
        assert runner.cancelled
        assert stopped == [True]
        assert app.state.inference_jobs.status()["state"] == "interrupted"

    asyncio.run(scenario())


@pytest.mark.parametrize("path", [
    "/", "/status", "/status/assets/app.js", "/status/data", "/healthz", "/readyz", "/api/version",
])
def test_existing_page_and_status_reads_never_start_diagnostic(setup_inference, path):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    response = asyncio.run(request(app, path=path, headers=AUTH))
    assert response.status_code in {200, 503}
    assert runner.calls == []
    assert app.state.inference_jobs.status()["state"] == "idle"


def test_existing_metadata_self_test_does_not_launch_inference(setup_inference, monkeypatch):  # type: ignore[no-untyped-def]
    app, _, runner = setup_inference
    metadata_calls = []

    async def metadata_only(*args, **kwargs):  # type: ignore[no-untyped-def]
        metadata_calls.append(True)
        return []

    monkeypatch.setattr(gateway_module, "run_backend_checks", metadata_only)
    response = asyncio.run(request(app, "POST", "/status/self-test", headers={
        **AUTH, "X-LLM-Router-Self-Test": "1",
    }))
    assert response.status_code == 200
    assert runner.calls == []
    assert app.state.inference_jobs.status()["state"] == "idle"


def test_explicit_api_job_runs_real_bounded_engine_with_mock_backends_only():
    from llm_router.inference_test import MAX_OUTPUT_TOKENS, PROMPT, run_inference_checks

    seen = []
    private_reply = "provider-private-generated-text"

    async def backend(request):  # type: ignore[no-untyped-def]
        seen.append(request)
        assert KEY not in str(request.headers)
        assert request.headers.get("authorization") is None
        if request.method == "GET":
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json={"models": [
                {"name": "frontier", "size": 100}, {"name": "budget", "size": 50},
            ]})
        assert request.method == "POST" and request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "user", "content": PROMPT}]
        assert body["options"]["num_predict"] == MAX_OUTPUT_TOKENS == 16
        assert body["stream"] is False
        return httpx.Response(200, json={"message": {"content": private_reply}, "done": True})

    async def runner(config, *, on_result):  # type: ignore[no-untyped-def]
        return await run_inference_checks(config, on_result=on_result, transport=httpx.MockTransport(backend))

    service = RouterGateway(discovery=False)
    router = LLMRouter(make_config())
    service._router = router
    app = create_app(gateway=service, inference_test_runner=runner)

    async def scenario():
        assert (await request(app, headers=AUTH)).json()["state"] == "idle"
        assert seen == []
        assert (await request(app, "POST", headers=MUTATE)).status_code == 202

        async def finished():
            while app.state.inference_jobs.status()["state"] == "running":
                await asyncio.sleep(0.002)

        await asyncio.wait_for(finished(), timeout=1.0)
        response = await request(app, headers=AUTH)
        payload = response.json()
        assert payload["state"] == "complete"
        assert payload["completed"] == payload["total"] == 2
        assert all(row["status"] == "pass" for row in payload["checks"])
        assert private_reply not in response.text
        assert len(seen) == 4
        assert sorted((item.url.host, item.method) for item in seen) == [
            ("source-a.invalid", "GET"), ("source-a.invalid", "POST"),
            ("source-b.invalid", "GET"), ("source-b.invalid", "POST"),
        ]
        assert service._router is router
        safe_response(response)

    asyncio.run(scenario())
