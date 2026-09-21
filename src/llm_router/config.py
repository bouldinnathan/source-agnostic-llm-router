"""Strict JSON/TOML configuration loading."""

from __future__ import annotations

import json
import os
try:
    import tomllib
except ImportError:  # Python 3.10 / Ubuntu 22.04
    import tomli as tomllib
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .schema import (
    DEFAULT_STRATEGIES,
    AuthConfig,
    EndpointConfig,
    ModelConfig,
    PolicyConfig,
    RouterConfig,
)

DEFAULT_CONFIG_LOCATIONS = (
    Path("router.toml"),
    Path("router.json"),
    Path.home() / ".config" / "llm-router" / "router.toml",
    Path.home() / ".config" / "llm-router" / "router.json",
)


def find_config_path(explicit_path: str | os.PathLike[str] | None = None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"Router config does not exist: {path}")
        return path.resolve()

    env_path = os.environ.get("LLM_ROUTER_CONFIG")
    if env_path:
        return find_config_path(env_path)

    for candidate in DEFAULT_CONFIG_LOCATIONS:
        if candidate.is_file():
            return candidate.resolve()

    checked = ", ".join(str(path) for path in DEFAULT_CONFIG_LOCATIONS)
    raise ConfigError(
        "No router config found. Set LLM_ROUTER_CONFIG or create one of: " + checked
    )


def load_config(path: str | os.PathLike[str] | None = None) -> RouterConfig:
    config_path = find_config_path(path)
    try:
        with config_path.open("rb") as handle:
            if config_path.suffix.lower() == ".toml":
                payload = tomllib.load(handle)
            elif config_path.suffix.lower() == ".json":
                payload = json.load(handle)
            else:
                raise ConfigError("Router config must use a .toml or .json extension")
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc
    return config_from_mapping(payload, source_path=str(config_path))


