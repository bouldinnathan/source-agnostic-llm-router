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
        <p class="muted">A quick check that your gateway and model backends are available.</p>
      </div>
      <button id="refresh-button" type="button">Refresh status</button>
    </section>

    <section id="update-panel" class="card update-panel" aria-labelledby="update-title">
      <h2 id="update-title">Router software updates</h2>
      <p id="update-warning" class="muted">Check for updates checks official main and automatically installs a newer commit. Installation briefly restarts the router and can interrupt requests. Your API key is required.</p>
      <p id="update-message" class="muted" role="status">Unlock backend details to enable software updates.</p>
      <div id="update-details" hidden>
        <p class="update-stage">Stage: <strong id="update-stage"></strong></p>
        <progress id="update-progress" aria-label="Router update in progress" hidden></progress>
        <p id="update-observed" class="muted"></p>
        <button id="update-refresh-button" type="button">Refresh update status</button>
        <p class="muted">Status refresh reads the local update job only; it never starts another update.</p>
      </div>
    </section>

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

    <section class="card public-summary" aria-labelledby="public-summary-title">
      <div class="card-heading"><div><h2 id="public-summary-title">Network summary</h2><p class="muted">Cached counts only; this summary does not include addresses or model names. No network scan or inference runs when this page refreshes.</p></div></div>
      <div class="public-summary-counts">
        <div><span class="label">Known servers</span><strong id="public-count-servers">Unknown</strong></div>
        <div><span class="label">Listed model copies</span><strong id="public-count-models">Unknown</strong></div>
        <div><span class="label">Last successful metadata check</span><strong id="public-last-verified">Unknown</strong></div>
      </div>
      <p id="public-summary-note" class="muted">Waiting for a current summary. Counts do not prove models are loaded or inference works.</p>
    </section>

    <section class="card quick-links" aria-labelledby="links-title">
      <h2 id="links-title">Router links</h2>
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
      <p class="muted">These are API responses, not separate apps. Protected links may show 401 because new tabs do not receive this page’s API key. Model-list links list metadata; they do not run models.</p>
    </section>

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
      <div class="section-heading"><h2>Backend details</h2></div>
      <section class="card" aria-labelledby="hosts-title">
        <div class="card-heading"><div><h2 id="hosts-title">Saved backend addresses</h2>
          <p class="muted">Save an IP address or hostname to automatically enroll its discovered models for client routing. The router checks saved addresses at startup and every 30 seconds using metadata only: no prompts, model loading, or downloads. Listed models may not be loaded; inference is not tested. Only the addresses you save are checked, not whole subnets.</p>
          <p class="muted">Remove stops checks for that address and removes routes owned only by it. Explicitly configured routes are preserved.</p>
        </div></div>
        <form id="host-form" class="host-form" autocomplete="off">
          <label for="host-address">IP address, hostname, or backend URL</label>
          <div class="key-controls"><input id="host-address" name="backend-address" type="text" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="256" placeholder="192.168.194.0" required aria-describedby="host-help hosts-message">
            <button id="host-save-button" type="submit" class="primary">Save &amp; enable routing</button></div>
          <p id="host-help" class="muted">A bare IP address or hostname checks Ollama on 11434 and LM Studio on 1234. A URL checks only its own port, for example http://192.168.194.0:1234/v1. Do not include passwords or API keys.</p>
        </form>
        <div class="hosts-toolbar"><p id="hosts-message" class="muted" role="status">Unlock details to manage saved addresses.</p>
          <div class="hosts-actions"><button id="hosts-reload-button" type="button">Reload saved</button><button id="hosts-check-button" type="button">Check all saved</button></div></div>
        <div id="hosts-results" class="table-scroll"><table><caption class="sr-only">Saved addresses and metadata checks</caption>
          <thead><tr><th scope="col">Saved address</th><th scope="col">Servers and model lists</th><th scope="col">Last checked</th><th scope="col">Actions</th></tr></thead>
          <tbody id="hosts-body"></tbody>
        </table></div>
      </section>
      <section class="card" aria-labelledby="self-test-title">
        <div class="card-heading self-test-heading"><div><h2 id="self-test-title">Connection self-test</h2>
          <p class="muted">Checks router APIs and backend metadata only. Never sends prompts, runs inference, loads models, or downloads anything.</p>
        </div><button id="self-test-button" type="button">Run self-test (no models)</button></div>
        <p id="self-test-message" class="self-test-message muted" role="status">Runs only when you click. No self-test has been run in this page.</p>
        <div id="self-test-results" class="table-scroll" hidden><table><caption class="sr-only">Connection self-test results</caption>
          <thead><tr><th scope="col">Check / target</th><th scope="col">Result</th><th scope="col">Detail</th><th scope="col">Time</th></tr></thead>
          <tbody id="self-test-body"></tbody>
        </table></div>
      </section>
      <div class="stats" aria-label="Backend counts">
        <div class="stat"><span class="label">Known backends</span><strong id="count-endpoints">—</strong></div>
        <div class="stat"><span class="label">Backends online</span><strong id="count-online">—</strong></div>
        <div class="stat"><span class="label">Available / enabled models</span><strong id="count-models">—</strong></div>
        <div class="stat"><span class="label">Model aliases</span><strong id="count-aliases">—</strong></div>
      </div>
      <div id="setup-help" class="help-box" hidden>
        <h3>No usable model yet</h3>
        <p>Start Ollama or LM Studio, make a chat model available, and save its IP address above to enroll discovered models automatically.
        You can also explicitly configure backends using <code>LLM_ROUTER_DISCOVERY_URLS</code> in <code>router.env</code> and restart the router.
        If a backend is offline, check its address, firewall, and VPN connection.</p>
      </div>

      <section class="card" aria-labelledby="backends-title">
        <div class="card-heading"><div><h2 id="backends-title">Backends</h2><p class="muted">Known machines and their latest reachability checks. Address links open each backend’s own port; it may show an API response or 404 instead of a homepage. Your browser needs network/VPN access. The router key is never forwarded.</p></div></div>
        <div class="table-scroll"><table><caption class="sr-only">Backend reachability</caption>
          <thead><tr><th scope="col">Machine / backend</th><th scope="col">API address</th><th scope="col">Status</th><th scope="col">Models ready</th><th scope="col">Last probe</th></tr></thead>
          <tbody id="endpoints-body"></tbody>
        </table></div>
      </section>

      <section class="card" aria-labelledby="models-title">
        <div class="card-heading"><div><h2 id="models-title">Model deployments</h2><p class="muted">Each model copy on each machine; readiness is not a test generation.</p></div></div>
        <div class="table-scroll"><table><caption class="sr-only">Model deployments and request counters</caption>
          <thead><tr><th scope="col">Model / deployment</th><th scope="col">Machine</th><th scope="col">Status</th><th scope="col">Active requests</th><th scope="col">Succeeded / failed</th></tr></thead>
          <tbody id="models-body"></tbody>
        </table></div>
      </section>

      <section class="card" aria-labelledby="aliases-title">
        <div class="card-heading"><div><h2 id="aliases-title">Client model names</h2><p class="muted">HA shares replicas; preferred tries one machine first; pinned never fails over.</p></div></div>
        <div class="table-scroll"><table><caption class="sr-only">High availability and machine-specific aliases</caption>
          <thead><tr><th scope="col">Alias</th><th scope="col">Routing</th><th scope="col">Status</th><th scope="col">Deployments</th></tr></thead>
          <tbody id="aliases-body"></tbody>
        </table></div>
      </section>
      <section class="card" aria-labelledby="performance-title">
        <div class="card-heading"><div><h2 id="performance-title">Observed model performance</h2>
          <p class="muted">Measured passively from real requests through this router and saved across restarts and updates. Refresh never runs a benchmark or model. Smoothed averages use EWMA, which gives recent samples more weight. Request time covers the whole upstream call; token rates and load/setup time require backend-reported timings, which some backends do not provide.</p>
        </div></div>
        <p id="performance-message" class="performance-message muted" role="status">Waiting for saved performance observations.</p>
        <div class="table-scroll"><table><caption class="sr-only">Saved request performance by model and server</caption>
          <thead><tr><th scope="col">Model / server</th><th scope="col">Input tok/s</th><th scope="col">Output tok/s</th><th scope="col">Reported load / setup</th><th scope="col">Request time (wall clock)</th><th scope="col">Requests</th><th scope="col">Last observed</th></tr></thead>
          <tbody id="performance-body"></tbody>
        </table></div>
        <p class="performance-note muted">Load/setup times are backend-reported. Slow reported loads may be cold starts, but do not prove disk I/O. Missing timings are not estimated from request latency. Historical rows preserve observations for deployments no longer in the current routing configuration.</p>
      </section>
      <p class="muted snapshot-note">Last discovery: <span id="last-discovery">—</span>. Refresh reads the current snapshot; it does not scan your network or run a model.</p>
    </section>
    <noscript><p class="help-box">Enable JavaScript to view live status. The JSON readiness endpoint is <a href="/healthz">/healthz</a>.</p></noscript>
  </main>
  <footer><p>Status refreshes every 10 seconds · saved backends are rechecked every 30 seconds · self-tests run only on click · metadata checks do not prove inference works</p></footer>
