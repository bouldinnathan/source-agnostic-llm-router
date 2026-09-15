# Source-Agnostic LLM Router

An installable Python package, HTTP gateway, MCP server, and Codex skill that sends each query to the most capable eligible deployment—not to a hard-coded vendor. It automatically enrolls common local runtimes and credential-backed cloud providers, can provision a missing local Ollama model when host resources permit, and presents the fleet through one stable Ollama/OpenAI-compatible service.

```text
Home Assistant / OpenAI clients / CLI / Python / MCP
        |
        v
constraints + local task inference
        |
        v
capability / quality / health / latency / cost scoring
        |
        +--> source A, deployment 1
        +--> source B, deployment 1 replica   (failover)
        +--> source C, different model        (failover)
```

The dry-run routing path never sends the prompt upstream. The completion path attempts ranked deployments, tracks process-local latency and reliability, opens circuits after repeated failures, and prioritizes a different endpoint for each fallback. Discovery failures are isolated, and background refresh keeps the last working registry if a new scan fails.

## Install

Python 3.10 or newer is required, including Ubuntu 22.04's system Python. The MCP entry point uses the current v2 line of the official Python SDK.

### Copy-paste install with a background service (Linux/systemd)

Run this as your **normal user**, not with `sudo`. Requires `curl` and a normal
local/SSH login with a working systemd user session (Ubuntu 22.04+, Debian with
Python 3.10+, Arch, or Manjaro). The installer can use `sudo` for missing package
prerequisites and to enable startup at boot.

```bash
(
  set -eu
  router_installer="$(mktemp)"
  trap 'rm -f -- "$router_installer"' EXIT
  curl -fsSL https://raw.githubusercontent.com/bouldinnathan/source-agnostic-llm-router/main/install.sh -o "$router_installer"
  sh "$router_installer" --service --auto-update
)
```

This installs the latest `main`, including the HA and preferred-machine aliases,
and enables `llm-router.service` as a **systemd user service**. It starts immediately,
restarts after crashes, starts at boot, and continues after logout using systemd
lingering. The explicit `--auto-update` option also enables a daily software-update
timer. Omit `--auto-update` if you want manual software updates only. If lingering
cannot be enabled, installation reports an error with the
administrator command needed; it does not claim boot persistence succeeded.

Rerun the same block to update an older installation and enable the timer, or to
manually update the package and restart the service. Updates preserve
the service's environment file, API key, and optional `router.toml`. Changed unit
definitions are backed up alongside the unit; use `systemctl --user edit
llm-router.service` for persistent unit overrides. Updating this single proxy briefly
interrupts it; backend HA does not make proxy updates zero-downtime.

New service installs listen on **`http://127.0.0.1:8088`**; OpenAI clients use
**`http://127.0.0.1:8088/v1`**. A random client API key is saved in
`~/.config/llm-router/router.env` with owner-only permissions. If `XDG_CONFIG_HOME`
is set, that directory replaces `~/.config` for both settings and the user unit.
The service discovers local runtimes automatically and refreshes discovery every
30 seconds. It does not download models by default. An empty fleet is allowed;
`/healthz` reports unavailable until a usable backend/model is found.

```bash
# Edit settings and read the generated LLM_ROUTER_GATEWAY_API_KEY for your clients:
nano "${XDG_CONFIG_HOME:-$HOME/.config}/llm-router/router.env"
systemctl --user restart llm-router.service
systemctl --user status llm-router.service --no-pager
# Follow logs (Ctrl+C stops following, not the service):
journalctl --user -u llm-router.service -f
```

For your fleet, add this to `router.env`, substituting real DNS/VPN hostnames:

```ini
LLM_ROUTER_DISCOVERY_URLS="ollama@golemframe=http://golemframe.home.arpa:11434,openai@pantheon=http://pantheon.example-vpn:1234/v1"
```

To allow clients on other machines, also change `LLM_ROUTER_HOST=127.0.0.1` to
`LLM_ROUTER_HOST=0.0.0.0` in that file and restart the service. Keep the API key
enabled, restrict access to your trusted LAN/VPN with your firewall, and use TLS
when traffic is not protected by a trusted network/VPN. The installer does not
open firewall ports. Clients connect to `http://ROUTER_HOST:8088` (Ollama) or
`http://ROUTER_HOST:8088/v1` (OpenAI) and use the generated key. Shell `export`
commands do not configure an already-running service; put settings in `router.env`
(without `export`). You can also put explicit overrides in the same directory's
`router.toml`, or set `LLM_ROUTER_CONFIG` in `router.env` to an absolute config path.

### Automatic software updates

