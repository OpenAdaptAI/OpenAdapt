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
let copilotPort = null;
let copilotQueue = [];
let copilotHaltCount = 0;
let lastCopilotHighlights = [];

const scheduleTimer = typeof setTimeout === 'function' ? setTimeout : null;
const cancelTimer = typeof clearTimeout === 'function' ? clearTimeout : null;

if (
  typeof chrome !== 'undefined' &&
  chrome.sidePanel &&
  typeof chrome.sidePanel.setPanelBehavior === 'function'
) {
  try {
    const maybePromise = chrome.sidePanel.setPanelBehavior({
      openPanelOnActionClick: true,
    });
    if (maybePromise && typeof maybePromise.catch === 'function') {
      maybePromise.catch(() => {});
    }
  } catch (e) {}
}

function setBadge(active) {
  let text = '';
  let color = '#00000000';
  let title = 'OpenAdapt extension evidence (idle)';
  if (active) {
    text = 'REC';
    color = '#b00020';
    title = 'OpenAdapt extension evidence (recording)';
  } else if (copilotHaltCount > 0) {
    text = 'HALT';
    color = '#d97706';
    title = 'OpenAdapt Co-Pilot (action needed)';
  }
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setTitle({ title });
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

function failCopilotQueue(errorMessage) {
  const pending = copilotQueue.splice(0, copilotQueue.length);
  for (const entry of pending) {
    if (entry.timer && cancelTimer) cancelTimer(entry.timer);
    entry.resolve({ ok: false, error: errorMessage });
  }
}

function ensureCopilotPort() {
  if (copilotPort) return copilotPort;
  if (!chrome.runtime || typeof chrome.runtime.connectNative !== 'function') {
    return null;
  }
  try {
    const p = chrome.runtime.connectNative(NATIVE_HOST);
    p.onMessage.addListener((reply) => {
      const next = copilotQueue.shift();
      if (next) {
        if (next.timer && cancelTimer) cancelTimer(next.timer);
        next.resolve(reply);
      }
    });
    p.onDisconnect.addListener(() => {
      const err =
        chrome.runtime && chrome.runtime.lastError && chrome.runtime.lastError.message
          ? chrome.runtime.lastError.message
          : 'native_host_disconnected';
      if (copilotPort === p) {
        copilotPort = null;
      }
      failCopilotQueue(err);
    });
    copilotPort = p;
    return p;
  } catch (e) {
    copilotPort = null;
    return null;
  }
}

function sendCopilotNative(payload) {
  return new Promise((resolve) => {
    const p = ensureCopilotPort();
    if (!p) {
      resolve({ ok: false, error: 'native_host_unavailable' });
      return;
    }
    const entry = { resolve, timer: null };
    if (scheduleTimer) {
      entry.timer = scheduleTimer(() => {
        const idx = copilotQueue.indexOf(entry);
        if (idx !== -1) copilotQueue.splice(idx, 1);
        resolve({ ok: false, error: 'native_host_timeout' });
      }, 5000);
    }
    copilotQueue.push(entry);
    try {
      p.postMessage(payload);
    } catch (e) {
      const idx = copilotQueue.indexOf(entry);
      if (idx !== -1) copilotQueue.splice(idx, 1);
      if (entry.timer && cancelTimer) cancelTimer(entry.timer);
      copilotPort = null;
      resolve({
        ok: false,
        error: String((e && e.message) || e || 'native_post_failed'),
      });
    }
  });
}

function collectCopilotHighlights(state) {
  if (!state || typeof state !== 'object') return [];
  const list = [];
  const seen = new Set();
  for (const item of Array.isArray(state.field_highlights) ? state.field_highlights : []) {
    if (!item || typeof item !== 'object' || !item.dom_selector) continue;
    list.push({
      dom_selector: String(item.dom_selector),
      badge_state: String(item.badge_state || 'halt'),
      caption: String(item.caption || ''),
    });
    seen.add(String(item.dom_selector));
  }
  for (const halt of Array.isArray(state.halts) ? state.halts : []) {
    if (!halt || typeof halt !== 'object' || !halt.dom_selector) continue;
    const selector = String(halt.dom_selector);
    if (seen.has(selector)) continue;
    const isConflict =
      halt.kind === 'prefill_entity_conflict' || halt.kind === 'prefill_value_conflict';
    list.push({
      dom_selector: selector,
      badge_state: isConflict ? 'conflict' : 'halt',
      caption: String(halt.message || halt.field_label || ''),
    });
    seen.add(selector);
  }
  return list;
}

function isHighlightableTab(tab) {
  if (!tab || !Number.isInteger(tab.id)) return false;
  if (typeof tab.url === 'string' && tab.url) {
    return tab.url.startsWith('http://') || tab.url.startsWith('https://');
  }
  return true;
}

function broadcastCopilotHighlights(highlights, forceScroll) {
  lastCopilotHighlights = Array.isArray(highlights) ? highlights.slice() : [];
  return new Promise((resolve) => {
    try {
      chrome.tabs.query({}, (tabs) => {
        const targets = (tabs || []).filter(isHighlightableTab);
        if (!targets.length) {
          resolve();
          return;
        }
        let pending = targets.length;
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          resolve();
        };
        const guard = scheduleTimer ? scheduleTimer(finish, 400) : null;
        const onOneDone = () => {
          void (chrome.runtime && chrome.runtime.lastError);
          pending -= 1;
          if (pending <= 0) {
            if (guard && cancelTimer) cancelTimer(guard);
            finish();
          }
        };
        for (const tab of targets) {
          try {
            chrome.tabs.sendMessage(
              tab.id,
              {
                type: 'COPILOT_APPLY_HIGHLIGHTS',
                highlights: lastCopilotHighlights,
                forceScroll: Boolean(forceScroll),
              },
              onOneDone
            );
          } catch (e) {
            onOneDone();
          }
        }
      });
    } catch (e) {
      resolve();
    }
  });
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
  if (!recording) {
    queued = [];
    pendingSaves.clear();
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'SET_RECORDING') {
    setRecording(Boolean(msg.active));
    sendResponse({ ok: true, active: recording });
    return true;
  }
  if (msg.type === 'CONTENT_READY') {
    if (
      lastCopilotHighlights.length > 0 &&
      sender &&
      sender.tab &&
      Number.isInteger(sender.tab.id)
    ) {
      try {
        chrome.tabs.sendMessage(
          sender.tab.id,
          {
            type: 'COPILOT_APPLY_HIGHLIGHTS',
            highlights: lastCopilotHighlights,
            forceScroll: false,
          },
          () => {
            void (chrome.runtime && chrome.runtime.lastError);
          }
        );
      } catch (e) {}
    }
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
  if ((msg.type === 'COPILOT_SYNC_STATE' || msg.type === 'COPILOT_DECISION') &&
      sender.url !== chrome.runtime.getURL('sidepanel.html')) {
    sendResponse({ ok: false, error: 'copilot_requires_sidepanel' });
    return true;
  }
  if (msg.type === 'COPILOT_SYNC_STATE') {
    (async () => {
      const reply = await sendCopilotNative({ type: 'COPILOT_GET_STATE' });
      if (!reply || !reply.ok) {
        sendResponse({
          ok: false,
          error: (reply && reply.error) || 'copilot_sync_failed',
          state: null,
        });
        return;
      }
      const state = reply.state || null;
      copilotHaltCount =
        state && Array.isArray(state.halts) ? state.halts.length : 0;
      setBadge(recording);
      const highlights = collectCopilotHighlights(state);
      await broadcastCopilotHighlights(highlights, Boolean(msg.forceScroll));
      sendResponse({
        ok: true,
        type: 'COPILOT_STATE',
        state,
      });
    })();
    return true;
  }
  if (msg.type === 'COPILOT_DECISION') {
    if (!msg.decision || typeof msg.decision !== 'object') {
      sendResponse({ ok: false, error: 'decision object required' });
      return true;
    }
    (async () => {
      const reply = await sendCopilotNative({
        type: 'COPILOT_SUBMIT_DECISION',
        decision: msg.decision,
        binding: msg.binding,
      });
      sendResponse(
        reply || { ok: false, error: 'copilot_decision_failed' }
      );
    })();
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
    stampSender,
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
    get copilotHaltCount() {
      return copilotHaltCount;
    },
    _reset() {
      recording = false;
      queued = [];
      pendingSaves.clear();
      port = null;
      copilotHaltCount = 0;
      lastCopilotHighlights = [];
      if (copilotPort) {
        try {
          copilotPort.disconnect();
        } catch (e) {}
      }
      copilotPort = null;
      failCopilotQueue('reset');
      setBadge(false);
    },
  };
}

setBadge(false);
