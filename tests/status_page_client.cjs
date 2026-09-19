"use strict";

// No browser dependencies: exercise the actual shipped JavaScript with a tiny
// DOM/fetch harness. Python runs this file when Node is available.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const moduleSource = fs.readFileSync(path.join(__dirname, "../src/llm_router/status_page.py"), "utf8");
const script = moduleSource.match(/\nSTATUS_JS = r"""([\s\S]*?)"""/)[1];
const markup = moduleSource.match(/_STATUS_HTML = """([\s\S]*?)"""/)[1];

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.tagName === "#fragment") this.children.push(...node.children);
      else this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this._text = ""; this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
}

function harness(authRequired = true, autoLoadHosts = true, options = {}) {
  const elements = new Map();
  for (const match of markup.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Element(match[1]);
    node.hidden = /\bhidden\b/.test(match[0]);
    elements.set(match[2], node);
  }
  const element = id => {
    assert.ok(elements.has(id), `JavaScript requested missing HTML element ${id}`);
    return elements.get(id);
  };
  const requests = [];
  const hostRequests = [];
  const updateRequests = [];
  const allRequests = [];
  const historyCalls = [];
  const timeline = [];
  const location = new URL(options.url || "http://router.example:8088/status");
  const intervals = new Map();
  const timeouts = new Map();
  const timeoutDelays = new Map();
  const windowEvents = {};
  const documentEvents = {};
  let currentTime = Date.now();
  class ClockDate extends Date { static now() { return currentTime; } }
  let timerId = 0;
  const context = {
    document: {
      hidden: false,
      addEventListener: (name, callback) => { documentEvents[name] = callback; },
      body: {dataset: {authRequired: String(authRequired)}},
      getElementById: element,
      createElement: tag => new Element(tag),
      createDocumentFragment: () => new Element("#fragment"),
    },
    window: {
      location,
      history: {replaceState: (state, title, url) => {
        timeline.push("replaceState");
        historyCalls.push({state, title, url});
        if (options.replaceStateThrows) throw new Error("history blocked");
        location.href = new URL(url, location.href).href;
      }},
      addEventListener: (name, callback) => { windowEvents[name] = callback; },
    },
    Node: Element,
    URL,
    URLSearchParams,
    AbortController,
    Date: ClockDate,
    setTimeout: (callback, delay) => { const id = ++timerId; timeouts.set(id, callback); timeoutDelays.set(id, delay); return id; },
    clearTimeout: id => { timeouts.delete(id); timeoutDelays.delete(id); },
    setInterval: (callback, delay) => { const id = ++timerId; intervals.set(id, {callback, delay}); return id; },
    clearInterval: id => intervals.delete(id),
    fetch: (url, options) => new Promise((resolve, reject) => {
      timeline.push("fetch");
      const hostRequest = /^\/status\/hosts(?:\/[^/?#]+)?$/.test(url);
      const updateRequest = url === "/status/update";
      assert.ok(["/healthz", "/status/data", "/status/self-test"].includes(url) || hostRequest || updateRequest, "Only same-origin status endpoints may be fetched");
      if (!hostRequest && !updateRequest) assert.equal(options.method, url === "/status/self-test" ? "POST" : "GET");
      if (updateRequest) {
        assert.match(options.headers.Authorization, /^Bearer .+/);
        assert.equal(options.body, undefined, "Update requests may not supply a URL, revision, path or other body");
        if (options.method === "POST") assert.equal(options.headers["X-LLM-Router-Update"], "1");
        else assert.equal(options.method, "GET");
      }
      if (hostRequest) {
        assert.match(options.headers.Authorization, /^Bearer .+/);
        if (options.method !== "GET") {
          assert.equal(options.headers["X-LLM-Router-Hosts"], "1");
          assert.equal(options.headers["Content-Type"], "application/json");
        }
      }
      if (url === "/status/self-test") {
        assert.equal(options.headers["X-LLM-Router-Self-Test"], "1");
        assert.equal(options.body, undefined);
      }
      assert.equal(options.credentials, "omit");
      assert.equal(options.cache, "no-store");
      assert.equal(options.redirect, "error");
      const pending = {url, options, resolve, reject};
      allRequests.push(pending);
      (hostRequest ? hostRequests : updateRequest ? updateRequests : requests).push(pending);
      if (hostRequest && autoLoadHosts && options.method === "GET") resolve({status: 200, ok: true, json: async () => ({hosts: [], limit: 16})});
      if (updateRequest && context.autoLoadUpdates && options.method === "GET") resolve({status: 200, ok: true, json: async () => updateSnapshot()});
    }),
  };
  context.autoLoadUpdates = options.autoLoadUpdates !== false;
  vm.runInNewContext(script, context);
  return {
    element, requests, hostRequests, updateRequests, allRequests, timeouts, historyCalls, timeline, location,
    advanceTime: milliseconds => { currentTime += milliseconds; },
    expire: delay => {
      for (const [id, callback] of Array.from(timeouts.entries())) if (timeoutDelays.get(id) === delay) {
        timeouts.delete(id); timeoutDelays.delete(id); callback();
      }
    },
    visibility: hidden => { context.document.hidden = hidden; documentEvents.visibilitychange(); },
    update: () => element("update-button").events.click(),
    readUpdate: () => element("update-refresh-button").events.click(),
    tick: (delay = 10000) => { for (const timer of Array.from(intervals.values())) if (timer.delay === delay) timer.callback(); },
    enterKey: key => {
      element("api-key").value = key;
      element("key-form").events.submit({preventDefault() {}});
      assert.equal(element("api-key").value, "", "Input must not retain a submitted key");
    },
    lock: () => element("lock-button").events.click(),
    selfTest: () => element("self-test-button").events.click(),
    saveHost: address => {
      element("host-address").value = address;
      element("host-form").events.submit({preventDefault() {}});
    },
    checkHosts: () => element("hosts-check-button").events.click(),
    reloadHosts: () => element("hosts-reload-button").events.click(),
    hostAction: (row, button) => element("hosts-body").children[row].children[3].children[0].children[button].events.click(),
    event: (name, event = {}) => windowEvents[name](event),
    intervalCount: () => intervals.size,
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
async function reply(request, status, data) {
  request.resolve({status, ok: status >= 200 && status < 300, json: async () => data});
  await flush();
}

function snapshot() {
  return {
    ready: true, status: "ready", version: "0.3.2", uptime_seconds: 65,
    checked_at: "2026-09-16T12:00:00Z", last_discovery: null,
    counts: {endpoints: 1, online: 1, models: 1, available_models: 1, aliases: 1},
    endpoints: [{name: "backend", machine: "laptop", address: "http://private-backend:1234", state: "online", model_count: 1, available_models: 1}],
    models: [{name: '<img src=x onerror="alert(1)">', deployment: "qwen-copy", machine: "laptop", state: "available", active_requests: 0, successes: 5, failures: 1}],
    aliases: [{name: "qwen-ha", kind: "ha", available: true, deployments: 1}],
  };
}

function updateSnapshot(overrides = {}) {
  return {available: true, busy: false, state: "idle", stage: "idle", message: "Ready", run_id: null, updated_at: null, current_version: "0.3.2", ...overrides};
}

function selfTestResult(status = "pass") {
  return {
    status,
    checked_at: "2026-09-16T12:00:00Z",
    notice: "Metadata only. No models were used.",
    checks: [{name: "Backend metadata", target: "http://private-backend:1234", status: status === "partial" ? "skip" : status, detail: "API responded", elapsed_ms: 4.2, http_status: 200}],
  };
}

async function unlocked(authRequired = true) {
  const app = harness(authRequired);
  if (authRequired) app.enterKey("secret-key");
  await reply(app.requests.at(-1), 200, snapshot());
  return app;
}

async function publicReadiness() {
  for (const [httpStatus, status, tone, readiness] of [
    [200, "ready", "ready", "Models available"],
    [200, "degraded", "warning", "Models available"],
    [503, "degraded", "warning", "No model available"],
    [503, "unavailable", "warning", "No model available"],
  ]) {
    const app = harness();
    assert.equal(app.requests[0].url, "/healthz");
    assert.equal(app.requests[0].options.headers.Authorization, undefined);
    // The real public endpoint deliberately contains no ready field.
    await reply(app.requests[0], httpStatus, {status, version: "0.3.2"});
    assert.equal(app.element("health-panel").className, `health-panel tone-${tone}`);
    assert.equal(app.element("gateway-state").textContent, "Responding");
    assert.equal(app.element("model-readiness").textContent, readiness);
    assert.equal(app.element("version").textContent, "Version 0.3.2", "Version should remain visible while details are locked");
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("refresh-button").disabled, false);
  }
  const app = harness();
  await reply(app.requests[0], 200, {status: "invalid"});
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
}

async function topbarVersionTracksCurrentSnapshot() {
  const header = markup.match(/<header\b[^>]*class="topbar"[^>]*>([\s\S]*?)<\/header>/);
  assert.ok(header && /\bid="version"/.test(header[1]), "Version belongs at the top of the page, before locked details");
  assert.equal(Array.from(markup.matchAll(/\bid="version"/g)).length, 1);
  const app = harness();
  await reply(app.requests[0], 503, {status: "unavailable", version: "0.3.2"});
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("version").hidden, false);
  assert.equal(app.element("version").textContent, "Version 0.3.2", "Even an unavailable public gateway identifies its version");
  app.enterKey("secret-key");
  await reply(app.requests.at(-1), 200, {...snapshot(), version: "0.4.0"});
  assert.equal(app.element("version").textContent, "Version 0.4.0");
  app.tick();
  app.requests.at(-1).reject(new Error("offline"));
  await flush();
  assert.equal(app.element("version").textContent, "", "Connection failure must not leave a stale header version");
  app.tick();
  await reply(app.requests.at(-1), 200, {...snapshot(), version: '<img src=x onerror="alert(1)">'});
  assert.equal(app.element("version")._text, 'Version <img src=x onerror="alert(1)">');
  assert.equal(app.element("version").children.length, 0, "Version strings must remain inert text");
  app.tick();
  await reply(app.requests.at(-1), 200, {...snapshot(), version: undefined});
  assert.equal(app.element("version").textContent, "", "Missing version must clear the prior value");
  app.event("pagehide");
  assert.equal(app.element("version").textContent, "");
}

async function nonoverlapAndNetworkFailure() {
  const app = harness();
  app.tick();
  app.element("refresh-button").events.click();
  assert.equal(app.requests.length, 1, "In-flight requests must not overlap");
  await reply(app.requests[0], 200, {status: "ready", version: "0.3.2"});
  app.tick();
  assert.equal(app.requests.length, 2);
  app.requests[1].reject(new Error("network offline"));
  await flush();
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
  assert.equal(app.element("gateway-state").textContent, "Unconfirmed");
  assert.equal(app.element("model-readiness").textContent, "Unknown");
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("checked-at").textContent, "No current snapshot");
  assert.equal(app.element("version").textContent, "", "Failed status must not leave stale version metadata");
}

async function authenticationAndSafeRendering() {
  const app = harness();
  await reply(app.requests[0], 503, {status: "unavailable", version: "0.3.2"});
  app.enterKey("secret-key");
  assert.equal(app.requests[1].url, "/status/data");
  assert.equal(app.requests[1].options.headers.Authorization, "Bearer secret-key");
  assert.equal(app.requests[1].url.includes("secret-key"), false);
  await reply(app.requests[1], 200, snapshot());
  assert.equal(app.element("details").hidden, false);
  assert.equal(app.element("count-endpoints").textContent, "1");
  assert.match(app.element("models-body").textContent, /<img src=x onerror="alert\(1\)">/);
  assert.equal(app.element("models-body").children[0].children[0]._text, '<img src=x onerror="alert(1)">');
  app.tick();
  app.requests[2].reject(new Error("network offline"));
  await flush();
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("endpoints-body").textContent, "");
  assert.equal(app.element("models-body").textContent, "");
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
}

