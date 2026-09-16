"""Dependency-free status page with read-only metadata diagnostics."""

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
    <span class="readonly">Read-only dashboard</span>
  </header>
  <main id="main">
    <section class="heading" aria-labelledby="page-title">
      <div><p class="eyebrow">Your models, one connection</p>
        <h1 id="page-title">Router overview</h1>
        <p class="muted">A quick check that your gateway and model backends are available.</p>
      </div>
      <button id="refresh-button" type="button">Refresh status</button>
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

    <section class="card quick-links" aria-labelledby="links-title">
      <h2 id="links-title">Router links</h2>
      <p class="muted">This router: <code id="router-origin">Current server</code>. Links open in a new tab.</p>
      <nav aria-label="Router API and diagnostic links">
        <a href="/healthz" target="_blank" rel="noopener noreferrer">Health</a>
        <a href="/readyz" target="_blank" rel="noopener noreferrer">Readiness</a>
        <a href="/status/data" target="_blank" rel="noopener noreferrer">Status JSON</a>
        <a href="/router/status" target="_blank" rel="noopener noreferrer">Router diagnostics</a>
        <a href="/api/version" target="_blank" rel="noopener noreferrer">Ollama API version</a>
        <a href="/api/tags" target="_blank" rel="noopener noreferrer">Ollama model list</a>
        <a href="/v1/models" target="_blank" rel="noopener noreferrer">OpenAI model list</a>
      </nav>
      <p class="muted">These are API responses, not separate apps. Protected links may show 401 because new tabs do not receive this page’s API key. Model-list links list metadata; they do not run models.</p>
    </section>

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
        <p>Start Ollama or LM Studio, download or load a chat model, and add its address to
        <code>LLM_ROUTER_DISCOVERY_URLS</code> in <code>router.env</code>. Restart the router after changing settings.
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
      <p class="muted snapshot-note">Last discovery: <span id="last-discovery">—</span>. Refresh reads the current snapshot; it does not scan your network or run a model.</p>
    </section>
    <noscript><p class="help-box">Enable JavaScript to view live status. The JSON readiness endpoint is <a href="/healthz">/healthz</a>.</p></noscript>
  </main>
  <footer><p id="version"></p><p>Status refreshes every 10 seconds · self-test runs only on click · metadata checks do not prove inference works</p></footer>
