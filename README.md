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
  # New installs: LAN/VPN access. Updates: preserve the existing binding.
  # Add --localhost for this machine only, or --lan to explicitly enable LAN.
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
manually update the package and restart the service. Updates without an access-mode
flag preserve the service's environment file, API key, and optional `router.toml`.
Changed unit definitions are backed up alongside the unit; use `systemctl --user edit
llm-router.service` for persistent unit overrides. Updating this single proxy briefly
interrupts it; backend HA does not make proxy updates zero-downtime.

**New service installs default to LAN/VPN access: `0.0.0.0:8088`.** Choose access
explicitly by adding one of these flags to the installer command above:

```bash
# Other machines can connect; also enables LAN on an existing installation:
sh install.sh --service --auto-update --lan
# Only this machine can connect:
sh install.sh --service --auto-update --localhost
```

These examples assume `install.sh` is in your current directory; in the download
block above, use `sh "$router_installer"` instead. Both flags require `--service`
and cannot be combined. Without either flag, new service installs use LAN, while
existing installations keep their current binding. An automatic software update
never switches an existing localhost installation to LAN.

`0.0.0.0` is a **listening address, not a client URL**. Use your router machine's
actual LAN/VPN IP or DNS name: **`http://ROUTER_IP:8088/status`** for the dashboard
and **`http://ROUTER_IP:8088/v1`** for OpenAI clients. Localhost-only installations
use `http://127.0.0.1:8088` and cannot be reached directly from another computer.
This includes any public IPv4 interface: `--lan` does not itself
enforce a LAN-only firewall rule. A random client API key is saved in
`~/.config/llm-router/router.env` with owner-only permissions. If `XDG_CONFIG_HOME`
is set, that directory replaces `~/.config` for both settings and the user unit.
The service discovers local runtimes automatically and refreshes discovery every
30 seconds. It does not download models by default. An empty fleet is allowed;
`/healthz` reports unavailable until a usable backend/model is found.

Explicit access-mode changes preserve existing nonempty API keys, ports, and
backend settings. Enabling LAN generates a key if the existing key is missing or
empty; it never deliberately exposes a keyless service. Configuration changes
are backed up privately beside `router.env`. Ambiguous hand-written environment
files must be edited manually instead of being rewritten automatically. A
service-unit override that sets the host separately still needs to be adjusted
with `systemctl --user edit llm-router.service`.

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

For an older localhost-only installation, use `--service --lan`, or change
`LLM_ROUTER_HOST=127.0.0.1` to `LLM_ROUTER_HOST=0.0.0.0` in that file and restart
the service. To switch back, use `--service --localhost` or set `127.0.0.1`.
Keep the API key enabled, restrict access to your trusted LAN/VPN with your
firewall, and use TLS when traffic is not protected by a trusted network/VPN.
The installer does not open firewall ports. Clients connect to
`http://ROUTER_HOST:8088` (Ollama) or
`http://ROUTER_HOST:8088/v1` (OpenAI) and use the generated key. Shell `export`
commands do not configure an already-running service; put settings in `router.env`
(without `export`). You can also put explicit overrides in the same directory's
`router.toml`, or set `LLM_ROUTER_CONFIG` in `router.env` to an absolute config path.

### Browser status page

Open **`http://127.0.0.1:8088/status`** in a browser on the router machine, or
**`http://ROUTER_IP:8088/status`** from your trusted LAN/VPN after enabling a reachable
bind address as described above. Browsers visiting `/` also see the status page;
ordinary API requests to `/` retain the existing plaintext liveness response.

The top header shows the installed router version, including while backend
details are locked and on small screens. The page distinguishes the running
gateway from available models. It refreshes every 10 seconds and shows API
reachability for each backend, available models,
machine-preference/HA aliases, and process uptime. "Unchecked" means no API probe
has confirmed that backend; an online API does not by itself prove model inference
will succeed. Losing contact with the router marks the display stale instead of
leaving a green success indicator behind.