async function lockLateResponsesAndRejectedKeys() {
  const app = harness();
  app.enterKey("first-key");
  assert.equal(app.requests[0].options.signal.aborted, true);
  await reply(app.requests[1], 200, snapshot());
  app.tick();
  app.lock();
  assert.equal(app.requests[2].options.signal.aborted, true);
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.requests[3].url, "/healthz");
  await reply(app.requests[2], 200, snapshot());
  assert.equal(app.element("details").hidden, true, "A late authenticated result must not unlock details");
  await reply(app.requests[0], 200, {status: "ready", version: "0.3.2"});
  assert.equal(app.element("health-panel").className, "health-panel tone-pending", "A cancelled old public request must not overwrite pending state");
  await reply(app.requests[3], 503, {status: "unavailable", version: "0.3.2"});
  app.enterKey("rejected-key");
  await reply(app.requests[4], 401, {});
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
  assert.equal(app.element("key-form").hidden, false);
  assert.equal(app.element("lock-button").hidden, true);
  assert.match(app.element("auth-message").textContent, /key was rejected/);
  app.tick();
  assert.equal(app.requests[5].url, "/healthz");
  assert.equal(app.requests[5].options.headers.Authorization, undefined);
}

async function keySwitchRace() {
  const app = harness();
  app.enterKey("old-key");
  app.enterKey("new-key");
  assert.equal(app.requests[1].options.signal.aborted, true);
  assert.equal(app.requests[2].options.headers.Authorization, "Bearer new-key");
  const newer = snapshot();
  newer.counts.endpoints = 7;
  await reply(app.requests[2], 200, newer);
  await reply(app.requests[1], 200, snapshot());
  assert.equal(app.element("count-endpoints").textContent, "7");
  assert.equal(app.element("details").hidden, false);
}

async function timeoutAndPageRestore() {
  const app = harness(false);
  assert.equal(app.requests[0].url, "/status/data");
  assert.equal(app.element("auth-section").hidden, true);
  for (const callback of app.timeouts.values()) callback();
  assert.equal(app.requests[0].options.signal.aborted, true, "Each fetch must have an abort timeout");
  app.requests[0].reject(new Error("aborted"));
  await flush();
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
  const locked = harness();
  locked.enterKey("memory-only-key");
  await reply(locked.requests[1], 200, snapshot());
  locked.event("pagehide");
  assert.equal(locked.element("details").hidden, true);
  assert.equal(locked.element("key-form").hidden, false);
  assert.equal(locked.intervalCount(), 0);
  locked.event("pageshow", {persisted: true});
  assert.equal(locked.intervalCount(), 2);
  assert.equal(locked.requests[2].url, "/healthz");
  assert.equal(locked.requests[2].options.headers.Authorization, undefined);
}

async function safeRouterAndBackendLinks() {
  const expectedLinks = ["/healthz", "/readyz", "/status/data", "/router/status", "/router/metrics", "/api/version", "/api/tags", "/v1/models"];
  for (const target of expectedLinks) {
    const tag = Array.from(markup.matchAll(/<a\b[^>]*>/g)).find(match => match[0].includes(`href="${target}"`) && match[0].includes('target="_blank"'));
    assert.ok(tag, `Missing quick link ${target}`);
    assert.match(tag[0], /rel="noopener noreferrer"/);
  }
  assert.match(markup, /may show 401/);
  assert.match(markup, /router key is never forwarded/);
  const app = harness(false);
  assert.equal(app.element("router-origin").textContent, "http://router.example:8088");
  const data = snapshot();
  const valid = ["http://backend:1234", "https://backend.example:8443", "http://[::1]:11434"];
  const invalid = ["javascript:alert(1)", "//untrusted.example", "http://user:secret@backend:1234", "http://backend:1234?token=secret", "http://backend:1234#secret", "http://backend:1234?", "http://backend:1234#", "http://backend:1234/v1", "http://backend:1234\\private", "http://backend:1234\n", "Custom endpoint", '<img src=x onerror="alert(1)">'];
  data.endpoints = [...valid, ...invalid].map(address => ({address, name: "Backend", state: "online"}));
  data.endpoints.push({address: "http://disabled:1234", state: "disabled"});
  data.endpoints.push({address: "http://disabled-too:1234", enabled: false});
  await reply(app.requests[0], 200, data);
  const rendered = app.element("endpoints-body").children;
  for (let index = 0; index < valid.length; index++) {
    const link = rendered[index].children[1].children[0];
    assert.equal(link.tagName, "a");
    assert.equal(link.href, valid[index]);
    assert.equal(link.target, "_blank");
    assert.equal(link.rel, "noopener noreferrer");
    assert.equal(link.href.includes("secret-key"), false);
  }
  for (let index = valid.length; index < rendered.length; index++) {
    assert.equal(rendered[index].children[1].children.length, 0, "Invalid or disabled origins must remain inert text");
    assert.equal(rendered[index].children[1].textContent, data.endpoints[index].address);
  }
}

async function selfTestIsExplicitAndIndependent() {
  const locked = harness();
  locked.selfTest();
  assert.equal(locked.requests.length, 1, "Locked details cannot start a self-test");
  const app = await unlocked();
  assert.ok(app.requests.every(request => request.options.method === "GET"), "Page loading must not run probes");
  app.tick();
  await reply(app.requests.at(-1), 200, snapshot());
  assert.ok(app.requests.every(request => request.options.method === "GET"), "Automatic refresh must not run probes");
  app.selfTest();
  const testRequest = app.requests.at(-1);
  assert.equal(testRequest.url, "/status/self-test");
  assert.equal(testRequest.options.headers.Authorization, "Bearer secret-key");
  assert.equal(testRequest.options.headers["X-LLM-Router-Self-Test"], "1");
  assert.equal(app.element("self-test-button").disabled, true);
  const requestCount = app.requests.length;
  app.selfTest();
  assert.equal(app.requests.length, requestCount, "Duplicate clicks cannot start another self-test");
  app.tick();
  assert.equal(app.requests.at(-1).url, "/status/data");
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(testRequest.options.signal.aborted, false, "Successful status refresh must not cancel probes");
  await reply(testRequest, 200, selfTestResult());
  assert.equal(app.element("self-test-button").disabled, false);
  assert.equal(app.element("self-test-results").hidden, false);
  assert.match(app.element("self-test-message").textContent, /Metadata checks passed/);
  assert.match(app.element("self-test-body").textContent, /HTTP 200/);
  assert.match(app.element("self-test-body").textContent, /4 ms/);
  app.tick();
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.element("self-test-results").hidden, false, "Normal refresh must retain explicit test results");
  const noAuth = await unlocked(false);
  noAuth.selfTest();
  assert.equal(noAuth.requests.at(-1).options.headers.Authorization, undefined);
}

async function selfTestFailuresAndSafeRendering() {
  for (const status of ["pass", "fail", "partial"]) {
    const app = await unlocked();
    app.selfTest();
    const result = selfTestResult(status);
    result.checks[0].name = '<img src=x onerror="alert(1)">';
    result.checks[0].target = "javascript:alert(1)";
    result.checks[0].detail = '<script>alert("secret")</script>';
    result.notice = '<img src=x onerror="alert(1)">';
    await reply(app.requests.at(-1), 200, result);
    assert.equal(app.element("self-test-message").className, `self-test-message result-${status}`);
    const row = app.element("self-test-body").children[0];
    assert.equal(row.children[0]._text, result.checks[0].name);
    assert.equal(row.children[0].children[0].textContent, result.checks[0].target);
    assert.equal(row.children[2]._text, result.checks[0].detail);
    assert.ok(app.element("self-test-message").textContent.endsWith(result.notice));
  }
  for (const [httpStatus, body] of [[500, {}], [200, {status: "invalid"}], [200, {status: "pass", checks: []}], [200, {status: "pass", checks: [{status: "constructor"}]}]]) {
    const app = await unlocked();
    app.selfTest();
    await reply(app.requests.at(-1), httpStatus, body);
    assert.equal(app.element("self-test-results").hidden, true);
    assert.match(app.element("self-test-message").textContent, /could not complete/);
    assert.equal(app.element("self-test-button").disabled, false);
  }
  const busy = await unlocked();
  busy.selfTest();
  await reply(busy.requests.at(-1), 429, {});
  assert.match(busy.element("self-test-message").textContent, /busy or was run too recently/);
  assert.equal(busy.element("self-test-results").hidden, true);
  const offline = await unlocked();
  offline.selfTest();
  for (const callback of offline.timeouts.values()) callback();
  assert.equal(offline.requests.at(-1).options.signal.aborted, true);
  offline.requests.at(-1).reject(new Error("timed out"));
  await flush();
  assert.match(offline.element("self-test-message").textContent, /could not complete/);
  assert.equal(offline.element("self-test-results").hidden, true);
}

async function selfTestPrivacyAndRaceGuards() {
  for (const action of ["lock", "key", "pagehide", "refresh-fail", "refresh-auth"]) {
    const app = await unlocked();
    app.selfTest();
    const oldTest = app.requests.at(-1);
    if (action === "lock") app.lock();
    if (action === "key") app.enterKey("new-key");
    if (action === "pagehide") app.event("pagehide");
    if (action === "refresh-fail" || action === "refresh-auth") {
      app.tick();
      if (action === "refresh-fail") app.requests.at(-1).reject(new Error("network lost"));
      else await reply(app.requests.at(-1), 401, {});
      await flush();
    }
    assert.equal(oldTest.options.signal.aborted, true, `${action} must cancel self-tests`);
    await reply(oldTest, 200, selfTestResult());
    assert.equal(app.element("self-test-results").hidden, true, `${action}: late self-test must not expose results`);
    assert.equal(app.element("self-test-body").textContent, "");
  }
  const app = await unlocked();
  app.selfTest();
  const oldTest = app.requests.at(-1);
  app.enterKey("new-key");
  await reply(app.requests.at(-1), 200, snapshot());
  app.selfTest();
  const newTest = app.requests.at(-1);
  const newer = selfTestResult("partial");
  await reply(newTest, 200, newer);
  await reply(oldTest, 401, {});
  assert.equal(app.element("details").hidden, false, "Late rejected old key must not lock the new session");
  assert.equal(app.element("self-test-message").className, "self-test-message result-partial");

  const deferred = await unlocked();
  deferred.selfTest();
  let resolveBody;
  deferred.requests.at(-1).resolve({status: 200, ok: true, json: () => new Promise(resolve => { resolveBody = resolve; })});
  await flush();
  deferred.lock();
  resolveBody(selfTestResult());
  await flush();
  assert.equal(deferred.element("self-test-results").hidden, true, "Late JSON parsing must not expose old-key results");

  const completed = await unlocked();
  completed.selfTest();
  await reply(completed.requests.at(-1), 200, selfTestResult());
  completed.tick();
  completed.requests.at(-1).reject(new Error("network lost"));
  await flush();
  assert.equal(completed.element("self-test-body").textContent, "", "Failed refresh clears private completed results");
}

async function selfTestAuthenticationFailure() {
  for (const status of [401, 403]) {
    const app = await unlocked();
    app.selfTest();
    const testRequest = app.requests.at(-1);
    app.tick();
    const oldRefresh = app.requests.at(-1);
    await reply(testRequest, status, {});
    assert.equal(oldRefresh.options.signal.aborted, true, "Rejected test credentials invalidate an in-flight status response");
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("self-test-results").hidden, true);
    assert.equal(app.element("key-form").hidden, false);
    assert.equal(app.element("lock-button").hidden, true);
    assert.equal(app.element("refresh-button").disabled, false);
    assert.match(app.element("auth-message").textContent, /key was rejected/);
    await reply(oldRefresh, 200, snapshot());
    assert.equal(app.element("details").hidden, true);
    app.tick();
    assert.equal(app.requests.at(-1).url, "/healthz");
    assert.equal(app.requests.at(-1).options.headers.Authorization, undefined);
  }
}

