"""Build a router from optional explicit configuration plus automatic enrollment."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DEFAULT_CONFIG_LOCATIONS, load_config
from .discovery import DiscoveryReport, DiscoverySettings, ModelDiscovery, merge_router_configs
from .errors import ConfigError
from .router import LLMRouter
from .schema import RouterConfig


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    router: LLMRouter
    discovery: DiscoveryReport
    configured: RouterConfig | None


async def bootstrap_router(
    config_path: str | os.PathLike[str] | None = None,
    *,
    discovery: bool = True,
    settings: DiscoverySettings | None = None,
    model_discovery: ModelDiscovery | None = None,
    previous: LLMRouter | None = None,
) -> BootstrapResult:
    """Create a ready router while preserving explicit-config precedence."""

    configured = load_optional_config(config_path)
    effective_settings = settings or DiscoverySettings.from_env()
    if not discovery:
        effective_settings = replace(effective_settings, enabled=False)
    discoverer = model_discovery or ModelDiscovery(effective_settings, configured=configured)
    report = await discoverer.discover()
    merged = merge_router_configs(configured, report.config)
    if not merged.models and not any(endpoint.discover for endpoint in merged.endpoints.values()):
        raise ConfigError(
            "No LLM models were configured or discovered. Start Ollama/LM Studio/vLLM, "
            "set a supported provider API-key environment variable, supply "
            "LLM_ROUTER_DISCOVERY_URLS, or provide --config."
        )
    runtime = (
        previous.runtime
        if previous is not None and previous.config.policy == merged.policy
        else None
    )
    router = LLMRouter(merged, runtime=runtime)
    return BootstrapResult(router=router, discovery=report, configured=configured)


def load_optional_config(
    config_path: str | os.PathLike[str] | None = None,
) -> RouterConfig | None:
    """Load a config if selected or present; absence is valid for discovery mode."""

    if config_path is not None or os.environ.get("LLM_ROUTER_CONFIG"):
        return load_config(config_path)
    for candidate in DEFAULT_CONFIG_LOCATIONS:
        if Path(candidate).is_file():
            return load_config(candidate)
    return None


__all__ = ["BootstrapResult", "bootstrap_router", "load_optional_config"]