Every panel heading is collapsible: click it to hide or show that panel, or use
**Collapse all** / **Expand all** at the top of the page. Hidden panels keep
refreshing, and starting a software update reopens the update panel so its
progress is visible. This view state lives only in the open page; it is never
written to browser storage or URLs, and a reload or lock shows every panel again.

The page is grouped into three zones. **Overview** holds the health strip, the
client-traffic panel, and the network summary. **Fleet** holds saved backend
addresses, backends, model deployments, client model names, and observed
performance. **Operations** holds the connection self-test, explicit inference test, software updates,
router links, and a folded **How to read this page** panel that collects the
caveats instead of repeating them under every card. Each panel heading carries a
live count, such as `2 of 3 online` or `82 requests · last 24 hours`, so a
folded panel still tells you its state.

Enter the **router's** `LLM_ROUTER_GATEWAY_API_KEY` from `router.env` to unlock fleet
details. This is not an LM Studio/provider token. A key entered in the password
field is kept only in the page's memory, never added to URLs or browser storage,
and cleared when you lock details or reload/close the page. The public locked
view shows readiness/version, **known server count**, **listed model-copy count**,
and the **last successful cached metadata check**. `/healthz` and `/readyz` expose
the same aggregate `summary` object; no server addresses, model names,
credentials, or private error messages are public. If gateway authentication is
disabled, details follow that same unauthenticated-access policy.

The summary combines the cached routing fleet and saved-address check results.
Multiple APIs on the same server address are deduplicated; the same model on
different server addresses counts as separate copies. It counts known/listed
models, not necessarily loaded or currently usable models. A `+` after the model
count means a catalog was truncated and the count is a lower bound. The last
verified time comes from successful metadata checks or successful discovery,
not from viewing the page; it is unknown when no successful cached check exists.
Viewing public status never triggers a network scan or model request.

To unlock details directly from a URL, the status page accepts `api_key` in a URL
fragment (preferred) or query parameter:

```text
http://ROUTER_IP:8088/status#api_key=YOUR_ROUTER_API_KEY
http://ROUTER_IP:8088/status?api_key=YOUR_ROUTER_API_KEY
```

The browser removes the key from the address bar before making API requests,
then uses the normal Bearer header. Duplicate/ambiguous key parameters are
rejected. This works only for the browser status page, not as query-string
authentication for the API endpoints. Manually entered keys are never added to
links, and backend servers never receive your router key.

**Treat URLs containing keys as secrets.** Query-string keys may already have
reached proxy logs or browser history before the page clears them. The router
redacts `api_key` from its normal Uvicorn access logs, but cannot erase upstream
logs, shared links, or browser history/sync. URL fragments are not sent to the
server, making `#api_key=` preferable; entering the key in the password field
avoids putting it in a URL altogether. Use HTTPS or a trusted VPN and URL-encode
keys containing special characters. Rotate any key shared accidentally.

Quick links expose the router's health, readiness, diagnostics and compatible API
catalog endpoints on the same port. The page also shows your current router
address/port and links to each configured backend's sanitized HTTP(S) address.
Backend links open from your browser (so its LAN/VPN access matters) and never
include or forward your router key. These are API servers, not necessarily web
UIs; opening an API root can return 404. Protected router links can return 401
when opened in a new tab because the page does not put credentials in links.

Click **Run self-test (no models)** after unlocking details to check the router's
HTTP routes, authentication, cached model-list APIs, and configured backend
metadata APIs. Ollama is checked with `GET /api/version`; supported OpenAI-style
backends use their model-list endpoint, which lists metadata without loading or
running a model. Results show pass/fail/skipped, HTTP status, and elapsed time.
Backend checks run from the router, not the browser; they do not scan unknown
addresses or ports. Unknown adapters and excess targets are explicitly skipped.
Checks are bounded to 16 backends, four at a time, with a three-second timeout
per backend and a five-second cooldown between test runs. Redirects and arbitrary
custom health paths are never followed by this test.