function savedHost(id = "host-one", checked = false) {
  return {
    id, address: "192.168.194.0", checked_at: checked ? "2026-09-16T12:00:00Z" : null,
    checks: checked ? [{provider: "LM Studio / OpenAI-compatible", base_url: "http://192.168.194.0:1234/v1", status: "pass", detail: "Models metadata is reachable", http_status: 200, elapsed_ms: 12.3}] : [],
    routing: {status: checked ? "active" : "pending", model_count: checked ? 1 : 0, detail: checked ? "Models are enrolled for routing." : "Waiting for the next metadata check."},
  };
}

async function unlockedHosts(hosts = []) {
  const app = harness(true, false);
  app.enterKey("secret-key");
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.hostRequests.length, 1);
  assert.equal(app.hostRequests[0].options.method, "GET");
  await reply(app.hostRequests[0], 200, {hosts, limit: 16});
  return app;
}

async function savedHostLifecycle() {
  const app = await unlockedHosts();
  assert.match(app.element("hosts-body").textContent, /No saved addresses/);
  assert.equal(app.element("hosts-check-button").disabled, true);
  app.saveHost("  192.168.194.0  ");
  const save = app.hostRequests.at(-1);
  assert.equal(save.url, "/status/hosts");
  assert.equal(save.options.method, "POST");
  assert.deepEqual(JSON.parse(save.options.body), {address: "192.168.194.0"});
  assert.equal(save.options.headers.Authorization, "Bearer secret-key");
  assert.equal(save.url.includes("secret-key"), false);
  assert.equal(app.element("host-save-button").disabled, true);
  const pendingCount = app.hostRequests.length;
  app.saveHost("ignored.example");
  app.checkHosts();
  app.reloadHosts();
  assert.equal(app.hostRequests.length, pendingCount, "Only one host operation can be in flight");
  const found = savedHost("host-one", true);
  found.checks.push({provider: "Ollama", base_url: "http://192.168.194.0:11434", status: "fail", detail: "Connection unavailable", http_status: null, elapsed_ms: 1500});
  await reply(save, 201, {host: found});
  assert.equal(app.hostRequests.length, pendingCount, "Save already checks/enrolls; the browser must not send a second check POST");
  assert.equal(app.element("host-address").value, "");
  assert.equal(app.requests.at(-1).url, "/status/data", "Enrollment must refresh the router's model catalog");
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.element("host-save-button").disabled, false);
  assert.equal(app.element("hosts-check-button").disabled, false);
  assert.match(app.element("hosts-message").textContent, /Address saved.*1 backend API found/);
  assert.match(app.element("hosts-body").textContent, /Routing enabled/);
  assert.match(app.element("hosts-body").textContent, /LM Studio \/ OpenAI-compatible Found/);
  assert.match(app.element("hosts-body").textContent, /Ollama Not confirmed/);
  assert.match(app.element("hosts-body").textContent, /12 ms/);
  assert.match(app.element("hosts-body").textContent, /HTTP 200/);
  assert.match(app.element("hosts-body").textContent, /192\.168\.194\.0:1234\/v1/);
  const completedCount = app.hostRequests.length;
  app.tick();
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.hostRequests.length, completedCount, "Ordinary refresh must neither reload nor probe saved hosts");
  assert.match(app.element("hosts-body").textContent, /Found/);

  app.checkHosts();
  assert.deepEqual(JSON.parse(app.hostRequests.at(-1).options.body), {});
  assert.doesNotMatch(app.element("hosts-body").textContent, /Models metadata is reachable/, "New check must clear prior success while pending");
  app.tick();
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.hostRequests.at(-1).options.signal.aborted, false, "Status refresh must not interrupt explicit checks");
  await reply(app.hostRequests.at(-1), 200, {hosts: [found]});
  await reply(app.requests.at(-1), 200, snapshot());
  app.hostAction(0, 0);
  assert.deepEqual(JSON.parse(app.hostRequests.at(-1).options.body), {id: "host-one"});
  await reply(app.hostRequests.at(-1), 200, {hosts: [found]});
  await reply(app.requests.at(-1), 200, snapshot());
  app.hostAction(0, 1);
  assert.equal(app.hostRequests.at(-1).url, "/status/hosts/host-one");
  assert.equal(app.hostRequests.at(-1).options.method, "DELETE");
  assert.equal(app.hostRequests.at(-1).options.body, undefined);
  await reply(app.hostRequests.at(-1), 200, {removed: true});
  assert.match(app.element("hosts-body").textContent, /No saved addresses/);
  assert.match(app.element("hosts-message").textContent, /Address removed/);
  assert.match(app.element("hosts-message").textContent, /Explicitly configured routes are preserved/);
  assert.equal(app.requests.at(-1).url, "/status/data", "Removal must refresh the router's model catalog");
}

async function savedHostsRestoreAndRequireAuthentication() {
  const locked = harness();
  locked.saveHost("192.168.194.0");
  locked.checkHosts();
  locked.reloadHosts();
  assert.equal(locked.hostRequests.length, 0, "Locked clients cannot list, save, or probe hosts");
  const noKey = await unlocked(false);
  noKey.saveHost("192.168.194.0");
  noKey.checkHosts();
  noKey.reloadHosts();
  assert.equal(noKey.hostRequests.length, 0, "Keyless gateways never get address-management requests");
  assert.equal(noKey.element("host-address").disabled, true);
  assert.equal(noKey.element("host-save-button").disabled, true);
  assert.equal(noKey.element("hosts-check-button").disabled, true);
  assert.match(noKey.element("hosts-message").textContent, /LLM_ROUTER_GATEWAY_API_KEY/);

  for (const checked of [true, false]) {
    const app = await unlockedHosts([savedHost("saved-previously", checked)]);
    assert.equal(app.hostRequests.length, 1);
    assert.ok(app.allRequests.every(request => request.options.method === "GET"), "Reloading the page restores saved addresses without probing");
    assert.match(app.element("hosts-body").textContent, /192\.168\.194\.0/);
    assert.match(app.element("hosts-body").textContent, checked ? /Found/ : /Not checked/);
    app.reloadHosts();
    assert.equal(app.hostRequests.at(-1).options.method, "GET");
    await reply(app.hostRequests.at(-1), 200, {hosts: [savedHost()], limit: 1});
    assert.equal(app.element("host-save-button").disabled, true, "Save disabled at the server-provided limit");
    assert.match(app.element("hosts-message").textContent, /1 \/ 1 addresses/);
  }
  assert.match(markup, /automatically enroll its discovered models for client routing/);
  assert.match(markup, /at startup and every 30 seconds/);
  assert.match(markup, /not whole subnets/);
  assert.doesNotMatch(markup, /Read-only dashboard/);
  assert.match(markup, /no prompts, model loading, or downloads/);
}

async function savedHostFailuresAndSafeRendering() {
  for (const [status, expected] of [[400, /valid IP/], [404, /no longer exists/], [409, /limit was reached/], [429, /busy/], [503, /unavailable/], [500, /connection failed/]]) {
    const app = await unlockedHosts();
    app.saveHost("invalid-address");
    await reply(app.hostRequests.at(-1), status, {error: "secret-server-stack-trace"});
    assert.match(app.element("hosts-message").textContent, expected);
    assert.doesNotMatch(app.element("hosts-message").textContent, /secret-server-stack-trace/);
    assert.equal(app.element("host-save-button").disabled, false);
    assert.equal(app.element("host-address").value, "invalid-address");
  }
  const pendingCheck = await unlockedHosts();
  pendingCheck.saveHost("192.168.194.0");
  await reply(pendingCheck.hostRequests.at(-1), 201, {host: savedHost()});
  assert.match(pendingCheck.element("hosts-message").textContent, /Address saved.*Enrollment is pending/);
  assert.match(pendingCheck.element("hosts-body").textContent, /192\.168\.194\.0/);
  assert.equal(pendingCheck.element("hosts-check-button").disabled, false);

  const app = await unlockedHosts();
  const unsafe = savedHost("unsafe/id?redirect=other", true);
  unsafe.address = '<img src=x onerror="alert(1)">';
  unsafe.checks[0].provider = '<script>alert("name")</script>';
  unsafe.checks[0].base_url = "javascript:alert(1)";
  unsafe.checks[0].detail = '<img src=x onerror="alert(2)">';
  app.reloadHosts();
  await reply(app.hostRequests.at(-1), 200, {hosts: [unsafe], limit: 16});
  const row = app.element("hosts-body").children[0];
  assert.equal(row.children[0]._text, unsafe.address);
  const check = row.children[1].children[0].children[0];
  assert.equal(check.children[0].children[0]._text, unsafe.checks[0].provider + " ");
  assert.equal(check.children[1]._text, "API base: " + unsafe.checks[0].base_url);
  assert.ok(check.children[2]._text.startsWith(unsafe.checks[0].detail));
  app.hostAction(0, 1);
  assert.equal(app.hostRequests.at(-1).url, "/status/hosts/unsafe%2Fid%3Fredirect%3Dother");
  await reply(app.hostRequests.at(-1), 500, {});
  assert.equal(app.element("hosts-body").children.length, 1, "Failed removal must retain saved row");
  for (const invalid of [{}, {hosts: [{}]}, {hosts: [savedHost(), savedHost()]}, {hosts: [{...savedHost(), checks: [{status: "constructor"}]}]}]) {
    app.reloadHosts();
    await reply(app.hostRequests.at(-1), 200, invalid);
    assert.match(app.element("hosts-message").textContent, /response was invalid/);
    assert.equal(app.element("hosts-reload-button").disabled, false);
  }
}

async function savedHostPrivacyAndRaceGuards() {
  for (const action of ["lock", "key", "pagehide", "refresh-fail", "refresh-auth"]) {
    const app = await unlockedHosts([savedHost()]);
    app.hostAction(0, 0);
    const old = app.hostRequests.at(-1);
    if (action === "lock") app.lock();
    if (action === "key") app.enterKey("new-key");
    if (action === "pagehide") app.event("pagehide");
    if (action === "refresh-fail" || action === "refresh-auth") {
      app.tick();
      if (action === "refresh-fail") app.requests.at(-1).reject(new Error("network lost"));
      else await reply(app.requests.at(-1), 401, {});
      await flush();
    }
    assert.equal(old.options.signal.aborted, true, `${action} must cancel host checks`);
    await reply(old, 200, {hosts: [savedHost("host-one", true)]});
    assert.equal(app.element("hosts-body").textContent, "", `${action}: late checks cannot restore private rows`);
    assert.equal(app.element("host-address").value, "");
  }
  const app = await unlockedHosts([savedHost()]);
  app.checkHosts();
  const old = app.hostRequests.at(-1);
  app.enterKey("new-key");
  await reply(app.requests.at(-1), 200, snapshot());
  await reply(app.hostRequests.at(-1), 200, {hosts: [savedHost("new-session", true)], limit: 16});
  await reply(old, 401, {});
  assert.equal(app.element("details").hidden, false);
  assert.match(app.element("hosts-body").textContent, /Found/);
  assert.doesNotMatch(app.element("auth-message").textContent, /rejected/);

  const deferred = await unlockedHosts([savedHost()]);
  deferred.checkHosts();
  let resolveBody;
  deferred.hostRequests.at(-1).resolve({status: 200, ok: true, json: () => new Promise(resolve => { resolveBody = resolve; })});
  await flush();
  deferred.lock();
  resolveBody({hosts: [savedHost("host-one", true)]});
  await flush();
  assert.equal(deferred.element("hosts-body").textContent, "", "Late JSON body cannot restore private rows");
}

