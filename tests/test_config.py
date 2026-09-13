from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.config import config_from_mapping, load_config
from llm_router.errors import ConfigError


def test_example_config_loads_twelve_deployments() -> None:
    config = load_config(Path("config/router.example.toml"))

    assert len(config.endpoints) == 9
    assert len(config.models) == 12
    assert config.policy.default_strategy == "quality"
    assert all(not model.enabled for model in config.models)


def test_json_config_and_auth_alias_load(tmp_path: Path) -> None:
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": {
                    "one": {
                        "adapter": "openai-chat",
                        "base_url": "https://example.invalid/v1",
                        "api_key_env": "ONE_KEY",
                    }
                },
                "models": [
                    {
                        "id": "one-model",
                        "endpoint": "one",
                        "model": "upstream-name",
                        "capabilities": {"coding": True, "vision": False},
                    }
                ],
            }
        )
    )

    config = load_config(path)

    assert config.endpoints["one"].auth.key_env == "ONE_KEY"
    assert config.models[0].upstream_model == "upstream-name"
    assert config.models[0].capabilities == {"coding": 1.0, "vision": 0.0}


def test_rejects_duplicate_deployment_ids() -> None:
    payload = {
        "endpoints": {"one": {"adapter": "ollama", "base_url": "http://localhost"}},
        "models": [
            {"id": "same", "endpoint": "one", "upstream_model": "a"},
            {"id": "same", "endpoint": "one", "upstream_model": "b"},
        ],
    }

    with pytest.raises(ConfigError, match="Duplicate"):
        config_from_mapping(payload)


def test_rejects_unknown_endpoint() -> None:
    with pytest.raises(ConfigError, match="unknown endpoint"):
        config_from_mapping(
            {
                "endpoints": {"one": {"adapter": "ollama"}},
                "models": [
                    {"id": "bad", "endpoint": "missing", "upstream_model": "model"}
                ],
            }
        )


def test_rejects_out_of_range_capability() -> None:
    with pytest.raises(ConfigError, match="between 0 and 1"):
        config_from_mapping(
            {
                "endpoints": {"one": {"adapter": "ollama"}},
                "models": [
                    {
                        "id": "bad",
                        "endpoint": "one",
                        "upstream_model": "model",
                        "capabilities": {"coding": 1.1},
                    }
                ],
            }
        )


def test_rejects_non_string_base_url() -> None:
    with pytest.raises(ConfigError, match="base_url must be a string"):
        config_from_mapping(
            {
                "endpoints": {"one": {"adapter": "ollama", "base_url": 123}},
                "models": [
                    {"id": "bad", "endpoint": "one", "upstream_model": "model"}
                ],
            }
        )


def test_endpoint_only_discovery_and_health_policy():
    config = config_from_mapping({
        "router": {"health_check_interval_seconds": 12, "health_check_timeout_seconds": 1.5},
        "endpoints": {"laptop": {"adapter": "ollama-chat", "base_url": "http://laptop.vpn:11434", "machine_id": "pantheon", "discover": True, "health_path": "/api/version"}},
    })
    assert not config.models
    assert config.endpoints["laptop"].machine_id == "pantheon"
    assert config.endpoints["laptop"].discover
    assert config.endpoints["laptop"].health_path == "/api/version"
    assert config.policy.health_check_interval_seconds == 12
    assert config.policy.health_check_timeout_seconds == 1.5


@pytest.mark.parametrize("field,value", [("machine_id", ""), ("machine_id", 1), ("discover", "true"), ("health_path", " ")])
def test_rejects_invalid_machine_settings(field, value):
    with pytest.raises(ConfigError):
        config_from_mapping({
            "endpoints": {"laptop": {"adapter": "ollama-chat", "discover": True, field: value}},
            "models": [{"id": "one", "endpoint": "laptop", "upstream_model": "qwen"}],
        })


def test_ha_example_produces_expected_aliases():
    from llm_router.aliases import build_aliases
    config = load_config("config/router.ha.example.toml")
    assert {"qwen-ha", "qwen-golemframe", "qwen-pantheon", "qwen-golemframe-nofailover", "qwen-pantheon-nofailover"} == set(build_aliases(config))
