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
let copilotPending = new Map(); // requestId -> { resolve, timer, expectedType }
let nextCopilotRequestId = 1;
let copilotHaltCount = 0;
let lastCopilotHighlights = [];
let copilotRunActive = false;
let activeRunTabId = null;
// Ephemeral MV3 service worker state is mirrored into chrome.storage.session
// whenever available so popup tracking & per-tab auth pauses survive worker sleep.
let trackedPopups = new Map(); // childTabId -> { childTabId, childWindowId, parentTabId, parentWindowId, focused, openedAt }
let authPauseByTab = new Map(); // tabId -> { paused: true, kind, tabId, origin, signals, detectedAt, hostAcked, mode }

const SESSION_POPUPS_KEY = 'openadapt_copilot_popups';
const SESSION_AUTH_KEY = 'openadapt_copilot_auth_pause';
const VALID_AUTH_KINDS = new Set(['mfa', 'signin', 'sso']);

const EXPECTED_REPLY_TYPES = {
  COPILOT_GET_STATE: 'COPILOT_STATE',
  COPILOT_SUBMIT_DECISION: 'COPILOT_DECISION_ACK',
  COPILOT_AUTH_PAUSED: 'COPILOT_AUTH_PAUSED_ACK',
  COPILOT_AUTH_RESUMED: 'COPILOT_AUTH_RESUMED_ACK',
};

function hasActiveRun() {
  return Boolean(recording || copilotRunActive || Number.isInteger(activeRunTabId));
}

function isTabAuthPaused(tabId) {
  return Boolean(Number.isInteger(tabId) && authPauseByTab.has(tabId));
}

function notifyTabAuthPaused(tabId, paused) {
  if (!Number.isInteger(tabId)) return;
  try {
    chrome.tabs.sendMessage(
      tabId,
      { type: 'SET_AUTH_PAUSED', paused: Boolean(paused) },
      () => {
        void (chrome.runtime && chrome.runtime.lastError);
      }
    );
  } catch (e) {}
}

function setActiveRunTab(tabId) {
  const prevTabId = activeRunTabId;
  const nextTabId = Number.isInteger(tabId) ? tabId : null;
  activeRunTabId = nextTabId;
  if (Number.isInteger(prevTabId) && prevTabId !== nextTabId) {
    try {
      chrome.tabs.sendMessage(
        prevTabId,
        { type: 'SET_RUN_ACTIVE', active: false },
        () => {
          void (chrome.runtime && chrome.runtime.lastError);
        }
      );
    } catch (e) {}
  }
  if (Number.isInteger(activeRunTabId)) {
    try {
      chrome.tabs.sendMessage(
        activeRunTabId,
        { type: 'SET_RUN_ACTIVE', active: true },
        () => {
          void (chrome.runtime && chrome.runtime.lastError);
        }
      );
    } catch (e) {}
  }
}

function resolveAndSetActiveRunTab(explicitTabId) {
  if (Number.isInteger(explicitTabId)) {
    setActiveRunTab(explicitTabId);
    return Promise.resolve(explicitTabId);
  }
  if (Number.isInteger(activeRunTabId)) {
    // Re-assert SET_RUN_ACTIVE in case the tab was already open before run start.
    setActiveRunTab(activeRunTabId);
    return Promise.resolve(activeRunTabId);
  }
  return new Promise((resolve) => {
    try {
      chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
        const match = (tabs || []).find(isHighlightableTab);
        if (match && Number.isInteger(match.id)) {
          setActiveRunTab(match.id);
          resolve(match.id);
          return;
        }
        chrome.tabs.query({ active: true }, (fallbackTabs) => {
          const fallback = (fallbackTabs || []).find(isHighlightableTab);
          if (fallback && Number.isInteger(fallback.id)) {
            setActiveRunTab(fallback.id);
            resolve(fallback.id);
            return;
          }
          resolve(null);
        });
      });
    } catch (e) {
      resolve(null);
    }
  });
}

function serializeAuthPauses() {
  return Array.from(authPauseByTab.values());
}