`sh install.sh --service --auto-update` opts into installing future commits from
the **official repository's `main` branch**, not just tagged releases. It creates
`llm-router-update.timer` and `llm-router-update.service` alongside the gateway's
user service. Existing installations do not acquire this behavior until you opt
in. This updates the router software and its Python dependencies, not Ollama,
LM Studio, your operating system, or backend model files.

The timer checks daily around midnight in the server's local timezone, with up to
one hour of randomized delay. Missed checks are caught up after downtime. An
unchanged commit causes no reinstall or restart. Network/download failures leave
the active runtime alone and are retried at the next scheduled check.

For a new commit, the updater installs that exact commit into a separate virtual
environment and checks imports and gateway startup without contacting your
backends. It switches runtimes only after these checks pass. If restarting the
gateway fails its process-stability checks, it restores the previous runtime and
attempts to restart it. These checks do not guarantee that every application-level
behavior in a new version is correct. Updates briefly interrupt a running proxy;
backend HA does not provide redundancy for the proxy itself. A deliberately
stopped gateway is not started by the updater.

Configuration and credentials are preserved. Previous runtimes are retained under
`~/.local/share/source-agnostic-llm-router/releases` (or your chosen installation
directory) for recovery and are not automatically pruned; allow disk space for
them. A shared lock prevents manual installs and scheduled updates from modifying
the same installation concurrently.

```bash
# Next scheduled check:
systemctl --user list-timers llm-router-update.timer --all
# Check/install now:
systemctl --user start llm-router-update.service
# Update logs:
journalctl --user -u llm-router-update.service -n 50 --no-pager
# Disable future checks, then cancel any in-progress update:
systemctl --user disable --now llm-router-update.timer
systemctl --user stop llm-router-update.service
```

Rerunning the installer without `--auto-update` does not disable an existing timer;
use the commands above. Local, editable, fork, wheel and explicitly pinned installs
are not eligible for automatic updates. Combining those sources with
`--auto-update` fails before package installation. If you later manually pin or
replace an auto-updated installation, the updater refuses to switch it back to
`main`; disable the timer as well to stop unnecessary scheduled checks.

To stop the gateway and disable future startup, disable/cancel updates as above,
then run `systemctl --user disable --now llm-router.service`. This keeps the
installation and configuration. Lingering is left enabled because other user
services may depend on it.

### Package only / pinned installs

Omit both `--service` and `--auto-update` from the copy-paste block to install only the commands, with no
service or lingering changes. Set `LLM_ROUTER_VERSION` when invoking the downloaded
installer to pin a Git tag or commit, for example
`LLM_ROUTER_VERSION=YOUR_COMMIT sh "$router_installer" --service`. A pinned revision
must include service support to use `--service`; the old `v0.3.0` tag predates both
the HA update and this service installer.

The installer creates an isolated virtual environment under `~/.local/share`, links
commands into `~/.local/bin`, and never installs router dependencies into system
Python. Ubuntu compatibility is continuously checked on 22.04 and 24.04 in
`.github/workflows/ubuntu.yml`. If `curl` is missing, install it with `sudo apt-get
install curl` (Ubuntu/Debian) or `sudo pacman -S --needed curl` (Arch/Manjaro).

