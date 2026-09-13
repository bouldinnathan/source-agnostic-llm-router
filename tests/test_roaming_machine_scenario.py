"""The two-machine agent/HA scenario, exercising all production routing layers.

Only HTTP transport is simulated: discovery, health checks, config reloads,
gateway requests, ranking, failover, and both backend adapters run normally.
The fake DNS map represents an address update; it does not test an actual VPN
or the operating system's DNS resolver.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from llm_router.discovery import DiscoverySettings
from llm_router.gateway import RouterGateway, create_app
from llm_router.provisioning import ProvisioningSettings


OLD_ADDRESS = "192.0.2.10"
NEW_ADDRESS = "198.51.100.20"
BACKUP_ADDRESS = "192.0.2.11"
PREFERRED_ALIAS = "qwen-golemframe"
FILE_CONTENTS = "The task file says to continue after the laptop reconnects."
TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]


class RoamingFleet:
    """An Ollama laptop and LM Studio replica with independently changing links."""

    def __init__(self) -> None:
        self.dns = {"golemframe.vpn.test": OLD_ADDRESS}
        self.addresses = {"golemframe": OLD_ADDRESS, "pantheon": BACKUP_ADDRESS}
        self.online = {"golemframe": True, "pantheon": True}
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        address = self.dns.get(request.url.host, request.url.host)
        machine = next((name for name, ip in self.addresses.items() if ip == address), None)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append({
            "machine": machine, "address": address, "path": path,
            "method": request.method, "body": body,
        })
        if machine is None or not self.online[machine]:
            raise httpx.ConnectError("simulated machine offline", request=request)

        if machine == "golemframe" and path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": "qwen"}]})
        if machine == "golemframe" and path == "/api/show":
            return httpx.Response(200, json={
                "capabilities": ["completion", "tools"],
                "model_info": {"context_length": 32768},
            })
        if machine == "pantheon" and path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "qwen", "capabilities": ["completion", "tools"]},
                # This remains available even if the Qwen group cannot serve.
                {"id": "unrelated-coder-32b", "capabilities": ["completion", "tools"]},
            ]})

        expected_path = "/api/chat" if machine == "golemframe" else "/v1/chat/completions"
        if path != expected_path or request.method != "POST":
            return httpx.Response(404, json={"error": "unexpected API path"})

        finished_tool = any(message["role"] == "tool" for message in body["messages"])
        message = {"role": "assistant", "content": "Continued using the tool result." if finished_tool else ""}
        if not finished_tool:
            function = {"name": "read_file", "arguments": {"path": "task.txt"}}
            if machine == "golemframe":
                message["tool_calls"] = [{"function": function}]
            else:
                message["tool_calls"] = [{
                    "id": "call_read", "type": "function",
                    "function": {**function, "arguments": json.dumps(function["arguments"])},
                }]

        if machine == "golemframe":
            return httpx.Response(200, json={"message": message, "done": True})
        return httpx.Response(200, json={
            "choices": [{"message": message, "finish_reason": "stop" if finished_tool else "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        })

    def generations_since(self, index: int) -> list[dict]:
        return [call for call in self.calls[index:] if call["path"] in {"/api/chat", "/v1/chat/completions"}]


def write_config(path: Path, primary_host: str) -> None:
    path.write_text(f"""
[router]
max_attempts = 3
health_check_interval_seconds = 3600
circuit_breaker_failures = 3

[endpoints.golemframe]
adapter = "ollama-chat"
base_url = "http://{primary_host}:11434"
machine_id = "golemframe"
discover = true