function getActiveAuthPause(tabId) {
  if (Number.isInteger(tabId) && authPauseByTab.has(tabId)) {
    return authPauseByTab.get(tabId);
  }
  if (Number.isInteger(activeRunTabId) && authPauseByTab.has(activeRunTabId)) {
    return authPauseByTab.get(activeRunTabId);
  }
  const list = serializeAuthPauses();
  return list.length ? list[list.length - 1] : null;
}

function persistSession(key, value) {
  try {
    if (chrome && chrome.storage && chrome.storage.session && typeof chrome.storage.session.set === 'function') {
      const p = chrome.storage.session.set({ [key]: value });
      if (p && typeof p.catch === 'function') p.catch(() => {});
    }
  } catch (e) {}
}

function hydrateSessionState() {
  try {
    if (chrome && chrome.storage && chrome.storage.session && typeof chrome.storage.session.get === 'function') {
      const p = chrome.storage.session.get([SESSION_POPUPS_KEY, SESSION_AUTH_KEY]);
      if (p && typeof p.then === 'function') {
        p.then((data) => {
          if (data && Array.isArray(data[SESSION_POPUPS_KEY])) {
            trackedPopups = new Map(
              data[SESSION_POPUPS_KEY]
                .filter((e) => e && Number.isInteger(e.childTabId))
                .map((e) => [e.childTabId, e])
            );
          }
          if (data && Array.isArray(data[SESSION_AUTH_KEY])) {
            authPauseByTab = new Map(
              data[SESSION_AUTH_KEY]
                .filter((e) => e && e.paused && Number.isInteger(e.tabId))
                .map((e) => [e.tabId, e])
            );
            setBadge(recording);
          }
        }).catch(() => {});
      }
    }
  } catch (e) {}
}

function serializePopups() {
  return Array.from(trackedPopups.values());
}

function getActivePopupSummary() {
  const list = serializePopups();
  return list.length ? list[list.length - 1] : null;
}

async function focusParentTab(entry) {
  if (!entry || !Number.isInteger(entry.parentTabId)) return false;
  try {
    if (chrome && chrome.tabs && typeof chrome.tabs.update === 'function') {
      await chrome.tabs.update(entry.parentTabId, { active: true });
    }
    if (
      Number.isInteger(entry.parentWindowId) &&
      chrome &&
      chrome.windows &&
      typeof chrome.windows.update === 'function'
    ) {
      await chrome.windows.update(entry.parentWindowId, { focused: true });
    }
    return true;
  } catch (e) {
    return false;
  }
}

async function trackOpenedChildTab(tab) {
  // Never track popups or steal focus outside an active OpenAdapt run or recording.
  if (!hasActiveRun()) return null;
  if (!tab || !Number.isInteger(tab.id) || !Number.isInteger(tab.openerTabId)) return null;
  const openerMatchesActiveRun =
    Number.isInteger(activeRunTabId) && tab.openerTabId === activeRunTabId;
  if (!openerMatchesActiveRun && !recording) {
    return null;
  }
  let parentWindowId = tab.windowId;
  try {
    if (chrome && chrome.tabs && typeof chrome.tabs.get === 'function') {
      const parent = await chrome.tabs.get(tab.openerTabId);
      if (parent && Number.isInteger(parent.windowId)) {
        parentWindowId = parent.windowId;
      }
    }
  } catch (e) {}
  let childOrigin = null;
  const rawUrl = typeof tab.url === 'string' && tab.url ? tab.url : tab.pendingUrl;
  if (typeof rawUrl === 'string' && rawUrl) {
    try {
      const parsed = new URL(rawUrl);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        childOrigin = parsed.origin;
      }
    } catch (e) {}
  }
  const entry = {
    childTabId: tab.id,
    childWindowId: Number.isInteger(tab.windowId) ? tab.windowId : null,
    childOrigin,
    parentTabId: tab.openerTabId,
    parentWindowId: Number.isInteger(parentWindowId) ? parentWindowId : null,
    focused: Boolean(tab.active),
    openedAt: Date.now(),
  };
  trackedPopups.set(tab.id, entry);
  persistSession(SESSION_POPUPS_KEY, serializePopups());
  try {
    chrome.tabs.sendMessage(
      tab.id,
      { type: 'SET_RUN_ACTIVE', active: true },
      () => {
        void (chrome.runtime && chrome.runtime.lastError);
      }
    );
  } catch (e) {}
  return entry;
}