To install the local wheel instead:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install ./dist/source_agnostic_llm_router-0.3.0-py3-none-any.whl
llm-router --json discover
```

No config file is required. Start a supported local runtime or export one of the provider credentials below; discovery enrolls its chat models. Explicit `router.toml` configuration remains available for metadata, policy, authentication, or endpoint overrides and is merged over automatic enrollment.

The virtual environment is required on distributions such as Arch Linux that mark the system Python as externally managed under PEP 668. Activate it again with `source .venv/bin/activate` in each new shell. Do not use `--break-system-packages` for this application.

For development:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Use it

### Automatic discovery

The default scan is bounded to well-known loopback services:

- Ollama on port `11434`;
- LM Studio on `1234`;
- vLLM on `8000`;
- llama.cpp or LocalAI on `8080`;
- text-generation-webui on `5000`, KoboldCpp on `5001`, and Jan on `1337`.

Cloud providers self-enroll only when their credential is present:

| Provider | Environment variable |
|---|---|
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Groq | `GROQ_API_KEY` |
| Together | `TOGETHER_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| xAI | `XAI_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |

Add known machines without maintaining model lists yourself:

```bash
export LLM_ROUTER_DISCOVERY_URLS="ollama=http://192.168.1.20:11434,openai=http://192.168.1.21:8000/v1"
llm-router --json discover --failures
```

`OLLAMA_HOST` and `OPENAI_BASE_URL` are also respected when those standard overrides are already present in the gateway process environment.

LAN-wide probing is never implicit. Opt into bounded scans with `LLM_ROUTER_SCAN_CIDRS=192.168.1.0/24`; `LLM_ROUTER_MAX_SCAN_HOSTS` defaults to 64. Set `LLM_ROUTER_DISCOVERY_REFRESH` to change the gateway refresh interval from 300 seconds.

Discovered context/tool metadata is used when the source reports it; remaining quality and capability values use conservative model-family/size heuristics. They are routing estimates, not live benchmarks. Override them in `router.toml` when exact fleet metadata matters.

### Resource-aware local model installation

When loopback Ollama is reachable but contains no tool-capable chat model, the persistent gateway starts a failure-isolated background pull. It measures available CPU, memory, and storage, retains 5 GiB of disk headroom by default, and will not exceed an 8 GiB artifact unless configured otherwise.

Assess or install explicitly:

```bash
llm-router provision --dry-run
llm-router provision
```

The bounded default choices are tool-capable [Qwen 3.5](https://ollama.com/library/qwen3.5) tiers from 0.8B through 27B. `balanced` selects the largest fitting tier through 9B, `quality` permits the 27B tier when the download cap and hardware allow it, and `smallest` minimizes resource use. Ollama documents model installation through [`POST /api/pull`](https://docs.ollama.com/api/pull).

```bash
export LLM_ROUTER_PROVISION_PRIORITY="quality"
export LLM_ROUTER_PROVISION_MAX_GB="20"
export LLM_ROUTER_PROVISION_MODELS="qwen3.5:9b,qwen3.5:4b,qwen3.5:2b"
```

Automatic provisioning is local-only because the router cannot measure another host through Ollama's API. Set `LLM_ROUTER_PROVISION_REMOTE=1` or use `provision --allow-remote` only when the remote server is expected to enforce its own limits. Disable downloads with `LLM_ROUTER_AUTO_PROVISION=0` or `serve --no-provision`. Failed pulls are reported in gateway status and never remove healthy discovered models.

### Prioritization

The normal strategies remain capability-aware. Use the `priority` strategy when operator order should dominate scoring:

```bash
export LLM_ROUTER_SOURCE_PRIORITY="local,anthropic,openai,cloud"
llm-router route --strategy priority --prefer local "Review this code"
```

Priority labels may be `local`, `cloud`, a provider such as `ollama`, or a discovered source name. Explicit TOML models can set integer `priority` and `routing_weight`. Hard requirements such as `tool_use`, vision, context, and cost ceilings always take precedence over preferences.

### Unified gateway and Home Assistant

Set an inbound key and start the persistent gateway on an address Home Assistant can reach:

```bash
export LLM_ROUTER_GATEWAY_API_KEY="replace-with-a-long-random-value"
llm-router serve --host 0.0.0.0 --port 8088
```

In Home Assistant, use its [official Ollama integration](https://www.home-assistant.io/integrations/ollama/):

1. Go to **Settings → Devices & services → Add integration → Ollama**.
2. Set URL to `http://ROUTER_HOST:8088`; do not use `localhost` unless the gateway runs inside the Home Assistant host/container.
3. Enter `LLM_ROUTER_GATEWAY_API_KEY` as the API key.
4. Add a Conversation or AI Task entry and select model `auto`.
5. When enabling Home Assistant control, expose only the intended entities. Requests containing Assist tools automatically require a tool-capable deployment.

Home Assistant sees virtual models: the `auto` presets plus model-specific HA, preferred-machine, and machine-only aliases described below. It does not need to know whether an answer came from Ollama, another LAN host, or a cloud provider. Response `router` metadata identifies the actual deployment for diagnostics. Use the Ollama path because Home Assistant's [official OpenAI integration](https://www.home-assistant.io/integrations/openai_conversation) intentionally accepts only the official OpenAI endpoint.

The gateway also serves OpenAI-compatible `GET /v1/models` and `POST /v1/chat/completions`, plus health and diagnostics at `/healthz` and authenticated `/router/status`.

### Model replicas, preferred machines, and HA

Every enabled model automatically gets an HA alias and two aliases for each machine
hosting it. Both the Ollama and OpenAI model lists advertise them. For a model group
named `qwen` on machines `golemframe` and `pantheon`:

| Client model | Routing behavior |
|---|---|
| `qwen-ha` | Rank matching replicas by latency, load, health, and capability; retry on failure. |
| `qwen-golemframe` | Try Golemframe first when eligible, then matching replicas elsewhere. |
| `qwen-pantheon` | Try Pantheon first when eligible, then matching replicas elsewhere. |
| `qwen-golemframe-nofailover` | Only Golemframe may receive the request; return 503 if it cannot serve. |
| `qwen-pantheon-nofailover` | Only Pantheon may receive the request; return 503 if it cannot serve. |