</body>
</html>
"""


STATUS_CSS = """
:root{color-scheme:light;--navy:#14253d;--ink:#1c3048;--muted:#52657b;--line:#d9e2ec;--surface:#fff;--background:#f3f6fa;--green:#176943;--amber:#845108;--red:#a22b35}
*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.6}
[hidden]{display:none!important}a{color:#245aba}button,input{font:inherit}button{cursor:pointer;border:1px solid #a9b9cb;border-radius:8px;padding:9px 15px;color:var(--ink);background:#fff;font-weight:600;white-space:nowrap}button:hover{background:#edf3fa}button:disabled{cursor:wait;opacity:.65}button.primary{background:var(--navy);color:#fff;border-color:var(--navy)}button.primary:hover{background:#263e5d}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #669eea;outline-offset:3px}input{min-width:0;width:100%;border:1px solid #9aaec3;border-radius:8px;padding:10px 12px;color:var(--ink);background:#fff}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.9em;overflow-wrap:anywhere}
.topbar{background:var(--navy);color:#fff;display:flex;align-items:center;justify-content:space-between;padding:18px max(24px,calc((100% - 1160px)/2));gap:20px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:-.02em;font-size:19px}.brand-mark{display:grid;place-items:center;width:32px;height:32px;border:1px solid #6e83a1;border-radius:9px;font-size:24px;line-height:1}.brand-subtitle{font-weight:400;color:#bdcce0}.readonly{font-size:12px;color:#d1deee;letter-spacing:.03em}
main{max-width:1208px;margin:auto;padding:38px 24px 24px}.heading,.section-heading,.card-heading{display:flex;align-items:center;justify-content:space-between;gap:20px}.heading{margin-bottom:26px}.heading p{margin:7px 0 0}.eyebrow{text-transform:uppercase;font-size:11px;font-weight:700;letter-spacing:.15em;color:var(--muted)}h1{font-size:32px;letter-spacing:-.04em;line-height:1.2;margin:8px 0}h2{font-size:18px;letter-spacing:-.02em;margin:0}h3{font-size:15px;margin:0 0 5px}p{margin:0}.muted{color:var(--muted);font-size:13px}
.health-panel{display:flex;align-items:flex-start;gap:14px;border:1px solid;border-radius:12px;padding:21px 24px}.health-panel p{font-size:14px;margin-top:3px}.health-dot{width:12px;height:12px;flex:0 0 auto;border-radius:50%;margin-top:8px;background:currentColor}.tone-pending{background:#edf2f9;border-color:#c6d5e6;color:#354d6c}.tone-ready{background:#edf8f1;border-color:#b2d7c0;color:var(--green)}.tone-warning{background:#fff7e8;border-color:#e5ce9e;color:var(--amber)}.tone-error{background:#fff0f0;border-color:#eab9bf;color:var(--red)}
.overview{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin:24px 0 30px}.overview-item{padding-left:16px;border-left:2px solid #cbd7e5}.label{display:block;color:var(--muted);font-size:12px;font-weight:500}.overview strong{display:block;font-size:15px;margin-top:3px}.card{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:20px;overflow:hidden}.card-heading{padding:19px 22px}.card-heading p{margin-top:3px}.auth-card{padding:22px;display:grid;grid-template-columns:1.15fr 1fr;column-gap:36px;row-gap:16px}.auth-card h2{margin-bottom:7px}.auth-card p+p{margin-top:5px}.auth-card label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}.key-controls{display:flex;gap:10px}.auth-actions{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:16px}.section-heading{margin:30px 0 15px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}.stat{border:1px solid var(--line);background:var(--surface);border-radius:10px;padding:17px 20px}.stat strong{display:block;font-size:28px;font-weight:650;letter-spacing:-.03em;margin-top:4px;line-height:1.3}.help-box{padding:18px 22px;background:#fff8ea;border:1px solid #e6d1a6;border-radius:10px;margin:0 0 22px;color:#674810}.help-box p{font-size:14px}.table-scroll{width:100%;overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#f7f9fc;white-space:nowrap}th,td{padding:12px 22px;border-top:1px solid #e3e9f0;vertical-align:top}td{overflow-wrap:anywhere;max-width:360px}td .secondary{display:block;font-size:12px;color:var(--muted);margin-top:2px}.address{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.badge{display:inline-block;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:650;white-space:nowrap}.badge-ready{background:#e7f5ed;color:var(--green)}.badge-warning{background:#fff2d8;color:var(--amber)}.badge-error{background:#ffe9eb;color:var(--red)}.badge-neutral{background:#edf1f6;color:#53647a}.empty-row{text-align:center;color:var(--muted);padding:24px}.snapshot-note{margin:24px 0 0}footer{max-width:1208px;margin:0 auto;padding:0 24px 30px;color:var(--muted);font-size:12px}.sr-only,.skip-link:not(:focus){position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip-link:focus{position:absolute;top:8px;left:8px;z-index:10;background:white;padding:8px}
@media(max-width:800px){.auth-card{grid-template-columns:1fr}.stats,.overview{grid-template-columns:repeat(2,1fr)}.auth-actions{grid-column:auto}th,td{padding:12px 16px}.heading{align-items:flex-start}.heading button{margin-top:12px}}
@media(max-width:500px){main{padding:24px 16px}.topbar{padding:16px}.readonly{display:none}.heading{display:block}h1{font-size:28px}.heading button{width:100%;margin-top:18px}.health-panel{padding:18px}.auth-card{padding:18px}.key-controls{flex-direction:column}.auth-actions{align-items:flex-start;flex-direction:column}.stats{gap:10px}.stat{padding:14px}.stat strong{font-size:25px}.section-heading{align-items:flex-start;flex-direction:column;gap:3px}footer{padding:0 16px 24px}.brand{font-size:17px}}
.quick-links{padding:19px 22px}.quick-links p{margin-top:6px}.quick-links nav{display:flex;flex-wrap:wrap;gap:8px 20px;margin:12px 0}.quick-links nav a{font-size:13px}.self-test-message{padding:0 22px 19px}.self-test-message.result-pass{color:var(--green)}.self-test-message.result-fail{color:var(--red)}.self-test-message.result-partial{color:var(--amber)}
@media(max-width:800px){.self-test-heading{align-items:flex-start;flex-direction:column}}
"""


STATUS_JS = r"""
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const authRequired = document.body.dataset.authRequired === "true";
  let apiKey = "";
  let generation = 0;
  let activeRequest = null;
  let selfTestGeneration = 0;
  let activeSelfTest = null;
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
  }
  function clearDetails() {
    clearSelfTest();
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
    clearDetails();
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
  function renderHealth(data, detailed) {
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
  function renderDetails(data) {
    renderHealth(data, true);
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
  el("key-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const enteredKey = el("api-key").value.trim();
    el("api-key").value = "";
    if (!enteredKey) return;
    cancelRefresh();
    apiKey = enteredKey;
    clearDetails();
    authControls("Checking your key…");
    health("pending", "Checking router…", "Requesting a fresh authenticated status snapshot.", "Checking…", "Checking…");
    refresh();
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
  text("router-origin", safeOrigin(window.location.origin) || "Current server");
  authControls();
  refresh();
  let timer = setInterval(refresh, 10000);
  window.addEventListener("pagehide", () => {
    apiKey = "";
    el("api-key").value = "";
    cancelRefresh();
    clearDetails();
    authControls();
    text("checked-at", "No current snapshot");
    health("pending", "Checking router…", "Waiting for a fresh status snapshot.", "Checking…", "Checking…");
    clearInterval(timer);
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    refresh();
    timer = setInterval(refresh, 10000);
  });
})();
"""