function clearAuthStateForTab(tabId) {
  if (!Number.isInteger(tabId) || !authPauseByTab.has(tabId)) return false;
  const entry = authPauseByTab.get(tabId);
  authPauseByTab.delete(tabId);
  persistSession(SESSION_AUTH_KEY, serializeAuthPauses());
  notifyTabAuthPaused(tabId, false);
  setBadge(recording);
  if (entry && entry.hostAcked) {
    void sendCopilotNative({
      type: 'COPILOT_AUTH_RESUMED',
      tab_id: entry.tabId,
      kind: entry.kind,
    });
  }
  return true;
}

async function handleClosedChildTab(tabId) {
  clearAuthStateForTab(tabId);
  if (activeRunTabId === tabId) activeRunTabId = null;
  if (!trackedPopups.has(tabId)) return false;
  const entry = trackedPopups.get(tabId);
  trackedPopups.delete(tabId);
  persistSession(SESSION_POPUPS_KEY, serializePopups());
  return await focusParentTab(entry);
}

async function handleClosedChildWindow(windowId) {
  let returned = false;
  for (const [childTabId, entry] of Array.from(trackedPopups.entries())) {
    if (entry.childWindowId === windowId) {
      trackedPopups.delete(childTabId);
      if (await focusParentTab(entry)) returned = true;
    }
  }
  persistSession(SESSION_POPUPS_KEY, serializePopups());
  return returned;
}

function handleWindowFocusChanged(windowId) {
  if (!Number.isInteger(windowId)) return;
  let changed = false;
  for (const entry of trackedPopups.values()) {
    const nextFocused = entry.childWindowId === windowId;
    if (entry.focused !== nextFocused) {
      entry.focused = nextFocused;
      changed = true;
    }
  }
  if (changed) persistSession(SESSION_POPUPS_KEY, serializePopups());
}

async function recordAuthInterstitial(msg, sender) {
  const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
  if (!Number.isInteger(tabId)) {
    return { ok: false, error: 'auth_sender_missing_tab' };
  }
  // Ignore subframe/ad-iframe detections: only top-level frame auth walls pause a run.
  if (Number.isInteger(sender.frameId) && sender.frameId !== 0) {
    return { ok: true, ignored: true, reason: 'subframe_ignored' };
  }
  // Pause on active run tab, during user-started recording, or in a tracked SSO/child popup tab.
  const onActiveRunTab = Number.isInteger(activeRunTabId) && tabId === activeRunTabId;
  const onTrackedPopup = trackedPopups.has(tabId);
  if (!onActiveRunTab && !recording && !onTrackedPopup) {
    return { ok: true, ignored: true, reason: 'no_active_run_on_tab' };
  }

  const kind = VALID_AUTH_KINDS.has(msg && msg.kind) ? msg.kind : 'signin';
  const rawSignals = Array.isArray(msg && msg.signals) ? msg.signals : [];
  const signals = rawSignals
    .filter((s) => typeof s === 'string' && /^[a-z0-9_]{1,32}$/.test(s))
    .slice(0, 8);
  const origin = sender && typeof sender.origin === 'string' ? sender.origin : '';

  // Zero credential storage: strictly structural metadata only, keyed by tabId.
  // Set local pause state BEFORE awaiting native-host ack so no network POST or
  // DOM evidence slips through during the native-host round-trip.
  const entry = {
    paused: true,
    kind,
    tabId,
    origin,
    signals,
    detectedAt: Date.now(),
    hostAcked: false,
    mode: 'capture_paused_local',
  };
  authPauseByTab.set(tabId, entry);
  persistSession(SESSION_AUTH_KEY, serializeAuthPauses());
  setBadge(recording);

  // Pause browser evidence & network capture immediately on this tab.
  notifyTabAuthPaused(tabId, true);
  for (const [reqId, pending] of Array.from(pendingSaves.entries())) {
    if (pending && pending.tabId === tabId) pendingSaves.delete(reqId);
  }

  // Notify local native host of the structural auth pause.
  let hostAcked = false;
  try {
    const reply = await sendCopilotNative({
      type: 'COPILOT_AUTH_PAUSED',
      tab_id: tabId,
      kind,
      signals,
      origin,
    });
    hostAcked = Boolean(
      reply && reply.ok === true && reply.type === 'COPILOT_AUTH_PAUSED_ACK'
    );
  } catch (e) {
    hostAcked = false;
  }

  // Only update if this pause entry is still the active one for tabId.
  if (authPauseByTab.get(tabId) === entry) {
    entry.hostAcked = hostAcked;
    entry.mode = hostAcked ? 'capture_paused_host_acked' : 'capture_paused_local';
    persistSession(SESSION_AUTH_KEY, serializeAuthPauses());
    setBadge(recording);
  }
  return entry;
}

