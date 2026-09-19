from __future__ import annotations

import asyncio

import pytest

from llm_router import inference_jobs as jobs_module
from llm_router.inference_jobs import InferenceJobError, InferenceJobs
from llm_router.schema import EndpointConfig, RouterConfig


def config():
    return RouterConfig(endpoints={name: EndpointConfig(name, "ollama", f"http://{name}.invalid") for name in ("one", "two")}, models=())


def row(name="one", status="pass"):
    return {"name": name, "target": f"http://{name}.invalid", "status": status, "model": "tiny:0.5b",
            "selection": "Smallest reported model size.", "detail": "Model produced output.", "elapsed_ms": 1, "http_status": 200}


def test_reads_do_not_start_work_and_snapshots_are_detached():
    async def forbidden(*args, **kwargs):
        pytest.fail("Reading diagnostic status must not invoke any model")

    jobs = InferenceJobs(forbidden)
    assert jobs.status()["state"] == "idle"
    snapshot = jobs.status()
    snapshot["checks"].append(row())
    snapshot["state"] = "complete"
    assert jobs.status()["checks"] == []
    assert jobs.status()["state"] == "idle"


def test_progress_completion_isolation_busy_and_cooldown(monkeypatch):
    async def scenario():
        next_row = asyncio.Event()
        first_row = asyncio.Event()
        async def runner(captured, *, on_result):
            assert set(captured.endpoints) == {"one", "two"}
            await on_result(row())
            first_row.set()
            await next_row.wait()
            await on_result(row("two", "fail"))
            return [row(), row("two", "fail")]

        jobs = InferenceJobs(runner)
        original = config()
        accepted = jobs.start(original)
        original.endpoints.clear()  # Mutating config after POST cannot expand/change this job.
        assert accepted["state"] == "running"
        assert len(accepted["run_id"]) == 32
        await first_row.wait()
        assert jobs.status()["completed"] == 1
        with pytest.raises(InferenceJobError) as busy:
            jobs.start(config())
        assert busy.value.status_code == 409
        next_row.set()
        await jobs._task
        status = jobs.status()
        assert status["state"] == "complete"  # Complete is not a claim that all rows passed.
        assert status["completed"] == status["total"] == 2
        assert status["run_id"] == accepted["run_id"]
        assert status["finished_at"] >= status["started_at"]
        status["checks"][0]["model"] = "changed"
        assert jobs.status()["checks"][0]["model"] == "tiny:0.5b"
        with pytest.raises(InferenceJobError) as recent:
            jobs.start(config())
        assert recent.value.status_code == 429
        jobs._next_allowed = 0
        again = jobs.start(config())
        assert again["run_id"] != accepted["run_id"] and again["completed"] == 0
        await jobs.close()
        assert jobs.status()["state"] == "interrupted"
        with pytest.raises(InferenceJobError) as closed:
            jobs.start(config())
        assert closed.value.status_code == 503
    asyncio.run(scenario())


@pytest.mark.parametrize("fleet", [None, RouterConfig(endpoints={}, models=())])
def test_empty_fleet_does_not_call_runner(fleet):
    async def forbidden(*args, **kwargs):
        pytest.fail("Empty fleet must not invoke any model or discovery")

    async def scenario():
        jobs = InferenceJobs(forbidden)
        jobs.start(fleet)
        await jobs._task
        assert jobs.status()["state"] == "complete"
        assert jobs.status()["total"] == jobs.status()["completed"] == 0
        await jobs.close()
    asyncio.run(scenario())


def test_shutdown_cancels_and_joins_running_engine():
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def runner(*args, on_result):
            await on_result(row())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        jobs = InferenceJobs(runner)
        jobs.start(config())
        await started.wait()
        await jobs.close()
        assert stopped.is_set() and jobs._task.done()
        assert jobs.status()["state"] == "interrupted"
        assert jobs.status()["completed"] == 1
        assert jobs.status()["finished_at"]
    asyncio.run(scenario())


def test_job_deadline_is_interrupted_not_a_pass(monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_TIMEOUT_SECONDS", 0.01)
    async def scenario():
        async def runner(*args, on_result):
            await on_result(row())
            await asyncio.Event().wait()
        jobs = InferenceJobs(runner)
        jobs.start(config())
        await jobs._task
        assert jobs.status()["state"] == "interrupted"
        assert jobs.status()["completed"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["exception", "missing", "unknown", "oversized", "invalid_status", "duplicate", "invalid_elapsed", "invalid_http"])
def test_bad_runner_never_claims_complete_or_exposes_errors(mode):
    secret = "PRIVATE-PROVIDER-KEY https://user:password@private.invalid"
    async def scenario():
        async def runner(*args, on_result):
            await on_result(row())
            if mode == "exception":
                raise RuntimeError(secret)
            if mode == "missing":
                return [row()]
            value = row("two")
            if mode == "unknown": value["name"] = "unknown"
            if mode == "oversized": value["detail"] = "x" * 2049
            if mode == "invalid_status": value["status"] = "maybe"
            if mode == "duplicate": value = row(status="fail")
            if mode == "invalid_elapsed": value["elapsed_ms"] = True
            if mode == "invalid_http": value["http_status"] = 999
            await on_result(value)
            return [row(), value]
        jobs = InferenceJobs(runner)
        jobs.start(config())
        await jobs._task
        result = jobs.status()
        assert result["state"] == "interrupted"
        assert result["completed"] == 1
        assert secret not in str(result)
    asyncio.run(scenario())
