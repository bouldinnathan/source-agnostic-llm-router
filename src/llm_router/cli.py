"""Command-line interface for routing, querying, and diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import sysconfig
from pathlib import Path
from typing import Any, Sequence

from .bootstrap import bootstrap_router
from .discovery import DiscoverySettings, ModelDiscovery
from .errors import AllModelsFailed, NoEligibleModel, RouterError
from .provisioning import OllamaProvisioner, ProvisioningSettings
from .schema import QueryRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-router",
        description="Route prompts to the most capable eligible LLM deployment.",
    )
    parser.add_argument(
        "--config",
        help="Optional TOML/JSON overrides (or set LLM_ROUTER_CONFIG)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="Use only explicit configuration; skip automatic local/cloud enrollment",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route", help="Rank deployments without sending the prompt")
    _add_query_arguments(route)
    route.add_argument("--top-k", type=int, default=3)

    ask = subparsers.add_parser("ask", help="Send the prompt with automatic failover")
    _add_query_arguments(ask)
    ask.add_argument("--system")
    ask.add_argument("--temperature", type=float)

    models = subparsers.add_parser("models", help="List enrolled deployments")
    models.add_argument("--all", action="store_true", help="Include disabled deployments")

    subparsers.add_parser("status", help="Show process-local health and circuit state")
    subparsers.add_parser("check", help="Validate config, adapters, and credential presence")
    discover = subparsers.add_parser(
        "discover", help="Probe and display automatically enrolled model sources"
    )
    discover.add_argument(
        "--failures", action="store_true", help="Include unreachable default probes"
    )

    provision = subparsers.add_parser(
        "provision",
        help="Install a resource-appropriate tool-capable model into local Ollama",
    )
    provision.add_argument("--dry-run", action="store_true", help="Assess without downloading")
    provision.add_argument("--model", help="Request one bounded model tier explicitly")
    provision.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a pull through a non-loopback OLLAMA_HOST",
    )

    serve = subparsers.add_parser(
        "serve", help="Serve one Ollama/OpenAI-compatible auto-routing gateway"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8088)
    serve.add_argument("--refresh-seconds", type=float)
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--no-provision",
        action="store_true",
        help="Do not install a local model when Ollama has no tool-capable model",
    )
    subparsers.add_parser(
        "plugin-path", help="Print the bundled Codex plugin path; no router config is required"
    )
    return parser


def _add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", help="Prompt text, or - to read stdin")
    parser.add_argument(
        "--strategy",
        help="Routing objective, such as quality, balanced, cost, latency, or a custom strategy",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Hard capability requirement; may be repeated",
    )
    parser.add_argument("--min-context", type=int)
    parser.add_argument("--max-input-cost", type=float, metavar="USD_PER_MILLION")
    parser.add_argument("--max-output-cost", type=float, metavar="USD_PER_MILLION")
    parser.add_argument("--max-tokens", type=int, default=1_024)
    parser.add_argument(
        "--prefer",
        action="append",
        default=[],
        metavar="TAG",
        help="Soft-prioritize a source tag such as local, cloud, or ollama; may be repeated",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plugin-path":
        try:
            path = _plugin_path()
        except RouterError as exc:
            _emit_error(str(exc), as_json=args.json)
            return 2
        if args.json:
            print(json.dumps({"ok": True, "plugin_path": str(path)}, indent=2))
        else:
            print(path)
        return 0
    if args.command == "serve":
        from .gateway import main as gateway_main

        gateway_args = [
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--log-level",
            args.log_level,
        ]
        if args.config:
            gateway_args.extend(["--config", args.config])
        if args.no_discovery:
            gateway_args.append("--no-discovery")
        if args.refresh_seconds is not None:
            gateway_args.extend(["--refresh-seconds", str(args.refresh_seconds)])
        if args.no_provision:
            gateway_args.append("--no-provision")
        return gateway_main(gateway_args)
    if args.command == "provision":
        try:
            settings = ProvisioningSettings.from_env()
            report = asyncio.run(
                OllamaProvisioner(settings).provision(
                    dry_run=args.dry_run,
                    requested_model=args.model,
                    allow_remote=args.allow_remote or settings.allow_remote,
                )
            )
            _emit(report.to_dict(), as_json=args.json)
            if report.ok:
                return 0
            return 3 if report.status == "skipped" else 4
        except RouterError as exc:
            _emit_error(str(exc), as_json=args.json)
            return 2
    if args.command == "discover":
        try:
            settings = DiscoverySettings.from_env()
            if args.no_discovery:
                settings = DiscoverySettings(enabled=False)
            report = asyncio.run(ModelDiscovery(settings).discover())
            payload = {
                "ok": bool(report.config.models),
                **report.to_dict(include_failures=args.failures),
                "models": [
                    {
                        "deployment": model.id,
                        "endpoint": model.endpoint,
                        "upstream_model": model.upstream_model,
                        "quality": model.quality,
                        "priority": model.priority,
                        "capabilities": dict(model.capabilities),
                        "tags": list(model.tags),
                    }
                    for model in report.config.models
                ],
            }
            _emit(payload, as_json=args.json)
            return 0 if payload["ok"] else 3
        except RouterError as exc:
            _emit_error(str(exc), as_json=args.json)
            return 2
    try:
        bootstrapped = asyncio.run(
            bootstrap_router(args.config, discovery=not args.no_discovery)
        )
        router = bootstrapped.router
        if args.command == "route":
            decision = router.route(_request_from_args(args))
            payload = {"ok": True, **decision.to_dict(top_k=max(1, args.top_k))}
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "ask":
            result = asyncio.run(router.complete(_request_from_args(args, system=args.system)))
            payload = {"ok": True, **result.to_dict()}
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "models":
            payload = {"ok": True, "models": router.list_models(include_disabled=args.all)}
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "status":
            payload = {"ok": True, **router.status()}
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "check":
            payload = router.diagnostics()
            _emit(payload, as_json=args.json)
            return 0 if payload["ok"] else 2
    except NoEligibleModel as exc:
        _emit_error(str(exc), details={"excluded": exc.excluded}, as_json=args.json)
        return 3
    except AllModelsFailed as exc:
        _emit_error(
            str(exc),
            details={"failures": [item.to_dict() for item in exc.failures]},
            as_json=args.json,
        )
        return 4
    except RouterError as exc:
        _emit_error(str(exc), as_json=args.json)
        return 2
    return 0


def _plugin_path() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "plugins" / "llm-router"
    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "source-agnostic-llm-router"
        / "plugins"
        / "llm-router"
    )
    for candidate in (source_tree, installed):
        if (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate
    raise RouterError("Bundled Codex plugin files were not found")


def _request_from_args(args: argparse.Namespace, *, system: str | None = None) -> QueryRequest:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    return QueryRequest.from_prompt(
        prompt,
        system=system,
        required_capabilities=args.require,
        strategy=args.strategy,
        min_context_window=args.min_context,
        max_input_cost_per_million=args.max_input_cost,
        max_output_cost_per_million=args.max_output_cost,
        max_tokens=args.max_tokens,
        temperature=getattr(args, "temperature", None),
        preferred_tags=tuple(dict.fromkeys(args.prefer)),
    )


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if "text" in payload:
        print(payload["text"])
        print(
            f"\n[routed to {payload['deployment']} via {payload['endpoint']}; "
            f"score={payload['routing_score']}]"
        )
    elif "selected" in payload:
        selected = payload["selected"]
        print(
            f"{selected['deployment']} via {selected['endpoint']} "
            f"(score {selected['score']})"
        )
        for candidate in payload.get("candidates", [])[1:]:
            print(
                f"  fallback: {candidate['deployment']} via {candidate['endpoint']} "
                f"(score {candidate['score']})"
            )
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _emit_error(
    message: str,
    *,
    details: dict[str, Any] | None = None,
    as_json: bool,
) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details:
        payload.update(details)
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"error: {message}", file=sys.stderr)
        if details:
            print(json.dumps(details, indent=2, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
