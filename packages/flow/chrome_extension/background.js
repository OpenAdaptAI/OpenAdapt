// OpenAdapt optional extension evidence (service worker).
// Relays content-script evidence to the local native host only while recording.
// Shows a visible REC badge. No remote endpoints.
'use strict';

// Network-save redaction (Part of #1872). Without it, no network evidence is
// captured at all: the listeners below fail closed.
if (typeof importScripts === 'function') {
  try {
    importScripts('network_redact.js');
  } catch (e) {}
}

const NATIVE_HOST = 'com.openadapt.extension';
const SCHEMA = 'openadapt.extension-evidence/1';

let recording = false;
let port = null;
let queued = [];

function setBadge(active) {
  const text = active ? 'REC' : '';
  const color = active ? '#b00020' : '#00000000';
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setTitle({
      title: active
        ? 'OpenAdapt extension evidence (recording)'
        : 'OpenAdapt extension evidence (idle)',
    });
  } catch (e) {}
}

function ensurePort() {
  if (port) return port;
  try {
    port = chrome.runtime.connectNative(NATIVE_HOST);
    port.onDisconnect.addListener(() => {
      port = null;
    });
  } catch (e) {
    port = null;
  }
  return port;
}

function deliver(evidence) {
  if (!recording) return;
  const p = ensurePort();
  if (!p) {
    // Native host missing: keep a short in-memory queue for tests / attach later.
    if (queued.length < 1000) queued.push(evidence);
    return;
  }
  try {
    p.postMessage({ type: 'EXTENSION_EVIDENCE', schema: SCHEMA, evidence });
  } catch (e) {
    if (queued.length < 1000) queued.push(evidence);
  }
}

function broadcastRecording(active) {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs || []) {
      if (!tab.id) continue;
      try {
        chrome.tabs.sendMessage(tab.id, { type: 'SET_RECORDING', active });
      } catch (e) {}
    }
  });
}

function setRecording(active) {
  recording = Boolean(active);
  setBadge(recording);
  broadcastRecording(recording);
  if (!recording) {
    queued = [];
    pendingSaves.clear();
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'SET_RECORDING') {
    setRecording(Boolean(msg.active));
    sendResponse({ ok: true, active: recording });
    return true;
  }
  if (msg.type === 'CONTENT_READY') {
    sendResponse({ active: recording });
    return true;
  }
  if (msg.type === 'EXTENSION_EVIDENCE') {
    if (recording && msg.evidence) deliver(msg.evidence);
    sendResponse({ ok: true, active: recording });
    return true;
  }
  if (msg.type === 'GET_STATE') {
    sendResponse({ active: recording, queued: queued.length });
    return true;
  }
});

// --- Network saves (chrome.webRequest, observe only) -----------------------
// Only state-changing requests from tabs, only while recording. Cookies are
// not requested (no 'extraHeaders'), and every record passes the fail-closed
// redactor before it is queued or sent. A record it cannot redact is dropped.
const SAVE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const MAX_PENDING_SAVES = 200;
const pendingSaves = new Map();

function networkRedactor() {
  const r = globalThis.OpenAdaptNetworkRedact;
  return r && typeof r.redactNetworkSave === 'function' ? r : null;
}

function isTrackedSave(details) {
  if (!recording || !details) return false;
  if (!SAVE_METHODS.has(String(details.method || '').toUpperCase())) return false;
  // tabId -1: the extension's own or browser-internal traffic.
  return Number.isInteger(details.tabId) && details.tabId >= 0;
}

function onSaveRequest(details) {
  if (!isTrackedSave(details) || !networkRedactor()) return;
  if (pendingSaves.size >= MAX_PENDING_SAVES) return;
  pendingSaves.set(details.requestId, {
    method: details.method,
    url: details.url,
    type: details.type,
    tabId: details.tabId,
    frameId: details.frameId,
    timeStamp: details.timeStamp,
    requestBody: details.requestBody || null,
  });
}

function onSaveHeaders(details) {
  const pending = pendingSaves.get(details && details.requestId);
  if (pending) pending.requestHeaders = details.requestHeaders || [];
}

function onSaveCompleted(details) {
  const pending = pendingSaves.get(details && details.requestId);
  if (!pending) return;
  pendingSaves.delete(details.requestId);
  const redactor = networkRedactor();
  if (!recording || !redactor) return;
  const record = redactor.redactNetworkSave(
    Object.assign({}, pending, {
      statusCode: details.statusCode,
      responseHeaders: details.responseHeaders || [],
    })
  );
  if (record) deliver(record);
}

function onSaveFailed(details) {
  pendingSaves.delete(details && details.requestId);
}

function installNetworkListeners() {
  const wr = chrome.webRequest;
  if (!wr) return false;
  const filter = { urls: ['http://*/*', 'https://*/*'] };
  try {
    wr.onBeforeRequest.addListener(onSaveRequest, filter, ['requestBody']);
    wr.onBeforeSendHeaders.addListener(onSaveHeaders, filter, ['requestHeaders']);
    wr.onCompleted.addListener(onSaveCompleted, filter, ['responseHeaders']);
    wr.onErrorOccurred.addListener(onSaveFailed, filter);
  } catch (e) {
    return false;
  }
  return true;
}

const networkListening = installNetworkListeners();

// Expose for the Node harness.
if (typeof globalThis !== 'undefined') {
  globalThis.__openadaptExtensionBackground = {
    setRecording,
    deliver,
    get recording() {
      return recording;
    },
    get queued() {
      return queued.slice();
    },
    get networkListening() {
      return networkListening;
    },
    get pendingSaves() {
      return pendingSaves.size;
    },
    _reset() {
      recording = false;
      queued = [];
      pendingSaves.clear();
      port = null;
      setBadge(false);
    },
  };
}

setBadge(false);
