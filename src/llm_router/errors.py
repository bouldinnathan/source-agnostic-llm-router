"""Error types exposed by the router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class RouterError(Exception):
    """Base class for expected router failures."""


class ConfigError(RouterError):
    """Raised when router configuration is missing or invalid."""


class RequestError(RouterError):
    """Raised when a provider-neutral request is internally inconsistent."""


class NoEligibleModel(RouterError):
    """Raised when every configured deployment violates a routing constraint."""

    def __init__(self, message: str, excluded: dict[str, list[str]] | None = None) -> None:
        super().__init__(message)
        self.excluded = excluded or {}


@dataclass(slots=True)
class UpstreamFailure:
    deployment: str
    endpoint: str
    reason: str
    retryable: bool
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "deployment": self.deployment,
            "endpoint": self.endpoint,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class UpstreamError(RouterError):
    """A safe, secret-free representation of an upstream request failure."""

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code


class AllModelsFailed(RouterError):
    """Raised after all selected deployments fail."""

    def __init__(self, failures: list[UpstreamFailure]) -> None:
        self.failures = failures
        attempted = ", ".join(item.deployment for item in failures) or "none"
        super().__init__(f"All selected LLM deployments failed (attempted: {attempted})")