The self-test sends **no prompts**, performs **no inference, model loads or
downloads**, and does not trigger discovery, change routing health, restart
services, or install updates. Catalog checks are skipped if no cached fleet
exists, avoiding discovery as a side effect. A passing test confirms API
connectivity, not inference or HA failover performance. Automatic 10-second
refresh remains a cached read and never starts the self-test. The page reports
the gateway process, not the systemd service/timer state. The update timer can
still be checked with the commands below. No external fonts, scripts or analytics
are used.

For an actual generation check, use the separate **Test smallest model on each
backend (runs inference)** button and confirm the warning. It requires a
configured router API key. This sends one tiny, fixed prompt directly to one
enabled chat model on each supported backend, with a small output-token limit.
It may load a model from disk, consume RAM/VRAM, evict another model according to
the backend's settings, or incur provider charges. It never downloads models,
retries inference, or fails over to a different server. The existing **Run
self-test (no models)** button remains metadata-only.

The test chooses the smallest eligible model using reported file sizes where
available, then parameter counts/name estimates, and a deterministic fallback
when sizes are unknown. The result identifies the selected model, server,
selection basis, pass/fail/skipped status and elapsed time. Unknown or partially
known sizes are labeled, not represented as proof of the absolute smallest model.
Unsupported adapters, non-chat models, and excess targets are explicitly skipped.
At most 16 backends are tested, two concurrently. Progress shows completed
backends; polling only reads results and never starts another generation.

Only an explicit, confirmed click starts a test. Refreshing the page, reading
health/status, checking saved addresses and automatic scans do not start it.
There is one job per gateway process, a 30-second cooldown after completion and
a 15-minute whole-job deadline. Results are in memory, not retained across a
router restart. If the connection is lost, the page only checks the job's status;
it never automatically repeats the inference request. A timeout/cancel may not
stop work already accepted by a backend. These direct diagnostics do not affect
routing health or passive model-performance metrics, and do not prove tool use
or HA failover works.

For scripts, `POST /status/inference-test` starts the same job (HTTP 202), and
`GET /status/inference-test` reads progress/results without invoking anything.
Both require `Authorization: Bearer YOUR_ROUTER_KEY`; POST also requires
`X-LLM-Router-Inference-Test: 1` and an empty body. Query parameters, caller-picked
models/prompts/targets, and cross-origin POSTs are rejected.

For localhost-only access, select `--localhost` at installation; you can then use
an SSH tunnel instead of exposing port 8088. Manually launched `llm-router serve`
and `llm-router-gateway` commands still default to localhost unless `--host` or
`LLM_ROUTER_HOST` says otherwise; they do not generate an API key for you.
Do not expose the dashboard or send your API key over an untrusted plaintext
HTTP connection; use TLS or a trusted VPN. Backend credentials and private URL
query strings are never included in dashboard data; saved checks display only
validated addresses and fixed metadata API paths.

### Client traffic

Unlock `/status` and open **Client traffic** to see how clients have used the
router. Counts are **per client request**, not per upstream attempt: a request
that fails on one backend and is rerouted to another is one request, one reroute,
and one failed attempt. Four tiles show, for the selected window, client requests
with the succeeded/failed split, tokens in and out for **successful requests
only**, HA reroutes with how many were rescued and how many still failed, and
failed backend attempts broken down by kind. A stacked bar chart shows requests
per hour, succeeded in green and failed in red, with a hover value per hour and a
folded table view of the same numbers. Buttons switch between the **last 24
hours**, the **last 7 days**, and **all time**; the chart keeps the seven-day
view for all time.

Failure kinds are a fixed vocabulary: `timeout`, `connection`, `http_5xx`,
`http_4xx`, `invalid_response`, `configuration`, `adapter`, `no_eligible_model`,
and `other`. They come from the classified upstream error, never from response
text, and the same `kind` field appears on each failed attempt in completion
diagnostics. Requests rejected before any backend was tried, because no
configured deployment satisfied the request constraints, count as failed requests
of kind `no_eligible_model`. Cancelled requests are not counted.

