---
name: route-llm-query
description: Select, prioritize, provision, compare, or call the best eligible language model through the LLM Router MCP server. Use when a user asks to route or delegate a prompt, choose among LLMs/providers/endpoints, prefer local or named sources, optimize capability, quality, cost, latency, context, or availability, install a suitable missing local Ollama model, or retry across independent sources.
---

# Route LLM Query

Route a provider-neutral request through the enrolled deployment fleet. The server automatically discovers common local runtimes and cloud providers with credentials, then merges explicit configuration over its inferred metadata. Treat its returned deployment metadata as authoritative for the current process.

## Workflow

1. Preserve the user's task and constraints. Convert only explicit must-haves into `required_capabilities`; allow the router's local analyzer to treat other signals as soft preferences.
2. Choose `quality` when capability is paramount, `balanced` for ordinary work, `cost` for budget-sensitive work, `latency` for interactive speed, or `priority` when configured source order should dominate. Pass `preferred_tags` such as `local`, `cloud`, or `ollama` for a soft per-request preference.
3. Call `route_llm_query` when the user wants a recommendation, comparison, explanation, or dry run. This ranks locally and does not transmit the prompt upstream.
4. Call `ask_best_llm` when the user asks to send, answer, delegate, or execute the request. This transmits the prompt to the selected source and automatically fails over.
5. Report the selected deployment and endpoint with the answer. Mention failed attempts only when failover occurred or the user asks for diagnostics.
6. If no suitable local model exists, call `provision_local_llm` with `dry_run=true` to report the CPU, memory, storage decision. Set `dry_run=false` only when the user requested installation or has already authorized automatic provisioning.

## Constraint Mapping

- Map code implementation, debugging, or review to `coding` when it is a hard requirement.
- Map proofs, deep analysis, architecture, or difficult planning to `reasoning`.
- Map supplied images or screenshots to `vision`.
- Map function/API use to `tool_use` and schema-constrained results to `structured_output`.
- Pass `min_context_window` when the prompt plus expected answer needs a known context floor.
- Pass cost ceilings only when prices are configured; unknown prices are excluded under a hard cost ceiling.
- Use `max_tokens` for the requested answer budget, not the upstream context size.

## Safety and Failure Handling

- Do not send credentials, private keys, or unrelated sensitive context to `ask_best_llm`. Redact or ask the user before transmission when the request contains secrets not needed by the task.
- Do not manually repeat the same call after an upstream failure; the server already attempts diversified fallbacks and isolates failed discovery probes.
- Do not force remote provisioning based on local resource measurements. Keep `allow_remote=false` unless the user explicitly accepts that the remote Ollama host must enforce its own limits.
- Call `llm_router_status` after a discovery, credential, health, or exhaustion error. Call `list_llm_models` when the user asks what self-enrolled or configured models are available.
- Do not claim that a deployment is globally "best." State that it ranked highest for the supplied constraints and current process-local health.

Read [references/configuration.md](references/configuration.md) only for setup, custom adapters, or troubleshooting.