</body>
</html>
"""


STATUS_CSS = """
:root{color-scheme:light;--navy:#14253d;--ink:#1c3048;--muted:#52657b;--line:#d9e2ec;--surface:#fff;--background:#f3f6fa;--green:#176943;--amber:#845108;--red:#a22b35}
*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.6}
[hidden]{display:none!important}a{color:#245aba}button,input{font:inherit}button{cursor:pointer;border:1px solid #a9b9cb;border-radius:8px;padding:9px 15px;color:var(--ink);background:#fff;font-weight:600;white-space:nowrap}button:hover{background:#edf3fa}button:disabled{cursor:wait;opacity:.65}button.primary{background:var(--navy);color:#fff;border-color:var(--navy)}button.primary:hover{background:#263e5d}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #669eea;outline-offset:3px}input{min-width:0;width:100%;border:1px solid #9aaec3;border-radius:8px;padding:10px 12px;color:var(--ink);background:#fff}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.9em;overflow-wrap:anywhere}
.topbar{background:var(--navy);color:#fff;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;padding:18px max(24px,calc((100% - 1160px)/2));gap:20px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:-.02em;font-size:19px}.brand-mark{display:grid;place-items:center;width:32px;height:32px;border:1px solid #6e83a1;border-radius:9px;font-size:24px;line-height:1}.brand-subtitle{font-weight:400;color:#bdcce0}.topbar-meta{display:flex;flex-wrap:wrap;align-items:center;gap:12px}.version-badge{display:inline-block;padding:3px 10px;border:1px solid #6e83a1;border-radius:999px;color:#fff;font-size:12px;font-weight:600;white-space:nowrap}.version-badge:empty{display:none}.readonly{font-size:12px;color:#d1deee;letter-spacing:.03em}
main{max-width:1208px;margin:auto;padding:38px 24px 24px}.heading,.section-heading,.card-heading{display:flex;align-items:center;justify-content:space-between;gap:20px}.heading{margin-bottom:26px}.heading p{margin:7px 0 0}.eyebrow{text-transform:uppercase;font-size:11px;font-weight:700;letter-spacing:.15em;color:var(--muted)}h1{font-size:32px;letter-spacing:-.04em;line-height:1.2;margin:8px 0}h2{font-size:18px;letter-spacing:-.02em;margin:0}h3{font-size:15px;margin:0 0 5px}p{margin:0}.muted{color:var(--muted);font-size:13px}
.health-panel{display:flex;align-items:flex-start;gap:14px;border:1px solid;border-radius:12px;padding:21px 24px}.health-panel p{font-size:14px;margin-top:3px}.health-dot{width:12px;height:12px;flex:0 0 auto;border-radius:50%;margin-top:8px;background:currentColor}.tone-pending{background:#edf2f9;border-color:#c6d5e6;color:#354d6c}.tone-ready{background:#edf8f1;border-color:#b2d7c0;color:var(--green)}.tone-warning{background:#fff7e8;border-color:#e5ce9e;color:var(--amber)}.tone-error{background:#fff0f0;border-color:#eab9bf;color:var(--red)}
.overview{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin:24px 0 30px}.overview-item{padding-left:16px;border-left:2px solid #cbd7e5}.label{display:block;color:var(--muted);font-size:12px;font-weight:500}.overview strong{display:block;font-size:15px;margin-top:3px}.card{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:20px;overflow:hidden}.card-heading{padding:19px 22px}.card-heading p{margin-top:3px}.auth-card{padding:22px;display:grid;grid-template-columns:1.15fr 1fr;column-gap:36px;row-gap:16px}.auth-card h2{margin-bottom:7px}.auth-card p+p{margin-top:5px}.auth-card label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}.key-controls{display:flex;gap:10px}.auth-actions{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:16px}.section-heading{margin:30px 0 15px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}.stat{border:1px solid var(--line);background:var(--surface);border-radius:10px;padding:17px 20px}.stat strong{display:block;font-size:28px;font-weight:650;letter-spacing:-.03em;margin-top:4px;line-height:1.3}.help-box{padding:18px 22px;background:#fff8ea;border:1px solid #e6d1a6;border-radius:10px;margin:0 0 22px;color:#674810}.help-box p{font-size:14px}.table-scroll{width:100%;overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#f7f9fc;white-space:nowrap}th,td{padding:12px 22px;border-top:1px solid #e3e9f0;vertical-align:top}td{overflow-wrap:anywhere;max-width:360px}td .secondary{display:block;font-size:12px;color:var(--muted);margin-top:2px}.address{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.badge{display:inline-block;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:650;white-space:nowrap}.badge-ready{background:#e7f5ed;color:var(--green)}.badge-warning{background:#fff2d8;color:var(--amber)}.badge-error{background:#ffe9eb;color:var(--red)}.badge-neutral{background:#edf1f6;color:#53647a}.empty-row{text-align:center;color:var(--muted);padding:24px}.snapshot-note{margin:24px 0 0}footer{max-width:1208px;margin:0 auto;padding:0 24px 30px;color:var(--muted);font-size:12px}.sr-only,.skip-link:not(:focus){position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip-link:focus{position:absolute;top:8px;left:8px;z-index:10;background:white;padding:8px}
@media(max-width:800px){.auth-card{grid-template-columns:1fr}.stats,.overview{grid-template-columns:repeat(2,1fr)}.auth-actions{grid-column:auto}th,td{padding:12px 16px}.heading{align-items:flex-start}.heading button{margin-top:12px}}
@media(max-width:500px){main{padding:24px 16px}.topbar{padding:16px}.readonly{display:none}.heading{display:block}h1{font-size:28px}.heading button{width:100%;margin-top:18px}.health-panel{padding:18px}.auth-card{padding:18px}.key-controls{flex-direction:column}.auth-actions{align-items:flex-start;flex-direction:column}.stats{gap:10px}.stat{padding:14px}.stat strong{font-size:25px}.section-heading{align-items:flex-start;flex-direction:column;gap:3px}footer{padding:0 16px 24px}.brand{font-size:17px}}
.quick-links{padding:19px 22px}.quick-links p{margin-top:6px}.quick-links nav{display:flex;flex-wrap:wrap;gap:8px 20px;margin:12px 0}.quick-links nav a{font-size:13px}.self-test-message{padding:0 22px 19px}.self-test-message.result-pass{color:var(--green)}.self-test-message.result-fail{color:var(--red)}.self-test-message.result-partial{color:var(--amber)}
.host-form{padding:0 22px 16px}.host-form label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}.host-form p{margin-top:7px}.hosts-toolbar{padding:0 22px 19px;display:flex;align-items:center;justify-content:space-between;gap:16px}.hosts-actions{display:flex;flex-wrap:wrap;gap:8px}.hosts-actions button{padding:6px 10px;font-size:12px}.host-check+.host-check{margin-top:12px}.host-check .badge{margin-left:6px}.host-check .secondary{overflow-wrap:anywhere}#hosts-message.result-fail{color:var(--red)}#hosts-message.result-pass{color:var(--green)}
.host-check{padding:14px;border:1px solid var(--line);border-radius:8px;background:#fafcfe}.host-check-heading{display:flex;align-items:center;flex-wrap:wrap;gap:5px}.host-check p{margin-top:7px}.host-check .host-api-address{display:block;font-size:12px;overflow-wrap:anywhere}.host-catalog{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}.host-catalog h3{font-size:13px;margin-bottom:5px}.host-catalog .catalog-warning{color:var(--amber)}.host-model-table{margin-top:10px;table-layout:fixed}.host-model-table th,.host-model-table td{padding:8px 10px;max-width:none;overflow-wrap:anywhere}.host-model-table th{white-space:normal}.host-model-table td{background:var(--surface)}.host-model-table th:first-child{width:43%}#hosts-results>table>thead>tr>th:nth-child(2){width:55%}#hosts-results>table>tbody>tr>td:nth-child(2){min-width:330px;max-width:none}
.public-summary-counts{display:grid;grid-template-columns:1fr 1fr 1.6fr;gap:18px;padding:0 22px 16px}.public-summary-counts strong{display:block;font-size:20px;line-height:1.5;overflow-wrap:anywhere}.public-summary-counts>div:last-child strong{font-size:15px}.public-summary>p{padding:0 22px 19px}
.performance-message{padding:0 22px 19px}.performance-message.result-warning{color:var(--amber)}.performance-note{padding:16px 22px;border-top:1px solid var(--line)}.metric-value{display:block;font-weight:650;white-space:nowrap}.metric-detail{display:block;min-width:130px;color:var(--muted);font-size:11px;margin-top:4px}.performance-identity{min-width:180px}.performance-identity .badge{margin-top:7px}.performance-identity code{display:block;margin-top:5px}.slow-load-count{margin-top:9px;font-size:12px}
.host-routing{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:12px;margin-top:10px}.host-routing .secondary{margin-top:6px}
.update-panel{padding:18px 22px}.update-panel p{margin-top:7px}.update-panel progress{display:block;width:min(100%,480px);height:14px;margin:12px 0}.update-panel button{margin-top:12px}.update-panel .result-fail{color:var(--red)}.update-panel .result-pass{color:var(--green)}.update-stage{font-size:14px}
@media(max-width:800px){.self-test-heading{align-items:flex-start;flex-direction:column}}
@media(max-width:600px){.hosts-toolbar{align-items:flex-start;flex-direction:column}.host-form .key-controls{flex-direction:column}}
@media(max-width:600px){.public-summary-counts{grid-template-columns:1fr 1fr}.public-summary-counts>div:last-child{grid-column:1/-1}}
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
  const text = (id, value) => { el(id).textContent = String(value); };
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
  }
  function authControls(message) {
    el("auth-section").hidden = !authRequired;
    el("key-form").hidden = Boolean(apiKey);
    el("lock-button").hidden = !apiKey;
    text("auth-title", apiKey ? "Backend details unlocked" : "Unlock backend details");
    text("auth-message", message || (apiKey ? "Your key is held only in this page’s memory." : "Backend addresses and model details are locked."));
    updateControls();
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
  function clearDetails(keepUpdate = false) {
    clearSelfTest();
    clearHosts();
    clearPerformance();
    if (!keepUpdate) clearUpdate();
    el("details").hidden = true;
    for (const id of ["endpoints-body", "models-body", "aliases-body"]) el(id).replaceChildren();
    for (const id of ["count-endpoints", "count-online", "count-models", "count-aliases", "uptime", "last-discovery"]) text(id, "—");
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
    clearDetails(Boolean(apiKey) && updateWatching);
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
    el("host-address").value = "";
    el("hosts-body").replaceChildren();
    hostMessage(authRequired ? "Unlock details to manage saved addresses." : "Address management is disabled. Set LLM_ROUTER_GATEWAY_API_KEY in router.env and restart the router to enable it.");
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
  function hostCatalog(check) {
    const catalog = document.createElement("div");
    catalog.className = "host-catalog";
    const title = document.createElement("h3");
    title.textContent = "Models listed by this server";
    const summary = document.createElement("p");
    summary.className = "muted";
    catalog.append(title, summary);
    if (check.catalog_status === undefined) {
      summary.textContent = "Check again to retrieve model list.";
      return catalog;
    }
    if (check.catalog_status === "error") {
      summary.className = "muted catalog-warning";
      summary.textContent = `Model list unavailable; model count is unknown. ${check.catalog_detail}`;
    } else {
      summary.textContent = check.model_count === 0 ? "No models listed by this server." : `${check.model_count} model${check.model_count === 1 ? "" : "s"} listed by this server.`;
      if (check.models_truncated) {
        const truncated = document.createElement("p");
        truncated.className = "muted catalog-warning";
        truncated.textContent = `Showing ${check.models.length} of ${check.model_count} models. The list is truncated.`;
        catalog.append(truncated);
      }
      if (check.models.length) {
        const table = document.createElement("table");
        table.className = "host-model-table";
        const caption = document.createElement("caption");
        caption.className = "sr-only";
        caption.textContent = `Model IDs and API addresses reported by ${check.provider}`;
        const head = document.createElement("thead");
        const heading = document.createElement("tr");
        for (const label of ["Model ID", "API address"]) {
          const column = document.createElement("th");
          column.setAttribute("scope", "col");
          column.textContent = label;
          heading.append(column);
        }
        head.append(heading);
        const body = document.createElement("tbody");
        for (const model of check.models) {
          const row = document.createElement("tr");
          for (const value of [model.id, model.address]) {
            const code = document.createElement("code");
            code.textContent = value;
            cell(row, code);
          }
          body.append(row);
        }
        table.append(caption, head, body);
        catalog.append(table);
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
  }
  function renderSummary(summary) {
    clearSummary();
    if (!summary || typeof summary !== "object") return;
    text("public-count-servers", Number.isSafeInteger(summary.servers) && summary.servers >= 0 ? summary.servers : "Unknown");
    text("public-count-models", Number.isSafeInteger(summary.models) && summary.models >= 0 ? `${summary.models}${summary.models_truncated === true ? "+" : ""}` : "Unknown");
    if (summary.last_verified_at === null) text("public-last-verified", "Not yet verified");
    else if (typeof summary.last_verified_at === "string") text("public-last-verified", date(summary.last_verified_at));
    text("public-summary-note", summary.models_truncated === true
      ? "Model lists are incomplete; the model count is a lower bound. Cached metadata does not prove models are loaded or inference works."
      : "Counts reflect cached metadata, not a live network scan. They do not prove models are loaded or inference works.");
  }
  function renderHosts() {
    rows("hosts-body", savedHosts, 4, "No saved addresses. Add a machine above to check its backend ports.", (row, item) => {
      cell(row, item.address, null, "address");
      row.children[0].append(hostRouting(item.routing));
      const results = document.createElement("div");
      if (!item.checks.length) results.textContent = "Not checked in this router session";
      for (const check of item.checks) {
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
        result.append(heading, address, detail, hostCatalog(check));
        results.append(result);
      }
      cell(row, results);
      cell(row, date(item.checked_at));
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
      cell(row, actions);
    });
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
      rows("performance-body", [], 7, "No performance data received.", () => {});
      return;
    }
    const validRows = Array.isArray(performance.deployments) && performance.deployments.every(item => item && typeof item === "object" &&
      typeof item.id === "string" && typeof item.model === "string" && typeof item.machine === "string" && typeof item.endpoint === "string");
    if (performance.available !== true || !validRows) {
      el("performance-message").className = "performance-message muted result-warning";
      text("performance-message", "Saved performance history is unavailable. Check the service account’s metrics-storage access and the router’s service logs. No current metrics are being shown.");
      rows("performance-body", [], 7, "Performance history is unavailable.", () => {});
      return;
    }
    const updated = performance.updated_at ? date(performance.updated_at) : "Not yet recorded";
    text("performance-message", `Saved observations updated: ${updated}. This table reads saved observations only; it does not generate traffic to models.`);
    if (performance.error) {
      el("performance-message").className = "performance-message muted result-warning";
      text("performance-message", `Performance storage reported a problem; saved observations may be incomplete. Last saved update: ${updated}. Check service permissions and logs.`);
    }
    rows("performance-body", performance.deployments, 7, "No routed requests yet. Performance will appear after real requests pass through this router.", (row, item) => {
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
  function renderDetails(data) {
    renderHealth(data, true);
    renderPerformance(data.performance);
    const totals = data.counts || {};
    text("count-endpoints", count(totals.endpoints));
    text("count-online", count(totals.online));
    text("count-models", `${count(totals.available_models)} / ${count(totals.models)}`);
    text("count-aliases", count(totals.aliases));
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
    const kinds = {ha: "High availability", preferred: "Preferred + failover", pinned: "Pinned · no failover"};
    rows("aliases-body", data.aliases, 4, "No HA or machine aliases are available yet.", (row, item) => {
      cell(row, item.name);
      cell(row, Object.prototype.hasOwnProperty.call(kinds, item.kind) ? kinds[item.kind] : "Unknown");
      cell(row, item.available ? badge("Available", "ready") : badge("Unavailable", "warning"));
      cell(row, count(item.deployments));
    });
    el("details").hidden = false;
    hostControls();
    if (authRequired && apiKey && !hostsLoaded && !activeHosts) hostOperation("load");
    if (!authRequired) hostMessage("Address management is disabled. Set LLM_ROUTER_GATEWAY_API_KEY in router.env and restart the router to enable it.");
    updateControls();
    if (authRequired && apiKey && !updateLoaded && !activeUpdate) updateRequest("GET");
    if (!authRequired) clearUpdate();
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
  el("update-button").addEventListener("click", () => updateRequest("POST"));
  el("update-refresh-button").addEventListener("click", () => updateRequest("GET"));
  el("host-form").addEventListener("submit", (event) => { event.preventDefault(); hostOperation("save"); });
  el("hosts-reload-button").addEventListener("click", () => hostOperation("load"));
  el("hosts-check-button").addEventListener("click", () => hostOperation("check"));
  text("router-origin", safeOrigin(window.location.origin) || "Current server");
  apiKey = consumeURLKey();
  authControls(apiKey ? "Checking your URL key…" : undefined);
  refresh();
  let timer = setInterval(refresh, 10000);
  const reloadSavedSnapshot = () => { if (!el("host-address").value.trim()) hostOperation("load"); };
  let hostsTimer = setInterval(reloadSavedSnapshot, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
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
    } else if (pageActive && authRequired && apiKey && updateLoaded) updateRequest("GET");
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