def config_from_mapping(payload: Mapping[str, Any], *, source_path: str | None = None) -> RouterConfig:
    root = _mapping(payload, "config")
    endpoints_raw = _mapping(root.get("endpoints"), "endpoints")
    models_raw = root.get("models", [])
    if not isinstance(models_raw, list):
        raise ConfigError("models must be an array")

    endpoints: dict[str, EndpointConfig] = {}
    for name, raw_value in endpoints_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("endpoint names must be non-empty strings")
        raw = _mapping(raw_value, f"endpoints.{name}")
        adapter = _required_string(raw, "adapter", f"endpoints.{name}")
        base_url_value = raw.get("base_url", "")
        if not isinstance(base_url_value, str):
            raise ConfigError(f"endpoints.{name}.base_url must be a string")
        auth_raw = _mapping(raw.get("auth", {}), f"endpoints.{name}.auth")
        key_env = auth_raw.get("key_env", raw.get("api_key_env"))
        if key_env is not None and not isinstance(key_env, str):
            raise ConfigError(f"endpoints.{name}.auth.key_env must be a string")
        auth = AuthConfig(
            key_env=key_env,
            scheme=_optional_string(auth_raw, "scheme", f"endpoints.{name}.auth"),
            header=_optional_string(auth_raw, "header", f"endpoints.{name}.auth"),
            prefix=_optional_string(auth_raw, "prefix", f"endpoints.{name}.auth"),
            query_param=_optional_string(auth_raw, "query_param", f"endpoints.{name}.auth"),
        )
        headers_raw = _mapping(raw.get("headers", {}), f"endpoints.{name}.headers")
        headers: dict[str, str] = {}
        for header_name, header_value in headers_raw.items():
            if not isinstance(header_name, str) or not isinstance(header_value, str):
                raise ConfigError(f"endpoints.{name}.headers must contain string values")
            headers[header_name] = header_value
        endpoints[name] = EndpointConfig(
            name=name,
            adapter=adapter,
            base_url=base_url_value.rstrip("/"),
            auth=auth,
            headers=headers,
            options=_mapping(raw.get("options", {}), f"endpoints.{name}.options"),
            timeout_seconds=_positive_float(
                raw.get("timeout_seconds", 90.0), f"endpoints.{name}.timeout_seconds"
            ),
            verify_tls=_boolean(raw.get("verify_tls", True), f"endpoints.{name}.verify_tls"),
            machine_id=_optional_nonempty_string(raw, "machine_id", f"endpoints.{name}"),
            discover=_boolean(raw.get("discover", False), f"endpoints.{name}.discover"),
            health_path=_optional_nonempty_string(raw, "health_path", f"endpoints.{name}"),
            max_concurrent_requests=_optional_positive_int(
                raw.get("max_concurrent_requests"), f"endpoints.{name}.max_concurrent_requests"
            ),
        )

    models: list[ModelConfig] = []
    seen_ids: set[str] = set()
    for index, raw_value in enumerate(models_raw):
        location = f"models[{index}]"
        raw = _mapping(raw_value, location)
        model_id = _required_string(raw, "id", location)
        if model_id in seen_ids:
            raise ConfigError(f"Duplicate model deployment id: {model_id}")
        seen_ids.add(model_id)
        endpoint = _required_string(raw, "endpoint", location)
        if endpoint not in endpoints:
            raise ConfigError(f"{location}.endpoint references unknown endpoint '{endpoint}'")
        upstream_model = raw.get("upstream_model", raw.get("model"))
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise ConfigError(f"{location}.upstream_model must be a non-empty string")

        capabilities_raw = _mapping(raw.get("capabilities", {}), f"{location}.capabilities")
        capabilities: dict[str, float] = {}
        for capability, value in capabilities_raw.items():
            if not isinstance(capability, str) or not capability.strip():
                raise ConfigError(f"{location}.capabilities keys must be non-empty strings")
            if isinstance(value, bool):
                score = 1.0 if value else 0.0
            else:
                score = _bounded_float(value, f"{location}.capabilities.{capability}")
            capabilities[capability.strip().lower().replace("-", "_")] = score

        tags_value = raw.get("tags", [])
        if not isinstance(tags_value, list) or not all(isinstance(tag, str) for tag in tags_value):
            raise ConfigError(f"{location}.tags must be an array of strings")

        models.append(
            ModelConfig(
                id=model_id,
                endpoint=endpoint,
                upstream_model=upstream_model.strip(),
                capabilities=capabilities,
                quality=_bounded_float(raw.get("quality", 0.5), f"{location}.quality"),
                context_window=_positive_int(
                    raw.get("context_window", 8_192), f"{location}.context_window"
                ),
                max_output_tokens=_positive_int(
                    raw.get("max_output_tokens", 2_048), f"{location}.max_output_tokens"
                ),
                input_cost_per_million=_optional_nonnegative_float(
                    raw.get("input_cost_per_million"), f"{location}.input_cost_per_million"
                ),
                output_cost_per_million=_optional_nonnegative_float(
                    raw.get("output_cost_per_million"), f"{location}.output_cost_per_million"
                ),
                estimated_latency_ms=_positive_float(
                    raw.get("estimated_latency_ms", 2_000), f"{location}.estimated_latency_ms"
                ),
                reliability=_bounded_float(
                    raw.get("reliability", 0.95), f"{location}.reliability"
                ),
                enabled=_boolean(raw.get("enabled", True), f"{location}.enabled"),
                priority=_integer(raw.get("priority", 0), f"{location}.priority"),
                routing_weight=_positive_float(
                    raw.get("routing_weight", 1.0), f"{location}.routing_weight"
                ),
                tags=tuple(dict.fromkeys(tag.strip() for tag in tags_value if tag.strip())),
                replica_group=_optional_nonempty_string(raw, "replica_group", location),
            )
        )

    if not models and not any(endpoint.discover for endpoint in endpoints.values()):
        raise ConfigError("models must be a non-empty array unless an endpoint enables discovery")
    policy = _parse_policy(_mapping(root.get("router", {}), "router"))
    return RouterConfig(
        endpoints=endpoints,
        models=tuple(models),
        policy=policy,
        source_path=source_path,
    )