async function savedHostAuthRejectionAndTimeout() {
  for (const status of [401, 403]) {
    const app = await unlockedHosts([savedHost()]);
    app.checkHosts();
    const hostRequest = app.hostRequests.at(-1);
    app.selfTest();
    const selfTest = app.requests.at(-1);
    app.tick();
    const refresh = app.requests.at(-1);
    await reply(hostRequest, status, {});
    assert.equal(refresh.options.signal.aborted, true);
    assert.equal(selfTest.options.signal.aborted, true);
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("hosts-body").textContent, "");
    assert.equal(app.element("key-form").hidden, false);
    await reply(refresh, 200, snapshot());
    await reply(selfTest, 200, selfTestResult());
    assert.equal(app.element("details").hidden, true);
  }
  const timedOut = await unlockedHosts([savedHost()]);
  timedOut.checkHosts();
  const pending = timedOut.hostRequests.at(-1);
  for (const callback of timedOut.timeouts.values()) callback();
  assert.equal(pending.options.signal.aborted, true);
  await reply(pending, 200, {hosts: [savedHost("host-one", true)]});
  assert.match(timedOut.element("hosts-message").textContent, /timed out/);
  assert.doesNotMatch(timedOut.element("hosts-body").textContent, /Found/, "Even a late successful reply after timeout must not look current");
  assert.equal(timedOut.element("hosts-check-button").disabled, false);
  timedOut.reloadHosts();
  timedOut.hostRequests.at(-1).reject(new Error("offline"));
  await flush();
  assert.match(timedOut.element("hosts-message").textContent, /connection failed/);
  assert.equal(timedOut.element("hosts-reload-button").disabled, false);
}

function hostWithCatalog(overrides = {}) {
  const host = savedHost("host-one", true);
  host.checks[0] = {
    ...host.checks[0], catalog_status: "ok", catalog_detail: "Model list is reachable",
    catalog_url: "http://192.168.194.0:1234/v1/models", model_count: 2, models_truncated: false,
    models: [
      {id: "qwen3.5:9b", address: "http://192.168.194.0:1234/v1"},
      {id: "gemma3:12b", address: "http://192.168.194.0:1234/v1"},
    ], ...overrides,
  };
  return host;
}

function descendants(node, className) {
  const matches = node.className.split(" ").includes(className) ? [node] : [];
  return matches.concat(...node.children.map(child => descendants(child, className)));
}

async function savedHostModelCatalogs() {
  const host = hostWithCatalog();
  host.checks.push({
    provider: "Ollama", base_url: "http://192.168.194.0:11434", status: "pass", detail: "Ollama server reachable",
    http_status: 200, elapsed_ms: 18, catalog_status: "ok", catalog_detail: "Model list is reachable",
    catalog_url: "http://192.168.194.0:11434/api/tags", model_count: 1, models_truncated: false,
    models: [{id: "qwen3.5:9b", address: "http://192.168.194.0:11434"}],
  });
  const app = await unlockedHosts([host]);
  assert.ok(app.allRequests.every(request => request.options.method === "GET"), "Displaying cached model lists must not run backend operations");
  const body = app.element("hosts-body");
  assert.match(body.textContent, /2 models listed by this server/);
  assert.match(body.textContent, /1 model listed by this server/);
  assert.match(body.textContent, /API base: http:\/\/192\.168\.194\.0:11434/);
  assert.match(body.textContent, /Model-list endpoint: http:\/\/192\.168\.194\.0:11434\/api\/tags/);
  assert.match(body.textContent, /Listed models may not be loaded; inference is not tested/);
  const tables = descendants(body, "host-model-table");
  assert.equal(tables.length, 2, "Each server needs its own labeled model table");
  const first = tables[0].children[2].children;
  assert.equal(first.length, 2);
  assert.equal(first[0].children[0].children[0].textContent, "qwen3.5:9b");
  assert.equal(first[0].children[1].children[0].textContent, "http://192.168.194.0:1234/v1");
  assert.equal(first[1].children[0].children[0].textContent, "gemma3:12b");
  assert.equal(tables[1].children[2].children[0].children[1].children[0].textContent, "http://192.168.194.0:11434", "A repeated model on another provider must show that provider's address");
  assert.equal(tables[0].children[1].children[0].children[0].textContent, "Model ID");
  assert.equal(tables[0].children[1].children[0].children[1].textContent, "API address");
  const headers = descendants(body, "host-check-heading");
  assert.equal(headers[0].children[0].textContent, "LM Studio / OpenAI-compatible ", "Provider and status need a text-space, not only CSS spacing");
  assert.equal(headers[1].children[1].textContent, "Found");
  const unicode = hostWithCatalog({models: [{id: "😀".repeat(512), address: "http://192.168.194.0:1234/v1"}], model_count: 1});
  app.reloadHosts();
  await reply(app.hostRequests.at(-1), 200, {hosts: [unicode], limit: 16});
  assert.ok(body.textContent.includes("😀".repeat(512)), "Model-ID limit must match backend Unicode code points, not UTF-16 units");
}

async function savedHostCatalogEmptyErrorTruncatedAndLegacy() {
  const empty = await unlockedHosts([hostWithCatalog({models: [], model_count: 0})]);
  assert.match(empty.element("hosts-body").textContent, /No models listed by this server/);
  assert.equal(descendants(empty.element("hosts-body"), "host-model-table").length, 0);
  assert.doesNotMatch(empty.element("hosts-body").textContent, /unavailable|unknown/);
  const unavailable = await unlockedHosts([hostWithCatalog({catalog_status: "error", catalog_detail: "Model catalog timed out", models: [], model_count: null})]);
  assert.match(unavailable.element("hosts-body").textContent, /OpenAI-compatible Found/);
  assert.match(unavailable.element("hosts-body").textContent, /Model list unavailable; model count is unknown. Model catalog timed out/);
  assert.doesNotMatch(unavailable.element("hosts-body").textContent, /No models listed|0 models/);
  assert.equal(descendants(unavailable.element("hosts-body"), "host-model-table").length, 0);
  const truncated = await unlockedHosts([hostWithCatalog({model_count: 251, models_truncated: true})]);
  assert.match(truncated.element("hosts-body").textContent, /251 models listed/);
  assert.match(truncated.element("hosts-body").textContent, /Showing 2 of 251 models. The list is truncated/);
  const legacy = await unlockedHosts([savedHost("host-one", true)]);
  assert.match(legacy.element("hosts-body").textContent, /Check again to retrieve model list/);
  assert.doesNotMatch(legacy.element("hosts-body").textContent, /No models listed|0 models/);
}

async function savedHostCatalogEscapingAndValidation() {
  const hostile = hostWithCatalog({
    catalog_url: "javascript:alert(1)", catalog_detail: '<script>alert("detail")</script>', model_count: 1,
    models: [{id: '<img src=x onerror="alert(1)">', address: "javascript:alert(2)"}],
  });
  const app = await unlockedHosts([hostile]);
  const body = app.element("hosts-body");
  const row = descendants(body, "host-model-table")[0].children[2].children[0];
  assert.equal(row.children[0].children[0].tagName, "code");
  assert.equal(row.children[0].children[0]._text, hostile.checks[0].models[0].id);
  assert.equal(row.children[1].children[0]._text, hostile.checks[0].models[0].address);
  const source = descendants(body, "host-api-address").at(-1);
  assert.equal(source.tagName, "p");
  assert.equal(source._text, "Model-list endpoint: javascript:alert(1)");
  assert.equal(source.children.length, 0, "Catalog addresses must remain inert text, not payload links");
  app.reloadHosts();
  hostile.checks[0].catalog_status = "error";
  hostile.checks[0].model_count = null;
  hostile.checks[0].models = [];
  await reply(app.hostRequests.at(-1), 200, {hosts: [hostile], limit: 16});
  const warning = descendants(body, "catalog-warning")[0];
  assert.equal(warning._text, 'Model list unavailable; model count is unknown. <script>alert("detail")</script>');
  assert.equal(warning.children.length, 0);
  const invalidCases = [
    {catalog_status: "constructor"}, {models: null}, {models: [{id: "ok", address: null}]},
    {models: [{id: "x".repeat(513), address: "http://backend:11434"}]},
    {models: [{id: "😀".repeat(513), address: "http://backend:11434"}]},
    {models: [{id: "ok", address: "x".repeat(513)}]},
    {models: Array.from({length: 201}, (_, i) => ({id: String(i), address: "http://backend:11434"})), model_count: 201},
    {model_count: -1}, {model_count: null}, {model_count: 1}, {models_truncated: "yes"}, {catalog_url: null},
  ];
  for (const invalid of invalidCases) {
    app.reloadHosts();
    await reply(app.hostRequests.at(-1), 200, {hosts: [hostWithCatalog(invalid)], limit: 16});
    assert.match(app.element("hosts-message").textContent, /response was invalid/);
    assert.equal(app.element("hosts-reload-button").disabled, false);
  }
}

async function savedHostCatalogStaleAndPrivate() {
  const app = await unlockedHosts([hostWithCatalog()]);
  assert.match(app.element("hosts-body").textContent, /qwen3\.5:9b/);
  app.checkHosts();
  const pending = app.hostRequests.at(-1);
  assert.doesNotMatch(app.element("hosts-body").textContent, /qwen3\.5:9b/, "A new check must clear stale model lists while pending");
  await reply(pending, 200, {hosts: [hostWithCatalog({catalog_status: "error", catalog_detail: "Catalog unavailable", model_count: null, models: []})]});
  assert.doesNotMatch(app.element("hosts-body").textContent, /qwen3\.5:9b/);
  assert.match(app.element("hosts-body").textContent, /model count is unknown/);
  app.checkHosts();
  const late = app.hostRequests.at(-1);
  app.lock();
  await reply(late, 200, {hosts: [hostWithCatalog()]});
  assert.equal(app.element("hosts-body").textContent, "", "Locked sessions must not restore model IDs from late responses");
  const completed = await unlockedHosts([hostWithCatalog()]);
  completed.event("pagehide");
  assert.equal(completed.element("hosts-body").textContent, "", "Page navigation clears private cached model IDs and addresses");
}

async function publicCachedSummary() {
  assert.ok(markup.indexOf('id="public-summary-title"') < markup.indexOf('id="details"'), "Public summary must be outside the locked detail section");
  const app = harness();
  await reply(app.requests[0], 200, {status: "ready", summary: {servers: 3, models: 8, last_verified_at: "2026-09-16T12:00:00Z", models_truncated: false}});
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("public-count-servers").textContent, "3");
  assert.equal(app.element("public-count-models").textContent, "8");
  assert.notEqual(app.element("public-last-verified").textContent, "Unknown");
  assert.match(app.element("public-summary-note").textContent, /cached metadata, not a live network scan/);
  assert.equal(app.hostRequests.length, 0, "Public summary must not list or check saved hosts");
  app.tick();
  await reply(app.requests.at(-1), 503, {status: "unavailable", summary: {servers: 0, models: 0, last_verified_at: null, models_truncated: false}});
  assert.equal(app.element("public-count-servers").textContent, "0");
  assert.equal(app.element("public-count-models").textContent, "0");
  assert.equal(app.element("public-last-verified").textContent, "Not yet verified");
  app.tick();
  await reply(app.requests.at(-1), 200, {status: "ready", summary: {servers: 2, models: 200, last_verified_at: null, models_truncated: true}});
  assert.equal(app.element("public-count-models").textContent, "200+");
  assert.match(app.element("public-summary-note").textContent, /lower bound/);
  app.tick();
  app.requests.at(-1).reject(new Error("offline"));
  await flush();
  for (const id of ["public-count-servers", "public-count-models", "public-last-verified"]) assert.equal(app.element(id).textContent, "Unknown", "Network failure clears stale public stats");
  app.enterKey("secret-key");
  const detailed = snapshot();
  detailed.summary = {servers: 4, models: 5, last_verified_at: "2026-09-16T12:00:00Z", models_truncated: false};
  await reply(app.requests.at(-1), 200, detailed);
  assert.equal(app.element("public-count-servers").textContent, "4");
  assert.equal(app.element("public-count-models").textContent, "5");
  app.event("pagehide");
  assert.equal(app.element("public-count-models").textContent, "Unknown");

  for (const summary of [undefined, {}, {servers: "private-backend", models: -1, last_verified_at: "not-a-date"}, {servers: null, models: null, last_verified_at: 0}]) {
    const invalid = harness();
    await reply(invalid.requests[0], 200, {status: "ready", summary});
    for (const id of ["public-count-servers", "public-count-models", "public-last-verified"]) assert.equal(invalid.element(id).textContent, "Unknown");
  }
}

