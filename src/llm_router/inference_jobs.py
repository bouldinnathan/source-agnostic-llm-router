"""Explicit, in-memory inference diagnostics; reading status never starts work."""

from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .schema import RouterConfig


COOLDOWN_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 15 * 60.0
NOTICE = (
    "Explicit inference test: one tiny request per supported backend, with no retry or "
    "failover. This may load a model and use memory or provider credits. No models are "
    "downloaded. Results are diagnostic only and do not change routing health."
)
Runner = Callable[..., Awaitable[list[dict[str, Any]]]]


class InferenceJobError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InferenceJobs:
    """One bounded job per gateway process, independent of the HTTP connection.

    Only start() may schedule work. The gateway owns cancellation at shutdown;
    browser polling/disconnection cannot trigger a second generation request.
    Results intentionally do not persist across process restarts.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._task: asyncio.Task[None] | None = None
        self._next_allowed = 0.0
        self._closed = False
        self._snapshot: dict[str, Any] = {
            "state": "idle", "run_id": None, "started_at": None, "finished_at": None,
            "total": 0, "completed": 0, "checks": [], "notice": NOTICE,
        }

    def status(self) -> dict[str, Any]:
        return deepcopy(self._snapshot)

    def start(self, config: RouterConfig | None) -> dict[str, Any]:
        # No await between testing and setting state: concurrent ASGI requests
        # cannot interleave starts on this process's event loop.
        if self._closed:
            raise InferenceJobError("Inference testing is unavailable during shutdown.", 503)
        if self._task is not None and not self._task.done():
            raise InferenceJobError("An inference test is already running. Read its progress instead.", 409)
        if time.monotonic() < self._next_allowed:
            raise InferenceJobError("The last inference test just finished. Wait 30 seconds before testing again.", 429)
        captured = replace(config, endpoints=dict(config.endpoints), models=tuple(config.models)) if config else None
        self._snapshot = {
            "state": "running", "run_id": uuid.uuid4().hex,
            "started_at": _now(), "finished_at": None,
            "total": len(captured.endpoints) if captured else 0,
            "completed": 0, "checks": [], "notice": NOTICE,
        }
        self._task = asyncio.create_task(self._run(captured), name="llm-router-inference-test")
        return self.status()

    async def _run(self, config: RouterConfig | None) -> None:
        expected = {endpoint.name for endpoint in config.endpoints.values()} if config else set()
        results: dict[str, dict[str, Any]] = {}

        async def on_result(row: dict[str, Any]) -> None:
            # Only our internal engine is called, never plugins. Bound and copy
            # the public schema so a malformed internal result cannot grow the
            # dashboard indefinitely or attach raw response/error objects.
            if not isinstance(row, Mapping) or row.get("name") not in expected:
                raise ValueError("Invalid diagnostic result")
            name = row["name"]
            if row.get("status") not in {"pass", "fail", "skip"}:
                raise ValueError("Invalid diagnostic status")
            public: dict[str, Any] = {}
            for key in ("name", "target", "selection", "detail"):
                value = row.get(key)
                if not isinstance(value, str) or len(value) > 2048:
                    raise ValueError("Invalid diagnostic text")
                public[key] = value
            model = row.get("model")
            elapsed = row.get("elapsed_ms")
            http_status = row.get("http_status")
            if model is not None and (not isinstance(model, str) or len(model) > 2048):
                raise ValueError("Invalid diagnostic model")
            if type(elapsed) is not int or elapsed < 0:
                raise ValueError("Invalid diagnostic duration")
            if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
                raise ValueError("Invalid diagnostic HTTP status")
            public.update(status=row["status"], model=model, elapsed_ms=elapsed, http_status=http_status)
            if name in results and results[name] != public:
                raise ValueError("Duplicate diagnostic result")
            results[name] = public
            self._snapshot["checks"] = list(results.values())
            self._snapshot["completed"] = len(results)

        try:
            if config is not None and config.endpoints:
                rows = await asyncio.wait_for(self._runner(config, on_result=on_result), timeout=JOB_TIMEOUT_SECONDS)
                for row in rows:
                    await on_result(row)
            self._snapshot["state"] = "complete" if len(results) == self._snapshot["total"] else "interrupted"
        except asyncio.CancelledError:
            self._snapshot["state"] = "interrupted"
            raise
        except Exception:
            # Never surface completion text, provider error bodies, or secrets.
            self._snapshot["state"] = "interrupted"
        finally:
            self._snapshot["finished_at"] = _now()
            self._next_allowed = time.monotonic() + COOLDOWN_SECONDS

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            # A task cancelled before its first turn never runs its finally.
            if self._snapshot["state"] == "running":
                self._snapshot["state"] = "interrupted"
                self._snapshot["finished_at"] = _now()