def _parse_policy(raw: Mapping[str, Any]) -> PolicyConfig:
    strategies = {name: dict(weights) for name, weights in DEFAULT_STRATEGIES.items()}
    custom_raw = _mapping(raw.get("strategies", {}), "router.strategies")
    for name, weights_value in custom_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("router.strategies names must be non-empty strings")
        weights_raw = _mapping(weights_value, f"router.strategies.{name}")
        unknown = set(weights_raw) - {
            "quality",
            "capability",
            "reliability",
            "latency",
            "cost",
            "load",
            "priority",
        }
        if unknown:
            raise ConfigError(
                f"router.strategies.{name} contains unknown weights: {', '.join(sorted(unknown))}"
            )
        weights = {key: _nonnegative_float(value, f"router.strategies.{name}.{key}") for key, value in weights_raw.items()}
        if not weights or sum(weights.values()) <= 0:
            raise ConfigError(f"router.strategies.{name} must have a positive total weight")
        strategies[name] = weights

    default_strategy = str(raw.get("default_strategy", "quality"))
    if default_strategy not in strategies:
        raise ConfigError(f"router.default_strategy references unknown strategy '{default_strategy}'")
    return PolicyConfig(
        default_strategy=default_strategy,
        strategies=strategies,
        max_attempts=_positive_int(raw.get("max_attempts", 3), "router.max_attempts"),
        capability_threshold=_bounded_float(
            raw.get("capability_threshold", 0.5), "router.capability_threshold"
        ),
        circuit_breaker_failures=_positive_int(
            raw.get("circuit_breaker_failures", 3), "router.circuit_breaker_failures"
        ),
        circuit_breaker_cooldown_seconds=_positive_float(
            raw.get("circuit_breaker_cooldown_seconds", 30.0),
            "router.circuit_breaker_cooldown_seconds",
        ),
        latency_ewma_alpha=_bounded_float(
            raw.get("latency_ewma_alpha", 0.25), "router.latency_ewma_alpha"
        ),
        diversify_fallbacks=_boolean(
            raw.get("diversify_fallbacks", True), "router.diversify_fallbacks"
        ),
        health_check_interval_seconds=_positive_float(
            raw.get("health_check_interval_seconds", 15.0),
            "router.health_check_interval_seconds",
        ),
        health_check_timeout_seconds=_positive_float(
            raw.get("health_check_timeout_seconds", 2.0),
            "router.health_check_timeout_seconds",
        ),
    )


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{location} must be an object/table")
    return value


def _required_string(raw: Mapping[str, Any], key: str, location: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(raw: Mapping[str, Any], key: str, location: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{location}.{key} must be a string")
    return value


def _optional_nonempty_string(raw: Mapping[str, Any], key: str, location: str) -> str | None:
    value = _optional_string(raw, key, location)
    if value is not None:
        if not value.strip():
            raise ConfigError(f"{location}.{key} must be a non-empty string")
        return value.strip()
    return None


def _boolean(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{location} must be true or false")
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{location} must be an integer")
    return value


def _positive_int(value: Any, location: str) -> int:
    parsed = _integer(value, location)
    if parsed <= 0:
        raise ConfigError(f"{location} must be greater than zero")
    return parsed


def _float(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{location} must be a number")
    return float(value)


def _optional_positive_int(value: object, location: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{location} must be a positive whole number")
    return value


def _positive_float(value: Any, location: str) -> float:
    parsed = _float(value, location)
    if parsed <= 0:
        raise ConfigError(f"{location} must be greater than zero")
    return parsed


def _nonnegative_float(value: Any, location: str) -> float:
    parsed = _float(value, location)
    if parsed < 0:
        raise ConfigError(f"{location} must be zero or greater")
    return parsed


def _optional_nonnegative_float(value: Any, location: str) -> float | None:
    if value is None:
        return None
    return _nonnegative_float(value, location)


def _bounded_float(value: Any, location: str) -> float:
    parsed = _float(value, location)
    if not 0 <= parsed <= 1:
        raise ConfigError(f"{location} must be between 0 and 1")
    return parsed