async function resumeAfterAuth(tabId) {
  const entry = getActiveAuthPause(tabId);
  if (!entry) {
    return { ok: true, resumed: false };
  }
  // If the native host acknowledged the pause, require an explicit matched
  // COPILOT_AUTH_RESUMED_ACK (ok:true) before clearing the pause state.
  if (entry.hostAcked) {
    let reply = null;
    try {
      reply = await sendCopilotNative({
        type: 'COPILOT_AUTH_RESUMED',
        tab_id: entry.tabId,
        kind: entry.kind,
      });
    } catch (e) {
      reply = { ok: false, error: 'native_host_error' };
    }
    if (!reply || reply.ok !== true || reply.type !== 'COPILOT_AUTH_RESUMED_ACK') {
      return {
        ok: false,
        resumed: false,
        error: (reply && reply.error) || 'native_host_rejected',
      };
    }
  }
  authPauseByTab.delete(entry.tabId);
  persistSession(SESSION_AUTH_KEY, serializeAuthPauses());
  notifyTabAuthPaused(entry.tabId, false);
  setBadge(recording);
  return {
    ok: true,
    resumed: true,
    tabId: entry.tabId,
    hostAcked: Boolean(entry.hostAcked),
  };
}

if (typeof chrome !== 'undefined') {
  hydrateSessionState();
  try {
    if (chrome.tabs && chrome.tabs.onCreated && typeof chrome.tabs.onCreated.addListener === 'function') {
      chrome.tabs.onCreated.addListener((tab) => {
        void trackOpenedChildTab(tab);
      });
    }
    if (chrome.tabs && chrome.tabs.onUpdated && typeof chrome.tabs.onUpdated.addListener === 'function') {
      chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
        // Keep sign-in/MFA pause active across password -> MFA redirects until
        // the operator explicitly clicks Resume (COPILOT_RESUME_AUTH) or closes
        // the tab; re-notify the new document once navigation completes.
        if (changeInfo && changeInfo.status === 'complete' && isTabAuthPaused(tabId)) {
          notifyTabAuthPaused(tabId, true);
        }
      });
    }
    if (chrome.tabs && chrome.tabs.onRemoved && typeof chrome.tabs.onRemoved.addListener === 'function') {
      chrome.tabs.onRemoved.addListener((tabId) => {
        void handleClosedChildTab(tabId);
      });
    }
    if (chrome.windows && chrome.windows.onRemoved && typeof chrome.windows.onRemoved.addListener === 'function') {
      chrome.windows.onRemoved.addListener((windowId) => {
        void handleClosedChildWindow(windowId);
      });
    }
    if (chrome.windows && chrome.windows.onFocusChanged && typeof chrome.windows.onFocusChanged.addListener === 'function') {
      chrome.windows.onFocusChanged.addListener(handleWindowFocusChanged);
    }
  } catch (e) {}
}

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
    title = 'OpenAdapt Co-Pilot (recording)';
  } else if (getActiveAuthPause()) {
    text = 'AUTH';
    color = '#7c3aed';
    title = 'OpenAdapt Co-Pilot (sign-in / MFA pause)';
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
  const pending = Array.from(copilotPending.values());
  copilotPending.clear();
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
      if (!reply || typeof reply !== 'object') return;
      if (typeof reply.request_id === 'string' && reply.request_id) {
        const matched = copilotPending.get(reply.request_id);
        if (!matched) {
          // Late or unsolicited reply for a timed-out/unknown request_id: drop it.
          return;
        }
        copilotPending.delete(reply.request_id);
        if (matched.timer && cancelTimer) cancelTimer(matched.timer);
        matched.resolve(reply);
        return;
      }
      // Fallback for legacy/mock hosts that omit request_id: only match by exact
      // expected reply type so a late COPILOT_STATE can never resolve resumeAfterAuth.
      if (typeof reply.type === 'string' && reply.type) {
        for (const [reqId, entry] of copilotPending.entries()) {
          if (entry.expectedType === reply.type) {
            copilotPending.delete(reqId);
            if (entry.timer && cancelTimer) cancelTimer(entry.timer);
            entry.resolve(reply);
            return;
          }
        }
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
    const requestId = `req_${nextCopilotRequestId++}`;
    const expectedType =
      payload && typeof payload.type === 'string'
        ? EXPECTED_REPLY_TYPES[payload.type] || null
        : null;
    const entry = { resolve, timer: null, expectedType };
    if (scheduleTimer) {
      entry.timer = scheduleTimer(() => {
        if (copilotPending.delete(requestId)) {
          resolve({ ok: false, error: 'native_host_timeout' });
        }
      }, 5000);
    }
    copilotPending.set(requestId, entry);
    try {
      p.postMessage(Object.assign({}, payload, { request_id: requestId }));
    } catch (e) {
      copilotPending.delete(requestId);
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

function clearTrackedPopupsIfIdle() {
  if (hasActiveRun() || trackedPopups.size === 0) return;
  trackedPopups.clear();
  persistSession(SESSION_POPUPS_KEY, serializePopups());
}

function setRecording(active) {
  recording = Boolean(active);
  setBadge(recording);
  broadcastRecording(recording);
  if (!recording) {
    queued = [];
    pendingSaves.clear();
    clearTrackedPopupsIfIdle();
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'GET_RECORDING') {
    const isSidePanel =
      sender &&
      !sender.tab &&
      sender.url === chrome.runtime.getURL('sidepanel.html');
    if (!isSidePanel) {
      sendResponse({ ok: false, error: 'recording_requires_sidepanel' });
      return true;
    }
    sendResponse({ ok: true, active: recording });
    return true;
  }
  if (msg.type === 'SET_RECORDING') {
    const isSidePanel =
      sender &&
      !sender.tab &&
      sender.url === chrome.runtime.getURL('sidepanel.html');
    if (!isSidePanel) {
      sendResponse({ ok: false, error: 'recording_requires_sidepanel' });
      return true;
    }
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
    const senderTabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
    const isTrackedPopup =
      hasActiveRun() && Number.isInteger(senderTabId) && trackedPopups.has(senderTabId);
    const runActiveForTab =
      (hasActiveRun() && Number.isInteger(activeRunTabId) && senderTabId === activeRunTabId) ||
      isTrackedPopup;
    sendResponse({
      active: recording,
      runActive: runActiveForTab,
      authPaused: isTabAuthPaused(senderTabId),
    });
    return true;
  }
  if (msg.type === 'EXTENSION_EVIDENCE') {
    const senderTabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
    if (isTabAuthPaused(senderTabId)) {
      sendResponse({ ok: true, active: recording, paused: true });
      return true;
    }
    if (recording && msg.evidence) deliver(stampSender(msg.evidence, sender));
    sendResponse({ ok: true, active: recording });
    return true;
  }
  if (msg.type === 'GET_STATE') {
    sendResponse({ active: recording, queued: queued.length });
    return true;
  }
  if (msg.type === 'AUTH_INTERSTITIAL_DETECTED') {
    (async () => {
      const pause = await recordAuthInterstitial(msg, sender);
      if (pause && pause.ok === false) {
        sendResponse(pause);
        return;
      }
      if (pause && pause.ignored) {
        sendResponse(pause);
        return;
      }
      sendResponse({ ok: true, authPause: pause });
    })();
    return true;
  }
  const sidePanelOnlyTypes = new Set([
    'COPILOT_SYNC_STATE',
    'COPILOT_DECISION',
    'COPILOT_GET_POPUP_STATE',
    'COPILOT_RETURN_TO_PARENT',
    'COPILOT_GET_AUTH_STATE',
    'COPILOT_AUTH_RESUME',
  ]);
  if (
    sidePanelOnlyTypes.has(msg.type) &&
    sender.url !== chrome.runtime.getURL('sidepanel.html')
  ) {
    sendResponse({ ok: false, error: 'copilot_requires_sidepanel' });
    return true;
  }
  if (msg.type === 'COPILOT_GET_POPUP_STATE') {
    sendResponse({ ok: true, popup: getActivePopupSummary(), popups: serializePopups() });
    return true;
  }
  if (msg.type === 'COPILOT_RETURN_TO_PARENT') {
    (async () => {
      const target =
        Number.isInteger(msg.childTabId) && trackedPopups.has(msg.childTabId)
          ? trackedPopups.get(msg.childTabId)
          : getActivePopupSummary();
      const returned = target ? await focusParentTab(target) : false;
      sendResponse({ ok: returned, popup: target });
    })();
    return true;
  }
  if (msg.type === 'COPILOT_GET_AUTH_STATE') {
    const targetTabId = Number.isInteger(msg.tabId) ? msg.tabId : undefined;
    sendResponse({
      ok: true,
      authPause: getActiveAuthPause(targetTabId),
      authPauses: serializeAuthPauses(),
    });
    return true;
  }
  if (msg.type === 'COPILOT_AUTH_RESUME') {
    (async () => {
      const targetTabId = Number.isInteger(msg.tabId) ? msg.tabId : undefined;
      const res = await resumeAfterAuth(targetTabId);
      sendResponse(res);
    })();
    return true;
  }
  if (msg.type === 'COPILOT_SYNC_STATE') {
    (async () => {
      const reply = await sendCopilotNative({ type: 'COPILOT_GET_STATE' });
      if (!reply || !reply.ok) {
        copilotRunActive = false;
        if (!recording && Number.isInteger(activeRunTabId)) {
          setActiveRunTab(null);
        }
        clearTrackedPopupsIfIdle();
        sendResponse({
          ok: false,
          error: (reply && reply.error) || 'copilot_sync_failed',
          state: null,
          popupState: getActivePopupSummary(),
          authPause: getActiveAuthPause(),
        });
        return;
      }
      const state = reply.state || null;
      copilotRunActive = Boolean(
        state && state.run_id && state.outcome !== 'verified_done'
      );
      if (copilotRunActive) {
        const explicitTabId = Number.isInteger(msg.tabId) ? msg.tabId : undefined;
        await resolveAndSetActiveRunTab(explicitTabId);
      } else if (!recording && Number.isInteger(activeRunTabId)) {
        setActiveRunTab(null);
      }
      clearTrackedPopupsIfIdle();
      copilotHaltCount =
        state && Array.isArray(state.halts) ? state.halts.length : 0;
      setBadge(recording);
      const highlights = collectCopilotHighlights(state);
      await broadcastCopilotHighlights(highlights, Boolean(msg.forceScroll));
      sendResponse({
        ok: true,
        type: 'COPILOT_STATE',
        state,
        popupState: getActivePopupSummary(),
        authPause: getActiveAuthPause(),
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
  if (!Number.isInteger(details.tabId) || details.tabId < 0) return false;
  if (isTabAuthPaused(details.tabId)) return false;
  return true;
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
  if (!recording || !redactor || isTabAuthPaused(pending.tabId)) return;
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
    setActiveRunTab,
    deliver,
    stampSender,
    trackOpenedChildTab,
    handleClosedChildTab,
    handleClosedChildWindow,
    handleWindowFocusChanged,
    recordAuthInterstitial,
    clearAuthStateForTab,
    resumeAfterAuth,
    getActivePopupSummary,
    getActiveAuthPause,
    get recording() {
      return recording;
    },
    get activeRunTabId() {
      return activeRunTabId;
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
    get authPauseState() {
      return getActiveAuthPause();
    },
    get authPauses() {
      return serializeAuthPauses();
    },
    get trackedPopups() {
      return serializePopups();
    },
    _reset() {
      recording = false;
      copilotRunActive = false;
      activeRunTabId = null;
      queued = [];
      pendingSaves.clear();
      port = null;
      copilotHaltCount = 0;
      lastCopilotHighlights = [];
      trackedPopups = new Map();
      authPauseByTab = new Map();
      persistSession(SESSION_POPUPS_KEY, []);
      persistSession(SESSION_AUTH_KEY, []);
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
