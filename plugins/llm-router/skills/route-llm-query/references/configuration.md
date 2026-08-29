# Router Configuration Reference

Use this reference only when configuring or troubleshooting the router.

## Automatic enrollment

No config file is required. By default the router probes common loopback ports for Ollama, LM Studio, vLLM, llama.cpp/LocalAI, text-generation-webui, KoboldCpp, and Jan. It also enrolls supported hosted providers when their standard API-key environment variable is present.

- Add specific local or remote servers with comma-separated `LLM_ROUTER_DISCOVERY_URLS`. Prefix entries with `ollama=` or `openai=` when auto-detection would be ambiguous.
- Opt into bounded LAN probing with `LLM_ROUTER_SCAN_CIDRS`; LAN scanning never occurs by default.
- Disable all discovery with `LLM_ROUTER_DISCOVERY=0` or the relevant `--no-discovery` flag.
- Inspect enrollment with `llm-router --json discover --failures`.
- Order automatically enrolled sources with `LLM_ROUTER_SOURCE_PRIORITY=local,anthropic,openai,cloud`, then use strategy `priority` or Home Assistant model `auto:priority`.

Discovery refreshes keep the last working registry when a refresh fails. Explicit configuration is merged over discovered entries and remains the source of truth for overrides.

## Resource-aware provisioning

When loopback Ollama is reachable but has no tool-capable chat model, the persistent gateway can install a bounded Qwen 3.5 tier after checking available CPU, memory, and free storage. Run `llm-router provision --dry-run` before a manual download and `llm-router provision` to execute it.

- Set `LLM_ROUTER_AUTO_PROVISION=0` or pass `serve --no-provision` to disable it.
- Set `LLM_ROUTER_PROVISION_PRIORITY` to `balanced`, `quality`, or `smallest`.
- Override the ordered candidates with `LLM_ROUTER_PROVISION_MODELS`.
- Bound the artifact with `LLM_ROUTER_PROVISION_MAX_GB` and retained free space with `LLM_ROUTER_PROVISION_DISK_RESERVE_GB`.
- Keep remote provisioning disabled unless explicitly accepted; the router cannot measure another machine's CPU, memory, or disk through the Ollama API.

Provisioning failures remain status data and do not remove healthy cloud or local deployments.

## Optional configuration

The MCP server and CLI check, in order:

1. An explicit `--config` path.
2. `LLM_ROUTER_CONFIG`.
3. `router.toml` or `router.json` in the current directory.
4. `~/.config/llm-router/router.toml` or `router.json`.

Start from `config/router.example.toml` in the package repository. Set secrets in environment variables named by `auth.key_env`; never place secret values in TOML.

## Unified HTTP gateway

Run `llm-router serve --host 0.0.0.0 --port 8088` to expose Ollama-compatible `/api/*` and OpenAI-compatible `/v1/*` routes. Set `LLM_ROUTER_GATEWAY_API_KEY` before binding beyond loopback. Home Assistant should use its Ollama integration and one of `auto`, `auto:priority`, `auto:local`, or the quality/cost/latency variants; tool-bearing requests automatically require a tool-capable deployment.

## Built-in adapters

- `openai-responses`: OpenAI Responses-compatible JSON APIs.
- `openai-chat` or `openai-compatible`: Chat Completions-compatible APIs.
- `anthropic-messages`: Anthropic Messages-compatible APIs.
- `gemini-generate`: Gemini `generateContent`-compatible APIs.
- `ollama-chat`: Ollama chat endpoints.
- `generic-json`: Configurable JSON POST endpoints. Set field and response paths under the endpoint's `options` table.

Custom adapters may use `module:object` or register the `llm_router.adapters` Python entry-point group. The object must implement async `complete(endpoint, model, request)` and return `UpstreamResult`.

## Diagnostics

Run `llm-router --config PATH --json check` for explicit configuration, or `llm-router --json discover --failures` for enrollment. Missing credential environment variables are warnings; invalid adapters or auth definitions are errors. Use `llm_router_status` for circuit state after runtime failures.

Health is process-local. Restarting the MCP process resets latency averages, failure counts, and open circuits.