The default group is the exact upstream model name, including its version/size tag.
For example, `qwen3:14b` produces `qwen3-14b-ha`. Names are lowercased with punctuation
converted to hyphens. Distinct names are never merged just because their aliases
look alike: ambiguous aliases are omitted and reported under `alias_conflicts` in
`/router/status`.

Set `replica_group = "qwen"` on explicit model entries to use a short group name or
map equivalent models that Ollama and LM Studio advertise under different names.
This is an operator declaration of equivalence: verify weights, version, size, and
quantization yourself. Automatic matching by name is not a checksum verification.
All fallbacks must remain in the selected group and satisfy tool, vision, context,
and other request requirements. Machine preference overrides scoring only after
those hard requirements pass. The existing `auto` presets can still choose other
models across the fleet.

If several service endpoints share a `machine_id`, the machine aliases cover those
services together. `-nofailover` restricts the machine, not a particular port.
Requests keep their selected alias in responses, even when another replica answers.
Failover retries inference with the supplied history; the agent still owns tool
execution and must include conversation/tool results in subsequent requests. The
gateway does not migrate backend-owned sessions, files, or tool execution.

### Known machines, health checks, and changing IP addresses

Use a stable DNS or VPN hostname and give each machine a persistent name:

```bash
export LLM_ROUTER_DISCOVERY_URLS="ollama@golemframe=http://golemframe.home.arpa:11434,openai@pantheon=http://pantheon.example-vpn:1234/v1"
```

The `@name` is the router's machine identity. Changing its URL retains its deployment
IDs and aliases; keeping the hostname while its DNS address changes needs no router
configuration update. Plain `ollama=URL`/`openai=URL` still works, with machine labels
derived from the hostname or IP (`local` for loopback). Two distinct services of the
same protocol on one machine should use separate configured endpoint names and the
same `machine_id`.

An endpoints-only configuration can discover all models without maintaining lists:

```toml
[router]
health_check_interval_seconds = 15
health_check_timeout_seconds = 2

[endpoints.golemframe-ollama]
adapter = "ollama-chat"
base_url = "http://golemframe.home.arpa:11434"
machine_id = "golemframe"
discover = true

[endpoints.pantheon-studio]
adapter = "openai-compatible"
base_url = "http://pantheon.example-vpn:1234/v1"
machine_id = "pantheon"
discover = true
auth = { scheme = "none" }
```

Replace those hostnames with names resolvable from the router. For authenticated
servers, configure `auth`/`api_key_env` as with inference. See
[`config/router.ha.example.toml`](config/router.ha.example.toml) for an explicit
cross-runtime group example. Existing configured endpoints with enabled models are
also discovered when gateway discovery is enabled; explicit model metadata takes
precedence over discovered metadata, including disabled entries.

The gateway probes each enabled or explicitly discoverable endpoint's model-list
API every 15 seconds by default, with a 2-second timeout and bounded concurrency.
These checks verify API reachability without loading a model or making an inference
request. Failed endpoints are skipped until a probe succeeds. Model inference
failures retain their own circuit breakers: a successful model-list probe does not
prove generation will work or clear an inference circuit prematurely. Preferred
machines become eligible again on later requests after recovery; in-flight work
continues on its selected backend.

Health checks run even with `serve --no-discovery`. Model discovery separately runs
every `LLM_ROUTER_DISCOVERY_REFRESH` seconds (300 by default), and recovering known
endpoints trigger another scan promptly when discovery is enabled. Failed discovery
retains previously discovered model aliases while health marks the source offline.
Explicit discoverable sources can start offline and be enrolled when they return.
For custom/generic adapters, set an API-relative `health_path` (for example `/health`)
to enable probes; otherwise they use inference failures for health. Probe paths are
appended to `base_url`, and configured credentials/TLS settings apply.

