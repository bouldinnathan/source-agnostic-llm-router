"""Dependency-free status page with saved targets and metadata diagnostics."""

from __future__ import annotations


def render_status_html(*, api_key_required: bool) -> str:
    """Render the public shell; backend details arrive only after authentication."""
    return _STATUS_HTML.replace("__AUTH_REQUIRED__", "true" if api_key_required else "false")


_STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>LLM Router · Status</title>
  <link rel="stylesheet" href="/status/assets/style.css">
  <script src="/status/assets/app.js" defer></script>
</head>
<body data-auth-required="__AUTH_REQUIRED__">
  <a class="skip-link" href="#main">Skip to status</a>
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true">↗</span>
      <span>LLM Router <span class="brand-subtitle">/ Status</span></span>
    </div>
    <div class="topbar-meta">
      <span id="version" class="version-badge" aria-label="Router version" aria-live="polite"></span>
      <button id="update-button" type="button" disabled aria-describedby="update-warning">Check for updates</button>
      <span class="readonly">Status &amp; connection checks</span>
    </div>
  </header>
  <main id="main">
    <section class="heading" aria-labelledby="page-title">
      <div><p class="eyebrow">Your models, one connection</p>
        <h1 id="page-title">Router overview</h1>
        <p class="muted">A quick check that your gateway and model backends are available. Click a panel heading to hide or show it; hidden panels keep refreshing.</p>
      </div>
      <div class="heading-actions">
        <button id="collapse-all-button" type="button">Collapse all</button>
        <button id="expand-all-button" type="button">Expand all</button>
        <button id="refresh-button" type="button">Refresh status</button>
      </div>
    </section>

    <div class="zone-heading"><h2>Overview</h2></div>
    <section id="health-panel" class="health-panel tone-pending" aria-labelledby="health-title" aria-live="polite" aria-atomic="true">
      <span class="health-dot" aria-hidden="true"></span>
      <div><h2 id="health-title">Checking router…</h2>
        <p id="health-message">Waiting for a fresh status snapshot.</p>
      </div>
    </section>

    <section class="overview" aria-label="Connection overview">
      <div class="overview-item"><span class="label">Gateway</span><strong id="gateway-state">Checking…</strong></div>
      <div class="overview-item"><span class="label">Model readiness</span><strong id="model-readiness">Checking…</strong></div>
      <div class="overview-item"><span class="label">Gateway uptime</span><strong id="uptime">—</strong></div>
      <div class="overview-item"><span class="label">Last checked</span><strong id="checked-at">—</strong></div>
    </section>

    <details id="traffic-panel" class="card traffic-panel" open hidden>
      <summary class="card-summary"><h2 id="traffic-title">Client traffic <span class="card-summary-meta" id="traffic-meta"></span></h2></summary>
      <div class="card-intro traffic-intro">
        <p class="muted">Requests that clients sent through this router, counted once each even when a failed backend attempt was rerouted. Tokens count successful requests only. Nothing here sends prompts or runs benchmarks.</p>
        <div class="traffic-windows" role="group" aria-label="Traffic time window">
          <button id="traffic-window-24h" type="button" class="window-button" aria-pressed="true">Last 24 hours</button>
          <button id="traffic-window-7d" type="button" class="window-button" aria-pressed="false">Last 7 days</button>
          <button id="traffic-window-all" type="button" class="window-button" aria-pressed="false">All time</button>
        </div>
      </div>
      <p id="traffic-message" class="traffic-message muted" role="status">Unlock backend details to see client traffic.</p>
      <div id="traffic-tiles" class="traffic-tiles" aria-label="Traffic totals"></div>
      <div class="traffic-chart-wrap">
        <div class="traffic-chart-heading"><h3 id="traffic-chart-title">Requests per hour</h3>
          <ul class="chart-legend" aria-label="Chart legend"><li><span class="swatch swatch-ok" aria-hidden="true"></span>Succeeded</li><li><span class="swatch swatch-failed" aria-hidden="true"></span>Failed</li></ul></div>
        <div id="traffic-chart" class="traffic-chart"></div>
        <details class="traffic-table"><summary class="host-catalog-summary"><span class="host-catalog-title">Table view</span><span id="traffic-table-hint" class="muted"></span></summary>
          <div class="table-scroll"><table><caption class="sr-only">Hourly client requests</caption>
            <thead><tr><th scope="col">Hour</th><th scope="col">Succeeded</th><th scope="col">Failed</th><th scope="col">Reroutes (rescued / failed)</th><th scope="col">Tokens in</th><th scope="col">Tokens out</th></tr></thead>
            <tbody id="traffic-table-body"></tbody>
          </table></div>
        </details>
      </div>
      <details id="failures-panel" class="traffic-failures" open hidden>
        <summary class="host-catalog-summary"><span class="host-catalog-title">Recent failed requests</span><span id="failures-hint" class="muted"></span></summary>
        <p class="muted failures-note">The router’s own diagnosis for the last failed client requests: the model name the client asked for, the HTTP status it received, and why. No prompt text is kept. Home Assistant’s system log only ever shows the first traceback it recorded, so read the current reason here.</p>
        <div class="table-scroll"><table><caption class="sr-only">Recent failed client requests</caption>
          <thead><tr><th scope="col">Time</th><th scope="col">Requested model</th><th scope="col">Result</th><th scope="col">Router diagnosis</th></tr></thead>
          <tbody id="failures-body"></tbody>
        </table></div>
      </details>
    </details>

    <details id="summary-panel" class="card public-summary" open>
      <summary class="card-summary"><h2 id="public-summary-title">Network summary <span class="card-summary-meta" id="summary-meta"></span></h2></summary>
      <div class="card-intro"><p class="muted">Cached counts only, with no addresses or model names. Refreshing this page never scans the network or runs inference.</p></div>
      <div class="public-summary-counts">
        <div><span class="label">Known servers</span><strong id="public-count-servers">Unknown</strong></div>
        <div><span class="label">Listed model copies</span><strong id="public-count-models">Unknown</strong></div>
        <div><span class="label">Last successful metadata check</span><strong id="public-last-verified">Unknown</strong></div>
      </div>
      <p id="public-summary-note" class="muted">Waiting for a current summary. Counts do not prove models are loaded or inference works.</p>
    </details>

    <p id="url-key-message" class="help-box" role="status" hidden></p>
    <section id="auth-section" class="card auth-card" aria-labelledby="auth-title" hidden>
      <div><h2 id="auth-title">Unlock backend details</h2>
        <p class="muted">Use the router’s client API key from <code>router.env</code>, not an LM Studio key.
        Your key stays only in this page’s memory and is forgotten when you reload or lock the page.</p>
        <p class="muted">Use HTTPS or a trusted private connection when entering your key.</p>
      </div>
      <form id="key-form" autocomplete="off">
        <label for="api-key">Router API key</label>
        <div class="key-controls">
          <input id="api-key" name="router-api-key" type="password" autocomplete="off" autocapitalize="none" spellcheck="false" required aria-describedby="auth-message">
          <button type="submit" class="primary">Unlock</button>
        </div>
      </form>
      <div class="auth-actions"><p id="auth-message" class="muted" role="status">Backend addresses and model details are locked.</p>
        <button id="lock-button" type="button" hidden>Clear key / Lock details</button>
      </div>
    </section>

    <section id="details" aria-label="Backend details" hidden>
      <div class="zone-heading"><h2>Fleet</h2></div>
      <div class="stats" aria-label="Backend counts">
        <div class="stat"><span class="label">Known backends</span><strong id="count-endpoints">—</strong></div>
        <div class="stat"><span class="label">Backends online</span><strong id="count-online">—</strong></div>
        <div class="stat"><span class="label">Available / enabled models</span><strong id="count-models">—</strong></div>
        <div class="stat"><span class="label">Model aliases</span><strong id="count-aliases">—</strong></div>
      </div>
      <div id="setup-help" class="help-box" hidden>
        <h3>No usable model yet</h3>
        <p>Start Ollama or LM Studio, make a chat model available, and save its IP address below to enroll discovered models automatically.
        You can also explicitly configure backends using <code>LLM_ROUTER_DISCOVERY_URLS</code> in <code>router.env</code> and restart the router.
        If a backend is offline, check its address, firewall, and VPN connection.</p>
      </div>
      <details id="settings-panel" class="card settings-panel" open>
        <summary class="card-summary"><h2 id="settings-title">Routing settings <span class="card-summary-meta" id="settings-meta"></span></h2></summary>
        <div class="card-body">
          <p id="settings-message" class="muted" role="status">Unlock backend details to change routing settings.</p>
          <form id="settings-form" class="settings-form" autocomplete="off">
            <label class="setting"><input id="setting-advertise-machine" type="checkbox" disabled>
              <span><strong>Advertise per-machine model names</strong><span class="muted">Lists the <code>…-machine</code> and <code>…-machine-nofailover</code> names, whose machine part can be an IP address, in /api/tags and /v1/models. Off leaves clients only the <code>auto</code> presets and the <code>…-ha</code> names; a hidden name still works for clients already using it.</span></span></label>
            <label class="setting"><input id="setting-prefer-fastest" type="checkbox" disabled>
              <span><strong>Prefer the fastest replica</strong><span class="muted">When the chosen model runs on several machines, try the one with the lowest observed latency first. Quality and capability still decide which model is chosen.</span></span></label>
            <label class="setting"><input id="setting-race" type="checkbox" disabled>
              <span><strong>Occasionally race all replicas</strong><span class="muted">Every Nth request for a model with several available replicas is sent to all of them at once. The first answer is returned and the others finish in the background, so every replica’s latency is re-measured. Those requests run once per replica.</span></span></label>
            <label class="setting setting-number"><span><strong>Race every</strong></span><input id="setting-race-every" type="number" min="2" max="1000" step="1" inputmode="numeric" disabled><span class="muted">requests per model (2 to 1000)</span></label>
            <label class="setting"><input id="setting-prefer-first-token" type="checkbox" disabled>
              <span><strong>Rank the fastest replica by first token</strong><span class="muted">Use time to first token instead of total answer time when preferring the fastest replica. Better for voice, where the wait before speech starts is what you feel.</span></span></label>
            <p class="muted setting-note">Backend answers are read as token streams, so these limits apply to silences, not to the whole answer. Loading a model and evaluating a long prompt happen before the first token.</p>
            <label class="setting setting-number"><span><strong>First-token timeout</strong></span><input id="setting-first-token-timeout" type="number" min="5" max="3600" step="1" inputmode="numeric" disabled><span class="muted">seconds a backend may stay silent before its first token (5 to 3600)</span></label>
            <label class="setting setting-number"><span><strong>Idle timeout</strong></span><input id="setting-idle-timeout" type="number" min="5" max="3600" step="1" inputmode="numeric" disabled><span class="muted">seconds between tokens before an attempt is abandoned (5 to 3600)</span></label>
            <label class="setting setting-number"><span><strong>Request cap</strong></span><input id="setting-max-request" type="number" min="0" max="86400" step="1" inputmode="numeric" disabled><span class="muted">seconds for a whole answer; 0 means no cap (0 to 86400)</span></label>
          </form>
          <div id="settings-races" class="settings-races" hidden><h3>Recent races</h3><ul id="settings-races-list"></ul></div>
        </div>
      </details>
      <details id="hosts-panel" class="card" open>
        <summary class="card-summary"><h2 id="hosts-title">Saved backend addresses <span class="card-summary-meta" id="hosts-meta"></span></h2></summary>
        <div class="card-intro">
          <p class="muted">Save an IP address or hostname to automatically enroll its discovered models for client routing. The router checks saved addresses at startup and every 30 seconds using metadata only: no prompts, model loading, or downloads. Only the addresses you save are checked, not whole subnets.</p>
          <p class="muted">Remove withdraws routes owned only by that address; explicitly configured routes are preserved. Server checks sit side by side, Hide results shrinks an address to one line, and each model list folds from its heading.</p>
        </div>
        <form id="host-form" class="host-form" autocomplete="off">
          <label for="host-address">IP address, hostname, or backend URL</label>
          <div class="key-controls"><input id="host-address" name="backend-address" type="text" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="256" placeholder="192.168.194.0" required aria-describedby="host-help hosts-message">
            <button id="host-save-button" type="submit" class="primary">Save &amp; enable routing</button></div>
          <p id="host-help" class="muted">A bare IP address or hostname checks Ollama on 11434 and LM Studio on 1234. A URL checks only its own port, for example http://192.168.194.0:1234/v1. Do not include passwords or API keys.</p>
        </form>
        <div class="hosts-toolbar"><p id="hosts-message" class="muted" role="status">Unlock details to manage saved addresses.</p>
          <div class="hosts-actions"><button id="hosts-reload-button" type="button">Reload saved</button><button id="hosts-check-button" type="button">Check all saved</button></div></div>
        <div id="hosts-results" class="host-list"><ul id="hosts-body" class="host-entries" aria-label="Saved addresses and metadata checks"></ul></div>
      </details>

      <details id="backends-panel" class="card" open>
        <summary class="card-summary"><h2 id="backends-title">Backends <span class="card-summary-meta" id="backends-meta"></span></h2></summary>
        <div class="card-intro"><p class="muted">Known machines and their latest metadata reachability checks. Address links open each backend’s own API port in your browser; the router key is never forwarded.</p></div>
        <div class="table-scroll"><table><caption class="sr-only">Backend reachability</caption>
          <thead><tr><th scope="col">Machine / backend</th><th scope="col">API address</th><th scope="col">Status</th><th scope="col">Models ready</th><th scope="col">Last probe</th></tr></thead>
          <tbody id="endpoints-body"></tbody>
        </table></div>
      </details>

      <details id="models-panel" class="card" open>
        <summary class="card-summary"><h2 id="models-title">Model deployments <span class="card-summary-meta" id="models-meta"></span></h2></summary>
        <div class="card-intro"><p class="muted">Each model copy on each machine; readiness is not a test generation.</p></div>
        <div class="table-scroll"><table><caption class="sr-only">Model deployments and request counters</caption>
          <thead><tr><th scope="col">Model / deployment</th><th scope="col">Machine</th><th scope="col">Status</th><th scope="col">Active requests</th><th scope="col">Succeeded / failed</th></tr></thead>
          <tbody id="models-body"></tbody>
        </table></div>
      </details>

      <details id="aliases-panel" class="card" open>
        <summary class="card-summary"><h2 id="aliases-title">Client model names <span class="card-summary-meta" id="aliases-meta"></span></h2></summary>
        <div class="card-intro"><p class="muted">HA shares replicas; preferred tries one machine first; pinned never fails over.</p></div>
        <p id="alias-conflicts" class="alias-conflicts" role="status" hidden></p>
        <div class="table-scroll"><table><caption class="sr-only">High availability and machine-specific aliases</caption>
          <thead><tr><th scope="col">Alias</th><th scope="col">Routing</th><th scope="col">Status</th><th scope="col">Deployments</th></tr></thead>
          <tbody id="aliases-body"></tbody>
        </table></div>
      </details>
      <details id="performance-panel" class="card" open>
        <summary class="card-summary"><h2 id="performance-title">Observed model performance <span class="card-summary-meta" id="performance-meta"></span></h2></summary>
        <div class="card-intro"><p class="muted">Measured passively from real requests through this router and saved across restarts and updates. Refresh never runs a benchmark or model. Smoothed averages use EWMA; token rates and load/setup time need backend-reported timings. Request time covers the whole upstream call.</p></div>
        <p id="performance-message" class="performance-message muted" role="status">Waiting for saved performance observations.</p>
        <div class="table-scroll"><table><caption class="sr-only">Saved request performance by model and server</caption>
          <thead><tr><th scope="col">Model / server</th><th scope="col">Input tok/s</th><th scope="col">Output tok/s</th><th scope="col">Reported load / setup</th><th scope="col">Request time (wall clock)</th><th scope="col">First token</th><th scope="col">Requests</th><th scope="col">Last observed</th></tr></thead>
          <tbody id="performance-body"></tbody>
        </table></div>
        <p class="performance-note muted">Load/setup times are backend-reported. Slow reported loads may be cold starts, but do not prove disk I/O. Missing timings are not estimated from request latency. Historical rows keep observations for deployments no longer in the routing configuration.</p>
      </details>
      <p class="muted snapshot-note">Last discovery: <span id="last-discovery">—</span>. Refresh reads the current snapshot; it does not scan your network or run a model.</p>
    </section>

    <div class="zone-heading"><h2>Operations</h2></div>
    <details id="self-test-panel" class="card" open hidden>
      <summary class="card-summary"><h2 id="self-test-title">Connection self-test <span class="card-summary-meta" id="self-test-meta"></span></h2></summary>
      <div class="card-intro self-test-intro"><p class="muted">Checks router APIs and backend metadata only. Never sends prompts, runs inference, loads models, or downloads anything.</p>
        <button id="self-test-button" type="button">Run self-test (no models)</button></div>
      <p id="self-test-message" class="self-test-message muted" role="status">Runs only when you click. No self-test has been run in this page.</p>
      <div id="self-test-results" class="table-scroll" hidden><table><caption class="sr-only">Connection self-test results</caption>
        <thead><tr><th scope="col">Check / target</th><th scope="col">Result</th><th scope="col">Detail</th><th scope="col">Time</th></tr></thead>
        <tbody id="self-test-body"></tbody>
      </table></div>
    </details>

    <details id="inference-panel" class="card inference-panel" open hidden>
      <summary class="card-summary"><h2>Smallest-model inference test <span class="card-summary-meta" id="inference-meta"></span></h2></summary>
      <div class="card-body">
        <p id="inference-warning" class="muted">This separate, optional test sends one tiny prompt to the smallest eligible chat model on each backend (up to 16 backends per run). It may load models from storage and use RAM / GPU memory, evict a loaded model, or use provider credits. No models are downloaded, no fallback backend is used, and nothing runs until you click and confirm. Your router API key is required.</p>
        <button id="inference-button" type="button" class="primary" disabled aria-describedby="inference-warning">Test smallest model on each backend (runs inference)</button>
        <p id="inference-message" class="muted" role="status">No inference test has been requested in this page.</p>
        <progress id="inference-progress" max="1" value="0" aria-label="Backend inference tests completed" hidden></progress>
        <button id="inference-refresh-button" type="button" hidden>Refresh test status (no inference)</button>
      </div>
      <div id="inference-results" class="table-scroll" hidden><table><caption class="sr-only">Explicit inference test results by backend</caption>
        <thead><tr><th scope="col">Backend / address</th><th scope="col">Result</th><th scope="col">Selected model / selection basis</th><th scope="col">Detail</th><th scope="col">Time</th></tr></thead>
        <tbody id="inference-body"></tbody>
      </table></div>
    </details>

    <details id="update-panel" class="card update-panel" open>
      <summary class="card-summary"><h2 id="update-title">Router software updates <span class="card-summary-meta" id="update-meta"></span></h2></summary>
      <div class="card-body">
        <p id="update-warning" class="muted">Check for updates checks official main and automatically installs a newer commit. Installation briefly restarts the router and can interrupt requests. Your API key is required.</p>
        <p id="update-message" class="muted" role="status">Unlock backend details to enable software updates.</p>
        <div id="update-details" hidden>
          <p class="update-stage">Stage: <strong id="update-stage"></strong></p>
          <progress id="update-progress" aria-label="Router update in progress" hidden></progress>
          <p id="update-observed" class="muted"></p>
          <button id="update-refresh-button" type="button">Refresh update status</button>
          <p class="muted">Status refresh reads the local update job only; it never starts another update.</p>
        </div>
      </div>
    </details>

    <details id="links-panel" class="card quick-links" open>
      <summary class="card-summary"><h2 id="links-title">Router links</h2></summary>
      <div class="card-body">
      <p class="muted">This router: <code id="router-origin">Current server</code>. Links open in a new tab.</p>
      <nav aria-label="Router API and diagnostic links">
        <a href="/healthz" target="_blank" rel="noopener noreferrer">Health</a>
        <a href="/readyz" target="_blank" rel="noopener noreferrer">Readiness</a>
        <a href="/status/data" target="_blank" rel="noopener noreferrer">Status JSON</a>
        <a href="/router/status" target="_blank" rel="noopener noreferrer">Router diagnostics</a>
        <a href="/router/metrics" target="_blank" rel="noopener noreferrer">Performance JSON</a>
        <a href="/api/version" target="_blank" rel="noopener noreferrer">Ollama API version</a>
        <a href="/api/tags" target="_blank" rel="noopener noreferrer">Ollama model list</a>
        <a href="/v1/models" target="_blank" rel="noopener noreferrer">OpenAI model list</a>
      </nav>
      <p class="muted">These are API responses, not separate apps. Protected links may show 401 because new tabs do not receive this page’s API key.</p>
      </div>
    </details>

    <details id="about-panel" class="card about-panel">
      <summary class="card-summary"><h2 id="about-title">How to read this page</h2></summary>
      <div class="card-body about-body">
        <dl>
          <dt>What refreshes do</dt>
          <dd>Status refreshes every 10 seconds and saved addresses every 30 seconds. Both read cached results. Save, Check, and Run self-test read metadata only. Only the separate Test smallest model button sends prompts, after an explicit confirmation; it may load a model. No action downloads models or scans whole subnets.</dd>
          <dt>Online is not inference</dt>
          <dd>“Online”, “Found”, and a listed model mean an API answered a metadata request. They do not prove a model is loaded or that generation will succeed.</dd>
          <dt>Client traffic</dt>
          <dd>Requests are counted once each, as clients see them. A reroute is a request that needed another backend after a failed attempt; “rescued” means it still succeeded. Every failed attempt counts once under its kind, so a rescued request still shows the failure that caused the reroute. Tokens are backend-reported and counted for successful requests only. Hourly history is kept for 30 days.</dd>
          <dt>Performance numbers</dt>
          <dd>Token rates and load times come from timings that backends include in ordinary responses. Smoothed values are exponentially weighted moving averages with alpha 0.2. Missing timings show as Not reported, never as zero. A slow reported load suggests a cold start without proving a disk read.</dd>
          <dt>Links and keys</dt>
          <dd>Router links open raw API responses, not apps, and protected ones may show 401 because new tabs never receive this page’s key. Your key stays in page memory only, and backends never receive it.</dd>
        </dl>
      </div>
    </details>
    <noscript><p class="help-box">Enable JavaScript to view live status. The JSON readiness endpoint is <a href="/healthz">/healthz</a>.</p></noscript>
  </main>
  <footer><p>Status refreshes every 10 seconds · saved backends are rechecked every 30 seconds · self-tests run only on click · metadata checks do not prove inference works</p></footer>
