from __future__ import annotations

import sys
from types import ModuleType

from llm_router import mcp_server


class FakeAnnotations:
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.values = kwargs


class FakeMCPServer:
    def __init__(self, name, **kwargs):  # type: ignore[no-untyped-def]
        self.name = name
        self.options = kwargs
        self.registered = {}

    def tool(self, **metadata):  # type: ignore[no-untyped-def]
        def decorator(function):  # type: ignore[no-untyped-def]
            self.registered[function.__name__] = {"function": function, **metadata}
            return function

        return decorator


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)


def test_create_server_registers_five_annotated_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    types_module = ModuleType("mcp.types")
    server_module.MCPServer = FakeMCPServer  # type: ignore[attr-defined]
    types_module.ToolAnnotations = FakeAnnotations  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)

    server = mcp_server.create_server("unused.toml")

    assert server.name == "llm-router"
    assert server.options["version"] == "0.3.10"
    assert set(server.registered) == {
        "route_llm_query",
        "ask_best_llm",
        "list_llm_models",
        "llm_router_status",
        "provision_local_llm",
    }
    route_annotations = server.registered["route_llm_query"]["annotations"].values
    ask_annotations = server.registered["ask_best_llm"]["annotations"].values
    provision_annotations = server.registered["provision_local_llm"]["annotations"].values
    assert route_annotations["read_only_hint"] is True
    assert route_annotations["open_world_hint"] is True
    assert ask_annotations["read_only_hint"] is False
    assert ask_annotations["open_world_hint"] is True
    assert provision_annotations["read_only_hint"] is False
    assert provision_annotations["destructive_hint"] is False


def test_streamable_http_transport_options(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = FakeRunner()
    monkeypatch.setattr(mcp_server, "create_server", lambda _: runner)

    exit_code = mcp_server.main(
        [
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.2",
            "--port",
            "9876",
            "--http-path",
            "/router-mcp",
        ]
    )

    assert exit_code == 0
    assert runner.calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.2",
            "port": 9876,
            "streamable_http_path": "/router-mcp",
            "stateless_http": True,
            "json_response": True,
        }
    ]
