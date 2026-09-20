"""MCP tools backed by the source-agnostic router."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from typing import Any

from .bootstrap import bootstrap_router
from .errors import AllModelsFailed, NoEligibleModel, RouterError
from .provisioning import OllamaProvisioner
from .router import LLMRouter
from .schema import QueryRequest


def create_server(config_path: str | None = None, *, discovery: bool = True) -> Any:
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised only in minimal installs.
        raise RuntimeError(
            "The MCP SDK is not installed. Reinstall source-agnostic-llm-router with dependencies."
        ) from exc

    server = MCPServer(
        "llm-router",
        version="0.3.9",
        instructions=(
            "Use route_llm_query to compare deployments without sending data upstream. "
            "Use ask_best_llm only when the user wants the query sent to an external model. "
            "Local runtimes and credential-backed cloud providers are enrolled automatically. "
            "Use provision_local_llm only when installation is requested or no suitable local "
            "model exists and the user permits a download. "
            "Pass hard modality, context, and budget constraints explicitly."
        ),
    )
    local_read = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
    external_call = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    provisioning_call = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    cached_router: LLMRouter | None = None
    router_lock = asyncio.Lock()

    async def router() -> LLMRouter:
        nonlocal cached_router
        if cached_router is None:
            async with router_lock:
                if cached_router is None:
                    cached_router = (
                        await bootstrap_router(config_path, discovery=discovery)
                    ).router
        return cached_router

    @server.tool(
        title="Route an LLM query",
        annotations=local_read,
    )
    async def route_llm_query(
        query: str,
        required_capabilities: list[str] | None = None,
        strategy: str | None = None,
        min_context_window: int | None = None,
        max_input_cost_per_million: float | None = None,
        max_output_cost_per_million: float | None = None,
        max_tokens: int = 1024,
        top_k: int = 3,
        preferred_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rank eligible LLM deployments locally; this does not send the query upstream."""

        try:
            request = QueryRequest.from_prompt(
                query,
                required_capabilities=required_capabilities or (),
                strategy=strategy,
                min_context_window=min_context_window,
                max_input_cost_per_million=max_input_cost_per_million,
                max_output_cost_per_million=max_output_cost_per_million,
                max_tokens=max_tokens,
                preferred_tags=tuple(preferred_tags or ()),
            )
            decision = (await router()).route(request)
            return {"ok": True, **decision.to_dict(top_k=max(1, top_k))}
        except NoEligibleModel as exc:
            return {"ok": False, "error": str(exc), "excluded": exc.excluded}
        except RouterError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        title="Ask the best LLM",
        annotations=external_call,
    )
    async def ask_best_llm(
        query: str,
        system: str | None = None,
        required_capabilities: list[str] | None = None,
        strategy: str | None = None,
        min_context_window: int | None = None,
        max_input_cost_per_million: float | None = None,
        max_output_cost_per_million: float | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        preferred_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send a query to the best eligible deployment with cross-source failover."""

        try:
            request = QueryRequest.from_prompt(
                query,
                system=system,
                required_capabilities=required_capabilities or (),
                strategy=strategy,
                min_context_window=min_context_window,
                max_input_cost_per_million=max_input_cost_per_million,
                max_output_cost_per_million=max_output_cost_per_million,
                max_tokens=max_tokens,
                temperature=temperature,
                preferred_tags=tuple(preferred_tags or ()),
            )
            result = await (await router()).complete(request)
            return {"ok": True, **result.to_dict()}
        except NoEligibleModel as exc:
            return {"ok": False, "error": str(exc), "excluded": exc.excluded}
        except AllModelsFailed as exc:
            return {
                "ok": False,
                "error": str(exc),
                "failures": [failure.to_dict() for failure in exc.failures],
            }
        except RouterError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        title="List LLM deployments",
        annotations=local_read,
    )
    async def list_llm_models(include_disabled: bool = False) -> dict[str, Any]:
        """List enrolled deployments, capabilities, prices, and current availability."""

        try:
            return {
                "ok": True,
                "models": (await router()).list_models(include_disabled=include_disabled),
            }
        except RouterError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        title="Get LLM router status",
        annotations=local_read,
    )
    async def llm_router_status() -> dict[str, Any]:
        """Show router configuration diagnostics and process-local health state."""

        try:
            instance = await router()
            return {"ok": True, "diagnostics": instance.diagnostics(), **instance.status()}
        except RouterError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        title="Provision a local Ollama model",
        annotations=provisioning_call,
    )
    async def provision_local_llm(
        dry_run: bool = True,
        model: str | None = None,
        allow_remote: bool = False,
    ) -> dict[str, Any]:
        """Assess resources or install one bounded, tool-capable Ollama model tier."""

        nonlocal cached_router
        report = await OllamaProvisioner().provision(
            dry_run=dry_run,
            requested_model=model,
            allow_remote=allow_remote,
        )
        if report.status == "installed":
            async with router_lock:
                cached_router = None
        return report.to_dict()

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-router-mcp")
    parser.add_argument("--config", help="TOML/JSON config path")
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="Disable automatic local and credential-backed provider enrollment",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Use stdio transport (the default; accepted for explicit plugin manifests)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    parser.add_argument("--http-path", default="/mcp", help="Streamable HTTP path")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["LLM_ROUTER_CONFIG"] = args.config
    if args.stdio and args.transport != "stdio":
        parser.error("--stdio cannot be combined with --transport streamable-http")
    server = (
        create_server(args.config, discovery=False)
        if args.no_discovery
        else create_server(args.config)
    )
    if args.transport == "streamable-http":
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.http_path,
            stateless_http=True,
            json_response=True,
        )
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
