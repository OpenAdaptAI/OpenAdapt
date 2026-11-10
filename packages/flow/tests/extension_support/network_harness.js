// Run the optional extension's network-save capture in Node.
// Part of OpenAdapt#1872.
//
// Usage:
//   node network_harness.js EXTENSION_DIR STEPS_JSON [--no-redactor]
//
// Loads background.js in a fresh context with a stubbed chrome API that
// includes chrome.webRequest. importScripts loads files from EXTENSION_DIR,
// so the real network_redact.js wiring is exercised. With --no-redactor,
// importScripts fails, as when the redactor file is missing.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const [extDir, stepsJson, flag] = process.argv.slice(2);
const steps = JSON.parse(stepsJson);
const noRedactor = flag === '--no-redactor';

function event() {
  return {
    _listeners: [],
    addListener(fn, filter, extra) {
      this._listeners.push({ fn, filter, extra: extra || [] });
    },
    _fire(details) {
      for (const l of this._listeners) l.fn(details);
    },
  };
}

const posted = [];
const chrome = {
  runtime: {
    sendMessage() {},
    onMessage: { addListener() {} },
    connectNative() {
      throw new Error('native host not available in harness');
    },
  },
  action: { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {} },
  tabs: {
    query(_q, cb) {
      cb([]);
    },
    sendMessage() {},
  },
  webRequest: {
    onBeforeRequest: event(),
    onBeforeSendHeaders: event(),
    onCompleted: event(),
    onErrorOccurred: event(),
  },
};

const context = {
  console,
  chrome,
  Date,
  URL,
  URLSearchParams,
  atob,
  TextEncoder,
  TextDecoder,
  Uint8Array,
  ArrayBuffer,
  JSON,
  globalThis: {},
  importScripts(...names) {
    if (noRedactor) throw new Error('importScripts: not found');
    for (const name of names) {
      const file = path.join(extDir, name);
      vm.runInContext(fs.readFileSync(file, 'utf8'), context, { filename: file });
    }
  },
};
context.globalThis = context;
vm.createContext(context);
const bg = path.join(extDir, 'background.js');
vm.runInContext(fs.readFileSync(bg, 'utf8'), context, { filename: bg });
const api = context.__openadaptExtensionBackground;

let nextId = 1;
const results = [];
for (const step of steps) {
  if (step.mode === 'record') api.setRecording(true);
  else if (step.mode === 'idle') api.setRecording(false);
  else if (step.request) {
    const r = step.request;
    const requestId = String(nextId++);
    let requestBody = r.requestBody || null;
    if (r.body_text != null) {
      const bytes = new TextEncoder().encode(r.body_text);
      requestBody = { raw: [{ bytes: bytes.buffer }] };
    }
    const base = {
      requestId,
      method: r.method || 'POST',
      url: r.url,
      type: r.type || 'xmlhttprequest',
      tabId: r.tabId === undefined ? 7 : r.tabId,
      frameId: r.frameId === undefined ? 0 : r.frameId,
      timeStamp: 1000,
    };
    chrome.webRequest.onBeforeRequest._fire(Object.assign({}, base, { requestBody }));
    chrome.webRequest.onBeforeSendHeaders._fire(
      Object.assign({}, base, { requestHeaders: r.requestHeaders || [] })
    );
    if (r.error) {
      chrome.webRequest.onErrorOccurred._fire(Object.assign({}, base, { error: r.error }));
    } else {
      chrome.webRequest.onCompleted._fire(
        Object.assign({}, base, {
          statusCode: r.statusCode || 200,
          responseHeaders: r.responseHeaders || [],
        })
      );
    }
  } else if (step.state) {
    const extras = {};
    for (const [name, ev] of Object.entries(chrome.webRequest)) {
      extras[name] = ev._listeners.map((l) => ({ filter: l.filter, extra: l.extra }));
    }
    results.push({
      recording: api.recording,
      queued: api.queued,
      networkListening: api.networkListening,
      pendingSaves: api.pendingSaves,
      redactorLoaded: Boolean(context.OpenAdaptNetworkRedact),
      listeners: extras,
    });
    continue;
  } else if (step.redact) {
    const d = Object.assign({}, step.redact);
    if (d.body_text != null) {
      d.requestBody = { raw: [{ bytes: new TextEncoder().encode(d.body_text).buffer }] };
      delete d.body_text;
    }
    results.push({ record: context.OpenAdaptNetworkRedact.redactNetworkSave(d) });
    continue;
  }
  results.push({ queued: api.queued.length });
}

process.stdout.write(JSON.stringify(results));