async function urlKeyBootstrapAndImmediateScrub() {
  for (const [url, expectedClean, key, queryWarning] of [
    ["http://router.example:8088/status?api_key=query-secret&view=summary#models", "/status?view=summary#models", "query-secret", true],
    ["http://router.example:8088/status?view=summary#api_key=fragment-secret&tab=hosts", "/status?view=summary#tab=hosts", "fragment-secret", false],
    ["http://router.example:8088/?view=summary#api_key=a%2Bb%2Fc%3D%3D", "/?view=summary", "a+b/c==", false],
    ["http://router.example:8088/status?%61pi_key=encoded-secret", "/status", "encoded-secret", true],
  ]) {
    const app = harness(true, true, {url});
    assert.deepEqual(app.timeline.slice(0, 2), ["replaceState", "fetch"], "Key must be removed from URL before first request");
    assert.equal(app.historyCalls.length, 1);
    assert.equal(app.historyCalls[0].state, null, "Never put a key in history.state");
    assert.equal(app.historyCalls[0].url, expectedClean);
    assert.equal(app.location.pathname + app.location.search + app.location.hash, expectedClean);
    assert.equal(app.requests[0].url, "/status/data");
    assert.equal(app.requests[0].options.headers.Authorization, `Bearer ${key}`);
    assert.equal(app.element("api-key").value, "");
    assert.equal(app.element("url-key-message").hidden, !queryWarning);
    if (queryWarning) assert.match(app.element("url-key-message").textContent, /may already be recorded.*prefer #api_key=/);
    for (const id of ["url-key-message", "auth-message", "router-origin"]) assert.equal(app.element(id).textContent.includes(key), false, "Key must never appear in page text");
    await reply(app.requests[0], 200, snapshot());
    assert.equal(app.element("details").hidden, false);
    assert.equal(app.hostRequests[0].options.headers.Authorization, `Bearer ${key}`);
    app.lock();
    assert.equal(app.requests.at(-1).url, "/healthz");
    assert.equal(app.requests.at(-1).options.headers.Authorization, undefined);
    app.event("pagehide");
    app.event("pageshow", {persisted: true});
    assert.equal(app.historyCalls.length, 1, "BFCache restoration must not reconsume the URL key");
    assert.equal(app.requests.at(-1).options.headers.Authorization, undefined);
  }
  const max = harness(true, true, {url: "http://router.example:8088/status#api_key=" + "x".repeat(4096)});
  assert.equal(max.requests[0].options.headers.Authorization.length, 7 + 4096);
  assert.equal(max.location.hash, "");
}

async function urlKeyInvalidAmbiguousAndCleanupFailure() {
  for (const suffix of [
    "?api_key=one&api_key=two&view=hosts#models", "#api_key=one&api_key=two&tab=hosts",
    "?api_key=one#api_key=two", "?api_key=", "#api_key=%20%20", "#api_key=a%0Ab",
    "#api_key=" + "x".repeat(4097),
  ]) {
    const app = harness(true, true, {url: "http://router.example:8088/status" + suffix});
    assert.equal(app.requests[0].url, "/healthz");
    assert.equal(app.requests[0].options.headers.Authorization, undefined);
    assert.equal(app.location.search.includes("api_key"), false);
    assert.equal(app.location.hash.includes("api_key"), false);
    assert.match(app.element("url-key-message").textContent, /removed but not used/);
    assert.equal(app.element("api-key").value, "");
  }
  const failure = harness(true, true, {url: "http://router.example:8088/status?api_key=do-not-send&view=hosts", replaceStateThrows: true});
  assert.equal(failure.requests[0].url, "/healthz");
  assert.equal(failure.requests[0].options.headers.Authorization, undefined);
  assert.match(failure.element("url-key-message").textContent, /could not be removed.*was not used/);
  assert.doesNotMatch(failure.element("url-key-message").textContent, /do-not-send/);
  const noKey = harness(false, true, {url: "http://router.example:8088/status#api_key=not-required"});
  assert.equal(noKey.location.hash, "");
  assert.equal(noKey.requests[0].options.headers.Authorization, undefined);
  assert.match(noKey.element("url-key-message").textContent, /removed and was not used/);
  const wrongPage = harness(true, true, {url: "http://router.example:8088/v1/models#api_key=not-an-api-key-login"});
  assert.equal(wrongPage.location.hash, "");
  assert.equal(wrongPage.requests[0].options.headers.Authorization, undefined);
  const ordinary = harness(true, true, {url: "http://router.example:8088/status?view=hosts#models"});
  assert.equal(ordinary.historyCalls.length, 0);
  assert.equal(ordinary.location.search + ordinary.location.hash, "?view=hosts#models");
}

async function urlKeyAuthenticationFailureAndPageRestore() {
  for (const status of [401, 403]) {
    const app = harness(true, true, {url: "http://router.example:8088/status#api_key=rejected-url-key"});
    assert.equal(app.requests[0].options.headers.Authorization, "Bearer rejected-url-key");
    await reply(app.requests[0], status, {});
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("key-form").hidden, false);
    assert.match(app.element("auth-message").textContent, /key was rejected/);
    app.tick();
    assert.equal(app.requests.at(-1).url, "/healthz");
    assert.equal(app.requests.at(-1).options.headers.Authorization, undefined);
  }
  const app = harness(true, true, {url: "http://router.example:8088/status#api_key=memory-only-url-key"});
  await reply(app.requests[0], 200, snapshot());
  app.event("pagehide");
  app.event("pageshow", {persisted: true});
  assert.equal(app.requests.at(-1).url, "/healthz");
  assert.equal(app.requests.at(-1).options.headers.Authorization, undefined);
  assert.equal(app.element("hosts-body").textContent, "");
  assert.equal(app.element("models-body").textContent, "");
}

async function liveFragmentKeyUnlockAndNavigation() {
  const app = harness();
  await reply(app.requests[0], 200, {status: "ready"});
  const before = app.allRequests.length;
  app.location.hash = "#models";
  app.event("hashchange");
  assert.equal(app.allRequests.length, before, "Ordinary anchor navigation must not fetch or probe");
  assert.equal(app.historyCalls.length, 0);
  app.location.hash = "#api_key=live%2Bkey%2F%3D&tab=hosts";
  const beforeKey = app.timeline.length;
  app.event("hashchange");
  assert.deepEqual(app.timeline.slice(beforeKey, beforeKey + 2), ["replaceState", "fetch"]);
  assert.equal(app.location.hash, "#tab=hosts");
  assert.equal(app.requests.at(-1).url, "/status/data");
  assert.equal(app.requests.at(-1).options.headers.Authorization, "Bearer live+key/=");
  assert.equal(app.element("api-key").value, "");
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.element("details").hidden, false);
  assert.equal(app.hostRequests.at(-1).options.headers.Authorization, "Bearer live+key/=");
  const unlockedCount = app.allRequests.length;
  app.location.hash = "#backends-title";
  app.event("hashchange");
  assert.equal(app.allRequests.length, unlockedCount);
  app.event("pagehide");
  const hiddenCount = app.allRequests.length;
  app.location.hash = "#api_key=hidden-page-key";
  app.event("hashchange");
  assert.equal(app.location.hash, "", "Even a hidden page must scrub a URL secret");
  assert.equal(app.allRequests.length, hiddenCount, "A hidden page must not unlock or send the supplied key");
  app.event("pageshow", {persisted: true});
  assert.equal(app.requests.at(-1).url, "/healthz");
  assert.equal(app.requests.at(-1).options.headers.Authorization, undefined, "Restoration must not reuse either fragment key");
}

async function liveFragmentKeyInvalidAndCleanupFailure() {
  const options = {};
  const app = harness(true, true, options);
  await reply(app.requests[0], 200, {status: "ready"});
  for (const hash of ["#api_key=one&api_key=two&tab=hosts", "#api_key=", "#api_key=a%0Ab", "#api_key=" + "x".repeat(4097)]) {
    const before = app.allRequests.length;
    app.location.hash = hash;
    app.event("hashchange");
    assert.equal(app.allRequests.length, before, "Invalid live fragment key must not trigger a request");
    assert.equal(app.location.hash.includes("api_key"), false);
    assert.match(app.element("url-key-message").textContent, /removed but not used/);
    assert.equal(app.element("details").hidden, true);
  }
  options.replaceStateThrows = true;
  const before = app.allRequests.length;
  app.location.hash = "#api_key=cleanup-failed-key";
  app.event("hashchange");
  assert.equal(app.allRequests.length, before);
  assert.equal(app.location.hash, "#api_key=cleanup-failed-key");
  assert.match(app.element("url-key-message").textContent, /could not be removed.*was not used/);
  assert.doesNotMatch(app.element("url-key-message").textContent, /cleanup-failed-key/);
  app.tick();
  assert.equal(app.requests.at(-1).options.headers.Authorization, undefined, "Failed URL cleanup must not install a key for later polling either");
}

async function liveFragmentKeyCancelsOldSession() {
  const app = await unlockedHosts([hostWithCatalog()]);
  app.checkHosts();
  const oldHost = app.hostRequests.at(-1);
  app.selfTest();
  const oldTest = app.requests.at(-1);
  app.tick();
  const oldRefresh = app.requests.at(-1);
  app.location.hash = "#api_key=new-live-key";
  app.event("hashchange");
  assert.equal(oldHost.options.signal.aborted, true);
  assert.equal(oldTest.options.signal.aborted, true);
  assert.equal(oldRefresh.options.signal.aborted, true);
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("hosts-body").textContent, "");
  assert.equal(app.requests.at(-1).options.headers.Authorization, "Bearer new-live-key");
  const newer = snapshot();
  newer.counts.endpoints = 9;
  await reply(app.requests.at(-1), 200, newer);
  assert.equal(app.hostRequests.at(-1).options.headers.Authorization, "Bearer new-live-key");
  await reply(app.hostRequests.at(-1), 200, {hosts: [hostWithCatalog()], limit: 16});
  await reply(oldRefresh, 200, snapshot());
  await reply(oldHost, 401, {});
  await reply(oldTest, 403, {});
  assert.equal(app.element("details").hidden, false, "Late old-key errors must not lock the new URL-key session");
  assert.equal(app.element("count-endpoints").textContent, "9", "Late old-key snapshot must not replace new state");
  assert.match(app.element("hosts-body").textContent, /qwen3\.5:9b/);
  assert.doesNotMatch(app.element("auth-message").textContent, /rejected/);
}

function performanceObservation(latest, ewma = latest, samples = 3) {
  return {latest, ewma, samples, updated_at: "2026-09-17T12:00:00Z"};
}

