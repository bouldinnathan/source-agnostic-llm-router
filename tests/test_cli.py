from __future__ import annotations

import json
from pathlib import Path

from llm_router.cli import main


def test_route_cli_emits_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": {
                    "local": {
                        "adapter": "ollama-chat",
                        "base_url": "http://localhost:11434",
                    }
                },
                "models": [
                    {
                        "id": "local-model",
                        "endpoint": "local",
                        "upstream_model": "model",
                        "capabilities": {"general": 1.0},
                    }
                ],
            }
        )
    )

    exit_code = main(
        [
            "--config",
            str(path),
            "--json",
            "--no-discovery",
            "route",
            "hello",
            "--top-k",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["selected"]["deployment"] == "local-model"


def test_check_reports_missing_auth_definition(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": {
                    "remote": {
                        "adapter": "openai-chat",
                        "base_url": "https://example.invalid/v1",
                    }
                },
                "models": [
                    {"id": "model", "endpoint": "remote", "upstream_model": "model"}
                ],
            }
        )
    )

    exit_code = main(["--config", str(path), "--json", "--no-discovery", "check"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["ok"] is False
    assert "auth.key_env" in payload["errors"][0]
