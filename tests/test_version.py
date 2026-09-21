"""Published package and gateway version surfaces must agree."""

import asyncio
from pathlib import Path

import httpx

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from llm_router import __version__
from llm_router.gateway import VERSION, RouterGateway, create_app


def test_project_package_and_gateway_versions_match():
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project.open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    assert version == __version__ == VERSION == "0.6.0"


def test_public_status_and_ollama_api_report_release_version(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    app = create_app(gateway=RouterGateway(discovery=False))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://router.test") as client:
            health = await client.get("/healthz")
            assert health.status_code == 503  # Version is available even without models.
            assert health.json()["version"] == __version__
            ollama = await client.get("/api/version")
            assert ollama.status_code == 200
            assert ollama.json()["version"] == f"llm-router-{__version__}"

    asyncio.run(scenario())