function performanceSnapshot() {
  return {
    available: true, error: null, updated_at: "2026-09-17T12:00:00Z",
    deployments: [{
      id: "qwen-golemframe", machine: "golemframe", endpoint: "golemframe-ollama", model: "qwen3:8b",
      address: "http://golemframe:11434", adapter: "ollama", successes: 7, failures: 2,
      input_tokens_total: 1000, output_tokens_total: 500, last_seen_at: "2026-09-17T12:00:00Z", current: true,
      metrics: {
        input_tokens_per_second: performanceObservation(100, 80, 10),
        output_tokens_per_second: performanceObservation(30, 25, 7),
        load_duration_ms: performanceObservation(2000, 800, 4),
        request_duration_ms: performanceObservation(1500, 1200, 9),
      }, slow_load_count: 2, slow_load_threshold_ms: 1000,
    }],
  };
}

async function unlockedPerformance(performance = performanceSnapshot()) {
  const app = harness();
  app.enterKey("secret-key");
  await reply(app.requests.at(-1), 200, {...snapshot(), performance});
  return app;
}

async function performanceIsPassiveAndPerDeployment() {
  assert.ok(markup.indexOf('id="performance-title"') > markup.indexOf('id="details"'), "Performance table belongs inside locked details");
  assert.match(markup, /saved across restarts and updates/);
  assert.match(markup, /Refresh never runs a benchmark or model/);
  assert.match(markup, /do not prove disk I\/O/);
  assert.match(markup, /Missing timings are not estimated from request latency/);
  assert.match(markup, /Reported load \/ setup/);
  assert.match(markup, /Request time \(wall clock\)/);
  assert.match(markup, /Request time covers the whole upstream call/);
  assert.match(markup, /href="\/router\/metrics"[^>]*>Performance JSON/);
  const locked = harness();
  await reply(locked.requests[0], 200, {status: "ready", performance: performanceSnapshot()});
  assert.equal(locked.element("performance-body").textContent, "", "A public readiness payload must never render detailed metrics");
  const performance = performanceSnapshot();
  performance.deployments.push({...performance.deployments[0], id: "qwen-pantheon", machine: "pantheon", endpoint: "old-lmstudio", address: "http://pantheon:1234", current: false});
  const app = await unlockedPerformance(performance);
  const rows = app.element("performance-body").children;
  assert.equal(rows.length, 2);
  assert.match(rows[0].children[0].textContent, /qwen3:8bServer: golemframe/);
  assert.match(rows[0].children[0].textContent, /http:\/\/golemframe:11434/);
  assert.match(rows[0].children[0].textContent, /Current deployment/);
  assert.match(rows[0].children[0].textContent, /API: ollama/);
  assert.equal(rows[0].children[0].textContent.includes(performance.deployments[0].id), false, "Opaque persisted deployment IDs belong in JSON, not the human-facing table");
  assert.match(rows[1].children[0].textContent, /Server: pantheon/);
  assert.match(rows[1].children[0].textContent, /Historical/);
  assert.doesNotMatch(rows[1].children[0].textContent, /Online|Available/, "A historical row does not claim current reachability");
  assert.equal(rows[0].children[1].children[0].children[0].textContent, "80 tok/s");
  assert.match(rows[0].children[1].textContent, /Smoothed \(EWMA\)/);
  assert.match(rows[0].children[1].textContent, /Latest: 100 tok\/s · 10 samples/);
  assert.match(rows[0].children[1].textContent, /Updated:/);
  assert.equal(rows[0].children[2].children[0].children[0].textContent, "25 tok/s");
  assert.equal(rows[0].children[3].children[0].children[0].textContent, "800 ms");
  assert.match(rows[0].children[3].textContent, /Slow reported loads \(≥1 s\): 2/);
  assert.equal(rows[0].children[4].children[0].children[0].textContent, "1,200 ms");
  assert.match(rows[0].children[5].textContent, /7 succeeded \/ 2 failed/);
  assert.match(rows[0].children[5].textContent, /Reported input tokens: 1000Reported output tokens: 500/);
  assert.notEqual(rows[0].children[6].textContent, "Not recorded");
  assert.match(app.element("performance-message").textContent, /does not generate traffic to models/);
  assert.ok(app.allRequests.every(request => request.options.method === "GET"));
  assert.equal(app.allRequests.some(request => request.url === "/router/metrics"), false, "Metrics arrive in the existing status snapshot, not another fetch");
  const oldRequests = app.allRequests.length;
  const oldHosts = app.hostRequests.length;
  app.tick();
  performance.deployments[0].metrics.input_tokens_per_second = performanceObservation(90, 85, 11);
  await reply(app.requests.at(-1), 200, {...snapshot(), performance});
  assert.equal(app.allRequests.length, oldRequests + 1);
  assert.equal(app.hostRequests.length, oldHosts);
  assert.match(app.element("performance-body").children[0].children[1].textContent, /^85 tok\/s/);
}

async function performanceUnknownZeroAndMissingTimings() {
  const performance = performanceSnapshot();
  const row = performance.deployments[0];
  row.metrics.input_tokens_per_second = performanceObservation(0, 0, 1);
  row.metrics.output_tokens_per_second = null;
  row.metrics.load_duration_ms = null;
  row.slow_load_count = 0;
  row.successes = 0;
  row.failures = 0;
  row.last_seen_at = null;
  const app = await unlockedPerformance(performance);
  let cells = app.element("performance-body").children[0].children;
  assert.match(cells[1].textContent, /^0 tok\/s/);
  assert.match(cells[1].textContent, /Latest: 0 tok\/s · 1 sample/);
  assert.equal(cells[2].textContent, "Not reported");
  assert.match(cells[3].textContent, /^Not reported/);
  assert.match(cells[3].textContent, /Slow reported loads \(≥1 s\): Not reported/);
  assert.match(cells[4].textContent, /^1,200 ms/);
  assert.match(cells[5].textContent, /0 succeeded \/ 0 failed/);
  assert.equal(cells[6].textContent, "Not recorded");
  row.metrics.load_duration_ms = performanceObservation(0, 0, 1);
  row.metrics.output_tokens_per_second = performanceObservation(0.001, 0.002, 1);
  app.tick();
  await reply(app.requests.at(-1), 200, {...snapshot(), performance});
  cells = app.element("performance-body").children[0].children;
  assert.match(cells[2].textContent, /^<0\.01 tok\/s/, "Small positive rates should not be rounded to measured zero");
  assert.match(cells[3].textContent, /^0 ms/);
  assert.match(cells[3].textContent, /Slow reported loads \(≥1 s\): 0/);
  for (const invalid of [undefined, {}, performanceObservation(-1), performanceObservation(Infinity), {...performanceObservation(1), ewma: "3"}, performanceObservation(1, 1, 0)]) {
    row.metrics.input_tokens_per_second = invalid;
    app.tick();
    await reply(app.requests.at(-1), 200, {...snapshot(), performance});
    assert.equal(app.element("performance-body").children[0].children[1].textContent, "Not reported");
  }
}

async function performanceEmptyOlderAndUnavailableStorage() {
  const older = await unlocked();
  assert.match(older.element("performance-message").textContent, /does not include performance metrics/);
  assert.match(older.element("performance-body").textContent, /No performance data received/);
  const empty = await unlockedPerformance({...performanceSnapshot(), deployments: [], updated_at: null});
  assert.match(empty.element("performance-body").textContent, /No routed requests yet/);
  assert.match(empty.element("performance-message").textContent, /Not yet recorded/);
  const app = await unlockedPerformance();
  for (const performance of [
    {...performanceSnapshot(), available: false, error: "private-file-path secret-key"},
    {...performanceSnapshot(), deployments: null},
    {...performanceSnapshot(), deployments: [null]},
    {...performanceSnapshot(), deployments: [{}]},
  ]) {
    app.tick();
    await reply(app.requests.at(-1), 200, {...snapshot(), performance});
    assert.match(app.element("performance-message").textContent, /history is unavailable/);
    assert.doesNotMatch(app.element("performance-message").textContent, /private-file-path|secret-key/);
    assert.doesNotMatch(app.element("performance-body").textContent, /qwen3:8b/);
    assert.equal(app.element("details").hidden, false, "Performance failure must not hide otherwise-valid router status");
  }
  app.tick();
  await reply(app.requests.at(-1), 200, {...snapshot(), performance: {...performanceSnapshot(), error: "private-internal-error"}});
  assert.match(app.element("performance-message").textContent, /storage reported a problem/);
  assert.doesNotMatch(app.element("performance-message").textContent, /private-internal-error/);
  assert.match(app.element("performance-body").textContent, /qwen3:8b/, "Available cached data may remain visible with an incomplete-storage warning");
}

async function performanceEscapingAndPrivateStateClearing() {
  const performance = performanceSnapshot();
  const item = performance.deployments[0];
  item.model = '<img src=x onerror="alert(1)">';
  item.machine = '<script>alert("machine")</script>';
  item.endpoint = '<script>alert("endpoint")</script>';
  item.id = '<script>alert("id")</script>';
  item.adapter = '<script>alert("adapter")</script>';
  item.address = "http://user:secret@backend:11434";
  const app = await unlockedPerformance(performance);
  const identity = app.element("performance-body").children[0].children[0].children[0];
  assert.equal(identity.children[0]._text, item.model);
  assert.equal(identity.children[0].children.length, 0);
  assert.ok(identity.children[1]._text.includes(item.machine));
  assert.ok(identity.children[1]._text.includes(item.endpoint));
  assert.equal(identity.textContent.includes(item.id), false);
  assert.ok(identity.children[2]._text.includes(item.adapter));
  assert.equal(identity.children[3].textContent, "API address unavailable", "Credential-bearing API addresses must not be exposed");
  assert.equal(identity.children[3].tagName, "code", "Performance addresses are inert text, never links");
  assert.doesNotMatch(app.element("performance-body").textContent, /user:secret/);
  for (const action of ["lock", "key", "pagehide", "refresh-fail", "refresh-auth"]) {
    const current = await unlockedPerformance();
    current.tick();
    const pending = current.requests.at(-1);
    if (action === "lock") current.lock();
    if (action === "key") current.enterKey("new-key");
    if (action === "pagehide") current.event("pagehide");
    if (action === "refresh-fail") { pending.reject(new Error("offline")); await flush(); }
    if (action === "refresh-auth") await reply(pending, 401, {});
    assert.equal(current.element("performance-body").textContent, "", `${action} clears stored model performance from the DOM`);
    assert.equal(current.element("performance-message").textContent, "");
    await reply(pending, 200, {...snapshot(), performance: performanceSnapshot()});
    assert.equal(current.element("performance-body").textContent, "", `${action}: late response must not restore metrics`);
  }
}

async function performanceLateBodyAndNewSessionGuards() {
  const app = await unlockedPerformance();
  app.tick();
  const old = app.requests.at(-1);
  app.enterKey("new-key");
  const newer = performanceSnapshot();
  newer.deployments[0].model = "new-session-model";
  await reply(app.requests.at(-1), 200, {...snapshot(), performance: newer});
  await reply(old, 401, {});
  assert.match(app.element("performance-body").textContent, /new-session-model/);
  assert.equal(app.element("details").hidden, false);
  app.tick();
  let resolveBody;
  app.requests.at(-1).resolve({status: 200, ok: true, json: () => new Promise(resolve => { resolveBody = resolve; })});
  await flush();
  app.lock();
  resolveBody({...snapshot(), performance: performanceSnapshot()});
  await flush();
  assert.equal(app.element("performance-body").textContent, "", "Delayed JSON parsing must not expose old authenticated metrics");
}