Traffic lives in the same private SQLite database as performance history, as
hourly buckets kept for **30 days** plus an all-time totals row. The `traffic`
object in `GET /router/metrics` and in detailed `/status/data` under
`performance` carries `available`, `since`, `retention_hours`, `totals`,
`windows` (`24h` and `7d`), and `hourly` (up to 168 rows, each with `hour`,
`requests_ok`, `requests_failed`, `reroutes_ok`, `reroutes_failed`,
`input_tokens`, `output_tokens`, and a `failures` object keyed by kind). Public
health summaries do not expose it. The metrics `schema_version` is now `2`.

Updating from 0.3.2 upgrades an existing metrics database **in place on the
first recorded request**: the traffic tables are added and the stored layout
version moves from 1 to 2 while deployment history is left untouched. Reads of a
not-yet-upgraded database work unchanged and simply show no traffic. Rolling the
runtime back to 0.3.2 after that upgrade makes the older code report the metrics
store as unavailable, because it refuses layouts it does not recognize; the data
is intact and is served again once 0.3.3 or later runs. Unexpected tables,
columns, or malformed rows are reported and never reset, as before.

### Passive model performance history

Unlock `/status` and open **Observed model performance** to see input and output tokens
per second, reported model load/setup time, successful/failed request counts,
and when each model/server was last observed. **No benchmarks or extra inference
requests are sent.** Only real requests routed through this process contribute;
requests sent directly to Ollama/LM Studio bypass the router and are not visible.
The existing buffered streaming path records one observation after the upstream
answer completes, not a live token counter while the answer is being generated.

Each measurement keeps its latest value, sample count, last-observed timestamp,
and an exponentially weighted moving average (`ewma`, alpha `0.2`) that adjusts
as new requests finish. A missing measurement is **Not reported**, not zero;
previous valid measurements retain their own timestamps rather than pretending
to have been measured again. Models no longer in the routing fleet retain their
history, marked historical. Aliases are resolved before recording, so a failover
is attributed to the actual server/model that handled each attempt.

Timing support depends on the backend's ordinary response:

- [Ollama](https://docs.ollama.com/api/chat) reports prompt-evaluation, generation,
  and model-load durations. Cached prompt tokens are excluded from input-speed
  calculation when their count is reported.
- [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
  can include prompt/generation `timings` in its compatible response.
- Other OpenAI-compatible servers, including LM Studio, may omit phase/load
  timing on `/v1/chat/completions`. Supported timing/statistics fields are used
  only when already present; the router does not switch APIs or probe/load a
  model to obtain them. Token usage alone cannot establish input or output speed.

Load/setup time is recorded independently from the whole upstream request
duration. **Slow reported loads** count reported loads of at least **1,000 ms**;
this suggests a possible cold load but does **not** prove a storage read. Warm
setup overhead can also be reported. Missing load telemetry, a slow response,
or a long time to first token is not classified as a cold load.

The read-only JSON API is **`GET /router/metrics`**. It uses the same Bearer key
policy as detailed status; URL-key unlocking is only a browser-page feature.

```bash
curl -fsS \
  -H 'Authorization: Bearer YOUR_ROUTER_API_KEY' \
  http://YOUR_ROUTER_IP:8088/router/metrics | python3 -m json.tool
```

The response includes `schema_version`, `available`, `updated_at`, the
`traffic` object described above, and a `deployments` list. Each row identifies `machine`, `endpoint`, `model`, sanitized
`address`, and whether it is `current`. The `metrics` object contains
`input_tokens_per_second`, `output_tokens_per_second`, `load_duration_ms`, and
`request_duration_ms`; each is `null` or an object with `latest`, `ewma`,
`samples`, and `updated_at`. Rows also include reported-token totals, `successes`,
`failures`, `slow_load_count`, and `slow_load_threshold_ms`. Failures contribute
attempt counts and elapsed time, not fabricated token rates. Attempts cancelled
before upstream completion are not counted. Detailed `/status/data` includes the same payload under
`performance`; public health summaries do not expose it. Reads never run a
model or create a new database. `/router/metrics` returns HTTP 503 with a generic
error if storage is unavailable or the latest write failed; successful recording
clears that warning. The dashboard itself remains accessible.

History is stored in a private SQLite database outside the managed virtual
environment: `~/.local/state/llm-router/metrics.sqlite3`, or
`$XDG_STATE_HOME/llm-router/metrics.sqlite3` when set. For the dedicated service
account, that is normally
`/home/llmrouter/.local/state/llm-router/metrics.sqlite3`. Set
`LLM_ROUTER_METRICS_FILE` in `router.env` only if you need another private,
service-user-owned location. **Software updates, runtime rollback, discovery
refreshes, and process restarts do not reset it.** Keep it outside the venv and
release directories. History follows `(machine identity, endpoint name, upstream
model ID)`; stable configured identities preserve it across address changes.
Different services/endpoints stay separate even on the same machine. Changing
those identity labels starts a separate history.

No prompts, generated text, tool arguments, credentials, or raw responses are
persisted. Storage is bounded to 4,096 model/server histories; existing rows can
continue updating at the limit, but adding further identities requires operator
attention. Storage failures never trigger repeat inference or discard a valid
answer. Unsafe/corrupt or unsupported database schemas are reported, never
silently reset. Back up the database while the router is stopped, or use a
SQLite-aware backup method.

### Save, check, and automatically enroll backend addresses

On `/status`, unlock details with your router API key, then use **Saved backend
addresses** to enter an IP address or hostname and click **Save & check**. For
example, enter `192.168.194.0` to check Ollama on port `11434` and LM Studio's
OpenAI-compatible API on port `1234`. To check a custom port, enter
`192.168.194.0:1234` or `http://192.168.194.0:1234/v1`. DNS/VPN hostnames are useful
for machines whose IP addresses change; an IP address alone cannot identify a
machine after it moves.

**Saving now adds reachable chat models to routing automatically.** No extra
`router.env` entry or service restart is needed. The router reads the model
catalog, enrolls supported chat models, and exposes their HA/preferred-machine
aliases through `/api/tags` and `/v1/models`. For example, saving
`192.168.42.43` enrolls its available Ollama models; Home Assistant continues
using the router URL (`http://192.168.37.37:8088`) and the router API key.
It does not need the backend address or a different key.

Use **Check** beside an address or **Check all saved** to run another check, and
**Remove** to forget one and withdraw routes owned only by that saved address.
Explicit configuration and other owners of the same server are preserved.
Up to 16 individual addresses can be saved; this is not
a subnet or port-range scanner. Checks run concurrently with short timeouts and
report elapsed time, HTTP status, and the latest check time. A bare address checks
only the two standard ports; an explicit port checks only that port.

Each result now shows the server's **model IDs and the API address for those
models**, not just a "Found" badge. Ollama's model list comes from
[`GET /api/tags`](https://docs.ollama.com/api/tags); its version check remains
[`GET /api/version`](https://docs.ollama.com/api/version). LM Studio and other
OpenAI-compatible servers use
[`GET /v1/models`](https://lmstudio.ai/docs/developer/openai-compat/models).
These are metadata requests only: no prompts, inference, model loading or
downloads. A listed model is not necessarily loaded, and listing it does not
verify that generation would succeed. Embedding models can also appear.
Non-chat models are displayed but are not enrolled as chat deployments. When
Ollama exposes both APIs at one address, routing prefers its native API and
does not create duplicate copies of the models.

One address's server checks (for example Ollama and the OpenAI-compatible API)
sit side by side on wide screens and stack on narrow ones. Model IDs appear as a
compact wrapped grid with the API address shown once per server, or beside each
model if a catalog mixes addresses. **Hide results** shrinks an address to one
line with its routing status, and each model list folds from its heading. These
choices survive the 30-second saved-address refresh until the page is locked or
reloaded.

For example, enter `192.168.42.43:11434` to look for both Ollama and
OpenAI-compatible APIs on **that IP and that port only**. Each displayed model
has the correct API base address (including `/v1` for the OpenAI API). The same
model can appear under both APIs if the server exposes both. The checker does
not infer a subnet or scan other machines from an individual IP address.

Successful empty lists explicitly show that no models were listed. A reachable
server whose model-list request fails is shown separately from an empty catalog,
with the catalog error instead of a stale list. Results show at most 200 unique
model IDs per API, with the total count and a truncation notice for larger lists.
The eight-request concurrency limit and 1.5-second per-request deadlines keep
checks bounded; Ollama's version and catalog requests run in parallel.

An OpenAI-compatible response does not uniquely identify LM Studio.
Authentication-required backends report their HTTP rejection; the router
API key is **never** forwarded to them. Configure separate backend credentials
in TOML for a server requiring authentication. Existing configured endpoints
take precedence: saved checks cannot override their credentials or re-enable
disabled models. Recognizable router-proxy catalogs are rejected to avoid
recursive routing. Redirects are not followed. Page refreshes only read cached
results; the router performs the checks in the background independently.

Addresses are saved on the **router**, not just in browser storage, in
`~/.config/llm-router/saved-hosts.json` under the account running the service
(`/home/llmrouter/.config/llm-router/saved-hosts.json` for the dedicated account).
`XDG_CONFIG_HOME` changes the config root; `LLM_ROUTER_SAVED_HOSTS_FILE` can select
a different file. The private file survives service restarts and software updates.
Saved addresses are rechecked and enrolled at startup, then every 30 seconds
(plus check time), even when general automatic discovery is disabled. Save
normally checks immediately; if a scan is already running or cooling down, it
queues a check and shows pending status. Offline addresses stay saved and are
retried. Last known deployments remain unavailable until a successful catalog
check; a successfully empty catalog removes its old chat models. Neither
enrollment nor these checks invoke, load, or download models. Existing saved
addresses from older versions are enrolled automatically after the update.
The controls require a configured router API key,
even if other gateway APIs intentionally allow keyless access.

### Automatic software updates

`sh install.sh --service --auto-update` opts into installing future commits from
the **official repository's `main` branch**, not just tagged releases. It creates
`llm-router-update.timer` and `llm-router-update.service` alongside the gateway's
user service. Existing installations do not acquire this behavior until you opt
in. This updates the router software and its Python dependencies, not Ollama,
LM Studio, your operating system, or backend model files.

#### Check and install from the status page

Starting with **0.3.2**, `/status` has a **Check for updates** button at the top.
Unlock the page with the router API key first. Clicking the button checks the
official `main` branch and **automatically installs a newer commit**, if one is
available; it is not a check-only button. A running router restarts briefly after
the new runtime passes validation. An unchanged commit does not reinstall or
restart anything. Anyone with the router API key can request this action, so
keep that key private and use a trusted LAN/VPN or HTTPS connection.

The progress indicator shows actual stages: checking, downloading, validating,
and restarting. It is indeterminate rather than an estimated download percentage.
The update runs in the separate `llm-router-update.service`, so it continues
while the gateway restarts or the browser closes. The page reconnects and reads
the saved result; a returning server alone is not treated as update success.
If a request's result is uncertain, the page checks status rather than silently
starting another update. Reloading the page requires unlocking it again.

This requires the official-main, non-root systemd user-service installation and
its updater unit, normally installed with `sh install.sh --service --auto-update`.
The timer may be disabled while the manual button remains available. Pinned,
fork, editable, manual-process and unsupported installations show an explanation
instead of being switched to a different source. Settings, keys, saved backend
addresses and performance history are preserved; checks never invoke models.
Older servers need one command-line update to acquire this button.

Protected JSON endpoints are `GET /status/update` (local progress only) and
`POST /status/update` (check and install). Both require the router Bearer key,
even for otherwise keyless gateways. POST additionally requires
`X-LLM-Router-Update: 1`, an empty body, and a same-origin browser request.
Neither endpoint accepts query parameters, repositories, revisions or paths.
Progress is kept privately in `.update-status.json` under the managed installation
directory, outside the replaced virtual environment. Raw package output, paths,
credentials and exception details are not returned by this API.

#### Scheduled updates and recovery

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

If the service was installed under the dedicated `llmrouter` account, run this
from your **root shell on the router host** to check/install now and show the logs:

```bash
runuser --login llmrouter --command '
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  systemctl --user start llm-router-update.service
  router_update_result=$?
  journalctl --user -u llm-router-update.service -n 50 --no-pager
  exit "$router_update_result"
'
```

This forces an immediate **update check**, not a reinstall of an unchanged commit.
It downloads only changes already published to official `main`; unpublished local
edits are not installed. No `cd`, `git pull`, root installer run, or update to your
chat client is needed. A new router version briefly restarts the running gateway;
LM Studio/Ollama and model files are not updated by this command.

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
python -m pip install ./dist/source_agnostic_llm_router-0.3.5-py3-none-any.whl
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

#### What is automatic, and what needs configuration?

| Feature | Behavior |
|---|---|
| Find and enroll servers on your LAN/VPN | Opt in with `LLM_ROUTER_SCAN_CIDRS`, or list known servers in `LLM_ROUTER_DISCOVERY_URLS`. A LAN/VPN range is never guessed from an entered IP. |
| Keep the routing model list current | Discovery repeats every 30 seconds in a new service installation, or every 300 seconds by default when launched manually. Existing settings are preserved; change `LLM_ROUTER_DISCOVERY_REFRESH` to choose another interval. |
| Status-page saved IP addresses | Saved addresses automatically enroll supported chat models, are restored at startup, and are rechecked every 30 seconds. No separate discovery URL is needed. |
| Detect known backends going offline/online | Ordinary endpoints have metadata health checks every 15 seconds by default; saved endpoints use their 30-second catalog checks. Eligible replicas can serve fallback requests; this is not a guarantee of uninterrupted service. |
| HA, preferred-machine and no-failover aliases | Generated for enrolled models. Replica groups use exact upstream model IDs unless `replica_group` explicitly joins different IDs. Use a stable `machine_id` and DNS/VPN hostname for a roaming machine. |
| Install router software updates | Daily, with up to one hour of jitter, **only after** installing with `--service --auto-update`. Updates preserve settings and restart an active service; a single router can briefly be unavailable during restart. |
| Status page and self-test | Public summaries and page refreshes read cached data. Saved checks and the self-test fetch metadata only; they never run inference, load models, or download models. |
| Explicit smallest-model inference test | Separate authenticated button, with confirmation. Runs one tiny prompt per supported backend and may load a model; never starts automatically, downloads models, retries inference or fails over. |

LAN discovery currently checks Ollama on `11434` and OpenAI-compatible APIs on
`1234`, `8000`, and `8080`. It examines the **first 64 host addresses per configured
range** by default, not necessarily every machine in that range. For a complete
IPv4 `/24`, set `LLM_ROUTER_MAX_SCAN_HOSTS=254` (the maximum is 256). At most eight
ranges are considered. Restricting a saved check to `IP:port` checks that one
machine only; it does not start a single-port subnet scan.

For the systemd installation, put discovery settings in the service user's
`~/.config/llm-router/router.env`, then restart `llm-router.service` using that
user's systemd manager. Saving an address in the dashboard does not edit this
file. Choose only network ranges and servers you are authorized to contact.

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

A **Context window size** set in the Ollama integration arrives as a float such as
`8192.0`, because Home Assistant's number selector stores whole numbers that way.
The router treats integral floats in `num_ctx`, `num_predict`, and `max_tokens` as
the integers they denote; fractional or non-finite values are still rejected with
HTTP 400. Routers before 0.3.5 rejected the float outright, which Home Assistant
logged as `min_context_window must be an integer (status code: 400)`.

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
