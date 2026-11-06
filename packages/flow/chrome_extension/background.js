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

// Frame identity comes from Chrome (MessageSender), not from the page: a
// content script cannot spoof which tab/frame/origin it runs in. Any
// page-supplied `frame` block is replaced. sender.url is not copied (it can
// carry query tokens); the content script sends a stripped frame_url.
function stampSender(evidence, sender) {
  const out = Object.assign({}, evidence);
  delete out.frame;
  if (!sender || typeof sender !== 'object') return out;
  const frame = {};
  if (sender.tab && Number.isInteger(sender.tab.id)) frame.tab_id = sender.tab.id;
  if (Number.isInteger(sender.frameId)) frame.frame_id = sender.frameId;
  if (typeof sender.documentId === 'string') frame.document_id = sender.documentId;
  if (typeof sender.origin === 'string') frame.origin = sender.origin;
  if (Object.keys(frame).length) out.frame = frame;
  return out;
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
    if (recording && msg.evidence) deliver(stampSender(msg.evidence, sender));
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
    stampSender,
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