async function savedHostRoutingStatesAndSafeDetails() {
  const labels = {active: "Routing enabled", offline: "Backend offline", empty: "No models enrolled", pending: "Enrollment pending", error: "Enrollment needs attention", managed: "Explicitly configured"};
  for (const [status, label] of Object.entries(labels)) {
    const host = savedHost("host-one", true);
    host.routing = {status, model_count: status === "empty" ? 0 : 2, detail: '<img src=x onerror="alert(1)">'};
    const app = await unlockedHosts([host]);
    const routing = descendants(app.element("hosts-body"), "host-routing")[0];
    assert.equal(routing.children[0].textContent, label);
    assert.equal(routing.children[1].textContent, `Known routing models: ${host.routing.model_count}`);
    assert.equal(routing.children[2]._text, host.routing.detail);
    assert.equal(routing.children[2].children.length, 0, "Routing details must remain escaped text");
    if (status !== "active") assert.doesNotMatch(routing.children[0].className, /badge-ready/, "Only active enrollment may use a ready badge");
  }
  const legacy = savedHost("legacy-host", true);
  delete legacy.routing;
  const app = await unlockedHosts([legacy]);
  assert.match(app.element("hosts-body").textContent, /Routing status unavailable/);
  assert.doesNotMatch(app.element("hosts-body").textContent, /Routing enabled/);
  for (const routing of [null, {status: "constructor", model_count: 1, detail: "no"}, {status: "active", model_count: -1, detail: "no"}, {status: "active", model_count: 1, detail: {unsafe: true}}]) {
    app.reloadHosts();
    await reply(app.hostRequests.at(-1), 200, {hosts: [{...savedHost(), routing}], limit: 16});
    assert.match(app.element("hosts-message").textContent, /response was invalid/);
    assert.doesNotMatch(app.element("hosts-body").textContent, /Routing enabled/);
  }
}

async function savedHostSaveEnrollmentAndRouterRefresh() {
  for (const status of ["active", "pending", "offline", "empty", "error", "managed"]) {
    const app = await unlockedHosts();
    app.tick();
    const stale = app.requests.at(-1);
    app.saveHost("192.168.194.0");
    const saved = savedHost("host-one", status !== "pending");
    saved.routing = {status, model_count: status === "empty" ? 0 : 1, detail: "Saved routing metadata."};
    const previousHostRequests = app.hostRequests.length;
    await reply(app.hostRequests.at(-1), 201, {host: saved});
    assert.equal(app.hostRequests.length, previousHostRequests, "Single save response replaces the former save-plus-check sequence");
    assert.equal(app.hostRequests.some(request => request.url === "/status/hosts/check"), false);
    assert.equal(stale.options.signal.aborted, true, "Enrollment must invalidate a pre-save router snapshot");
    assert.equal(app.requests.at(-1).url, "/status/data");
    assert.equal(app.requests.at(-1).options.headers.Authorization, "Bearer secret-key");
    assert.equal(app.element("host-address").value, "");
    assert.match(app.element("hosts-message").textContent, /Address saved/);
    if (status === "pending") assert.match(app.element("hosts-message").textContent, /Enrollment is pending; an automatic check is queued/);
    if (status === "error") assert.equal(app.element("hosts-message").className, "muted result-fail");
    const fresh = snapshot();
    fresh.counts.endpoints = 4;
    fresh.models[0].name = "newly-enrolled-model";
    await reply(app.requests.at(-1), 200, fresh);
    await reply(stale, 200, snapshot());
    assert.equal(app.element("count-endpoints").textContent, "4");
    assert.match(app.element("models-body").textContent, /newly-enrolled-model/);
    assert.equal(app.hostRequests.length, previousHostRequests, "Router refresh must not duplicate save probes");
  }
}

async function savedHostSnapshotPollingIsAuthenticatedAndReadOnly() {
  const locked = harness();
  locked.tick(30000);
  assert.equal(locked.hostRequests.length, 0);
  const noKey = await unlocked(false);
  noKey.tick(30000);
  assert.equal(noKey.hostRequests.length, 0);
  const app = await unlockedHosts([savedHost("host-one", true)]);
  const oldHostCount = app.hostRequests.length;
  const oldRouterCount = app.requests.length;
  app.tick(30000);
  assert.equal(app.hostRequests.length, oldHostCount + 1);
  const refresh = app.hostRequests.at(-1);
  assert.equal(refresh.url, "/status/hosts");
  assert.equal(refresh.options.method, "GET");
  assert.equal(refresh.options.body, undefined);
  assert.equal(refresh.options.headers.Authorization, "Bearer secret-key");
  app.tick(30000);
  assert.equal(app.hostRequests.length, oldHostCount + 1, "Saved-list polling cannot overlap itself");
  const offline = savedHost("host-one", true);
  offline.routing = {status: "offline", model_count: 1, detail: "The most recent automatic probe failed."};
  await reply(refresh, 200, {hosts: [offline], limit: 16});
  assert.match(app.element("hosts-body").textContent, /Backend offline/);
  assert.equal(app.requests.length, oldRouterCount, "Reading saved routing snapshots must not trigger another detailed refresh");
  assert.ok(app.allRequests.every(request => request.options.method === "GET"), "Timer callbacks read cached results; they never start backend scans");
  app.element("host-address").value = "partial-address";
  app.tick(30000);
  assert.equal(app.hostRequests.length, oldHostCount + 1, "Background reload must not interrupt address entry");
  app.element("host-address").value = "";
  app.tick(30000);
  const pending = app.hostRequests.at(-1);
  app.lock();
  assert.equal(pending.options.signal.aborted, true);
  await reply(pending, 200, {hosts: [savedHost("host-one", true)], limit: 16});
  assert.equal(app.element("hosts-body").textContent, "");
  const lockedCount = app.hostRequests.length;
  app.tick(30000);
  assert.equal(app.hostRequests.length, lockedCount);
  app.event("pagehide");
  assert.equal(app.intervalCount(), 0);
  app.event("pageshow", {persisted: true});
  assert.equal(app.intervalCount(), 2);
  app.tick(30000);
  assert.equal(app.hostRequests.length, lockedCount, "BFCache restores a locked session without reading saved hosts");
}

async function savedHostEnrollmentMutationRaceGuards() {
  for (const action of ["save", "remove"]) {
    const app = await unlockedHosts([savedHost("host-one", true)]);
    if (action === "save") app.saveHost("second-backend");
    else app.hostAction(0, 1);
    const mutation = app.hostRequests.at(-1);
    app.lock();
    const publicRequest = app.requests.at(-1);
    await reply(mutation, 200, action === "save" ? {host: savedHost("host-two", true)} : {removed: true});
    assert.equal(app.requests.at(-1), publicRequest, "Late enrollment/removal must not start a new authenticated refresh");
    assert.equal(app.element("hosts-body").textContent, "");
    assert.equal(app.element("details").hidden, true);
  }
  const deferred = await unlockedHosts();
  deferred.saveHost("new-backend");
  let resolveBody;
  deferred.hostRequests.at(-1).resolve({status: 201, ok: true, json: () => new Promise(resolve => { resolveBody = resolve; })});
  await flush();
  deferred.enterKey("new-key");
  const newKeyRequest = deferred.requests.at(-1);
  resolveBody({host: savedHost("host-one", true)});
  await flush();
  assert.equal(deferred.requests.at(-1), newKeyRequest);
  assert.equal(deferred.element("hosts-body").textContent, "", "Late save JSON may not expose enrollment under a different key");
}

async function unlockedUpdates(status = updateSnapshot()) {
  const app = harness(true, true, {autoLoadUpdates: false});
  app.enterKey("secret-key");
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.updateRequests.length, 1);
  assert.equal(app.updateRequests[0].options.method, "GET");
  await reply(app.updateRequests[0], 200, status);
  return app;
}

async function updatesRequireExplicitAuthenticatedClick() {
  assert.match(markup.match(/<header[\s\S]*?<\/header>/)[0], /id="update-button"/);
  assert.match(markup, /automatically installs a newer commit/);
  assert.match(markup, /restarts the router and can interrupt requests/);
  const progressTag = markup.match(/<progress\b[^>]*id="update-progress"[^>]*>/)[0];
  assert.doesNotMatch(progressTag, /\bvalue=/, "Progress must be indeterminate, never simulated percent");
  const locked = harness();
  locked.update();
  locked.readUpdate();
  assert.equal(locked.updateRequests.length, 0);
  const noKey = await unlocked(false);
  noKey.update();
  noKey.readUpdate();
  assert.equal(noKey.updateRequests.length, 0);
  assert.equal(noKey.element("update-button").disabled, true);
  assert.match(noKey.element("update-message").textContent, /LLM_ROUTER_GATEWAY_API_KEY/);
  const app = await unlockedUpdates();
  assert.equal(app.element("update-button").disabled, false);
  app.tick();
  await reply(app.requests.at(-1), 200, snapshot());
  assert.equal(app.updateRequests.length, 1, "Routine dashboard refresh must not start or recheck remote updates");
  app.update();
  const post = app.updateRequests.at(-1);
  assert.equal(post.options.method, "POST");
  assert.equal(post.options.body, undefined);
  assert.equal(post.options.headers.Authorization, "Bearer secret-key");
  assert.equal(post.options.headers["X-LLM-Router-Update"], "1");
  app.update();
  app.readUpdate();
  assert.equal(app.updateRequests.length, 2, "Duplicate clicks must not duplicate requests");
  assert.equal(app.element("update-button").disabled, true);
  await reply(post, 202, updateSnapshot({busy: true, state: "queued", stage: "queued", run_id: null}));
  assert.match(app.element("update-message").textContent, /Waiting for a new update job/);
  app.expire(2000);
  assert.equal(app.updateRequests.at(-1).options.method, "GET");
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage: "checking", run_id: "run-one"}));
  assert.equal(app.element("update-stage").textContent, "Checking official main");
  for (const [stage, label] of [["downloading", "Downloading update"], ["validating", "Validating installation"], ["restarting", "Restarting router"]]) {
    app.expire(2000);
    await reply(app.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage, run_id: "run-one"}));
    assert.equal(app.element("update-stage").textContent, label);
    assert.equal(app.element("update-progress").hidden, false);
    assert.equal(app.element("update-progress").attributes.value, undefined);
    assert.doesNotMatch(app.element("update-message").textContent, /\d+%/);
  }
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({state: "succeeded", stage: "complete", run_id: "run-one", current_version: "0.3.3", updated_at: "2026-09-19T12:00:00Z"}));
  assert.equal(app.element("update-progress").hidden, true);
  assert.match(app.element("update-message").textContent, /Update completed successfully/);
  assert.match(app.element("update-observed").textContent, /0\.3\.3/);
  assert.equal(app.requests.at(-1).url, "/status/data", "Confirmed completion refreshes installed version and router state");
  const count = app.updateRequests.length;
  app.expire(2000);
  assert.equal(app.updateRequests.length, count, "Terminal jobs stop polling");
  assert.equal(app.updateRequests.filter(request => request.options.method === "POST").length, 1);
}

async function updatesReconnectWithoutRepostingOrOldSuccess() {
  const old = updateSnapshot({state: "succeeded", stage: "complete", run_id: "old-job"});
  const app = await unlockedUpdates(old);
  assert.match(app.element("update-message").textContent, /last saved update job/);
  app.update();
  app.updateRequests.at(-1).reject(new Error("restart before response"));
  await flush();
  assert.match(app.element("update-message").textContent, /Waiting for the router to restart or reconnect/);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, old);
  assert.match(app.element("update-message").textContent, /previous saved result does not confirm/);
  assert.equal(app.element("update-progress").hidden, false);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage: "restarting", run_id: "new-job"}));
  app.tick();
  app.requests.at(-1).reject(new Error("router disconnected"));
  await flush();
  assert.equal(app.element("details").hidden, true);
  assert.equal(app.element("update-details").hidden, false, "Expected restart does not discard the update monitor");
  app.expire(2000);
  app.updateRequests.at(-1).reject(new Error("still restarting"));
  await flush();
  assert.match(app.element("update-message").textContent, /outcome is not confirmed/);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({available: false, state: "unavailable", run_id: null}));
  assert.match(app.element("update-message").textContent, /temporarily unavailable/);
  assert.equal(app.element("update-progress").hidden, false, "Temporary installation identity unavailability is not terminal during restart");
  assert.equal(app.element("update-button").disabled, true);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({state: "succeeded", stage: "complete", run_id: "wrong-job"}));
  assert.match(app.element("update-message").textContent, /not the requested update job/);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({state: "current", stage: "complete", run_id: "new-job"}));
  assert.match(app.element("update-message").textContent, /Already up to date/);
  assert.equal(app.updateRequests.filter(request => request.options.method === "POST").length, 1);
}

