// OpenAdapt optional extension evidence (service worker).
// Relays content-script evidence to the local native host only while recording.
// Shows a visible REC badge. No remote endpoints.
'use strict';

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
  if (!recording) queued = [];
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
    _reset() {
      recording = false;
      queued = [];
      port = null;
      setBadge(false);
    },
  };
}

setBadge(false);