</body>
</html>
"""


STATUS_CSS = """
:root{color-scheme:light;--navy:#14253d;--ink:#1c3048;--muted:#52657b;--line:#d9e2ec;--surface:#fff;--background:#f3f6fa;--green:#176943;--amber:#845108;--red:#a22b35;--chart-ok:#238b6a;--chart-failed:#c0392b;--track:#e7edf4}
*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.6}
[hidden]{display:none!important}a{color:#245aba}button,input{font:inherit}button{cursor:pointer;border:1px solid #a9b9cb;border-radius:8px;padding:9px 15px;color:var(--ink);background:#fff;font-weight:600;white-space:nowrap}button:hover{background:#edf3fa}button:disabled{cursor:wait;opacity:.65}button.primary{background:var(--navy);color:#fff;border-color:var(--navy)}button.primary:hover{background:#263e5d}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #669eea;outline-offset:3px}input{min-width:0;width:100%;border:1px solid #9aaec3;border-radius:8px;padding:10px 12px;color:var(--ink);background:#fff}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.9em;overflow-wrap:anywhere}
.topbar{background:var(--navy);color:#fff;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;padding:18px max(24px,calc((100% - 1352px)/2));gap:20px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:-.02em;font-size:19px}.brand-mark{display:grid;place-items:center;width:32px;height:32px;border:1px solid #6e83a1;border-radius:9px;font-size:24px;line-height:1}.brand-subtitle{font-weight:400;color:#bdcce0}.topbar-meta{display:flex;flex-wrap:wrap;align-items:center;gap:12px}.version-badge{display:inline-block;padding:3px 10px;border:1px solid #6e83a1;border-radius:999px;color:#fff;font-size:12px;font-weight:600;white-space:nowrap}.version-badge:empty{display:none}.readonly{font-size:12px;color:#d1deee;letter-spacing:.03em}
main{max-width:1400px;margin:auto;padding:38px 24px 24px}.heading,.section-heading{display:flex;align-items:center;justify-content:space-between;gap:20px}.heading{margin-bottom:26px}.heading p{margin:7px 0 0}.heading-actions{display:flex;flex-wrap:wrap;gap:8px;flex:0 0 auto}.eyebrow{text-transform:uppercase;font-size:11px;font-weight:700;letter-spacing:.15em;color:var(--muted)}h1{font-size:32px;letter-spacing:-.04em;line-height:1.2;margin:8px 0}h2{font-size:18px;letter-spacing:-.02em;margin:0}h3{font-size:15px;margin:0 0 5px}p{margin:0}.muted{color:var(--muted);font-size:13px}
.health-panel{display:flex;align-items:flex-start;gap:14px;border:1px solid;border-radius:12px;padding:21px 24px}.health-panel p{font-size:14px;margin-top:3px}.health-dot{width:12px;height:12px;flex:0 0 auto;border-radius:50%;margin-top:8px;background:currentColor}.tone-pending{background:#edf2f9;border-color:#c6d5e6;color:#354d6c}.tone-ready{background:#edf8f1;border-color:#b2d7c0;color:var(--green)}.tone-warning{background:#fff7e8;border-color:#e5ce9e;color:var(--amber)}.tone-error{background:#fff0f0;border-color:#eab9bf;color:var(--red)}
.overview{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin:24px 0 30px}.overview-item{padding-left:16px;border-left:2px solid #cbd7e5}.label{display:block;color:var(--muted);font-size:12px;font-weight:500}.overview strong{display:block;font-size:15px;margin-top:3px}.card{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:20px;overflow:hidden}.card-summary{list-style:none;display:flex;align-items:center;gap:12px;padding:17px 22px;cursor:pointer;user-select:none}.card-summary::-webkit-details-marker{display:none}.card-summary::before{content:"";flex:0 0 auto;width:9px;height:9px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:translate(-2px,0) rotate(-45deg);transition:transform .15s}details[open]>.card-summary::before{transform:translate(0,-2px) rotate(45deg)}.card-summary:hover{background:#f7f9fc}.card-summary:focus-visible{outline:3px solid #669eea;outline-offset:-3px}.card-summary h2{flex:1 1 auto;min-width:0}.card-summary::after{content:"Hide";font-size:12px;font-weight:500;color:var(--muted)}details:not([open])>.card-summary::after{content:"Show"}.card-intro{padding:0 22px 16px}.card-intro p{max-width:100ch}.card-intro p+p{margin-top:5px}.card-body{padding:0 22px 20px}.auth-card{padding:22px;display:grid;grid-template-columns:1.15fr 1fr;column-gap:36px;row-gap:16px}.auth-card h2{margin-bottom:7px}.auth-card p+p{margin-top:5px}.auth-card label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}.key-controls{display:flex;gap:10px}.auth-actions{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:16px}.section-heading{margin:30px 0 15px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}.stat{border:1px solid var(--line);background:var(--surface);border-radius:10px;padding:17px 20px}.stat strong{display:block;font-size:28px;font-weight:650;letter-spacing:-.03em;margin-top:4px;line-height:1.3}.help-box{padding:18px 22px;background:#fff8ea;border:1px solid #e6d1a6;border-radius:10px;margin:0 0 22px;color:#674810}.help-box p{font-size:14px}.table-scroll{width:100%;overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#f7f9fc;white-space:nowrap}th,td{padding:12px 22px;border-top:1px solid #e3e9f0;vertical-align:top}td{overflow-wrap:anywhere;max-width:360px}.secondary{display:block;font-size:12px;color:var(--muted);margin-top:2px}.address{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.badge{display:inline-block;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:650;white-space:nowrap}.badge-ready{background:#e7f5ed;color:var(--green)}.badge-warning{background:#fff2d8;color:var(--amber)}.badge-error{background:#ffe9eb;color:var(--red)}.badge-neutral{background:#edf1f6;color:#53647a}.empty-row{text-align:center;color:var(--muted);padding:24px}.snapshot-note{margin:24px 0 0}footer{max-width:1400px;margin:0 auto;padding:0 24px 30px;color:var(--muted);font-size:12px}.sr-only,.skip-link:not(:focus){position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip-link:focus{position:absolute;top:8px;left:8px;z-index:10;background:white;padding:8px}
@media(max-width:800px){.auth-card{grid-template-columns:1fr}.stats,.overview{grid-template-columns:repeat(2,1fr)}.auth-actions{grid-column:auto}th,td{padding:12px 16px}.heading{align-items:flex-start}.heading-actions{margin-top:12px}}
@media(max-width:500px){main{padding:24px 16px}.topbar{padding:16px}.readonly{display:none}.heading{display:block}h1{font-size:28px}.heading-actions{margin-top:18px}.heading-actions button{flex:1 1 auto}.health-panel{padding:18px}.auth-card{padding:18px}.key-controls{flex-direction:column}.auth-actions{align-items:flex-start;flex-direction:column}.stats{gap:10px}.stat{padding:14px}.stat strong{font-size:25px}.section-heading{align-items:flex-start;flex-direction:column;gap:3px}footer{padding:0 16px 24px}.brand{font-size:17px}.card-summary{padding:15px 16px}.card-intro,.card-body{padding-left:16px;padding-right:16px}.host-list{padding:0 12px 16px}}
.quick-links .card-body p{margin-top:6px}.quick-links nav{display:flex;flex-wrap:wrap;gap:8px 20px;margin:12px 0}.quick-links nav a{font-size:13px}.self-test-intro{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.self-test-intro button{flex:0 0 auto}.self-test-message{padding:0 22px 19px}.self-test-message.result-pass{color:var(--green)}.self-test-message.result-fail{color:var(--red)}.self-test-message.result-partial{color:var(--amber)}
.host-form{padding:0 22px 16px}.host-form label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}.host-form p{margin-top:7px}.hosts-toolbar{padding:0 22px 19px;display:flex;align-items:center;justify-content:space-between;gap:16px}.hosts-actions{display:flex;flex-wrap:wrap;gap:8px}.hosts-actions button{padding:6px 10px;font-size:12px}.host-check .badge{margin-left:6px}.host-check .secondary{overflow-wrap:anywhere}#hosts-message.result-fail{color:var(--red)}#hosts-message.result-pass{color:var(--green)}
.host-list{padding:0 22px 22px}.host-entries{list-style:none;margin:0;padding:0}.host-entry{border:1px solid var(--line);border-radius:10px;background:var(--surface);overflow:hidden}.host-entry+.host-entry{margin-top:14px}.host-entry-head{display:flex;flex-wrap:wrap;align-items:flex-start;gap:12px 28px;padding:14px 16px;background:#f7f9fc}.host-entry-identity{flex:1 1 280px;min-width:0}.host-entry-identity .address{font-size:14px;font-weight:650;overflow-wrap:anywhere}.host-entry-checked{flex:0 1 auto;font-size:13px}.host-entry-head .hosts-actions{margin-left:auto}.host-checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:12px;padding:14px 16px;border-top:1px solid var(--line)}.host-checks>p{grid-column:1/-1}.host-check{padding:14px;border:1px solid var(--line);border-radius:8px;background:#fafcfe;min-width:0}.host-check-heading{display:flex;align-items:center;flex-wrap:wrap;gap:5px}.host-check p{margin-top:7px}.host-check .host-api-address{display:block;font-size:12px;overflow-wrap:anywhere}.host-catalog{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}.host-catalog-summary{list-style:none;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;font-size:13px;cursor:pointer;user-select:none}.host-catalog-summary::-webkit-details-marker{display:none}.host-catalog-summary::before{content:"";flex:0 0 auto;align-self:center;width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:translate(-1px,0) rotate(-45deg)}.host-catalog[open]>.host-catalog-summary::before{transform:translate(0,-2px) rotate(45deg)}.host-catalog-title{font-weight:600}.host-catalog .catalog-warning{color:var(--amber)}.host-model-list{list-style:none;margin:10px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,190px),1fr));gap:6px}.host-model-list li{min-width:0;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:var(--surface)}.host-model-list code{font-size:12px}.host-model-list .secondary{font-size:11px;overflow-wrap:anywhere}
.public-summary-counts{display:grid;grid-template-columns:1fr 1fr 1.6fr;gap:18px;padding:0 22px 16px}.public-summary-counts strong{display:block;font-size:20px;line-height:1.5;overflow-wrap:anywhere}.public-summary-counts>div:last-child strong{font-size:15px}.public-summary>p{padding:0 22px 19px}
.performance-message{padding:0 22px 19px}.performance-message.result-warning{color:var(--amber)}.performance-note{padding:16px 22px;border-top:1px solid var(--line)}.metric-value{display:block;font-weight:650;white-space:nowrap}.metric-detail{display:block;min-width:130px;color:var(--muted);font-size:11px;margin-top:4px}.performance-identity{min-width:180px}.performance-identity .badge{margin-top:7px}.performance-identity code{display:block;margin-top:5px}.slow-load-count{margin-top:9px;font-size:12px}
.host-routing{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:12px;margin-top:10px}.host-routing .secondary{margin-top:6px}
.update-panel .card-body{padding-bottom:18px}.update-panel .card-body p{margin-top:7px}.update-panel progress{display:block;width:min(100%,480px);height:14px;margin:12px 0}.update-panel button{margin-top:12px}.update-panel .result-fail{color:var(--red)}.update-panel .result-pass{color:var(--green)}.update-stage{font-size:14px}
.inference-panel .card-body p{margin-top:8px}.inference-panel button{margin-top:12px;white-space:normal;text-align:left}.inference-panel progress{display:block;width:min(100%,480px);height:14px;margin:12px 0}.inference-panel .result-fail{color:var(--red)}.inference-panel .result-pass{color:var(--green)}.inference-panel .result-partial{color:var(--amber)}
@media(max-width:800px){.self-test-intro{align-items:flex-start;flex-direction:column}}
@media(max-width:600px){.hosts-toolbar{align-items:flex-start;flex-direction:column}.host-form .key-controls{flex-direction:column}.host-entry-head .hosts-actions{margin-left:0}}
@media(max-width:600px){.public-summary-counts{grid-template-columns:1fr 1fr}.public-summary-counts>div:last-child{grid-column:1/-1}}
.zone-heading{display:flex;align-items:center;gap:14px;margin:34px 0 14px}.zone-heading h2{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);font-weight:700;margin:0}.zone-heading::after{content:"";flex:1 1 auto;height:1px;background:var(--line)}.heading+.zone-heading{margin-top:0}
.card-summary-meta{font-size:12px;font-weight:500;color:var(--muted);margin-left:10px;letter-spacing:0;white-space:nowrap}.card-summary-meta:empty{display:none}
.traffic-intro{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:12px 24px}.traffic-intro p{flex:1 1 320px}.traffic-windows{display:flex;flex-wrap:wrap;gap:6px}.window-button{padding:6px 10px;font-size:12px}.window-button[aria-pressed="true"]{background:var(--navy);color:#fff;border-color:var(--navy)}
.traffic-message{padding:0 22px 14px}.traffic-message.result-warning{color:var(--amber)}
.traffic-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:0 22px 18px}.traffic-tiles:empty{padding:0}.tile{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:#fbfcfe;min-width:0}.tile-value{display:block;font-size:26px;font-weight:650;letter-spacing:-.03em;line-height:1.25;margin-top:4px;overflow-wrap:anywhere}.tile-value small{font-size:13px;font-weight:500;color:var(--muted);letter-spacing:0;margin-left:6px}.tile-caption{display:block;font-size:12px;color:var(--muted);margin-top:8px}
.seg-bar{display:flex;gap:2px;height:8px;margin-top:10px;border-radius:4px;overflow:hidden;background:var(--track)}.seg{height:100%;min-width:0}.seg-ok{background:var(--chart-ok)}.seg-failed{background:var(--chart-failed)}
.bar-rows{margin-top:10px;display:grid;gap:6px}.bar-row{display:grid;grid-template-columns:minmax(64px,38%) 1fr auto;align-items:center;gap:8px;font-size:12px}.bar-row .label{margin:0;font-size:12px;color:var(--ink);overflow-wrap:anywhere}.bar-track{height:8px;border-radius:4px;background:var(--track);overflow:hidden}.bar-fill{height:100%;border-radius:4px;background:var(--chart-ok)}.bar-fill.bar-failed{background:var(--chart-failed)}.bar-fill.bar-neutral{background:#5f7fa8}.bar-row .count{font-variant-numeric:tabular-nums;color:var(--muted)}
.traffic-chart-wrap{padding:0 22px 18px}.traffic-chart-heading{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 16px;margin-bottom:8px}.traffic-chart-heading h3{font-size:13px;margin:0}.chart-legend{display:flex;gap:16px;list-style:none;margin:0;padding:0;font-size:12px;color:var(--muted)}.chart-legend li{display:flex;align-items:center;gap:6px}.swatch{width:10px;height:10px;border-radius:2px;display:inline-block}.swatch-ok{background:var(--chart-ok)}.swatch-failed{background:var(--chart-failed)}
.traffic-chart svg{display:block;width:100%;height:auto}.traffic-chart .bar-ok{fill:var(--chart-ok)}.traffic-chart .bar-failed{fill:var(--chart-failed)}.traffic-chart .axis{stroke:#cbd5e1;stroke-width:1}.traffic-chart .grid{stroke:var(--track);stroke-width:1}.traffic-chart text{font-size:11px;fill:var(--muted);font-family:inherit}.traffic-chart .hit{fill:transparent}.traffic-chart g:hover .hit{fill:rgba(20,37,61,.06)}.traffic-chart .chart-empty{font-size:12px}
.traffic-failures{margin:0 22px 18px;padding-top:12px;border-top:1px solid var(--line)}.failures-note{margin:6px 0 8px}.traffic-failures .table-scroll{margin:0 -22px}.traffic-failures td{vertical-align:top}.traffic-table{margin-top:10px}.traffic-table .table-scroll{margin-top:8px}.traffic-table th,.traffic-table td{padding:8px 12px}.traffic-table td{font-variant-numeric:tabular-nums}
.alias-conflicts{padding:0 22px 16px;font-size:13px;color:var(--amber);overflow-wrap:anywhere}
.settings-form{display:grid;gap:12px;margin-top:12px}.setting-note{margin-top:4px}.setting{display:grid;grid-template-columns:auto 1fr;gap:10px 12px;align-items:start;font-size:14px;cursor:pointer}.setting input[type="checkbox"]{width:18px;height:18px;margin:2px 0 0}.setting strong{display:block;font-weight:600}.setting .muted{display:block;margin-top:2px}.setting-number{grid-template-columns:auto auto 1fr;align-items:center}.setting-number input{width:96px;padding:6px 10px}.settings-races{margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}.settings-races h3{font-size:13px;margin-bottom:6px}.settings-races ul{margin:0;padding-left:18px;font-size:13px;display:grid;gap:6px}#settings-message.result-fail{color:var(--red)}#settings-message.result-pass{color:var(--green)}#settings-message.result-warning{color:var(--amber)}
@media(max-width:500px){.setting-number{grid-template-columns:1fr}}
.about-body dl{margin:0;display:grid;grid-template-columns:minmax(140px,190px) 1fr;gap:10px 18px;font-size:13px}.about-body dt{font-weight:600}.about-body dd{margin:0;color:var(--muted)}
@media(max-width:900px){.traffic-tiles{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.about-body dl{grid-template-columns:1fr;gap:4px}.about-body dd{margin-bottom:8px}}
@media(max-width:500px){.traffic-tiles{grid-template-columns:1fr;padding:0 16px 16px}.traffic-chart-wrap{padding:0 16px 16px}.traffic-message{padding:0 16px 14px}.card-summary-meta{display:block;margin:4px 0 0;white-space:normal}}
"""


STATUS_JS = r"""
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const authRequired = document.body.dataset.authRequired === "true";
  let apiKey = "";
  let pageActive = true;
  let generation = 0;
  let activeRequest = null;
  let selfTestGeneration = 0;
  let activeSelfTest = null;
  let inferenceGeneration = 0;
  let activeInference = null;
  let inferencePollTimer = null;
  let inferenceWatching = false;
  let inferenceAttempted = false;
  let inferenceDeadline = 0;
  let inferenceRunId = null;
  let inferenceBaselineId = null;
  let hostsGeneration = 0;
  let activeHosts = null;
  let hostsLoaded = false;
  let savedHosts = [];
  let hostsLimit = 16;
  let updateGeneration = 0;
  let activeUpdate = null;
  let updatePollTimer = null;
  let updateLoaded = false;
  let updateAvailable = false;
  let updateWatching = false;
  let updateDeadline = 0;
  let updateRunId = null;
  let updateStartRunId = null;
  let updateAwaitingRun = false;
  let updateLastStage = "";
  let updateLastRunId = null;
  // Per-address and per-server view state lives only in this page's memory and
  // is forgotten with the rest of the private details on lock or navigation.
  const collapsedHosts = new Set();
  const collapsedCatalogs = new Set();
  const panels = ["traffic-panel", "summary-panel", "settings-panel", "hosts-panel", "backends-panel", "models-panel", "aliases-panel", "performance-panel", "self-test-panel", "inference-panel", "update-panel", "links-panel", "about-panel"];
  const trafficKinds = {timeout: "Timeouts", connection: "Connection errors", http_5xx: "Backend 5xx errors", http_4xx: "Backend 4xx errors", invalid_response: "Invalid responses", configuration: "Configuration", adapter: "Adapter crashes", no_eligible_model: "No eligible model", other: "Other"};
  const trafficWindows = {"24h": ["Last 24 hours", 24], "7d": ["Last 7 days", 168], all: ["All time", 168]};
  let settingsGeneration = 0;
  let activeSettings = null;
  let routingSettings = null;
  const settingFields = {advertise_machine_aliases: "setting-advertise-machine", prefer_fastest_replica: "setting-prefer-fastest", prefer_first_token: "setting-prefer-first-token", race_replicas: "setting-race"};
  const settingNumbers = {
    race_every: ["setting-race-every", 2, 1000, "Race every"],
    first_token_timeout_seconds: ["setting-first-token-timeout", 5, 3600, "First-token timeout"],
    idle_timeout_seconds: ["setting-idle-timeout", 5, 3600, "Idle timeout"],
    max_request_seconds: ["setting-max-request", 0, 86400, "Request cap"],
  };
  let trafficWindow = "24h";
  let trafficData = null;
  let trafficCheckedAt = 0;
  const text = (id, value) => { el(id).textContent = String(value); };
  const meta = (id, value) => { el(id).textContent = String(value == null ? "" : value); };
  const compact = (value) => {
    if (!Number.isFinite(value) || value < 0) return "—";
    if (value < 10000) return value.toLocaleString(undefined, {maximumFractionDigits: 0});
    for (const [suffix, size] of [["B", 1e9], ["M", 1e6], ["K", 1e3]]) {
      if (value >= size) {
        const scaled = value / size;
        return `${scaled.toLocaleString(undefined, {maximumFractionDigits: scaled >= 100 ? 0 : 1})}${suffix}`;
      }
    }
    return String(value);
  };
  const percent = (part, whole) => whole > 0 ? `${(100 * part / whole).toLocaleString(undefined, {maximumFractionDigits: 1})}%` : "—";
  const count = (value) => Number.isFinite(value) && value >= 0 ? String(value) : "—";
  const date = (value) => {
    if (!value) return "Not yet checked";
    const parsed = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleString();
  };
  const uptime = (value) => {
    if (!Number.isFinite(value) || value < 0) return "—";
    const seconds = Math.floor(value);
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
    return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
  };
  function safeOrigin(value) {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value) || /[\\\s?#]/.test(value)) return null;
    try {
      const parsed = new URL(value);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash) return null;
      return parsed.origin;
    } catch (error) {
      return null;
    }
  }
  function backendLink(item) {
    const origin = safeOrigin(item.address);
    if (!origin || item.enabled === false || item.state === "disabled") return item.address;
    const link = document.createElement("a");
    link.href = origin;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = origin;
    return link;
  }
  function clearSelfTest() {
    selfTestGeneration += 1;
    if (activeSelfTest) activeSelfTest.abort();
    activeSelfTest = null;
    el("self-test-button").disabled = false;
    el("self-test-results").hidden = true;
    el("self-test-body").replaceChildren();
    el("self-test-message").className = "self-test-message muted";
    text("self-test-message", "Runs only when you click. No self-test has been run in this page.");
    el("self-test-results").setAttribute("aria-busy", "false");
    meta("self-test-meta", "Not run");
  }
  function authControls(message) {
    el("auth-section").hidden = !authRequired;
    el("key-form").hidden = Boolean(apiKey);
    el("lock-button").hidden = !apiKey;
    text("auth-title", apiKey ? "Backend details unlocked" : "Unlock backend details");
    text("auth-message", message || (apiKey ? "Your key is held only in this page’s memory." : "Backend addresses and model details are locked."));
    updateControls();
    inferenceControls();
  }
  function consumeURLKey() {
    const query = new URLSearchParams(window.location.search || "");
    const originalHash = window.location.hash || "";
    const fragment = new URLSearchParams(originalHash.replace(/^#/, ""));
    const queryKeys = query.getAll("api_key");
    const fragmentKeys = fragment.getAll("api_key");
    const keys = queryKeys.concat(fragmentKeys);
    if (!keys.length) return "";
    el("url-key-message").hidden = true;
    text("url-key-message", "");
    const warn = message => { el("url-key-message").hidden = false; text("url-key-message", message); };
    const logWarning = queryKeys.length ? " Query-string keys may already be recorded in server, proxy, or browser logs; prefer #api_key= over HTTPS or a trusted connection." : "";
    let search = window.location.search || "";
    let hash = originalHash;
    if (queryKeys.length) { query.delete("api_key"); search = query.toString() ? `?${query}` : ""; }
    if (fragmentKeys.length) { fragment.delete("api_key"); hash = fragment.toString() ? `#${fragment}` : ""; }
    try {
      window.history.replaceState(null, "", `${window.location.pathname}${search}${hash}`);
    } catch (error) {
      warn("The URL key could not be removed from the address bar, so it was not used. Remove api_key from the URL and unlock manually." + logWarning);
      return "";
    }
    if (keys.length !== 1 || !keys[0].trim() || keys[0].trim().length > 4096 || /[\u0000-\u0020\u007f]/.test(keys[0].trim())) {
      warn("The URL key was removed but not used because it was empty, invalid, or supplied more than once. Unlock manually with one router API key." + logWarning);
      return "";
    }
    if (!authRequired || !["/", "/status"].includes(window.location.pathname)) {
      warn("The URL key was removed and was not used. This page does not accept an unlock key in its current configuration." + logWarning);
      return "";
    }
    if (queryKeys.length) warn("The URL key was removed from the address bar and will be held only in this page’s memory." + logWarning);
    return keys[0].trim();
  }
  function clearDetails(keepUpdate = false, keepInference = false) {
    clearSelfTest();
    clearHosts();
    clearSettings();
    clearPerformance();
    clearTraffic();
    if (!keepUpdate) clearUpdate();
    if (!keepInference) clearInference();
    el("details").hidden = true;
    el("self-test-panel").hidden = true;
    for (const id of ["endpoints-body", "models-body", "aliases-body"]) el(id).replaceChildren();
    for (const id of ["count-endpoints", "count-online", "count-models", "count-aliases", "uptime", "last-discovery"]) text(id, "—");
    for (const id of ["backends-meta", "models-meta", "aliases-meta"]) meta(id, "");
    el("alias-conflicts").hidden = true;
    text("alias-conflicts", "");
    text("version", "");
    el("setup-help").hidden = true;
  }
  function health(tone, title, message, gateway, models) {
    el("health-panel").className = `health-panel tone-${tone}`;
    text("health-title", title);
    text("health-message", message);
    text("gateway-state", gateway);
    text("model-readiness", models);
  }
  function unavailable(message) {
    clearDetails(Boolean(apiKey) && updateWatching, Boolean(apiKey) && inferenceWatching);
    clearSummary();
    text("checked-at", "No current snapshot");
    health("error", "Status could not be confirmed", message, "Unconfirmed", "Unknown");
  }
  function badge(label, tone) {
    const node = document.createElement("span");
    node.className = `badge badge-${tone}`;
    node.textContent = label;
    return node;
  }
  function cell(row, value, secondary, className) {
    const node = document.createElement("td");
    if (value instanceof Node) node.append(value);
    else node.textContent = String(value == null ? "—" : value);
    if (className) node.className = className;
    if (secondary) {
      const small = document.createElement("span");
      small.className = "secondary";
      small.textContent = String(secondary);
      node.append(small);
    }
    row.append(node);
  }
  function rows(id, items, columns, empty, render) {
    const body = el(id);
    body.replaceChildren();
    if (!Array.isArray(items) || items.length === 0) {
      const row = document.createElement("tr");
      const node = document.createElement("td");
      node.colSpan = columns;
      node.className = "empty-row";
      node.textContent = empty;
      row.append(node);
      body.append(row);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const row = document.createElement("tr");
      render(row, item);
      fragment.append(row);
    }
    body.append(fragment);
  }
  function hostControls() {
    const enabled = authRequired && Boolean(apiKey) && !el("details").hidden;
    const busy = Boolean(activeHosts);
    el("host-address").disabled = !enabled || busy;
    el("host-save-button").disabled = !enabled || busy || savedHosts.length >= hostsLimit;
    el("hosts-reload-button").disabled = !enabled || busy;
    el("hosts-check-button").disabled = !enabled || busy || savedHosts.length === 0;
    el("hosts-results").setAttribute("aria-busy", String(busy));
  }
  function hostMessage(message, tone = "") {
    el("hosts-message").className = tone ? `muted result-${tone}` : "muted";
    text("hosts-message", message);
  }
  function clearHosts() {
    hostsGeneration += 1;
    if (activeHosts) activeHosts.abort();
    activeHosts = null;
    hostsLoaded = false;
    savedHosts = [];
    collapsedHosts.clear();
    collapsedCatalogs.clear();
    el("host-address").value = "";
    el("hosts-body").replaceChildren();
    hostMessage(authRequired ? "Unlock details to manage saved addresses." : "Address management is disabled. Set LLM_ROUTER_GATEWAY_API_KEY in router.env and restart the router to enable it.");
    meta("hosts-meta", "");
    hostControls();
  }
  function validHost(item) {
    return item && typeof item.id === "string" && item.id.length > 0 && item.id.length <= 64 &&
      typeof item.address === "string" && item.address.length > 0 && item.address.length <= 256 &&
      (item.checked_at === null || typeof item.checked_at === "string") && validRouting(item.routing) &&
      Array.isArray(item.checks) && item.checks.length <= 8 && item.checks.every(check => check &&
        ["pass", "fail"].includes(check.status) && typeof check.provider === "string" &&
        typeof check.base_url === "string" && typeof check.detail === "string" && validCatalog(check));
  }
  function validRouting(routing) {
    return routing === undefined || (routing && ["active", "offline", "empty", "pending", "error", "managed"].includes(routing.status) &&
      Number.isSafeInteger(routing.model_count) && routing.model_count >= 0 && typeof routing.detail === "string");
  }
  function hostRouting(routing) {
    const result = document.createElement("div");
    result.className = "host-routing";
    const states = {active: ["Routing enabled", "ready"], offline: ["Backend offline", "warning"], empty: ["No models enrolled", "warning"], pending: ["Enrollment pending", "neutral"], error: ["Enrollment needs attention", "error"], managed: ["Explicitly configured", "neutral"]};
    if (!routing) {
      result.append(badge("Routing status unavailable", "neutral"));
      return result;
    }
    const state = states[routing.status];
    result.append(badge(state[0], state[1]));
    for (const value of [`Known routing models: ${routing.model_count}`, routing.detail]) {
      const detail = document.createElement("span");
      detail.className = "secondary";
      detail.textContent = value;
      result.append(detail);
    }
    return result;
  }
  function validCatalog(check) {
    // Cached checks from an older router version do not include a catalog.
    if (check.catalog_status === undefined) return true;
    return ["ok", "error"].includes(check.catalog_status) &&
      typeof check.catalog_detail === "string" && typeof check.catalog_url === "string" &&
      typeof check.models_truncated === "boolean" && Array.isArray(check.models) && check.models.length <= 200 &&
      check.models.every(model => model && typeof model.id === "string" && model.id.length > 0 && model.id.length <= 1024 && Array.from(model.id).length <= 512 &&
        typeof model.address === "string" && model.address.length > 0 && model.address.length <= 512) &&
      (check.catalog_status === "error" ? check.model_count === null :
        Number.isSafeInteger(check.model_count) && check.model_count >= check.models.length);
  }
  function validatedHosts(data) {
    if (!data || !Array.isArray(data.hosts) || data.hosts.length > 16 || !data.hosts.every(validHost) ||
      new Set(data.hosts.map(item => item.id)).size !== data.hosts.length) throw new Error("invalid-hosts-response");
    return data.hosts;
  }
  function hostCatalog(item, check) {
    const catalog = document.createElement("details");
    catalog.className = "host-catalog";
    const key = `${item.id}\n${check.provider}\n${check.base_url}`;
    catalog.open = !collapsedCatalogs.has(key);
    catalog.addEventListener("toggle", () => { if (catalog.open) collapsedCatalogs.delete(key); else collapsedCatalogs.add(key); });
    const summary = document.createElement("summary");
    summary.className = "host-catalog-summary";
    const title = document.createElement("span");
    title.className = "host-catalog-title";
    title.textContent = "Models listed by this server";
    const state = document.createElement("span");
    state.className = "muted";
    summary.append(title, state);
    catalog.append(summary);
    if (check.catalog_status === undefined) {
      state.textContent = "Check again to retrieve model list.";
      return catalog;
    }
    if (check.catalog_status === "error") {
      state.className = "muted catalog-warning";
      state.textContent = `Model list unavailable; model count is unknown. ${check.catalog_detail}`;
    } else {
      state.textContent = check.model_count === 0 ? "No models listed by this server." : `${check.model_count} model${check.model_count === 1 ? "" : "s"} listed by this server.`;
      if (check.models_truncated) {
        const truncated = document.createElement("p");
        truncated.className = "muted catalog-warning";
        truncated.textContent = `Showing ${check.models.length} of ${check.model_count} models. The list is truncated.`;
        catalog.append(truncated);
      }
      if (check.models.length) {
        const list = document.createElement("ul");
        list.className = "host-model-list";
        list.setAttribute("aria-label", `Model IDs reported by ${check.provider}`);
        // The router reports one API address per server check; show it once
        // unless the catalog really does mix addresses.
        const shared = new Set(check.models.map(model => model.address)).size === 1 ? check.models[0].address : null;
        for (const model of check.models) {
          const entry = document.createElement("li");
          const code = document.createElement("code");
          code.textContent = model.id;
          entry.append(code);
          if (shared === null) {
            const where = document.createElement("span");
            where.className = "secondary";
            where.textContent = model.address;
            entry.append(where);
          }
          list.append(entry);
        }
        catalog.append(list);
        if (shared !== null) {
          const address = document.createElement("p");
          address.className = "muted host-api-address host-models-address";
          address.textContent = `Models API address: ${shared}`;
          catalog.append(address);
        }
      }
    }
    const source = document.createElement("p");
    source.className = "muted host-api-address";
    source.textContent = `Model-list endpoint: ${check.catalog_url}`;
    const notice = document.createElement("p");
    notice.className = "muted";
    notice.textContent = "Metadata only. Listed models may not be loaded; inference is not tested.";
    catalog.append(source, notice);
    return catalog;
  }
  function clearSummary() {
    for (const id of ["public-count-servers", "public-count-models", "public-last-verified"]) text(id, "Unknown");
    text("public-summary-note", "No current summary. Counts do not prove models are loaded or inference works.");
    meta("summary-meta", "");
  }
  function renderSummary(summary) {
    clearSummary();
    if (!summary || typeof summary !== "object") return;
    text("public-count-servers", Number.isSafeInteger(summary.servers) && summary.servers >= 0 ? summary.servers : "Unknown");
    text("public-count-models", Number.isSafeInteger(summary.models) && summary.models >= 0 ? `${summary.models}${summary.models_truncated === true ? "+" : ""}` : "Unknown");
    if (Number.isSafeInteger(summary.servers) && summary.servers >= 0 && Number.isSafeInteger(summary.models) && summary.models >= 0) {
      meta("summary-meta", `${summary.servers} server${summary.servers === 1 ? "" : "s"} · ${summary.models}${summary.models_truncated === true ? "+" : ""} model cop${summary.models === 1 ? "y" : "ies"}`);
    }
    if (summary.last_verified_at === null) text("public-last-verified", "Not yet verified");
    else if (typeof summary.last_verified_at === "string") text("public-last-verified", date(summary.last_verified_at));
    text("public-summary-note", summary.models_truncated === true
      ? "Model lists are incomplete; the model count is a lower bound. Cached metadata does not prove models are loaded or inference works."
      : "Counts reflect cached metadata, not a live network scan. They do not prove models are loaded or inference works.");
  }
  function hostCheck(item, check) {
    const result = document.createElement("div");
    result.className = "host-check";
    const heading = document.createElement("div");
    heading.className = "host-check-heading";
    const name = document.createElement("strong");
    name.textContent = `${check.provider} `;
    heading.append(name, badge(check.status === "pass" ? "Found" : "Not confirmed", check.status === "pass" ? "ready" : "warning"));
    const address = document.createElement("p");
    address.className = "host-api-address address";
    address.textContent = `API base: ${check.base_url}`;
    const detail = document.createElement("p");
    detail.className = "muted";
    const elapsed = Number.isFinite(check.elapsed_ms) && check.elapsed_ms >= 0 ? ` · ${Math.round(check.elapsed_ms)} ms` : "";
    detail.textContent = `${check.detail}${Number.isInteger(check.http_status) ? ` · HTTP ${check.http_status}` : ""}${elapsed}`;
    result.append(heading, address, detail, hostCatalog(item, check));
    return result;
  }
  function hostEntry(item, index) {
    const entry = document.createElement("li");
    entry.className = "host-entry";
    const head = document.createElement("div");
    head.className = "host-entry-head";
    const identity = document.createElement("div");
    identity.className = "host-entry-identity";
    const address = document.createElement("span");
    address.className = "address";
    address.textContent = item.address;
    identity.append(address, hostRouting(item.routing));
    const checked = document.createElement("div");
    checked.className = "host-entry-checked";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = "Last checked";
    const when = document.createElement("span");
    when.textContent = date(item.checked_at);
    checked.append(label, when);
    const results = document.createElement("div");
    results.className = "host-checks";
    results.id = `host-checks-${index}`;
    results.hidden = collapsedHosts.has(item.id);
    if (!item.checks.length) {
      const none = document.createElement("p");
      none.className = "muted";
      none.textContent = "Not checked in this router session";
      results.append(none);
    }
    for (const check of item.checks) results.append(hostCheck(item, check));
    const actions = document.createElement("div");
    actions.className = "hosts-actions";
    for (const [label, action] of [["Check", "check"], ["Remove", "remove"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.disabled = Boolean(activeHosts) || !authRequired || !apiKey;
      button.setAttribute("aria-label", `${label} ${item.address}`);
      button.addEventListener("click", () => hostOperation(action, item.id));
      actions.append(button);
    }
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "host-toggle";
    toggle.setAttribute("aria-controls", results.id);
    const describeToggle = () => {
      toggle.textContent = results.hidden ? "Show results" : "Hide results";
      toggle.setAttribute("aria-expanded", String(!results.hidden));
      toggle.setAttribute("aria-label", `${results.hidden ? "Show" : "Hide"} results for ${item.address}`);
    };
    describeToggle();
    toggle.addEventListener("click", () => {
      results.hidden = !results.hidden;
      if (results.hidden) collapsedHosts.add(item.id); else collapsedHosts.delete(item.id);
      describeToggle();
    });
    actions.append(toggle);
    head.append(identity, checked, actions);
    entry.append(head, results);
    return entry;
  }
  function renderHosts() {
    const body = el("hosts-body");
    body.replaceChildren();
    if (!savedHosts.length) {
      const empty = document.createElement("li");
      empty.className = "empty-row";
      empty.textContent = "No saved addresses. Add a machine above to check its backend ports.";
      body.append(empty);
    } else {
      const fragment = document.createDocumentFragment();
      savedHosts.forEach((item, index) => fragment.append(hostEntry(item, index)));
      body.append(fragment);
    }
    const offline = savedHosts.filter(item => item.routing && item.routing.status === "offline").length;
    meta("hosts-meta", savedHosts.length ? `${savedHosts.length} saved${offline ? ` · ${offline} offline` : ""}` : "None saved");
    hostControls();
  }
  async function hostOperation(action, id) {
    if (activeHosts || el("details").hidden || !authRequired || !apiKey) return;
    const address = el("host-address").value.trim();
    if (action === "save" && !address) return;
    const currentGeneration = ++hostsGeneration;
    const controller = new AbortController();
    activeHosts = controller;
    hostsLoaded = true;
    let refreshRouting = false;
    if (action === "check") savedHosts = savedHosts.map(item => !id || item.id === id ? {...item, checked_at: null, checks: [], routing: {status: "pending", model_count: item.routing ? item.routing.model_count : 0, detail: "Checking metadata and refreshing routing enrollment."}} : item);
    renderHosts();
    hostMessage(action === "load" ? "Loading saved addresses…" : action === "remove" ? "Removing saved address and its automatically managed routes…" : action === "save" ? "Saving address, checking metadata, and enabling discovered routes…" : "Checking saved addresses and updating routing… No models are being used.");
    const timeout = setTimeout(() => controller.abort(), 15000);
    const request = async (path, method = "GET", body) => {
      const headers = {Accept: "application/json", Authorization: `Bearer ${apiKey}`};
      if (method !== "GET") {
        headers["X-LLM-Router-Hosts"] = "1";
        headers["Content-Type"] = "application/json";
      }
      const options = {method, headers, credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal};
      if (body !== undefined) options.body = JSON.stringify(body);
      const response = await fetch(path, options);
      if (currentGeneration !== hostsGeneration || controller.signal.aborted) throw new Error("cancelled-hosts-request");
      if (!response.ok) {
        const error = new Error("hosts-response");
        error.httpStatus = response.status;
        throw error;
      }
      const data = await response.json();
      if (currentGeneration !== hostsGeneration || controller.signal.aborted) throw new Error("cancelled-hosts-request");
      return data;
    };
    try {
      if (action === "load") {
        const data = await request("/status/hosts");
        savedHosts = validatedHosts(data);
        if (Number.isInteger(data.limit) && data.limit > 0 && data.limit <= 16) hostsLimit = data.limit;
        hostMessage(`${savedHosts.length} / ${hostsLimit} addresses saved on this router. Metadata and routing are rechecked at startup and every 30 seconds. This list shows the latest cached results.`);
      } else if (action === "remove") {
        const data = await request(`/status/hosts/${encodeURIComponent(id)}`, "DELETE");
        if (!data || data.removed !== true) throw new Error("invalid-hosts-response");
        savedHosts = savedHosts.filter(item => item.id !== id);
        hostMessage("Address removed from future saved checks and its automatically managed routes. Explicitly configured routes are preserved.");
        refreshRouting = true;
      } else {
        let checked;
        if (action === "save") {
          const data = await request("/status/hosts", "POST", {address});
          if (!data || !validHost(data.host)) throw new Error("invalid-hosts-response");
          const existing = savedHosts.findIndex(item => item.id === data.host.id);
          if (existing >= 0) savedHosts[existing] = data.host;
          else savedHosts.push(data.host);
          el("host-address").value = "";
          checked = [data.host];
        } else {
          checked = validatedHosts(await request("/status/hosts/check", "POST", id ? {id} : {}));
          if (!id) savedHosts = checked;
          else {
            if (checked.length !== 1 || checked[0].id !== id) throw new Error("invalid-hosts-response");
            savedHosts = savedHosts.map(item => item.id === id ? checked[0] : item);
          }
        }
        const found = checked.reduce((total, item) => total + item.checks.filter(check => check.status === "pass").length, 0);
        const active = checked.some(item => item.routing && item.routing.status === "active");
        const pending = checked.some(item => item.routing && item.routing.status === "pending");
        const error = checked.some(item => item.routing && item.routing.status === "error");
        hostMessage(`${action === "save" ? "Address saved. " : "Check complete. "}${found} backend API${found === 1 ? "" : "s"} found. ${pending ? "Enrollment is pending; an automatic check is queued. " : ""}See each address’s routing status below. Metadata only; no models were used.`, error ? "fail" : active ? "pass" : "");
        refreshRouting = true;
      }
    } catch (error) {
      if (currentGeneration !== hostsGeneration) return;
      if (error.httpStatus === 401 || error.httpStatus === 403) {
        cancelRefresh();
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend details and saved-address results have been cleared.");
        return;
      }
      const messages = {
        400: "Enter one valid IP address, hostname, or HTTP(S) backend URL without credentials, query strings, or fragments. At most 16 addresses can be saved; remove one if the list is full.",
        404: "That saved address no longer exists. Reload the saved list and try again.",
        409: "The saved-address limit was reached or the list changed. Reload the list or remove an address, then try again.",
        429: "Checks are busy or were requested too recently. Wait a moment, then try again.",
        503: "Saved-address storage or checks are unavailable. Check that the service account can access its saved-address file and configuration directory, then retry.",
      };
      hostMessage(messages[error.httpStatus] || "The request timed out, the connection failed, or the response was invalid. Reload saved addresses to confirm any changes before retrying.", "fail");
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === hostsGeneration) {
        activeHosts = null;
        renderHosts();
        if (refreshRouting) {
          cancelRefresh();
          refresh();
        }
      }
    }
  }
  function renderHealth(data, detailed) {
    renderSummary(data.summary);
    const ready = data.ready === true;
    const degraded = data.status === "degraded";
    const tone = ready ? (degraded ? "warning" : "ready") : "warning";
    const title = ready ? (degraded ? "Models available · some attention needed" : "Ready to route requests") : "Gateway running · no model ready";
    const message = detailed && data.notice ? data.notice : (ready
      ? "The gateway is responding and reports available models. Backend reachability is not an inference test."
      : "The gateway is responding, but no enabled model is currently available. Check backend connections and loaded models.");
    health(tone, title, message, "Responding", ready ? "Models available" : "No model available");
    text("checked-at", date(data.checked_at || new Date().toISOString()));
    text("uptime", uptime(data.uptime_seconds));
    text("version", data.version ? `Version ${data.version}` : "");
  }
  function clearPerformance() {
    el("performance-body").replaceChildren();
    text("performance-message", "");
    el("performance-message").className = "performance-message muted";
    meta("performance-meta", "");
  }
  function validObservation(metric) {
    return metric && Number.isFinite(metric.latest) && metric.latest >= 0 &&
      Number.isFinite(metric.ewma) && metric.ewma >= 0 && Number.isSafeInteger(metric.samples) && metric.samples > 0;
  }
  function metricNumber(value, unit) {
    const number = value > 0 && value < 0.01 ? "<0.01" : value.toLocaleString(undefined, {maximumFractionDigits: 2});
    return `${number} ${unit}`;
  }
  function observation(metric, unit) {
    const result = document.createElement("div");
    const value = document.createElement("strong");
    value.className = "metric-value";
    result.append(value);
    if (!validObservation(metric)) {
      value.textContent = "Not reported";
      return result;
    }
    value.textContent = metricNumber(metric.ewma, unit);
    for (const detail of [
      "Smoothed (EWMA)",
      `Latest: ${metricNumber(metric.latest, unit)} · ${metric.samples} sample${metric.samples === 1 ? "" : "s"}`,
      `Updated: ${metric.updated_at ? date(metric.updated_at) : "Not recorded"}`,
    ]) {
      const line = document.createElement("span");
      line.className = "metric-detail";
      line.textContent = detail;
      result.append(line);
    }
    return result;
  }
  function renderPerformance(performance) {
    clearPerformance();
    if (!performance || typeof performance !== "object") {
      text("performance-message", "This snapshot does not include performance metrics. Older router versions may not report them.");
      rows("performance-body", [], 8, "No performance data received.", () => {});
      return;
    }
    const validRows = Array.isArray(performance.deployments) && performance.deployments.every(item => item && typeof item === "object" &&
      typeof item.id === "string" && typeof item.model === "string" && typeof item.machine === "string" && typeof item.endpoint === "string");
    if (performance.available !== true || !validRows) {
      el("performance-message").className = "performance-message muted result-warning";
      text("performance-message", "Saved performance history is unavailable. Check the service account’s metrics-storage access and the router’s service logs. No current metrics are being shown.");
      rows("performance-body", [], 8, "Performance history is unavailable.", () => {});
      meta("performance-meta", "Unavailable");
      return;
    }
    meta("performance-meta", `${performance.deployments.length} observed`);
    const updated = performance.updated_at ? date(performance.updated_at) : "Not yet recorded";
    text("performance-message", `Saved observations updated: ${updated}. This table reads saved observations only; it does not generate traffic to models.`);
    if (performance.error) {
      el("performance-message").className = "performance-message muted result-warning";
      text("performance-message", `Performance storage reported a problem; saved observations may be incomplete. Last saved update: ${updated}. Check service permissions and logs.`);
    }
    rows("performance-body", performance.deployments, 8, "No routed requests yet. Performance will appear after real requests pass through this router.", (row, item) => {
      const identity = document.createElement("div");
      identity.className = "performance-identity";
      const model = document.createElement("strong");
      model.textContent = item.model;
      const machine = document.createElement("span");
      machine.className = "secondary";
      machine.textContent = `Server: ${item.machine} · ${item.endpoint}`;
      const adapter = document.createElement("span");
      adapter.className = "secondary";
      adapter.textContent = `API: ${typeof item.adapter === "string" ? item.adapter : "Not reported"}`;
      const address = document.createElement("code");
      address.textContent = safeOrigin(item.address) || "API address unavailable";
      identity.append(model, machine, adapter, address, badge(item.current === true ? "Current deployment" : item.current === false ? "Historical" : "Configuration unknown", "neutral"));
      cell(row, identity);
      const metrics = item.metrics && typeof item.metrics === "object" ? item.metrics : {};
      cell(row, observation(metrics.input_tokens_per_second, "tok/s"));
      cell(row, observation(metrics.output_tokens_per_second, "tok/s"));
      const load = observation(metrics.load_duration_ms, "ms");
      const slow = document.createElement("p");
      slow.className = "slow-load-count muted";
      const threshold = Number.isFinite(item.slow_load_threshold_ms) && item.slow_load_threshold_ms > 0 ? item.slow_load_threshold_ms : 1000;
      const slowCount = validObservation(metrics.load_duration_ms) && Number.isSafeInteger(item.slow_load_count) && item.slow_load_count >= 0 ? item.slow_load_count : "Not reported";
      slow.textContent = `Slow reported loads (≥${metricNumber(threshold / 1000, "s")}): ${slowCount}`;
      load.append(slow);
      cell(row, load);
      cell(row, observation(metrics.request_duration_ms, "ms"));
      cell(row, observation(metrics.first_token_ms, "ms"));
      const requests = document.createElement("div");
      for (const label of [
        `${count(item.successes)} succeeded / ${count(item.failures)} failed`,
        `Reported input tokens: ${count(item.input_tokens_total)}`,
        `Reported output tokens: ${count(item.output_tokens_total)}`,
      ]) {
        const line = document.createElement("span");
        line.className = "secondary";
        line.textContent = label;
        requests.append(line);
      }
      cell(row, requests);
      cell(row, item.last_seen_at ? date(item.last_seen_at) : "Not recorded");
    });
  }
  function validSettings(settings) {
    return Boolean(settings) && typeof settings === "object" &&
      Object.keys(settingFields).every(name => typeof settings[name] === "boolean") &&
      Object.entries(settingNumbers).every(([name, [, low, high]]) => Number.isSafeInteger(settings[name]) && settings[name] >= low && settings[name] <= high);
  }
  function validRoutingState(routing) {
    return Boolean(routing) && typeof routing === "object" && validSettings(routing.settings) &&
      Boolean(routing.storage) && typeof routing.storage === "object" && typeof routing.storage.available === "boolean" &&
      (routing.storage.error === null || typeof routing.storage.error === "string") &&
      Array.isArray(routing.races) && routing.races.length <= 5;
  }
  function settingsControls() {
    const enabled = authRequired && Boolean(apiKey) && !el("details").hidden && routingSettings !== null && !activeSettings;
    for (const id of [...Object.values(settingFields), ...Object.values(settingNumbers).map(([id]) => id)]) el(id).disabled = !enabled;
  }
  function settingsMessage(message, tone = "") {
    el("settings-message").className = tone ? `muted result-${tone}` : "muted";
    text("settings-message", message);
  }
  function restoreSettingInputs() {
    if (!routingSettings) return;
    for (const [name, id] of Object.entries(settingFields)) el(id).checked = routingSettings[name];
    for (const [name, [id]] of Object.entries(settingNumbers)) el(id).value = String(routingSettings[name]);
  }
  function clearSettings() {
    settingsGeneration += 1;
    if (activeSettings) activeSettings.abort();
    activeSettings = null;
    routingSettings = null;
    for (const id of Object.values(settingFields)) el(id).checked = false;
    for (const [id] of Object.values(settingNumbers)) el(id).value = "";
    el("settings-races").hidden = true;
    el("settings-races-list").replaceChildren();
    meta("settings-meta", "");
    settingsMessage(authRequired ? "Unlock backend details to change routing settings." : "Routing settings are disabled. Set LLM_ROUTER_GATEWAY_API_KEY in router.env and restart the router to enable them.");
    settingsControls();
  }
  function renderRaces(races) {
    const list = el("settings-races-list");
    list.replaceChildren();
    const valid = races.filter(race => race && typeof race === "object" && typeof race.group === "string" &&
      (race.winner === null || typeof race.winner === "string") && Boolean(race.participants) && typeof race.participants === "object" && !Array.isArray(race.participants));
    el("settings-races").hidden = valid.length === 0;
    for (const race of valid) {
      const item = document.createElement("li");
      const head = document.createElement("strong");
      head.textContent = `${race.group} · ${typeof race.started_at === "string" ? date(race.started_at) : "Unknown time"}`;
      const detail = document.createElement("span");
      detail.className = "secondary";
      const parts = Object.entries(race.participants)
        .filter(([, result]) => result && typeof result === "object")
        .map(([name, result]) => `${name}${name === race.winner ? " (winner)" : ""}: ${result.success === true
          ? (Number.isFinite(result.latency_ms) ? `${Math.round(result.latency_ms)} ms` : "succeeded")
          : `failed${typeof result.kind === "string" ? ` (${result.kind})` : ""}`}`);
      detail.textContent = `${race.winner === null ? "No replica answered. " : ""}${parts.length ? parts.join(" · ") : "Waiting for results"}`;
      item.append(head, detail);
      list.append(item);
    }
  }
  function renderRouting(routing) {
    if (!validRoutingState(routing)) {
      routingSettings = null;
      settingsMessage("This router version does not report routing settings, or the snapshot was invalid. Update the router to change them here.", "warning");
      meta("settings-meta", "Unavailable");
      settingsControls();
      return;
    }
    routingSettings = routing.settings;
    restoreSettingInputs();
    meta("settings-meta", [
      routing.settings.advertise_machine_aliases ? "machine names shown" : "machine names hidden",
      routing.settings.prefer_fastest_replica ? (routing.settings.prefer_first_token ? "fastest first token first" : "fastest first") : "",
      routing.settings.race_replicas ? `race every ${routing.settings.race_every}` : "",
      `first token ${routing.settings.first_token_timeout_seconds}s / idle ${routing.settings.idle_timeout_seconds}s${routing.settings.max_request_seconds ? ` / cap ${routing.settings.max_request_seconds}s` : ""}`,
    ].filter(Boolean).join(" · "));
    if (!authRequired) {
      settingsMessage("Routing settings can be viewed here, but changing them requires LLM_ROUTER_GATEWAY_API_KEY in router.env and a router restart.");
    } else if (!activeSettings) {
      settingsMessage(routing.storage.available
        ? "Settings are saved on the router and apply to new requests immediately; no restart is needed."
        : routing.storage.error, routing.storage.available ? "" : "warning");
    }
    renderRaces(routing.races);
    settingsControls();
  }
  async function saveSettings(changes) {
    if (activeSettings || el("details").hidden || !authRequired || !apiKey || routingSettings === null) return;
    const currentGeneration = ++settingsGeneration;
    const controller = new AbortController();
    activeSettings = controller;
    settingsControls();
    settingsMessage("Saving routing settings…");
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/status/settings", {
        method: "POST", credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal,
        headers: {Accept: "application/json", Authorization: `Bearer ${apiKey}`, "X-LLM-Router-Settings": "1", "Content-Type": "application/json"},
        body: JSON.stringify(changes),
      });
      if (currentGeneration !== settingsGeneration) return;
      if (response.status === 401 || response.status === 403) {
        cancelRefresh();
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend details have been cleared.");
        return;
      }
      const data = await response.json();
      if (currentGeneration !== settingsGeneration) return;
      if (!response.ok) {
        const messages = {
          400: "That value was rejected; nothing changed.",
          503: "Settings could not be saved on the router; nothing changed. Check the service account’s configuration directory and routing-settings file.",
        };
        restoreSettingInputs();
        settingsMessage(messages[response.status] || "The request failed; nothing changed.", "fail");
        return;
      }
      if (!validRoutingState(data)) throw new Error("invalid-settings-response");
      activeSettings = null;
      renderRouting(data);
      settingsMessage("Saved. Changes apply to new requests immediately.", "pass");
      cancelRefresh();
      refresh();
    } catch (error) {
      if (currentGeneration !== settingsGeneration) return;
      restoreSettingInputs();
      settingsMessage("The request timed out, the connection failed, or the response was invalid. Reload the page to confirm the saved settings.", "fail");
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === settingsGeneration) {
        activeSettings = null;
        settingsControls();
      }
    }
  }
  function validBucket(bucket) {
    return Boolean(bucket) && typeof bucket === "object" && !Array.isArray(bucket) &&
      ["requests_ok", "requests_failed", "reroutes_ok", "reroutes_failed"].every(name => Number.isSafeInteger(bucket[name]) && bucket[name] >= 0) &&
      ["input_tokens", "output_tokens"].every(name => Number.isFinite(bucket[name]) && bucket[name] >= 0) &&
      Boolean(bucket.failures) && typeof bucket.failures === "object" && !Array.isArray(bucket.failures) &&
      Object.keys(bucket.failures).length <= 32 &&
      Object.entries(bucket.failures).every(([kind, total]) => kind.length <= 64 && Number.isSafeInteger(total) && total >= 0);
  }
  function validTraffic(traffic) {
    return Boolean(traffic) && typeof traffic === "object" && typeof traffic.available === "boolean" &&
      (traffic.since === null || typeof traffic.since === "string") &&
      validBucket(traffic.totals) && Boolean(traffic.windows) && typeof traffic.windows === "object" &&
      validBucket(traffic.windows["24h"]) && validBucket(traffic.windows["7d"]) &&
      Array.isArray(traffic.hourly) && traffic.hourly.length <= 744 &&
      traffic.hourly.every(row => validBucket(row) && typeof row.hour === "string" && Number.isFinite(Date.parse(row.hour)));
  }
  function trafficMessage(message, tone = "") {
    el("traffic-message").className = tone ? `traffic-message muted result-${tone}` : "traffic-message muted";
    text("traffic-message", message);
  }
  function validFailure(item) {
    return Boolean(item) && typeof item === "object" && typeof item.at === "string" && typeof item.model === "string" && item.model.length <= 128 &&
      typeof item.kind === "string" && item.kind.length <= 64 && typeof item.detail === "string" && item.detail.length <= 512 &&
      Number.isSafeInteger(item.status) && (item.api === "ollama" || item.api === "openai");
  }
  function renderRecentFailures(failures) {
    const body = el("failures-body");
    body.replaceChildren();
    const valid = Array.isArray(failures) ? failures.filter(validFailure).slice(0, 25) : [];
    el("failures-panel").hidden = valid.length === 0;
    text("failures-hint", valid.length ? `${valid.length} since the router started` : "");
    const kinds = {
      rejected: ["Rejected", "error"], no_eligible_model: ["No eligible model", "warning"], all_attempts_failed: ["All attempts failed", "error"],
      router_unavailable: ["Router unavailable", "warning"], router_error: ["Router error", "error"], internal_error: ["Internal error", "error"],
    };
    for (const item of valid) {
      const row = document.createElement("tr");
      cell(row, date(item.at));
      cell(row, item.model, item.api === "ollama" ? "Ollama API" : "OpenAI API", "address");
      const label = Object.prototype.hasOwnProperty.call(kinds, item.kind) ? kinds[item.kind] : [item.kind, "neutral"];
      cell(row, badge(label[0], label[1]), `HTTP ${item.status}`);
      cell(row, item.detail);
      body.append(row);
    }
  }
  function clearTraffic() {
    trafficData = null;
    trafficCheckedAt = 0;
    el("failures-body").replaceChildren();
    el("failures-panel").hidden = true;
    text("failures-hint", "");
    for (const id of ["traffic-tiles", "traffic-chart", "traffic-table-body"]) el(id).replaceChildren();
    text("traffic-table-hint", "");
    text("traffic-chart-title", "Requests per hour");
    meta("traffic-meta", "");
    trafficMessage("Unlock backend details to see client traffic.");
    el("traffic-panel").hidden = true;
  }
  function tile(label, value, unit) {
    const node = document.createElement("div");
    node.className = "tile";
    const name = document.createElement("span");
    name.className = "label";
    name.textContent = label;
    const figure = document.createElement("strong");
    figure.className = "tile-value";
    figure.textContent = value;
    if (unit) {
      const small = document.createElement("small");
      small.textContent = unit;
      figure.append(small);
    }
    node.append(name, figure);
    return node;
  }
  function caption(node, value) {
    const line = document.createElement("span");
    line.className = "tile-caption";
    line.textContent = value;
    node.append(line);
  }
  function segBar(parts, label) {
    const bar = document.createElement("div");
    bar.className = "seg-bar";
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", label);
    const total = parts.reduce((sum, [total]) => sum + total, 0);
    for (const [total_, className] of parts) {
      if (total_ <= 0) continue;
      const segment = document.createElement("span");
      segment.className = `seg ${className}`;
      segment.style.width = `${100 * total_ / total}%`;
      bar.append(segment);
    }
    return bar;
  }
  function barRows(entries, className) {
    const list = document.createElement("div");
    list.className = "bar-rows";
    const peak = Math.max(1, ...entries.map(([, total]) => total));
    for (const [label, total] of entries) {
      const row = document.createElement("div");
      row.className = "bar-row";
      const name = document.createElement("span");
      name.className = "label";
      name.textContent = label;
      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = `bar-fill ${className}`;
      fill.style.width = `${100 * total / peak}%`;
      track.append(fill);
      const value = document.createElement("span");
      value.className = "count";
      value.textContent = compact(total);
      row.append(name, track, value);
      list.append(row);
    }
    return list;
  }
  function renderTrafficTiles(bucket, windowLabel) {
    const tiles = el("traffic-tiles");
    tiles.replaceChildren();
    const requests = bucket.requests_ok + bucket.requests_failed;
    const reroutes = bucket.reroutes_ok + bucket.reroutes_failed;
    const failures = Object.values(bucket.failures).reduce((sum, total) => sum + total, 0);
    const requestsTile = tile("Client requests", compact(requests), windowLabel.toLowerCase());
    requestsTile.append(segBar([[bucket.requests_ok, "seg-ok"], [bucket.requests_failed, "seg-failed"]], `${percent(bucket.requests_ok, requests)} of requests succeeded`));
    caption(requestsTile, requests ? `${compact(bucket.requests_ok)} succeeded (${percent(bucket.requests_ok, requests)}) · ${compact(bucket.requests_failed)} failed` : "No requests in this window");
    const tokensTile = tile("Tokens, successful requests", `${compact(bucket.input_tokens)} in`, null);
    tokensTile.append(barRows([["In", bucket.input_tokens], ["Out", bucket.output_tokens]], "bar-neutral"));
    caption(tokensTile, `${compact(bucket.output_tokens)} out · backend-reported counts`);
    const reroutesTile = tile("HA reroutes", compact(reroutes), null);
    reroutesTile.append(segBar([[bucket.reroutes_ok, "seg-ok"], [bucket.reroutes_failed, "seg-failed"]], `${compact(bucket.reroutes_ok)} rescued, ${compact(bucket.reroutes_failed)} still failed`));
    caption(reroutesTile, reroutes
      ? `${compact(bucket.reroutes_ok)} rescued · ${compact(bucket.reroutes_failed)} still failed · ${(100 * reroutes / Math.max(1, requests)).toLocaleString(undefined, {maximumFractionDigits: 1})} per 100 requests`
      : "No reroutes were needed");
    const failuresTile = tile("Failed backend attempts", compact(failures), null);
    const merged = new Map();
    for (const [kind, total] of Object.entries(bucket.failures)) {
      if (total <= 0) continue;
      const label = Object.prototype.hasOwnProperty.call(trafficKinds, kind) ? trafficKinds[kind] : trafficKinds.other;
      merged.set(label, (merged.get(label) || 0) + total);
    }
    const sorted = Array.from(merged.entries()).sort((first, second) => second[1] - first[1]);
    if (sorted.length) failuresTile.append(barRows(sorted, "bar-failed"));
    else caption(failuresTile, "No failed attempts recorded");
    tiles.append(requestsTile, tokensTile, reroutesTile, failuresTile);
  }
  const SVG = "http://www.w3.org/2000/svg";
  function svgNode(tag, attributes) {
    const node = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  }
  function roundedTop(x, y, width, height, radius) {
    const r = Math.max(0, Math.min(radius, width / 2, height));
    return `M${x} ${y + height}V${y + r}Q${x} ${y} ${x + r} ${y}H${x + width - r}Q${x + width} ${y} ${x + width} ${y + r}V${y + height}Z`;
  }
  function renderTrafficChart(traffic, hours, now, windowLabel) {
    const chart = el("traffic-chart");
    const body = el("traffic-table-body");
    chart.replaceChildren();
    body.replaceChildren();
    const hourMs = 3600000;
    const current = Math.floor(now / hourMs) * hourMs;
    const byHour = new Map();
    for (const row of traffic.hourly) byHour.set(Math.floor(Date.parse(row.hour) / hourMs) * hourMs, row);
    const slots = [];
    for (let index = hours - 1; index >= 0; index -= 1) {
      const start = current - index * hourMs;
      slots.push({start, row: byHour.get(start) || null});
    }
    // Draw in real pixels so axis text keeps its size at every viewport width.
    const measured = chart.clientWidth;
    const width = Number.isFinite(measured) && measured >= 280 ? Math.round(measured) : 720;
    const height = 168, left = 40, right = 8, top = 10, baseline = 136;
    const plotWidth = width - left - right, plotHeight = baseline - top;
    const total = slot => slot.row ? slot.row.requests_ok + slot.row.requests_failed : 0;
    const peak = Math.max(1, ...slots.map(total));
    const okTotal = slots.reduce((sum, slot) => sum + (slot.row ? slot.row.requests_ok : 0), 0);
    const failedTotal = slots.reduce((sum, slot) => sum + (slot.row ? slot.row.requests_failed : 0), 0);
    const svg = svgNode("svg", {
      viewBox: `0 0 ${width} ${height}`, width, height, role: "img",
      "aria-label": `Requests per hour, ${windowLabel.toLowerCase()}: ${compact(okTotal)} succeeded, ${compact(failedTotal)} failed; busiest hour ${compact(okTotal + failedTotal ? peak : 0)} request${peak === 1 ? "" : "s"}.`,
    });
    svg.append(svgNode("line", {class: "grid", x1: left, x2: width - right, y1: top + plotHeight / 2, y2: top + plotHeight / 2}));
    svg.append(svgNode("line", {class: "axis", x1: left, x2: width - right, y1: baseline, y2: baseline}));
    const yPeak = svgNode("text", {x: left - 6, y: top + 4, "text-anchor": "end"});
    yPeak.textContent = compact(okTotal + failedTotal ? peak : 0);
    const yZero = svgNode("text", {x: left - 6, y: baseline, "text-anchor": "end"});
    yZero.textContent = "0";
    svg.append(yPeak, yZero);
    const slotWidth = plotWidth / hours;
    const barWidth = Math.max(1, Math.min(24, slotWidth - 2));
    const gap = 2;
    const dayStep = slotWidth * 24 >= 64 ? 1 : 2;
    let midnights = 0;
    slots.forEach((slot, index) => {
      const x = left + index * slotWidth + (slotWidth - barWidth) / 2;
      const ok = slot.row ? slot.row.requests_ok : 0;
      const failed = slot.row ? slot.row.requests_failed : 0;
      const when = new Date(slot.start);
      const group = svgNode("g", {});
      const title = svgNode("title", {});
      title.textContent = `${when.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"})} · ${compact(ok)} succeeded · ${compact(failed)} failed`;
      group.append(title, svgNode("rect", {class: "hit", x: left + index * slotWidth, y: top, width: slotWidth, height: plotHeight}));
      let y = baseline;
      if (ok > 0) {
        const barHeight = Math.max(1, plotHeight * ok / peak);
        y -= barHeight;
        group.append(barWidth >= 8 && failed === 0
          ? svgNode("path", {class: "bar-ok", d: roundedTop(x, y, barWidth, barHeight, 4)})
          : svgNode("rect", {class: "bar-ok", x, y, width: barWidth, height: barHeight}));
      }
      if (failed > 0) {
        const barHeight = Math.max(1, plotHeight * failed / peak);
        y -= barHeight + (ok > 0 ? gap : 0);
        group.append(barWidth >= 8
          ? svgNode("path", {class: "bar-failed", d: roundedTop(x, y, barWidth, barHeight, 4)})
          : svgNode("rect", {class: "bar-failed", x, y, width: barWidth, height: barHeight}));
      }
      svg.append(group);
      let labelled = index % 6 === 0;
      if (hours > 48) {
        labelled = when.getHours() === 0 && midnights % dayStep === 0;
        if (when.getHours() === 0) midnights += 1;
      }
      if (labelled) {
        const label = svgNode("text", {x: left + index * slotWidth + slotWidth / 2, y: baseline + 16, "text-anchor": "middle"});
        label.textContent = hours > 48 ? when.toLocaleDateString([], {weekday: "short", day: "numeric"}) : when.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
        svg.append(label);
      }
      if (slot.row && (ok || failed || slot.row.reroutes_ok || slot.row.reroutes_failed)) {
        const row = document.createElement("tr");
        cell(row, when.toLocaleString([], {weekday: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}));
        cell(row, compact(ok));
        cell(row, compact(failed));
        cell(row, `${compact(slot.row.reroutes_ok)} / ${compact(slot.row.reroutes_failed)}`);
        cell(row, compact(slot.row.input_tokens));
        cell(row, compact(slot.row.output_tokens));
        body.append(row);
      }
    });
    if (okTotal + failedTotal === 0) {
      const empty = svgNode("text", {class: "chart-empty", x: left + plotWidth / 2, y: top + plotHeight / 2, "text-anchor": "middle"});
      empty.textContent = "No requests in this window";
      svg.append(empty);
    }
    chart.append(svg);
    text("traffic-table-hint", `${hours} hours · ${body.children.length} with traffic`);
  }
  function drawTraffic() {
    if (!trafficData) return;
    for (const key of Object.keys(trafficWindows)) el(`traffic-window-${key}`).setAttribute("aria-pressed", String(key === trafficWindow));
    const [label, hours] = trafficWindows[trafficWindow];
    const bucket = trafficWindow === "all" ? trafficData.totals : trafficData.windows[trafficWindow];
    renderTrafficTiles(bucket, label);
    const chartLabel = trafficWindow === "all" ? trafficWindows["7d"][0] : label;
    renderTrafficChart(trafficData, hours, trafficCheckedAt, chartLabel);
    text("traffic-chart-title", `Requests per hour · ${chartLabel.toLowerCase()}`);
    meta("traffic-meta", `${compact(bucket.requests_ok + bucket.requests_failed)} requests · ${label.toLowerCase()}`);
  }
  function renderTraffic(traffic, checkedAt) {
    el("traffic-panel").hidden = false;
    trafficData = null;
    const stamp = Date.parse(typeof checkedAt === "string" ? checkedAt : "");
    trafficCheckedAt = Number.isFinite(stamp) ? stamp : Date.now();
    for (const id of ["traffic-tiles", "traffic-chart", "traffic-table-body"]) el(id).replaceChildren();
    text("traffic-table-hint", "");
    if (traffic === undefined || traffic === null) {
      trafficMessage("This router version does not report client traffic. Update the router to see request, token, and reroute counts.");
      meta("traffic-meta", "Not reported");
      return;
    }
    if (!validTraffic(traffic)) {
      trafficMessage("Client traffic could not be read from this snapshot, so no counts are shown.", "warning");
      meta("traffic-meta", "Unavailable");
      return;
    }
    if (traffic.available !== true) {
      trafficMessage("Client traffic history is unavailable. Check the service account’s metrics-storage access and the router’s service logs.", "warning");
      meta("traffic-meta", "Unavailable");
      return;
    }
    trafficData = traffic;
    const days = Number.isSafeInteger(traffic.retention_hours) && traffic.retention_hours > 0 ? Math.round(traffic.retention_hours / 24) : 30;
    trafficMessage(traffic.since
      ? `Counting since ${date(traffic.since)}. Hourly history is kept for ${days} days; requests are counted once each and tokens only for successful requests.`
      : "No client requests have been recorded yet. Counts appear after real requests pass through this router.");
    drawTraffic();
  }
  function renderDetails(data) {
    renderHealth(data, true);
    renderPerformance(data.performance);
    renderTraffic(data.performance && typeof data.performance === "object" ? data.performance.traffic : undefined, data.checked_at);
    renderRecentFailures(data.recent_failures);
    const totals = data.counts || {};
    text("count-endpoints", count(totals.endpoints));
    text("count-online", count(totals.online));
    text("count-models", `${count(totals.available_models)} / ${count(totals.models)}`);
    text("count-aliases", count(totals.aliases));
    meta("backends-meta", `${count(totals.online)} of ${count(totals.endpoints)} online`);
    meta("models-meta", `${count(totals.available_models)} of ${count(totals.models)} available`);
    meta("aliases-meta", `${count(totals.aliases)} client names`);
    text("last-discovery", date(data.last_discovery));
    el("setup-help").hidden = data.ready === true;
    rows("endpoints-body", data.endpoints, 5, "No backend has been discovered or configured yet.", (row, item) => {
      cell(row, item.machine || item.name, item.name && item.machine !== item.name ? item.name : item.adapter);
      cell(row, backendLink(item), null, "address");
      cell(row, item.state === "online" ? badge("Online", "ready") : item.state === "offline" ? badge("Offline", "error") : badge("Not checked", "neutral"));
      cell(row, `${count(item.available_models)} / ${count(item.model_count)}`);
      cell(row, date(item.last_checked));
    });
    const states = {available: ["Available", "ready"], offline: ["Backend offline", "error"], cooldown: ["Cooling down", "warning"], disabled: ["Disabled", "neutral"]};
    rows("models-body", data.models, 5, "No model deployments found. Start a backend and make a model available.", (row, item) => {
      const state = Object.prototype.hasOwnProperty.call(states, item.state) ? states[item.state] : ["Unknown", "neutral"];
      cell(row, item.name, item.deployment);
      cell(row, item.machine);
      cell(row, badge(state[0], state[1]));
      cell(row, count(item.active_requests));
      cell(row, `${count(item.successes)} / ${count(item.failures)}`);
    });
    const conflicts = Array.isArray(data.alias_conflicts) ? data.alias_conflicts.filter(name => typeof name === "string" && name.length > 0 && name.length <= 256).slice(0, 50) : [];
    el("alias-conflicts").hidden = conflicts.length === 0;
    text("alias-conflicts", conflicts.length
      ? `${conflicts.length} generated name${conflicts.length === 1 ? " was" : "s were"} left out because two different models or machines would share ${conflicts.length === 1 ? "it" : "them"}: ${conflicts.join(", ")}. Those models stay routable through the auto presets. Give one side a distinct replica_group or machine_id to get the names back.`
      : "");
    const kinds = {ha: "High availability", preferred: "Preferred + failover", pinned: "Pinned · no failover"};
    rows("aliases-body", data.aliases, 4, "No HA or machine aliases are available yet.", (row, item) => {
      cell(row, item.name, item.advertised === false ? "Hidden from client model lists" : null);
      cell(row, Object.prototype.hasOwnProperty.call(kinds, item.kind) ? kinds[item.kind] : "Unknown");
      cell(row, item.available ? badge("Available", "ready") : badge("Unavailable", "warning"));
      cell(row, count(item.deployments));
    });
    el("details").hidden = false;
    el("self-test-panel").hidden = false;
    renderRouting(data.routing);
    el("inference-panel").hidden = !authRequired || !apiKey;
    inferenceControls();
    hostControls();
    if (authRequired && apiKey && !hostsLoaded && !activeHosts) hostOperation("load");
    if (!authRequired) hostMessage("Address management is disabled. Set LLM_ROUTER_GATEWAY_API_KEY in router.env and restart the router to enable it.");
    updateControls();
    if (authRequired && apiKey && !updateLoaded && !activeUpdate) updateRequest("GET");
    if (!authRequired) clearUpdate();
  }
  function inferenceControls() {
    const authorized = authRequired && Boolean(apiKey) && pageActive && !document.hidden;
    el("inference-button").disabled = !authorized || el("details").hidden || inferenceWatching || Boolean(activeInference);
    el("inference-refresh-button").disabled = !authorized || Boolean(activeInference);
  }
  function inferenceMessage(message, tone = "") {
    el("inference-message").className = tone ? `muted result-${tone}` : "muted";
    text("inference-message", message);
  }
  function clearInference() {
    inferenceGeneration += 1;
    if (activeInference) activeInference.abort();
    if (inferencePollTimer !== null) clearTimeout(inferencePollTimer);
    activeInference = null;
    inferencePollTimer = null;
    inferenceWatching = false;
    inferenceAttempted = false;
    inferenceDeadline = 0;
    inferenceRunId = null;
    inferenceBaselineId = null;
    el("inference-panel").hidden = true;
    el("inference-results").hidden = true;
    el("inference-body").replaceChildren();
    el("inference-progress").hidden = true;
    el("inference-refresh-button").hidden = true;
    meta("inference-meta", "Not run");
    inferenceMessage("No inference test has been requested in this page.");
    inferenceControls();
  }
  function inferenceUnknown(message) {
    inferenceWatching = false;
    inferenceDeadline = 0;
    el("inference-progress").hidden = true;
    el("inference-refresh-button").hidden = false;
    meta("inference-meta", "Outcome unknown");
    inferenceMessage(message + " No inference request will be sent again automatically.", "fail");
  }
  function inferenceTimedOut() {
    if (!inferenceWatching || Date.now() < inferenceDeadline) return false;
    inferenceUnknown("Stopped waiting after 20 minutes. The test outcome is unknown; a backend may still be finishing a request. Refresh test status to read the local job.");
    return true;
  }
  function pollInference() {
    if (inferencePollTimer !== null) clearTimeout(inferencePollTimer);
    inferencePollTimer = null;
    if (!inferenceWatching || !apiKey || !pageActive || document.hidden || inferenceTimedOut()) { inferenceControls(); return; }
    inferencePollTimer = setTimeout(() => { inferencePollTimer = null; inferenceRequest("GET"); }, 2000);
  }
  function validInference(data) {
    const string = (value, limit) => typeof value === "string" && value.length <= limit;
    const timestamp = value => value === null || string(value, 64);
    return data && ["idle", "running", "complete", "interrupted"].includes(data.state) &&
      (data.run_id === null || string(data.run_id, 128) && data.run_id.length > 0) &&
      timestamp(data.started_at) && timestamp(data.finished_at) && string(data.notice, 2048) &&
      Number.isSafeInteger(data.total) && data.total >= 0 && data.total <= 10000 &&
      Number.isSafeInteger(data.completed) && data.completed >= 0 && data.completed <= data.total &&
      Array.isArray(data.checks) && data.checks.length === data.completed &&
      (data.state === "idle" ? data.run_id === null && data.total === 0 : Boolean(data.run_id)) &&
      (data.state !== "complete" || data.completed === data.total) &&
      data.checks.every(item => item && ["pass", "fail", "skip"].includes(item.status) &&
        string(item.name, 2048) && string(item.target, 2048) && (item.model === null || string(item.model, 2048)) &&
        string(item.selection, 2048) && string(item.detail, 4096) && Number.isSafeInteger(item.elapsed_ms) && item.elapsed_ms >= 0 &&
        (item.http_status === null || Number.isInteger(item.http_status) && item.http_status >= 100 && item.http_status <= 599));
  }
  function renderInference(data) {
    el("inference-panel").hidden = false;
    el("inference-refresh-button").hidden = false;
    if (!inferenceRunId) {
      if (data.run_id && data.run_id !== inferenceBaselineId) inferenceRunId = data.run_id;
      else {
        inferenceMessage("Waiting for a new test job to be confirmed. An older result or an idle router does not confirm that this request completed. Only local status will be retried.");
        return;
      }
    }
    if (data.run_id !== inferenceRunId) {
      inferenceUnknown("The requested test job is no longer available; the router may have restarted or another job replaced it. These results do not confirm completion.");
      return;
    }
    const labels = {pass: ["Pass", "ready"], fail: ["Fail", "error"], skip: ["Skipped", "warning"]};
    rows("inference-body", data.checks, 5, data.total ? "Waiting for the first backend result…" : "No backends were available to test.", (row, item) => {
      const label = labels[item.status];
      cell(row, item.name, item.target);
      cell(row, badge(label[0], label[1]));
      cell(row, item.model || "No eligible model selected", item.selection);
      cell(row, item.detail, item.http_status === null ? null : `HTTP ${item.http_status}`);
      cell(row, `${item.elapsed_ms.toLocaleString()} ms`);
    });
    el("inference-results").hidden = false;
    el("inference-progress").max = Math.max(1, data.total);
    el("inference-progress").value = data.completed;
    el("inference-progress").hidden = data.state !== "running";
    meta("inference-meta", `${data.completed} / ${data.total} checked`);
    if (data.state === "running") {
      inferenceWatching = true;
      if (!inferenceDeadline) inferenceDeadline = Date.now() + 20 * 60 * 1000;
      inferenceMessage(`${data.completed} / ${data.total} backends checked. A small inference request may be loading or running a model. Status polling never sends another prompt.`);
      return;
    }
    inferenceWatching = false;
    inferenceDeadline = 0;
    const passed = data.checks.filter(item => item.status === "pass").length;
    const failed = data.checks.filter(item => item.status === "fail").length;
    const skipped = data.checks.filter(item => item.status === "skip").length;
    inferenceMessage(`${data.state === "interrupted" ? "Test interrupted; not every backend is confirmed" : "Test finished"}: ${passed} passed, ${failed} failed, ${skipped} skipped (${data.completed} / ${data.total} checked). ${data.notice}`,
      failed || data.state === "interrupted" ? "fail" : skipped || !data.total ? "partial" : "pass");
  }
  async function inferenceRequest(method, prepare = false) {
    if (activeInference || !authRequired || !apiKey || !pageActive || document.hidden) return;
    if (method === "GET" && !prepare && (!inferenceAttempted || inferenceTimedOut())) { inferenceControls(); return; }
    if (method === "POST" && (inferenceWatching || el("details").hidden)) return;
    if (inferencePollTimer !== null) clearTimeout(inferencePollTimer);
    inferencePollTimer = null;
    const currentGeneration = ++inferenceGeneration;
    const controller = new AbortController();
    activeInference = controller;
    let launch = false;
    if (method === "POST") {
      inferenceAttempted = true;
      inferenceWatching = true;
      inferenceDeadline = Date.now() + 20 * 60 * 1000;
      inferenceRunId = null;
      el("inference-refresh-button").hidden = false;
      inferenceMessage("Requesting one tiny inference test per backend. Waiting for the new job; this page will not repeat the request automatically.");
    } else if (prepare) inferenceMessage("Reading local test status before starting. No inference has been requested yet.");
    inferenceControls();
    const headers = {Accept: "application/json", Authorization: `Bearer ${apiKey}`};
    if (method === "POST") headers["X-LLM-Router-Inference-Test"] = "1";
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/status/inference-test", {method, headers, credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal});
      if (currentGeneration !== inferenceGeneration) return;
      if (controller.signal.aborted) throw new Error("inference-request-aborted");
      if ([401, 403].includes(response.status)) {
        cancelRefresh();
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend and inference-test details have been cleared.");
        return;
      }
      if (method === "POST" && [400, 409, 429].includes(response.status)) {
        inferenceWatching = false;
        inferenceDeadline = 0;
        inferenceMessage(response.status === 409 ? "A test is already running; no new test was started. Refresh test status to inspect the local job."
          : response.status === 429 ? "Tests were requested too recently; no new test was started. Wait before clicking and confirming again."
          : "The inference request was rejected; no test was started.", "partial");
        return;
      }
      if (!response.ok || method === "POST" && response.status !== 202) throw new Error("inference-response");
      const data = await response.json();
      if (currentGeneration !== inferenceGeneration) return;
      if (controller.signal.aborted || !validInference(data)) throw new Error("invalid-inference-status");
      if (prepare) {
        if (data.state === "running") {
          inferenceAttempted = true;
          inferenceRunId = data.run_id;
          renderInference(data);
          inferenceMessage("An existing inference test is already running; following its progress without starting another request.");
        } else { inferenceBaselineId = data.run_id; launch = true; }
      } else renderInference(data);
    } catch (error) {
      if (currentGeneration !== inferenceGeneration) return;
      if (inferenceAttempted) {
        inferenceMessage("The test outcome could not be confirmed. Waiting for the router to reconnect; only local status will be read, never another inference request.", "partial");
      } else inferenceMessage("Local test status could not be read. No inference request was sent; click again when the router is reachable.", "fail");
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === inferenceGeneration) {
        activeInference = null;
        inferenceControls();
        if (launch) inferenceRequest("POST");
        else pollInference();
      }
    }
  }
  function runInferenceTest() {
    if (activeInference || inferenceWatching || !authRequired || !apiKey || !pageActive || document.hidden || el("details").hidden) return;
    if (!window.confirm("Run real inference on each backend (up to 16 per run)? This sends one tiny prompt to its smallest eligible chat model. It may load models from storage, use RAM / GPU memory or provider credits, and evict a loaded model. No models are downloaded and no fallback backend is used.")) return;
    clearInference();
    el("inference-panel").hidden = false;
    el("inference-panel").open = true;
    inferenceRequest("GET", true);
  }
  function updateControls() {
    const authorized = authRequired && Boolean(apiKey) && pageActive && !document.hidden;
    el("update-button").disabled = !authorized || !updateAvailable || updateWatching || Boolean(activeUpdate) || el("details").hidden;
    el("update-refresh-button").disabled = !authorized || Boolean(activeUpdate);
  }
  function updateMessage(message, tone = "") {
    el("update-message").className = tone ? `muted result-${tone}` : "muted";
    text("update-message", message);
  }
  function clearUpdate() {
    updateGeneration += 1;
    if (activeUpdate) activeUpdate.abort();
    if (updatePollTimer !== null) clearTimeout(updatePollTimer);
    activeUpdate = null;
    updatePollTimer = null;
    updateLoaded = false;
    updateAvailable = false;
    updateWatching = false;
    updateDeadline = 0;
    updateRunId = null;
    updateStartRunId = null;
    updateAwaitingRun = false;
    updateLastStage = "";
    updateLastRunId = null;
    el("update-details").hidden = true;
    el("update-progress").hidden = true;
    text("update-stage", "");
    text("update-observed", "");
    meta("update-meta", "");
    updateMessage(authRequired ? "Unlock backend details to enable software updates." : "Software updates are disabled without a router API key. Set LLM_ROUTER_GATEWAY_API_KEY and restart the router.");
    updateControls();
  }
  function updateTimedOut() {
    if (!updateWatching || Date.now() < updateDeadline) return false;
    updateWatching = false;
    updateAvailable = false;
    updateDeadline = 0;
    el("update-details").hidden = false;
    el("update-progress").hidden = true;
    text("update-stage", "Outcome unknown");
    updateMessage("Stopped waiting after 50 minutes. The update outcome is unknown; it may still be running. Refresh update status to read the local job. No update will be started again automatically.", "fail");
    return true;
  }
  function pollUpdate() {
    if (updatePollTimer !== null) clearTimeout(updatePollTimer);
    updatePollTimer = null;
    if (!updateWatching || !apiKey || !pageActive || document.hidden || updateTimedOut()) { updateControls(); return; }
    updatePollTimer = setTimeout(() => { updatePollTimer = null; updateRequest("GET"); }, 2000);
  }
  function validUpdate(data) {
    return data && typeof data.available === "boolean" && typeof data.busy === "boolean" &&
      ["idle", "queued", "running", "current", "succeeded", "failed", "unavailable", "interrupted"].includes(data.state) &&
      ["checking", "downloading", "validating", "restarting", "complete", "failed", "queued", "idle"].includes(data.stage) &&
      (data.run_id === null || typeof data.run_id === "string" && data.run_id.length > 0 && data.run_id.length <= 128) &&
      (data.updated_at === null || typeof data.updated_at === "string") && typeof data.current_version === "string" && data.current_version.length <= 64;
  }
  function renderUpdate(data) {
    const wasWatching = updateWatching;
    updateAvailable = data.available;
    el("update-details").hidden = false;
    if (wasWatching && (!data.available || data.state === "unavailable")) {
      el("update-progress").hidden = false;
      text("update-stage", updateLastStage || "Waiting for updater availability");
      updateMessage("Waiting for the router to restart or reconnect. Local updater verification is temporarily unavailable; the update outcome is not confirmed. Only saved status will be retried.");
      return;
    }
    if (updateAwaitingRun) {
      if (data.run_id && data.run_id !== updateStartRunId) { updateRunId = data.run_id; updateAwaitingRun = false; }
      else {
        el("update-progress").hidden = false;
        text("update-stage", "Awaiting job confirmation");
        updateMessage("Waiting for a new update job to be confirmed. A previous saved result does not confirm this request completed. No second update request will be sent.");
        return;
      }
    }
    if (updateRunId && data.run_id !== updateRunId) {
      updateWatching = true;
      if (!updateDeadline) updateDeadline = Date.now() + 50 * 60 * 1000;
      el("update-progress").hidden = false;
      text("update-stage", "Awaiting matching job result");
      updateMessage("The router is responding, but this is not the requested update job’s result. Completion is not confirmed; only local status will be polled.");
      return;
    }
    updateLastRunId = data.run_id;
    const stages = {checking: "Checking official main", downloading: "Downloading update", validating: "Validating installation", restarting: "Restarting router", complete: "Complete", failed: "Failed", queued: "Queued", idle: "Idle"};
    updateLastStage = stages[data.stage];
    text("update-stage", updateLastStage);
    meta("update-meta", updateLastStage);
    text("update-observed", `Installed version: ${data.current_version || "Unknown"} · Job status recorded: ${data.updated_at ? date(data.updated_at) : "Not yet recorded"}`);
    if (data.busy || ["queued", "running"].includes(data.state)) {
      updateWatching = true;
      if (!updateDeadline) updateDeadline = Date.now() + 50 * 60 * 1000;
      updateRunId = data.run_id;
      el("update-progress").hidden = false;
      updateMessage("Update in progress. The stage comes from the updater; no completion percentage is estimated. The router may briefly disconnect while restarting.");
      return;
    }
    if (wasWatching && (data.state === "idle" || !data.run_id)) {
      el("update-progress").hidden = false;
      text("update-stage", "Outcome not confirmed");
      updateMessage("The router is responding, but no terminal result for this update is available yet. Waiting for the local job record; no update will be retried automatically.");
      return;
    }
    updateWatching = false;
    updateDeadline = 0;
    updateRunId = null;
    updateStartRunId = null;
    updateAwaitingRun = false;
    el("update-progress").hidden = true;
    const messages = {
      idle: "Ready. Check for updates will check official main and install a newer commit, briefly restarting the router.",
      current: "Already up to date. The updater confirmed that no newer official-main commit needed installing.",
      succeeded: "Update completed successfully, as confirmed by the saved update job result.",
      failed: "The update job failed. Review the router update service logs before trying again. No update was retried automatically.",
      interrupted: "The update job was interrupted. Successful installation is not confirmed; inspect the update service logs before trying again.",
      unavailable: "Software updates are unavailable for this installation. " + (typeof data.message === "string" && data.message.length > 0 && data.message.length <= 1024 ? data.message : "Use the supported installer or update service."),
    };
    const message = !wasWatching && data.state === "current" ? "The last recorded check found no newer official-main commit. Reading this saved status does not check for new updates."
      : !wasWatching && data.state === "succeeded" ? "The last saved update job completed successfully. Reading this status does not check for new updates." : messages[data.state];
    updateMessage(message, ["current", "succeeded"].includes(data.state) ? "pass" : ["failed", "interrupted"].includes(data.state) ? "fail" : "");
    if (wasWatching) { cancelRefresh(); refresh(); }
  }
  async function updateRequest(method) {
    if (activeUpdate || !authRequired || !apiKey || !pageActive || document.hidden) return;
    if (method === "POST" && (!updateAvailable || updateWatching || el("details").hidden)) return;
    if (method === "GET" && updateTimedOut()) { updateControls(); return; }
    if (updatePollTimer !== null) clearTimeout(updatePollTimer);
    updatePollTimer = null;
    const currentGeneration = ++updateGeneration;
    const controller = new AbortController();
    activeUpdate = controller;
    updateLoaded = true;
    if (method === "POST") {
      updateWatching = true;
      updateDeadline = Date.now() + 50 * 60 * 1000;
      updateStartRunId = updateLastRunId;
      updateRunId = null;
      updateAwaitingRun = true;
      el("update-panel").open = true;
      text("update-stage", "Submitting update request");
      text("update-observed", "");
      el("update-details").hidden = false;
      el("update-progress").hidden = false;
      updateMessage("Requesting one official-main check and installation if newer. The router may restart; waiting for the update job record.");
    } else if (!updateWatching) {
      el("update-details").hidden = false;
      text("update-stage", "Reading local status");
      updateMessage("Reading the saved local update job; no remote update check is being started.");
    }
    updateControls();
    const headers = {Accept: "application/json", Authorization: `Bearer ${apiKey}`};
    if (method === "POST") headers["X-LLM-Router-Update"] = "1";
    const timeout = setTimeout(() => controller.abort(), method === "POST" ? 30000 : 15000);
    try {
      const response = await fetch("/status/update", {method, headers, credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal});
      if (currentGeneration !== updateGeneration) return;
      if (controller.signal.aborted) throw new Error("update-request-aborted");
      if (response.status === 401 || response.status === 403) {
        cancelRefresh();
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend and update details have been cleared.");
        return;
      }
      if (!response.ok && ![409, 503].includes(response.status)) throw new Error("update-response");
      const data = await response.json();
      if (currentGeneration !== updateGeneration) return;
      if (controller.signal.aborted) throw new Error("update-request-aborted");
      if (!validUpdate(data)) throw new Error("invalid-update-status");
      renderUpdate(data);
    } catch (error) {
      if (currentGeneration !== updateGeneration) return;
      el("update-details").hidden = false;
      if (updateWatching) {
        el("update-progress").hidden = false;
        text("update-stage", updateLastStage || "Awaiting job confirmation");
        updateMessage("Waiting for the router to restart or reconnect. The update outcome is not confirmed. This page will read local status only and will not submit another update request.");
      } else {
        updateAvailable = false;
        text("update-stage", "Status unavailable");
        updateMessage("Local update status could not be confirmed. Refresh update status to try reading it again; this does not start an update.", "fail");
      }
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === updateGeneration) { activeUpdate = null; updateControls(); pollUpdate(); }
    }
  }
  function cancelRefresh() {
    generation += 1;
    if (activeRequest) activeRequest.abort();
    activeRequest = null;
    el("refresh-button").disabled = false;
    el("health-panel").setAttribute("aria-busy", "false");
  }
  async function runSelfTest() {
    if (activeSelfTest || el("details").hidden || (authRequired && !apiKey)) return;
    clearSelfTest();
    const currentGeneration = ++selfTestGeneration;
    const controller = new AbortController();
    activeSelfTest = controller;
    const headers = {Accept: "application/json", "X-LLM-Router-Self-Test": "1"};
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    el("self-test-button").disabled = true;
    el("self-test-results").setAttribute("aria-busy", "true");
    text("self-test-message", "Checking router and backend metadata… No models are being used.");
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch("/status/self-test", {
        method: "POST", headers, credentials: "omit", cache: "no-store",
        redirect: "error", signal: controller.signal,
      });
      if (currentGeneration !== selfTestGeneration) return;
      if (response.status === 401 || response.status === 403) {
        cancelRefresh();
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend details and self-test results have been cleared.");
        return;
      }
      if (response.status === 429) {
        el("self-test-message").className = "self-test-message result-partial";
        text("self-test-message", "Self-test is busy or was run too recently. Wait a moment, then click to try again.");
        return;
      }
      if (!response.ok) throw new Error("self-test-response");
      const data = await response.json();
      if (currentGeneration !== selfTestGeneration) return;
      const validStatuses = ["pass", "fail", "skip"];
      if (!data || !["pass", "fail", "partial"].includes(data.status) || !Array.isArray(data.checks) || data.checks.length === 0 || data.checks.some(item => !item || !validStatuses.includes(item.status))) throw new Error("invalid-self-test");
      const labels = {pass: ["Pass", "ready"], fail: ["Fail", "error"], skip: ["Skipped", "warning"]};
      rows("self-test-body", data.checks, 4, "No checks returned.", (row, item) => {
        const state = labels[item.status];
        cell(row, item.name, item.target);
        cell(row, badge(state[0], state[1]));
        cell(row, item.detail, Number.isInteger(item.http_status) ? `HTTP ${item.http_status}` : null);
        cell(row, Number.isFinite(item.elapsed_ms) && item.elapsed_ms >= 0 ? `${Math.round(item.elapsed_ms)} ms` : "—");
      });
      el("self-test-results").hidden = false;
      el("self-test-message").className = `self-test-message result-${data.status}`;
      meta("self-test-meta", {pass: "Passed", fail: "Failed", partial: "Incomplete"}[data.status]);
      const summary = {pass: "Metadata checks passed", fail: "One or more checks failed", partial: "Self-test incomplete"};
      text("self-test-message", `${summary[data.status]} · ${date(data.checked_at)}. ${data.notice || "No models were used. This does not verify inference."}`);
    } catch (error) {
      if (currentGeneration !== selfTestGeneration) return;
      el("self-test-results").hidden = true;
      el("self-test-body").replaceChildren();
      el("self-test-message").className = "self-test-message result-fail";
      text("self-test-message", "Self-test could not complete. The request timed out, the connection failed, or the response was invalid. No successful result is being shown.");
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === selfTestGeneration) {
        activeSelfTest = null;
        el("self-test-button").disabled = false;
        el("self-test-results").setAttribute("aria-busy", "false");
      }
    }
  }
  async function refresh() {
    if (activeRequest) return;
    const currentGeneration = ++generation;
    const controller = new AbortController();
    activeRequest = controller;
    const detailed = !authRequired || Boolean(apiKey);
    const headers = {Accept: "application/json"};
    if (detailed && apiKey) headers.Authorization = `Bearer ${apiKey}`;
    el("refresh-button").disabled = true;
    el("health-panel").setAttribute("aria-busy", "true");
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(detailed ? "/status/data" : "/healthz", {
        method: "GET", headers, cache: "no-store", credentials: "omit",
        redirect: "error", signal: controller.signal,
      });
      if (currentGeneration !== generation) return;
      if (detailed && (response.status === 401 || response.status === 403)) {
        apiKey = "";
        el("api-key").value = "";
        authControls("The key was rejected. Enter the router’s client API key to try again.");
        unavailable("Authentication failed. Backend details have been cleared; enter a valid router API key.");
        return;
      }
      if (!response.ok && response.status !== 503) throw new Error("status-response");
      const data = await response.json();
      if (currentGeneration !== generation) return;
      if (!data || typeof data !== "object") throw new Error("invalid-snapshot");
      if (detailed) {
        if (typeof data.ready !== "boolean") throw new Error("invalid-snapshot");
        renderDetails(data);
      } else {
        if (!["ready", "degraded", "unavailable"].includes(data.status)) throw new Error("invalid-snapshot");
        clearDetails();
        renderHealth({...data, ready: response.ok}, false);
      }
    } catch (error) {
      if (currentGeneration !== generation) return;
      unavailable("Could not reach the status endpoint or read its response. Check that the router is running and your network connection is available.");
    } finally {
      clearTimeout(timeout);
      if (currentGeneration === generation) {
        activeRequest = null;
        el("refresh-button").disabled = false;
        el("health-panel").setAttribute("aria-busy", "false");
      }
    }
  }
  function unlockWithKey(enteredKey) {
    if (!enteredKey || !pageActive) return;
    cancelRefresh();
    apiKey = enteredKey;
    el("api-key").value = "";
    clearDetails();
    authControls("Checking your key…");
    health("pending", "Checking router…", "Requesting a fresh authenticated status snapshot.", "Checking…", "Checking…");
    refresh();
  }
  el("key-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const enteredKey = el("api-key").value.trim();
    el("api-key").value = "";
    unlockWithKey(enteredKey);
  });
  el("lock-button").addEventListener("click", () => {
    cancelRefresh();
    apiKey = "";
    el("api-key").value = "";
    clearDetails();
    authControls("Key cleared. Only public gateway readiness is shown.");
    health("pending", "Checking public readiness…", "Backend details are locked.", "Checking…", "Checking…");
    refresh();
  });
  el("refresh-button").addEventListener("click", refresh);
  el("self-test-button").addEventListener("click", runSelfTest);
  el("inference-button").addEventListener("click", runInferenceTest);
  el("inference-refresh-button").addEventListener("click", () => inferenceRequest("GET"));
  el("update-button").addEventListener("click", () => updateRequest("POST"));
  el("update-refresh-button").addEventListener("click", () => updateRequest("GET"));
  el("host-form").addEventListener("submit", (event) => { event.preventDefault(); hostOperation("save"); });
  el("hosts-reload-button").addEventListener("click", () => hostOperation("load"));
  el("hosts-check-button").addEventListener("click", () => hostOperation("check"));
  el("settings-form").addEventListener("submit", (event) => { event.preventDefault(); });
  for (const [name, id] of Object.entries(settingFields)) el(id).addEventListener("change", () => saveSettings({[name]: el(id).checked}));
  for (const [name, [id, low, high, label]] of Object.entries(settingNumbers)) {
    el(id).addEventListener("change", () => {
      const value = Number(el(id).value);
      if (Number.isSafeInteger(value) && value >= low && value <= high) saveSettings({[name]: value});
      else {
        restoreSettingInputs();
        settingsMessage(`${label} must be a whole number from ${low} to ${high}.`, "fail");
      }
    });
  }
  const setPanels = open => { for (const id of panels) el(id).open = open; };
  el("collapse-all-button").addEventListener("click", () => setPanels(false));
  el("expand-all-button").addEventListener("click", () => setPanels(true));
  for (const key of Object.keys(trafficWindows)) el(`traffic-window-${key}`).addEventListener("click", () => { trafficWindow = key; drawTraffic(); });
  el("traffic-panel").addEventListener("toggle", () => { if (el("traffic-panel").open) drawTraffic(); });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer !== null) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resizeTimer = null; drawTraffic(); }, 150);
  });
  text("router-origin", safeOrigin(window.location.origin) || "Current server");
  apiKey = consumeURLKey();
  authControls(apiKey ? "Checking your URL key…" : undefined);
  refresh();
  let timer = setInterval(refresh, 10000);
  const reloadSavedSnapshot = () => { if (!el("host-address").value.trim()) hostOperation("load"); };
  let hostsTimer = setInterval(reloadSavedSnapshot, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      inferenceGeneration += 1;
      if (activeInference) activeInference.abort();
      if (inferencePollTimer !== null) clearTimeout(inferencePollTimer);
      activeInference = null;
      inferencePollTimer = null;
      el("inference-results").hidden = true;
      el("inference-body").replaceChildren();
      el("inference-progress").hidden = true;
      meta("inference-meta", "");
      inferenceMessage(inferenceAttempted ? "Inference-test monitoring is paused while this page is hidden. A server-side test may continue; returning only reads status." : "No inference test has been requested in this page.");
      inferenceControls();
      updateGeneration += 1;
      if (activeUpdate) activeUpdate.abort();
      if (updatePollTimer !== null) clearTimeout(updatePollTimer);
      activeUpdate = null;
      updatePollTimer = null;
      el("update-details").hidden = true;
      el("update-progress").hidden = true;
      text("update-stage", "");
      text("update-observed", "");
      updateMessage(updateLoaded ? "Update monitoring is paused while this page is hidden. Any server-side job continues independently." : "Unlock backend details to enable software updates.");
      updateControls();
    } else {
      if (pageActive && authRequired && apiKey && inferenceAttempted) inferenceRequest("GET");
      inferenceControls();
      if (pageActive && authRequired && apiKey && updateLoaded) updateRequest("GET");
    }
  });
  window.addEventListener("hashchange", () => {
    // Same-page fragment navigation does not rerun this script. Scrub a newly
    // supplied key before deciding whether to unlock; ordinary anchors are inert.
    unlockWithKey(consumeURLKey());
  });
  window.addEventListener("pagehide", () => {
    pageActive = false;
    apiKey = "";
    el("api-key").value = "";
    cancelRefresh();
    clearDetails();
    clearSummary();
    authControls();
    text("checked-at", "No current snapshot");
    health("pending", "Checking router…", "Waiting for a fresh status snapshot.", "Checking…", "Checking…");
    clearInterval(timer);
    clearInterval(hostsTimer);
  });
  window.addEventListener("pageshow", (event) => {
    pageActive = true;
    if (!event.persisted) return;
    refresh();
    timer = setInterval(refresh, 10000);
    hostsTimer = setInterval(reloadSavedSnapshot, 30000);
  });
})();
"""