async function updatesBusyFailuresAndBoundedWaiting() {
  const app = await unlockedUpdates();
  app.update();
  await reply(app.updateRequests.at(-1), 409, {error: "busy", message: "internal-secret"});
  assert.doesNotMatch(app.element("update-message").textContent, /internal-secret/);
  app.expire(2000);
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage: "validating", run_id: "other-actor-job"}));
  assert.equal(app.element("update-stage").textContent, "Validating installation");
  app.advanceTime(50 * 60 * 1000 + 1);
  const before = app.updateRequests.length;
  app.expire(2000);
  assert.equal(app.updateRequests.length, before);
  assert.match(app.element("update-message").textContent, /Stopped waiting after 50 minutes/);
  assert.equal(app.element("update-progress").hidden, true);
  assert.equal(app.element("update-button").disabled, true);
  app.readUpdate();
  assert.equal(app.updateRequests.at(-1).options.method, "GET");
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({state: "failed", stage: "failed", run_id: "other-actor-job", message: "secret path"}));
  assert.match(app.element("update-message").textContent, /update job failed/);
  assert.doesNotMatch(app.element("update-message").textContent, /secret path/);
  assert.equal(app.updateRequests.filter(request => request.options.method === "POST").length, 1);
  for (const state of ["failed", "interrupted"]) {
    const terminal = await unlockedUpdates();
    terminal.update();
    await reply(terminal.updateRequests.at(-1), 202, updateSnapshot({busy: true, state: "running", stage: "checking", run_id: state}));
    terminal.expire(2000);
    await reply(terminal.updateRequests.at(-1), 200, updateSnapshot({state, stage: "failed", run_id: state}));
    assert.equal(terminal.element("update-progress").hidden, true);
    assert.equal(terminal.element("update-message").className, "muted result-fail");
  }
  const unsupported = await unlockedUpdates(updateSnapshot({available: false, state: "unavailable", message: "Install the managed updater unit to enable dashboard updates."}));
  unsupported.update();
  assert.equal(unsupported.updateRequests.length, 1);
  assert.match(unsupported.element("update-message").textContent, /unavailable/);
  assert.match(unsupported.element("update-message").textContent, /Install the managed updater unit/);
  const rejected = await unlockedUpdates();
  rejected.update();
  await reply(rejected.updateRequests.at(-1), 503, {error: "unsafe-internal-detail"});
  assert.match(rejected.element("update-message").textContent, /outcome is not confirmed/);
  assert.doesNotMatch(rejected.element("update-message").textContent, /unsafe-internal-detail/);
  const rejectedCount = rejected.updateRequests.length;
  rejected.expire(2000);
  assert.equal(rejected.updateRequests.length, rejectedCount + 1, "503 may follow an already-queued job and must recover using local GET");
  assert.equal(rejected.updateRequests.at(-1).options.method, "GET");
  await reply(rejected.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage: "checking", run_id: "queued-before-timeout"}));
  assert.equal(rejected.element("update-stage").textContent, "Checking official main");
  assert.equal(rejected.updateRequests.filter(request => request.options.method === "POST").length, 1);
}

async function updateAuthenticationAndRacePrivacy() {
  for (const action of ["lock", "key", "pagehide", "auth-reject"]) {
    const app = await unlockedUpdates();
    app.update();
    const pending = app.updateRequests.at(-1);
    if (action === "lock") app.lock();
    if (action === "key") app.enterKey("new-key");
    if (action === "pagehide") app.event("pagehide");
    if (action === "auth-reject") {
      app.tick();
      await reply(app.requests.at(-1), 401, {});
    }
    assert.equal(pending.options.signal.aborted, true);
    await reply(pending, 202, updateSnapshot({busy: true, state: "running", stage: "downloading", run_id: "old-key-job"}));
    assert.equal(app.element("update-details").hidden, true);
    assert.equal(app.element("update-observed").textContent, "");
    assert.equal(app.element("update-stage").textContent, "");
    const count = app.updateRequests.length;
    app.expire(2000);
    assert.equal(app.updateRequests.length, count);
  }
  for (const code of [401, 403]) {
    const app = await unlockedUpdates();
    app.update();
    const post = app.updateRequests.at(-1);
    app.tick();
    const oldStatus = app.requests.at(-1);
    await reply(post, code, {});
    assert.equal(oldStatus.options.signal.aborted, true);
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("update-details").hidden, true);
    assert.equal(app.element("key-form").hidden, false);
    await reply(oldStatus, 200, snapshot());
    assert.equal(app.element("details").hidden, true);
  }
  const late = await unlockedUpdates();
  late.update();
  let resolveBody;
  late.updateRequests.at(-1).resolve({status: 202, ok: true, json: () => new Promise(resolve => { resolveBody = resolve; })});
  await flush();
  late.lock();
  resolveBody(updateSnapshot({busy: true, state: "running", stage: "checking", run_id: "private-job"}));
  await flush();
  assert.equal(late.element("update-details").hidden, true);
}

async function updatesVisibilityAndReloadAreReadOnly() {
  const app = await unlockedUpdates();
  app.update();
  const post = app.updateRequests.at(-1);
  app.visibility(true);
  assert.equal(post.options.signal.aborted, true);
  assert.equal(app.element("update-details").hidden, true);
  const count = app.updateRequests.length;
  app.expire(2000);
  assert.equal(app.updateRequests.length, count);
  app.visibility(false);
  assert.equal(app.updateRequests.at(-1).options.method, "GET");
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({busy: true, state: "running", stage: "downloading", run_id: "surviving-job"}));
  await reply(post, 202, updateSnapshot({busy: true, state: "queued", stage: "queued", run_id: "stale-job"}));
  assert.equal(app.element("update-stage").textContent, "Downloading update");
  app.event("pagehide");
  app.event("pageshow", {persisted: true});
  app.expire(2000);
  assert.equal(app.element("update-details").hidden, true);
  assert.equal(app.updateRequests.filter(request => request.options.method === "POST").length, 1);
  const restored = await unlockedUpdates(updateSnapshot({busy: true, state: "running", stage: "validating", run_id: "surviving-job"}));
  assert.equal(restored.element("update-progress").hidden, false);
  restored.expire(2000);
  assert.ok(restored.updateRequests.every(request => request.options.method === "GET"), "Reloading an active job may only resume local polling");
  restored.visibility(true);
  restored.advanceTime(50 * 60 * 1000 + 1);
  restored.visibility(false);
  assert.match(restored.element("update-message").textContent, /Stopped waiting after 50 minutes/);
  assert.equal(restored.element("update-details").hidden, false, "After a long-hidden timeout, the local status recovery button must be visible");
  assert.equal(restored.element("update-refresh-button").disabled, false);
}

async function updateStatusValidationAndSafeRendering() {
  const app = await unlockedUpdates();
  for (const data of [{}, updateSnapshot({state: "constructor"}), updateSnapshot({stage: "imaginary"}), updateSnapshot({run_id: {bad: true}}), updateSnapshot({current_version: "x".repeat(65)})]) {
    app.readUpdate();
    await reply(app.updateRequests.at(-1), 200, data);
    assert.match(app.element("update-message").textContent, /could not be confirmed/);
    assert.equal(app.element("update-button").disabled, true);
  }
  app.readUpdate();
  await reply(app.updateRequests.at(-1), 200, updateSnapshot({state: "current", stage: "complete", run_id: "saved", current_version: '<img src=x onerror="alert(1)">', message: "private-key-value"}));
  assert.ok(app.element("update-observed")._text.includes('<img src=x onerror="alert(1)">'));
  assert.equal(app.element("update-observed").children.length, 0);
  assert.doesNotMatch(app.element("update-message").textContent, /private-key-value/);
  assert.match(app.element("update-message").textContent, /last recorded check/);
  app.update();
  const timed = app.updateRequests.at(-1);
  app.expire(30000);
  assert.equal(timed.options.signal.aborted, true);
  await reply(timed, 202, updateSnapshot({state: "succeeded", stage: "complete", run_id: "too-late"}));
  assert.match(app.element("update-message").textContent, /outcome is not confirmed/);
  assert.equal(app.updateRequests.filter(request => request.options.method === "POST").length, 1);
  const unsupported = await unlockedUpdates(updateSnapshot({available: false, state: "unavailable", message: '<img src=x onerror="alert(1)"> Updater unit missing.'}));
  assert.ok(unsupported.element("update-message")._text.includes('<img src=x onerror="alert(1)"> Updater unit missing.'));
  assert.equal(unsupported.element("update-message").children.length, 0, "Unsupported-install reason must render as inert text");
  for (const message of ["x".repeat(1025), {bad: true}, null]) {
    unsupported.readUpdate();
    await reply(unsupported.updateRequests.at(-1), 200, updateSnapshot({available: false, state: "unavailable", message}));
    assert.match(unsupported.element("update-message").textContent, /Use the supported installer or update service/);
    assert.ok(unsupported.element("update-message").textContent.length < 200);
  }
}

(async () => {
  for (const test of [publicReadiness, topbarVersionTracksCurrentSnapshot, nonoverlapAndNetworkFailure, authenticationAndSafeRendering, lockLateResponsesAndRejectedKeys, keySwitchRace, timeoutAndPageRestore, safeRouterAndBackendLinks, selfTestIsExplicitAndIndependent, selfTestFailuresAndSafeRendering, selfTestPrivacyAndRaceGuards, selfTestAuthenticationFailure, savedHostLifecycle, savedHostsRestoreAndRequireAuthentication, savedHostFailuresAndSafeRendering, savedHostPrivacyAndRaceGuards, savedHostAuthRejectionAndTimeout, savedHostModelCatalogs, savedHostCatalogEmptyErrorTruncatedAndLegacy, savedHostCatalogEscapingAndValidation, savedHostCatalogStaleAndPrivate, publicCachedSummary, urlKeyBootstrapAndImmediateScrub, urlKeyInvalidAmbiguousAndCleanupFailure, urlKeyAuthenticationFailureAndPageRestore, liveFragmentKeyUnlockAndNavigation, liveFragmentKeyInvalidAndCleanupFailure, liveFragmentKeyCancelsOldSession, performanceIsPassiveAndPerDeployment, performanceUnknownZeroAndMissingTimings, performanceEmptyOlderAndUnavailableStorage, performanceEscapingAndPrivateStateClearing, performanceLateBodyAndNewSessionGuards, savedHostRoutingStatesAndSafeDetails, savedHostSaveEnrollmentAndRouterRefresh, savedHostSnapshotPollingIsAuthenticatedAndReadOnly, savedHostEnrollmentMutationRaceGuards]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
  for (const test of [updatesRequireExplicitAuthenticatedClick, updatesReconnectWithoutRepostingOrOldSuccess, updatesBusyFailuresAndBoundedWaiting, updateAuthenticationAndRacePrivacy, updatesVisibilityAndReloadAreReadOnly, updateStatusValidationAndSafeRendering]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