There is no common machine UUID in the model-list APIs used by this router. Ollama's
[`digest`](https://docs.ollama.com/api/tags) identifies a model artifact, and
[LM Studio's API](https://lmstudio.ai/docs/developer/rest) exposes models and inference
state; those are not a cross-runtime machine identity. `machine_id` is a configured
label, not proof of the remote machine's identity. For a laptop/VPN, use stable DNS
or update its configured URL. The router does not guess that a newly seen IP belongs
to an old machine from model names, and a name alone cannot locate an unknown address.

Health timestamps and errors appear under `/router/status`; `/readyz` returns 503
when no enabled deployment is currently usable. Backend HA still requires a surviving
eligible replica within the configured retry budget and the client's timeout. The
gateway itself needs separate redundancy if its host must also tolerate failure.
Streaming responses are currently buffered until an entire upstream answer completes.

### CLI routing

Rank locally without sending the prompt:

```bash
llm-router route \
  --strategy quality \
  --require coding \
  --top-k 4 \
  "Review this concurrency bug and propose a patch"
```

Send the request with automatic cross-source failover:

```bash
llm-router ask \
  --strategy balanced \
  --require reasoning \
  --max-tokens 2000 \
  "Compare these two system designs"
```

Inspect configuration and runtime state:

```bash
llm-router discover
llm-router provision --dry-run
llm-router models
llm-router status
```

Python API:

```python
import asyncio

from llm_router import QueryRequest
from llm_router.bootstrap import bootstrap_router

async def main() -> None:
    router = (await bootstrap_router()).router
    request = QueryRequest.from_prompt(
        "Find the race condition in this code...",
        required_capabilities=("coding", "reasoning"),
        strategy="quality",
        max_tokens=2000,
    )

    decision = router.route(request)  # Ranking is local; discovery sent no prompt.
    answer = await router.complete(request)
    print(decision.candidates[0].model.id, answer.text)

asyncio.run(main())
```

## MCP and skill

Start the stdio server directly:

```bash
llm-router-mcp --stdio
```

Or expose one central Streamable HTTP endpoint (binds to loopback unless changed):

```bash
llm-router-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP URL is `http://127.0.0.1:8000/mcp`.

Put authentication and TLS in front of the server before binding it to a non-loopback interface.

It exposes five focused tools:

- `route_llm_query`: rank and explain choices without upstream transmission.
- `ask_best_llm`: execute with automatic failover.
- `list_llm_models`: inspect configured capabilities and prices.
- `llm_router_status`: inspect config, health, latency, and circuits.
- `provision_local_llm`: dry-run resource admission or install one bounded Ollama tier.

The installable Codex plugin is under `plugins/llm-router`. Its bundled `.mcp.json` launches the installed `llm-router-mcp` command and its `route-llm-query` skill teaches the agent when to dry-run versus transmit. The MCP server performs the same automatic enrollment; set `LLM_ROUTER_CONFIG` only when adding explicit overrides.

Wheels include the plugin and example config under the Python installation's `share/source-agnostic-llm-router` directory. Run `llm-router plugin-path` to print the exact plugin folder for either a source checkout or an installed wheel.

## Configuration model

An endpoint is a network source and adapter. A model entry is a deployable model at one endpoint. Discovery automatically creates separate deployment IDs for the same model at different IPs, keeping health, latency, and circuit state independent. Explicit entries with the same IDs override discovered metadata.

Hard constraints filter deployments before scoring:

- explicit capabilities and the configured capability threshold;
- context window and output limit;
- input/output price ceilings;
- enabled state, exclusions, and open circuits.

The selected strategy weights normalized quality, task-capability match, observed reliability, latency, estimated request cost, active load, and configured priority. Prompt classification is a local heuristic and only creates soft preferences. Explicit `required_capabilities` remain hard requirements.

Built-in adapters are `openai-responses`, `openai-chat`/`openai-compatible`, `anthropic-messages`, `gemini-generate`, `ollama-chat`, and `generic-json`. A third-party adapter can register the `llm_router.adapters` entry-point group or be referenced as `package.module:AdapterClass`; it implements async `complete(endpoint, model, request)` and returns `llm_router.schema.UpstreamResult`.

## Operational notes

- Runtime health is intentionally process-local; restarting resets learned latency and circuits.
- API health probes run independently of discovery and keep offline endpoints out of routing until recovery.
- Gateway discovery refreshes atomically. A total refresh failure leaves the previous router active and marks status as degraded.
- Individual unreachable discovery probes are reported but never cancel healthy probes.
- Provisioning checks CPU, available memory, free disk, artifact cap, and disk reserve before a local pull; failures remain isolated status data.
- Requests are sequential by default, so fallbacks do not multiply spend through speculative hedging.
- A hard cost ceiling excludes deployments whose price is unknown.
- Response bodies and credentials are not included in upstream error messages.
- Treat every remote endpoint as a data recipient. Use private/local tags for policy, but enforce data-governance requirements outside this scoring layer as well.
- Bind the gateway to loopback unless another machine needs it. When binding to a LAN interface, set `LLM_ROUTER_GATEWAY_API_KEY` and place TLS or a trusted reverse proxy in front of untrusted networks.
