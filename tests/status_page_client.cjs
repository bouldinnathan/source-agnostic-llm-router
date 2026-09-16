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

function harness(authRequired = true, autoLoadHosts = true) {
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
  const allRequests = [];
  const intervals = new Map();
  const timeouts = new Map();
  const windowEvents = {};
  let timerId = 0;
  const context = {
    document: {
      body: {dataset: {authRequired: String(authRequired)}},
      getElementById: element,
      createElement: tag => new Element(tag),
      createDocumentFragment: () => new Element("#fragment"),
    },
    window: {location: {origin: "http://router.example:8088"}, addEventListener: (name, callback) => { windowEvents[name] = callback; }},
    Node: Element,
    URL,
    AbortController,
    setTimeout: callback => { const id = ++timerId; timeouts.set(id, callback); return id; },
    clearTimeout: id => timeouts.delete(id),
    setInterval: callback => { const id = ++timerId; intervals.set(id, callback); return id; },
    clearInterval: id => intervals.delete(id),
    fetch: (url, options) => new Promise((resolve, reject) => {
      const hostRequest = /^\/status\/hosts(?:\/[^/?#]+)?$/.test(url);
      assert.ok(["/healthz", "/status/data", "/status/self-test"].includes(url) || hostRequest, "Only same-origin status endpoints may be fetched");
      if (!hostRequest) assert.equal(options.method, url === "/status/self-test" ? "POST" : "GET");
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
      (hostRequest ? hostRequests : requests).push(pending);
      if (hostRequest && autoLoadHosts && options.method === "GET") resolve({status: 200, ok: true, json: async () => ({hosts: [], limit: 16})});
    }),
  };
  vm.runInNewContext(script, context);
  return {
    element, requests, hostRequests, allRequests, timeouts,
    tick: () => { for (const callback of Array.from(intervals.values())) callback(); },
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
    ready: true, status: "ready", version: "0.3.0", uptime_seconds: 65,
    checked_at: "2026-09-16T12:00:00Z", last_discovery: null,
    counts: {endpoints: 1, online: 1, models: 1, available_models: 1, aliases: 1},
    endpoints: [{name: "backend", machine: "laptop", address: "http://private-backend:1234", state: "online", model_count: 1, available_models: 1}],
    models: [{name: '<img src=x onerror="alert(1)">', deployment: "qwen-copy", machine: "laptop", state: "available", active_requests: 0, successes: 5, failures: 1}],
    aliases: [{name: "qwen-ha", kind: "ha", available: true, deployments: 1}],
  };
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
    await reply(app.requests[0], httpStatus, {status, version: "0.3.0"});
    assert.equal(app.element("health-panel").className, `health-panel tone-${tone}`);
    assert.equal(app.element("gateway-state").textContent, "Responding");
    assert.equal(app.element("model-readiness").textContent, readiness);
    assert.equal(app.element("version").textContent, "Version 0.3.0", "Version should remain visible while details are locked");
    assert.equal(app.element("details").hidden, true);
    assert.equal(app.element("refresh-button").disabled, false);
  }
  const app = harness();
  await reply(app.requests[0], 200, {status: "invalid"});
  assert.equal(app.element("health-panel").className, "health-panel tone-error");
}

async function nonoverlapAndNetworkFailure() {
  const app = harness();
  app.tick();
  app.element("refresh-button").events.click();
  assert.equal(app.requests.length, 1, "In-flight requests must not overlap");
  await reply(app.requests[0], 200, {status: "ready", version: "0.3.0"});
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
  await reply(app.requests[0], 503, {status: "unavailable", version: "0.3.0"});
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
  await reply(app.requests[0], 200, {status: "ready", version: "0.3.0"});
  assert.equal(app.element("health-panel").className, "health-panel tone-pending", "A cancelled old public request must not overwrite pending state");
  await reply(app.requests[3], 503, {status: "unavailable", version: "0.3.0"});
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
  assert.equal(locked.intervalCount(), 1);
  assert.equal(locked.requests[2].url, "/healthz");
  assert.equal(locked.requests[2].options.headers.Authorization, undefined);
}

async function safeRouterAndBackendLinks() {
  const expectedLinks = ["/healthz", "/readyz", "/status/data", "/router/status", "/api/version", "/api/tags", "/v1/models"];
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
  await reply(save, 200, {host: savedHost()});
  const check = app.hostRequests.at(-1);
  assert.equal(check.url, "/status/hosts/check");
  assert.deepEqual(JSON.parse(check.options.body), {id: "host-one"});
  assert.equal(app.element("host-address").value, "");
  const found = savedHost("host-one", true);
  found.checks.push({provider: "Ollama", base_url: "http://192.168.194.0:11434", status: "fail", detail: "Connection unavailable", http_status: null, elapsed_ms: 1500});
  await reply(check, 200, {hosts: [found]});
  assert.equal(app.element("host-save-button").disabled, false);
  assert.equal(app.element("hosts-check-button").disabled, false);
  assert.match(app.element("hosts-message").textContent, /Address saved.*1 backend API found/);
  assert.match(app.element("hosts-body").textContent, /LM Studio \/ OpenAI-compatibleFound/);
  assert.match(app.element("hosts-body").textContent, /OllamaNot confirmed/);
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
  app.hostAction(0, 0);
  assert.deepEqual(JSON.parse(app.hostRequests.at(-1).options.body), {id: "host-one"});
  await reply(app.hostRequests.at(-1), 200, {hosts: [found]});
  app.hostAction(0, 1);
  assert.equal(app.hostRequests.at(-1).url, "/status/hosts/host-one");
  assert.equal(app.hostRequests.at(-1).options.method, "DELETE");
  assert.equal(app.hostRequests.at(-1).options.body, undefined);
  await reply(app.hostRequests.at(-1), 200, {removed: true});
  assert.match(app.element("hosts-body").textContent, /No saved addresses/);
  assert.match(app.element("hosts-message").textContent, /Address removed/);
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
  assert.match(markup, /Saving an address does not add its models to routing/);
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
  const failedCheck = await unlockedHosts();
  failedCheck.saveHost("192.168.194.0");
  await reply(failedCheck.hostRequests.at(-1), 200, {host: savedHost()});
  await reply(failedCheck.hostRequests.at(-1), 429, {});
  assert.match(failedCheck.element("hosts-message").textContent, /Address saved, but its check did not complete/);
  assert.match(failedCheck.element("hosts-body").textContent, /192\.168\.194\.0/);
  assert.equal(failedCheck.element("hosts-check-button").disabled, false);

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
  assert.equal(check.children[0]._text, unsafe.checks[0].provider);
  assert.equal(check.children[2]._text, unsafe.checks[0].base_url);
  assert.ok(check.children[3]._text.startsWith(unsafe.checks[0].detail));
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

(async () => {
  for (const test of [publicReadiness, nonoverlapAndNetworkFailure, authenticationAndSafeRendering, lockLateResponsesAndRejectedKeys, keySwitchRace, timeoutAndPageRestore, safeRouterAndBackendLinks, selfTestIsExplicitAndIndependent, selfTestFailuresAndSafeRendering, selfTestPrivacyAndRaceGuards, selfTestAuthenticationFailure, savedHostLifecycle, savedHostsRestoreAndRequireAuthentication, savedHostFailuresAndSafeRendering, savedHostPrivacyAndRaceGuards, savedHostAuthRejectionAndTimeout]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