[endpoints.pantheon]
adapter = "openai-compatible"
base_url = "http://{BACKUP_ADDRESS}:1234/v1"
machine_id = "pantheon"
discover = true
auth = {{ scheme = "none" }}
""", encoding="utf-8")


def assert_tool_history(call: dict) -> None:
    """Check actual wire-format tool association, not just the neutral request."""
    messages = call["body"]["messages"]
    assert messages[0] == {"role": "user", "content": "Read task.txt and continue."}
    invocation = messages[1]["tool_calls"][0]
    arguments = invocation["function"]["arguments"]
    if call["machine"] == "pantheon":
        assert isinstance(arguments, str)
        arguments = json.loads(arguments)
    else:
        assert isinstance(arguments, dict)
    assert invocation["function"]["name"] == "read_file"
    assert arguments == {"path": "task.txt"}
    assert messages[2]["role"] == "tool"
    assert messages[2]["content"] == FILE_CONTENTS
    assert call["body"]["tools"] == TOOLS
    if call["machine"] == "pantheon":
        assert messages[2]["tool_call_id"] == invocation["id"]
    else:
        assert messages[2]["tool_name"] == "read_file"


@pytest.mark.parametrize("client_api", ["ollama", "openai"])
@pytest.mark.parametrize("address_change", ["dns", "configured_ip"])
def test_agent_keeps_alias_through_outage_roaming_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_api: str, address_change: str,
) -> None:
    fleet = RoamingFleet()
    transport = httpx.MockTransport(fleet)
    real_client = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        # Production callers construct clients themselves. Intercept only their
        # transport, retaining real HTTP serialization and every routing layer.
        if kwargs.get("transport") is None:
            kwargs["transport"] = transport
        kwargs["trust_env"] = False
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_API_KEY", raising=False)
    config_path = tmp_path / "roaming.toml"
    primary_host = "golemframe.vpn.test" if address_change == "dns" else OLD_ADDRESS
    write_config(config_path, primary_host)
    original_config = config_path.read_text(encoding="utf-8")

    gateway = RouterGateway(
        config_path=str(config_path),
        settings=DiscoverySettings(include_loopback=False, include_cloud=False, refresh_seconds=3600),
        provisioning_settings=ProvisioningSettings(enabled=False),
    )
    app = create_app(gateway=gateway)
    chat_path = "/api/chat" if client_api == "ollama" else "/v1/chat/completions"
    model_path = "/api/tags" if client_api == "ollama" else "/v1/models"

    async def scenario() -> None:
        # Long background intervals eliminate timing races. Explicit checks run
        # the real production cycle at each controlled network transition.
        async with app.router.lifespan_context(app):
            async with real_client(
                transport=httpx.ASGITransport(app), base_url="http://router.test",
            ) as client:
                history = [{"role": "user", "content": "Read task.txt and continue."}]

                async def ask(alias=PREFERRED_ALIAS, expected=200) -> httpx.Response:
                    response = await client.post(chat_path, json={
                        "model": alias, "stream": False, "messages": history, "tools": TOOLS,
                    })
                    assert response.status_code == expected, response.text
                    if expected == 200:
                        assert response.json()["model"] == alias
                    return response

                async def catalog() -> set[str]:
                    response = await client.get(model_path)
                    assert response.status_code == 200
                    data = response.json()
                    return {row["model"] for row in data["models"]} if client_api == "ollama" else {row["id"] for row in data["data"]}

                async def deployments() -> set[tuple[str, str]]:
                    response = await client.get("/router/status")
                    status = response.json()["router"]
                    assert status["endpoint_count"] == 2
                    return {(model["endpoint"], model["deployment"]) for model in status["deployments"]}

                baseline_catalog = await catalog()
                baseline_deployments = await deployments()
                assert {"qwen-ha", "qwen-golemframe", "qwen-pantheon", "qwen-golemframe-nofailover", "qwen-pantheon-nofailover"} <= baseline_catalog

                # Agent starts on the preferred Ollama machine and executes a tool.
                initial = await ask()
                assert initial.json()["router"]["endpoint"] == "golemframe"
                message = initial.json()["message"] if client_api == "ollama" else initial.json()["choices"][0]["message"]
                history.append(message)
                tool_result = {"role": "tool", "content": FILE_CONTENTS}
                if client_api == "ollama":
                    tool_result["tool_name"] = "read_file"
                else:
                    tool_result["tool_call_id"] = message["tool_calls"][0]["id"]
                history.append(tool_result)

                # The link drops before the next health check. This same request
                # must retry on LM Studio with correctly translated tool history.
                fleet.online["golemframe"] = False
                before = len(fleet.calls)
                fallback = await ask()
                calls = fleet.generations_since(before)
                assert [call["machine"] for call in calls] == ["golemframe", "pantheon"]
                assert fallback.json()["router"]["endpoint"] == "pantheon"
                assert_tool_history(calls[-1])

                await gateway.check_health()
                assert (await client.post("/router/discover")).status_code == 200
                assert await catalog() == baseline_catalog
                assert await deployments() == baseline_deployments
                assert (await client.get("/readyz")).status_code == 200
                for alias in (PREFERRED_ALIAS, "qwen-ha"):
                    before = len(fleet.calls)
                    await ask(alias)
                    assert [call["machine"] for call in fleet.generations_since(before)] == ["pantheon"]
                before = len(fleet.calls)
                await ask("qwen-golemframe-nofailover", expected=503)
                assert not fleet.generations_since(before)

                # The offline laptop moves networks. DNS mode changes no router
                # config; IP mode updates only its configured URL, never its ID.
                fleet.addresses["golemframe"] = NEW_ADDRESS
                if address_change == "dns":
                    fleet.dns["golemframe.vpn.test"] = NEW_ADDRESS
                else:
                    write_config(config_path, NEW_ADDRESS)
                moved_at = len(fleet.calls)
                assert (await client.post("/router/discover")).status_code == 200
                assert await catalog() == baseline_catalog
                assert await deployments() == baseline_deployments
                assert (await ask()).json()["router"]["endpoint"] == "pantheon"

                fleet.online["golemframe"] = True
                await gateway.check_health()
                assert await catalog() == baseline_catalog
                assert await deployments() == baseline_deployments
                for alias in (PREFERRED_ALIAS, "qwen-golemframe-nofailover"):
                    before = len(fleet.calls)
                    recovered = await ask(alias)
                    calls = fleet.generations_since(before)
                    assert recovered.json()["router"]["endpoint"] == "golemframe"
                    assert len(calls) == 1 and calls[0]["address"] == NEW_ADDRESS
                    assert_tool_history(calls[0])
                assert any(call["address"] == NEW_ADDRESS and call["path"] == "/api/tags" for call in fleet.calls[moved_at:])
                assert all(call["address"] != OLD_ADDRESS for call in fleet.calls[moved_at:])
                if address_change == "dns":
                    assert config_path.read_text(encoding="utf-8") == original_config

                # Machine preference and strict behavior work symmetrically.
                assert (await ask("qwen-pantheon")).json()["router"]["endpoint"] == "pantheon"
                fleet.online["pantheon"] = False
                await gateway.check_health()
                assert (await ask("qwen-pantheon")).json()["router"]["endpoint"] == "golemframe"
                before = len(fleet.calls)
                await ask("qwen-pantheon-nofailover", expected=503)
                assert not fleet.generations_since(before)

                # Total outage is explicit; recovery needs no client reconfiguration.
                fleet.online["golemframe"] = False
                await gateway.check_health()
                assert (await client.get("/readyz")).status_code == 503
                await ask("qwen-ha", expected=503)
                assert await catalog() == baseline_catalog
                fleet.online.update(golemframe=True, pantheon=True)
                await gateway.check_health()
                assert (await client.get("/readyz")).status_code == 200
                assert (await ask()).json()["router"]["endpoint"] == "golemframe"

                # No alias or unrelated model name ever went upstream as a model ID.
                assert all(call["body"]["model"] == "qwen" for call in fleet.generations_since(0))
                assert all(call["machine"] is not None for call in fleet.calls)

        assert gateway._health_task is None
        assert gateway._refresh_task is None

    asyncio.run(scenario())
