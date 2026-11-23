"""Tests for the Chrome Web Store-ready MV3 OpenAdapt Co-Pilot Extension.

Covers:
1. MV3 manifest, icon PNG dimensions, CSP (no inline scripts/eval), and
   STORE_LISTING.md permission/privacy documentation.
2. Deterministic CWS zip packaging via ``scripts/package_chrome_extension.py``.
3. Pop-up window / estimator return handling in ``background.js`` (Node VM).
4. Operator-owned sign-in / MFA interstitial pause & resume in ``content.js``
   and ``background.js`` with strict zero-credential-capture proof (Node VM).
5. Side panel UI contracts for auth-pause and pop-up return controls.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXT_DIR = REPO_ROOT / "packages" / "flow" / "chrome_extension"
PACKAGER_SCRIPT = REPO_ROOT / "scripts" / "package_chrome_extension.py"


def _load_packager():
    spec = importlib.util.spec_from_file_location(
        "package_chrome_extension", PACKAGER_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip())


def test_manifest_and_extension_pass_cws_validation() -> None:
    packager = _load_packager()
    errors = packager.validate_extension_dir(EXT_DIR)
    assert errors == [], f"Unexpected CWS validation errors: {errors}"

    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert int(manifest["minimum_chrome_version"]) >= 116
    assert len(manifest["description"]) <= 132
    assert manifest["side_panel"]["default_path"] == "sidepanel.html"
    assert "sidePanel" in manifest["permissions"]
    assert "storage" in manifest["permissions"]
    # Least privilege: unused scripting, redundant activeTab, and redundant tabs must not be declared.
    assert "scripting" not in manifest["permissions"]
    assert "activeTab" not in manifest["permissions"]
    assert "tabs" not in manifest["permissions"]


def test_store_listing_covers_every_permission_and_zero_egress() -> None:
    listing_path = EXT_DIR / "STORE_LISTING.md"
    assert listing_path.is_file()
    text = listing_path.read_text(encoding="utf-8")

    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    for perm in manifest["permissions"]:
        assert f"`{perm}`" in text, (
            f"Permission {perm!r} missing justification in STORE_LISTING.md"
        )
    for host in manifest.get("host_permissions", []):
        assert f"`{host}`" in text, (
            f"Host permission {host!r} missing in STORE_LISTING.md"
        )

    assert manifest["description"] in text
    assert "Single Purpose Statement" in text
    assert "Privacy Policy" in text
    assert "Zero Credential Capture" in text
    assert "https://openadapt.ai/privacy-policy" in text
    assert "hello@openadapt.ai" in text
    assert "non-secret form values" in text
    assert "### `scripting`" not in text
    assert "### `activeTab`" not in text
    assert "### `tabs`" not in text

    # Ensure user-facing Store Listing Copy section avoids implementation jargon
    section1 = text.split("## 1. Store Listing Copy")[1].split("## 2. Single Purpose")[
        0
    ]
    for jargon in ("Manifest V3", "service worker", "content script", "chrome.storage"):
        assert jargon.lower() not in section1.lower(), (
            f"Implementation jargon {jargon!r} found in user-facing store listing copy"
        )


def test_cws_zip_packager_produces_clean_archive(tmp_path: Path) -> None:
    packager = _load_packager()
    out_zip = tmp_path / "openadapt-copilot-cws.zip"
    members = packager.build_cws_zip(EXT_DIR, out_zip)

    assert out_zip.is_file()
    assert "manifest.json" in members
    assert "background.js" in members
    assert "content.js" in members
    assert "sidepanel.html" in members
    assert "sidepanel.js" in members
    assert "sidepanel.css" in members
    assert "network_redact.js" in members
    assert "icons/icon16.png" in members
    assert "icons/icon48.png" in members
    assert "icons/icon128.png" in members

    # Must exclude developer / store docs and native host scripts
    for forbidden in ("STORE_LISTING.md", "README.md"):
        assert forbidden not in members
    assert not any(m.startswith("native_host/") for m in members)

    # Verify archive integrity
    with zipfile.ZipFile(out_zip, "r") as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == sorted(members)


def test_background_popup_tracking_and_return_to_parent() -> None:
    bg_path = str(EXT_DIR / "background.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

const listeners = {{
  onMessage: [],
  tabsCreated: [],
  tabsRemoved: [],
  windowsRemoved: [],
  windowsFocusChanged: [],
}};
const sessionStore = {{}};
const tabsUpdated = [];
const windowsUpdated = [];
const tabsById = {{
  10: {{ id: 10, windowId: 1, url: 'https://carrier.example.com/quote' }},
  25: {{ id: 25, windowId: 2, openerTabId: 10, url: 'https://estimator.example.com/calc' }},
}};

const chromeMock = {{
  runtime: {{
    lastError: null,
    getURL: (p) => 'chrome-extension://testext/' + p,
    onMessage: {{ addListener: (fn) => listeners.onMessage.push(fn) }},
    onInstalled: {{ addListener: () => {{}} }},
    connectNative: () => {{ throw new Error('offline'); }},
  }},
  action: {{
    onClicked: {{ addListener: () => {{}} }},
    setBadgeText: () => {{}},
    setBadgeBackgroundColor: () => {{}},
    setTitle: () => {{}},
  }},
  sidePanel: {{
    setPanelBehavior: () => Promise.resolve(),
  }},
  storage: {{
    session: {{
      get: (keys, cb) => cb && cb(Object.assign({{}}, sessionStore)),
      set: (obj, cb) => {{ Object.assign(sessionStore, obj); if (cb) cb(); }},
      remove: (key, cb) => {{ delete sessionStore[key]; if (cb) cb(); }},
    }},
  }},
  tabs: {{
    onCreated: {{ addListener: (fn) => listeners.tabsCreated.push(fn) }},
    onRemoved: {{ addListener: (fn) => listeners.tabsRemoved.push(fn) }},
    get: (tabId, cb) => {{ const t = tabsById[tabId] || null; if (cb) cb(t); return Promise.resolve(t); }},
    query: (q, cb) => cb(Object.values(tabsById)),
    update: (tabId, props, cb) => {{ tabsUpdated.push({{ tabId, props }}); if (cb) cb(); }},
    sendMessage: (tabId, msg, cb) => {{ if (cb) cb(); }},
  }},
  windows: {{
    onRemoved: {{ addListener: (fn) => listeners.windowsRemoved.push(fn) }},
    onFocusChanged: {{ addListener: (fn) => listeners.windowsFocusChanged.push(fn) }},
    update: (windowId, props, cb) => {{ windowsUpdated.push({{ windowId, props }}); if (cb) cb(); }},
  }},
  webRequest: {{
    onBeforeRequest: {{ addListener: () => {{}} }},
    onBeforeSendHeaders: {{ addListener: () => {{}} }},
    onCompleted: {{ addListener: () => {{}} }},
    onErrorOccurred: {{ addListener: () => {{}} }},
  }},
}};

const ctx = vm.createContext({{
  chrome: chromeMock,
  globalThis: {{}},
  importScripts: () => {{}},
  URL,
  Date,
  Map,
  Set,
  Promise,
  setTimeout,
  clearTimeout,
}});
ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync({json.dumps(bg_path)}, 'utf8'), ctx);

async function sendMsg(msg, senderUrl) {{
  return new Promise((resolve) => {{
    listeners.onMessage[0](msg, {{ url: senderUrl }}, resolve);
  }});
}}

(async () => {{
  // 0. Outside an active run, child tabs must NOT be tracked or steal focus
  listeners.tabsCreated[0](tabsById[25]);
  await new Promise((r) => setTimeout(r, 20));
  const outsideRunPopup = await sendMsg(
    {{ type: 'COPILOT_GET_POPUP_STATE' }},
    'chrome-extension://testext/sidepanel.html'
  );

  // Activate run on tab 10; child tab opened from unrelated tab 99 must be ignored
  ctx.__openadaptExtensionBackground.setActiveRunTab(10);
  listeners.tabsCreated[0]({{ id: 30, windowId: 3, openerTabId: 99, url: 'https://other.example.com' }});
  await new Promise((r) => setTimeout(r, 20));
  const unrelatedOpenerPopup = await sendMsg(
    {{ type: 'COPILOT_GET_POPUP_STATE' }},
    'chrome-extension://testext/sidepanel.html'
  );

  // 1. Child popup tab opens from active run's parent quoting tab 10
  listeners.tabsCreated[0](tabsById[25]);
  await new Promise((r) => setTimeout(r, 20));

  // 2. Non-sidepanel sender is rejected
  const rejected = await sendMsg({{ type: 'COPILOT_GET_POPUP_STATE' }}, 'https://evil.example.com/');

  // 3. Side panel queries popup state
  const stateResp = await sendMsg(
    {{ type: 'COPILOT_GET_POPUP_STATE' }},
    'chrome-extension://testext/sidepanel.html'
  );

  // 4. Operator clicks Return to Parent Tab
  const manualReturn = await sendMsg(
    {{ type: 'COPILOT_RETURN_TO_PARENT', childTabId: 25 }},
    'chrome-extension://testext/sidepanel.html'
  );

  // 5. Child popup closes -> auto-returns focus to parent tab 10 / window 1
  listeners.tabsRemoved[0](25);
  await new Promise((r) => setTimeout(r, 20));

  const afterClose = await sendMsg(
    {{ type: 'COPILOT_GET_POPUP_STATE' }},
    'chrome-extension://testext/sidepanel.html'
  );

  console.log(JSON.stringify({{
    outsideRunPopup: outsideRunPopup.popup,
    unrelatedOpenerPopup: unrelatedOpenerPopup.popup,
    rejected,
    popupBeforeClose: stateResp.popup,
    manualReturnOk: manualReturn.ok,
    afterClosePopup: afterClose.popup,
    tabsUpdated,
    windowsUpdated,
    persistedKeys: Object.keys(sessionStore),
  }}));
}})();
"""
    out = _run_node(script)
    assert out["outsideRunPopup"] is None
    assert out["unrelatedOpenerPopup"] is None
    assert out["rejected"]["ok"] is False
    assert out["rejected"]["error"] == "copilot_requires_sidepanel"
    assert out["popupBeforeClose"]["childTabId"] == 25
    assert out["popupBeforeClose"]["parentTabId"] == 10
    assert out["popupBeforeClose"]["childOrigin"] == "https://estimator.example.com"
    assert out["manualReturnOk"] is True
    assert out["afterClosePopup"] is None
    assert len(out["tabsUpdated"]) >= 2
    assert out["tabsUpdated"][-1] == {"tabId": 10, "props": {"active": True}}
    assert out["windowsUpdated"][-1] == {"windowId": 1, "props": {"focused": True}}


def test_auth_interstitial_detection_and_pause_never_reads_credentials() -> None:
    content_path = str(EXT_DIR / "content.js")
    bg_path = str(EXT_DIR / "background.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

// Part A: Test content.js detectAuthInterstitial with a trapping .value getter
let valueReadCount = 0;
function makeInput(attrs) {{
  const el = {{
    tagName: 'INPUT',
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
  }};
  Object.defineProperty(el, 'value', {{
    get() {{
      valueReadCount += 1;
      throw new Error('SECURITY VIOLATION: .value must never be read during auth detection');
    }},
  }});
  Object.defineProperty(el, 'defaultValue', {{
    get() {{
      valueReadCount += 1;
      throw new Error('SECURITY VIOLATION: .defaultValue must never be read');
    }},
  }});
  return el;
}}

const sentToRuntime = [];
const contentCtx = vm.createContext({{
  chrome: {{
    runtime: {{
      onMessage: {{ addListener: () => {{}} }},
      sendMessage: (msg, cb) => {{ sentToRuntime.push(msg); if (cb) cb({{ ok: true }}); }},
    }},
  }},
  window: {{
    addEventListener: () => {{}},
    location: {{ href: 'https://portal.example.com/login?token=secret', origin: 'https://portal.example.com', pathname: '/login' }},
    innerWidth: 1280,
    innerHeight: 800,
    scrollX: 0,
    scrollY: 0,
  }},
  document: {{
    addEventListener: () => {{}},
    readyState: 'complete',
    querySelectorAll: () => [],
    head: null,
    documentElement: null,
  }},
  location: {{ href: 'https://portal.example.com/login?token=secret', origin: 'https://portal.example.com', pathname: '/login' }},
  Element: function() {{}},
  URL,
  Date,
  Map,
  Set,
  WeakSet,
  WeakMap,
}});
contentCtx.globalThis = contentCtx;
vm.runInContext(fs.readFileSync({json.dumps(content_path)}, 'utf8'), contentCtx);

const {{ detectAuthInterstitial }} = contentCtx.__openadaptExtensionContent;

// 1. MFA / OTP screen
const mfaInput = makeInput({{ type: 'text', autocomplete: 'one-time-code', name: 'mfa_code' }});
const mfaDoc = {{
  querySelectorAll: (sel) => (sel === 'input' ? [mfaInput] : []),
}};
const mfaResult = detectAuthInterstitial(mfaDoc);

// 2. Password sign-in screen
const pwdInput = makeInput({{ type: 'password', autocomplete: 'current-password', name: 'pass' }});
const pwdDoc = {{
  querySelectorAll: (sel) => (sel === 'input' ? [pwdInput] : []),
}};
const pwdResult = detectAuthInterstitial(pwdDoc);

// 3. Normal non-auth form screen
const normalInput = makeInput({{ type: 'text', name: 'applicant_name' }});
const normalDoc = {{
  querySelectorAll: (sel) => (sel === 'input' ? [normalInput] : []),
}};
const normalResult = detectAuthInterstitial(normalDoc);

// Part B: Feed detection into background.js with native host ack + fail-closed resume + per-tab clearing
const bgListeners = [];
const tabUpdatedListeners = [];
const tabRemovedListeners = [];
const badgeHistory = [];
const sessionStore = {{}};
const nativeMessages = [];
let rejectNextResume = true;

function makeMockNativePort() {{
  const msgListeners = [];
  const disconnectListeners = [];
  return {{
    onMessage: {{ addListener: (fn) => msgListeners.push(fn) }},
    onDisconnect: {{ addListener: (fn) => disconnectListeners.push(fn) }},
    postMessage: (payload) => {{
      nativeMessages.push(payload);
      let reply = {{ ok: true, request_id: payload.request_id }};
      if (payload.type === 'COPILOT_AUTH_PAUSED') {{
        reply = {{ ok: true, type: 'COPILOT_AUTH_PAUSED_ACK', paused: true, tab_id: payload.tab_id, request_id: payload.request_id }};
      }} else if (payload.type === 'COPILOT_AUTH_RESUMED') {{
        if (rejectNextResume) {{
          rejectNextResume = false;
          reply = {{ ok: false, error: 'copilot_auth_resume_rejected', request_id: payload.request_id }};
        }} else {{
          // Inject a stale late COPILOT_STATE reply before the real ack to prove
          // request_id matching ignores mismatched/late replies.
          setTimeout(() => {{
            for (const fn of msgListeners) {{
              fn({{ ok: true, type: 'COPILOT_STATE', state: null, request_id: 'req_stale_999' }});
            }}
          }}, 1);
          reply = {{ ok: true, type: 'COPILOT_AUTH_RESUMED_ACK', paused: false, resumed: true, request_id: payload.request_id }};
        }}
      }} else if (payload.type === 'COPILOT_GET_STATE') {{
        reply = {{ ok: true, type: 'COPILOT_STATE', state: {{ run_id: 'run_1', outcome: 'halted_needs_human', halts: [] }}, request_id: payload.request_id }};
      }}
      setTimeout(() => {{
        for (const fn of msgListeners) fn(reply);
      }}, 5);
    }},
    disconnect: () => {{}},
  }};
}}

const tabMessages = [];
const webReqListeners = {{}};
const bgCtx = vm.createContext({{
  chrome: {{
    runtime: {{
      lastError: null,
      getURL: (p) => 'chrome-extension://testext/' + p,
      onMessage: {{ addListener: (fn) => bgListeners.push(fn) }},
      onInstalled: {{ addListener: () => {{}} }},
      connectNative: () => makeMockNativePort(),
    }},
    action: {{
      onClicked: {{ addListener: () => {{}} }},
      setBadgeText: (o) => badgeHistory.push(o.text),
      setBadgeBackgroundColor: () => {{}},
      setTitle: () => {{}},
    }},
    sidePanel: {{ setPanelBehavior: () => Promise.resolve() }},
    storage: {{
      session: {{
        get: (k, cb) => cb && cb(Object.assign({{}}, sessionStore)),
        set: (o, cb) => {{ Object.assign(sessionStore, o); if (cb) cb(); }},
        remove: (k, cb) => {{ delete sessionStore[k]; if (cb) cb(); }},
      }},
    }},
    tabs: {{
      onCreated: {{ addListener: () => {{}} }},
      onUpdated: {{ addListener: (fn) => tabUpdatedListeners.push(fn) }},
      onRemoved: {{ addListener: (fn) => tabRemovedListeners.push(fn) }},
      query: (q, cb) => cb([{{ id: 42, active: true, url: 'https://portal.example.com/quote' }}]),
      sendMessage: (id, m, cb) => {{ tabMessages.push({{ tabId: id, msg: m }}); if (cb) cb(); }},
    }},
    windows: {{
      onRemoved: {{ addListener: () => {{}} }},
      onFocusChanged: {{ addListener: () => {{}} }},
    }},
    webRequest: {{
      onBeforeRequest: {{ addListener: (fn) => {{ webReqListeners.before = fn; }} }},
      onBeforeSendHeaders: {{ addListener: (fn) => {{ webReqListeners.headers = fn; }} }},
      onCompleted: {{ addListener: (fn) => {{ webReqListeners.completed = fn; }} }},
      onErrorOccurred: {{ addListener: (fn) => {{ webReqListeners.error = fn; }} }},
    }},
  }},
  importScripts: () => {{}},
  URL,
  Date,
  Map,
  Set,
  Promise,
  setTimeout,
  clearTimeout,
}});
bgCtx.globalThis = bgCtx;
vm.runInContext(fs.readFileSync({json.dumps(bg_path)}, 'utf8'), bgCtx);

function sendBg(msg, sender) {{
  return new Promise((resolve) => bgListeners[0](msg, sender, resolve));
}}

(async () => {{
  // 0. SET_RECORDING rejects content-script/tab senders; idle-tab detection is ignored when recording=false and no active run
  const badSetRec = await sendBg(
    {{ type: 'SET_RECORDING', active: true }},
    {{ tab: {{ id: 42 }}, url: 'https://portal.example.com/quote' }}
  );
  const missingTabResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ origin: 'https://portal.example.com' }}
  );
  const idleResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );
  const okSetRec = await sendBg(
    {{ type: 'SET_RECORDING', active: true }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 2. Real COPILOT_SYNC_STATE wires activeRunTabId=42 from active tab and broadcasts SET_RUN_ACTIVE
  const initialSync = await sendBg(
    {{ type: 'COPILOT_SYNC_STATE' }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  const wiredRunTabId = bgCtx.__openadaptExtensionBackground.activeRunTabId;
  const subframeResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 2, origin: 'https://ad.example.com' }}
  );

  // 3. Top-frame detection on active run tab 42 sends COPILOT_AUTH_PAUSED,
  //    notifies content script SET_AUTH_PAUSED(true), and pauses capture
  const detectResp = await sendBg(
    {{
      type: 'AUTH_INTERSTITIAL_DETECTED',
      kind: mfaResult.kind,
      signals: mfaResult.signals,
    }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );

  // Evidence and webRequest capture for paused tab 42 MUST be dropped
  const pausedEvResp = await sendBg(
    {{ type: 'EXTENSION_EVIDENCE', evidence: {{ kind: 'target', event: 'input' }} }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );
  webReqListeners.before({{
    requestId: 'r_paused',
    method: 'POST',
    url: 'https://portal.example.com/login',
    tabId: 42,
    frameId: 0,
    timeStamp: Date.now(),
  }});
  const pendingWhilePaused = bgCtx.__openadaptExtensionBackground.pendingSaves;

  const syncResp = await sendBg(
    {{ type: 'COPILOT_SYNC_STATE' }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 4. Host rejects first resume -> fail closed, authPause MUST remain active
  const rejectedResume = await sendBg(
    {{ type: 'COPILOT_AUTH_RESUME', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  const afterRejectedResume = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 5. Host acks second resume (with stale COPILOT_STATE injected) -> clears authPause
  const acceptedResume = await sendBg(
    {{ type: 'COPILOT_AUTH_RESUME', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  const afterAcceptedResume = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 6. Per-tab auth state PERSISTS across navigation (password -> MFA redirect) and clears on tab close
  await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'signin', signals: ['password_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );
  const resumedCountBeforeNav = nativeMessages.filter((m) => m.type === 'COPILOT_AUTH_RESUMED').length;
  tabUpdatedListeners[0](42, {{ status: 'loading', url: 'https://portal.example.com/mfa' }});
  tabUpdatedListeners[0](42, {{ status: 'complete' }});
  await new Promise((r) => setTimeout(r, 20));
  const afterNavClear = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  tabRemovedListeners[0](42);
  await new Promise((r) => setTimeout(r, 20));
  const resumedCountAfterNav = nativeMessages.filter((m) => m.type === 'COPILOT_AUTH_RESUMED').length;

  console.log(JSON.stringify({{
    valueReadCount,
    mfaResult,
    pwdResult,
    normalResult,
    badSetRec,
    okSetRec,
    initialSyncOk: initialSync.ok,
    wiredRunTabId,
    sentRunActiveToTab42: tabMessages.some((m) => m.tabId === 42 && m.msg.type === 'SET_RUN_ACTIVE' && m.msg.active === true),
    sentAuthPausedToTab42: tabMessages.some((m) => m.tabId === 42 && m.msg.type === 'SET_AUTH_PAUSED' && m.msg.paused === true),
    pausedEvResp,
    pendingWhilePaused,
    missingTabResp,
    idleResp,
    subframeResp,
    detectAuthPause: detectResp.authPause,
    syncAuthPause: syncResp.authPause,
    badgeHistory,
    rejectedResume,
    stillPausedAfterReject: Boolean(afterRejectedResume.authPause && afterRejectedResume.authPause.paused),
    acceptedResume,
    afterAcceptedResume: afterAcceptedResume.authPause,
    afterNavClear: afterNavClear.authPause,
    navNotifiedHost: resumedCountAfterNav === resumedCountBeforeNav + 1,
    nativeTypes: nativeMessages.map((m) => m.type),
    allHaveRequestId: nativeMessages.every((m) => typeof m.request_id === 'string' && m.request_id.startsWith('req_')),
  }}));
}})();
"""
    out = _run_node(script)
    assert out["valueReadCount"] == 0, "Credential input .value must NEVER be accessed"
    assert out["mfaResult"]["detected"] is True
    assert out["mfaResult"]["kind"] == "mfa"
    assert out["pwdResult"]["detected"] is True
    assert out["pwdResult"]["kind"] == "signin"
    assert out["normalResult"]["detected"] is False

    assert out["badSetRec"]["ok"] is False
    assert out["badSetRec"]["error"] == "recording_requires_sidepanel"
    assert out["okSetRec"]["ok"] is True
    assert out["initialSyncOk"] is True
    assert out["wiredRunTabId"] == 42
    assert out["sentRunActiveToTab42"] is True
    assert out["sentAuthPausedToTab42"] is True
    assert out["pausedEvResp"]["paused"] is True
    assert out["pendingWhilePaused"] == 0
    assert out["missingTabResp"]["ok"] is False
    assert out["idleResp"]["ignored"] is True
    assert out["subframeResp"]["ignored"] is True
    assert out["detectAuthPause"]["paused"] is True
    assert out["detectAuthPause"]["hostAcked"] is True
    assert out["detectAuthPause"]["mode"] == "capture_paused_host_acked"
    assert out["syncAuthPause"]["paused"] is True
    assert "REC" in out["badgeHistory"]
    # Fail-closed resume: host rejection must NOT clear pause state
    assert out["rejectedResume"]["ok"] is False
    assert out["stillPausedAfterReject"] is True
    # Host-acked resume clears pause state cleanly
    assert out["acceptedResume"]["ok"] is True
    assert out["acceptedResume"]["resumed"] is True
    assert out["afterAcceptedResume"] is None
    assert out["afterNavClear"]["paused"] is True
    assert out["navNotifiedHost"] is True
    assert out["allHaveRequestId"] is True
    assert "COPILOT_AUTH_PAUSED" in out["nativeTypes"]
    assert "COPILOT_AUTH_RESUMED" in out["nativeTypes"]


def test_native_host_handles_auth_pause_and_resume_real_handler(tmp_path: Path) -> None:
    """Exercise the real native_host/openadapt_extension_host.py subprocess over stdio."""
    import struct
    import sys

    host_script = EXT_DIR / "native_host" / "openadapt_extension_host.py"
    assert host_script.is_file()

    def encode(obj: dict) -> bytes:
        raw = json.dumps(obj).encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    def decode_all(buf: bytes) -> list[dict]:
        out_msgs: list[dict] = []
        pos = 0
        while pos + 4 <= len(buf):
            (length,) = struct.unpack("<I", buf[pos : pos + 4])
            pos += 4
            out_msgs.append(json.loads(buf[pos : pos + length].decode("utf-8")))
            pos += length
        return out_msgs

    sample_evidence = {
        "schema": "openadapt.extension-evidence/1",
        "kind": "target",
        "event": "input",
        "tag": "input",
        "path": "input#applicant",
        "in_shadow": False,
        "closed_shadow": False,
        "frame_url": "https://portal.example.com/quote",
        "frame_origin": "https://portal.example.com",
        "is_iframe": False,
        "cross_origin_parent": False,
        "frame": {"tab_id": 42, "frame_id": 0, "origin": "https://portal.example.com"},
        "secret_field": False,
        "value": "Jane Doe",
        "seq": 1,
        "t_ms": 1700000000000,
    }

    stdin_bytes = b"".join(
        [
            encode(
                {
                    "type": "COPILOT_AUTH_PAUSED",
                    "tab_id": 42,
                    "kind": "mfa",
                    "signals": ["mfa_input"],
                    "origin": "https://portal.example.com",
                    "request_id": "req_1",
                }
            ),
            encode(
                {
                    "type": "EXTENSION_EVIDENCE",
                    "schema": "openadapt.extension-evidence/1",
                    "evidence": sample_evidence,
                    "request_id": "req_2",
                }
            ),
            encode(
                {
                    "type": "COPILOT_AUTH_PAUSED",
                    "tab_id": -1,
                    "kind": "mfa",
                    "request_id": "req_3",
                }
            ),
            encode(
                {
                    "type": "COPILOT_AUTH_RESUMED",
                    "tab_id": 42,
                    "kind": "mfa",
                    "request_id": "req_4",
                }
            ),
            encode(
                {
                    "type": "EXTENSION_EVIDENCE",
                    "schema": "openadapt.extension-evidence/1",
                    "evidence": sample_evidence,
                    "request_id": "req_5",
                }
            ),
        ]
    )
    proc = subprocess.run(
        [sys.executable, str(host_script), "--recording", str(tmp_path)],
        input=stdin_bytes,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"native host failed: {proc.stderr.decode('utf-8', 'replace')}"
    )
    replies = decode_all(proc.stdout)
    assert len(replies) == 5
    assert replies[0]["ok"] is True
    assert replies[0]["type"] == "COPILOT_AUTH_PAUSED_ACK"
    assert replies[0]["paused"] is True
    assert replies[0]["request_id"] == "req_1"
    # Evidence for paused tab 42 MUST be dropped and NOT written to disk
    assert replies[1]["ok"] is True
    assert replies[1]["paused"] is True
    assert replies[1]["dropped"] is True
    assert replies[1]["count"] == 0
    assert replies[1]["request_id"] == "req_2"
    assert replies[2]["ok"] is False
    assert replies[2]["error"] == "copilot_auth_pause_rejected"
    assert replies[2]["request_id"] == "req_3"
    assert replies[3]["ok"] is True
    assert replies[3]["type"] == "COPILOT_AUTH_RESUMED_ACK"
    assert replies[3]["resumed"] is True
    assert replies[3]["request_id"] == "req_4"
    # After resume, evidence for tab 42 is accepted and persisted
    assert replies[4]["ok"] is True
    assert replies[4]["count"] == 1
    assert replies[4]["request_id"] == "req_5"

    auth_state = json.loads(
        (tmp_path / "copilot_auth_state.json").read_text(encoding="utf-8")
    )
    assert auth_state["paused"] is False
    assert auth_state["tab_id"] == 42
    assert auth_state["kind"] == "mfa"
    evidence_lines = (
        (tmp_path / "extension_evidence.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    # 1 header line + 1 resumed event (the paused event was dropped)
    assert len(evidence_lines) == 2


def test_mfa_secret_redaction_covers_verification_code_and_totp() -> None:
    """Every MFA field pattern (verification_code, totp, mfa_code, 2fa, otp, pin) is redacted."""
    content_path = str(EXT_DIR / "content.js")
    redact_path = str(EXT_DIR / "network_redact.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

const contentCtx = vm.createContext({{
  chrome: {{
    runtime: {{
      onMessage: {{ addListener: () => {{}} }},
      sendMessage: (m, cb) => {{ if (cb) cb({{ ok: true }}); }},
    }},
  }},
  window: {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }},
  document: {{
    addEventListener: () => {{}},
    removeEventListener: () => {{}},
    readyState: 'complete',
    querySelectorAll: () => [],
  }},
  location: {{ href: 'https://portal.example.com/verify', origin: 'https://portal.example.com' }},
  URL,
  Date,
  Map,
  Set,
}});
contentCtx.globalThis = contentCtx;
vm.runInContext(fs.readFileSync({json.dumps(content_path)}, 'utf8'), contentCtx);

const {{ targetEvidence, isSecretField, setRecording, setAuthPaused }} = contentCtx.__openadaptExtensionContent;

const mfaFieldNames = ['verification_code', 'totp', 'mfa_code', '2fa', 'one-time-code', 'otp', 'pin'];
const contentResults = {{}};
for (const fieldName of mfaFieldNames) {{
  const el = {{
    nodeType: 1,
    tagName: 'INPUT',
    id: fieldName,
    value: '839201',
    getAttribute: (k) => (k === 'name' || k === 'id' ? fieldName : k === 'type' ? 'text' : null),
    getRootNode: () => contentCtx.document,
    parentElement: null,
  }};
  const ev = targetEvidence(el, 'input');
  contentResults[fieldName] = {{
    isSecret: isSecretField(el),
    secret_field: ev.secret_field,
    value: ev.value,
  }};
}}

// Verify setAuthPaused detaches content listeners while recording is active
setRecording(true);
const attachedWhenRecording = contentCtx.__openadaptExtensionContent.attached.document;
setAuthPaused(true);
const attachedWhenPaused = contentCtx.__openadaptExtensionContent.attached.document;
setAuthPaused(false);
const attachedWhenResumed = contentCtx.__openadaptExtensionContent.attached.document;

// Verify network_redact.js redacts verification_code, totp, mfa_code, 2fa, one-time-code, otp, pin
const redactCtx = vm.createContext({{
  URL,
  URLSearchParams,
  TextDecoder,
  TextEncoder,
  Uint8Array,
  Date,
  Set,
  Map,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
}});
redactCtx.globalThis = redactCtx;
vm.runInContext(fs.readFileSync({json.dumps(redact_path)}, 'utf8'), redactCtx);
const {{ redactNetworkSave }} = redactCtx.OpenAdaptNetworkRedact;

const bodyObj = {{
  applicant: 'Jane Doe',
  verification_code: '741852',
  totp: '963258',
  mfa_code: '159753',
  '2fa': '456123',
  'one-time-code': '321654',
  otp: '654987',
  pin: '4321',
}};
const rawBytes = new TextEncoder().encode(JSON.stringify(bodyObj));
const redacted = redactNetworkSave({{
  method: 'POST',
  url: 'https://portal.example.com/api/verify',
  type: 'xmlhttprequest',
  tabId: 42,
  frameId: 0,
  timeStamp: Date.now(),
  statusCode: 200,
  requestHeaders: [{{ name: 'Content-Type', value: 'application/json' }}],
  responseHeaders: [],
  requestBody: {{ raw: [{{ bytes: rawBytes }}] }},
}});

console.log(JSON.stringify({{
  contentResults,
  attachedWhenRecording,
  attachedWhenPaused,
  attachedWhenResumed,
  redactedBody: redacted && redacted.request_body,
}}));
"""
    out = _run_node(script)
    for field_name in (
        "verification_code",
        "totp",
        "mfa_code",
        "2fa",
        "one-time-code",
        "otp",
        "pin",
    ):
        assert out["contentResults"][field_name]["isSecret"] is True, field_name
        assert out["contentResults"][field_name]["secret_field"] is True, field_name
        assert out["contentResults"][field_name]["value"] == "<secret>", field_name
        assert out["redactedBody"][field_name] == "<secret>", field_name

    assert out["redactedBody"]["applicant"] == "Jane Doe"
    assert out["attachedWhenRecording"] == 3
    assert out["attachedWhenPaused"] == 0
    assert out["attachedWhenResumed"] == 3


def test_sidepanel_wires_auth_pause_and_popup_return_controls() -> None:
    html = (EXT_DIR / "sidepanel.html").read_text(encoding="utf-8")
    js = (EXT_DIR / "sidepanel.js").read_text(encoding="utf-8")

    assert 'id="record-btn"' in html
    assert 'id="auth-pause-container"' in html
    assert 'id="popup-banner-container"' in html
    assert "SET_RECORDING" in js
    assert "GET_RECORDING" in js
    assert "COPILOT_AUTH_RESUME" in js
    assert "COPILOT_RETURN_TO_PARENT" in js
    assert "I Signed In" in js
    assert "Return to Parent Tab" in js


def test_secret_redaction_covers_api_keys_tokens_cards_ssn_cvv_across_all_three_layers() -> (
    None
):
    from openadapt_flow.extension_evidence import is_secret_field

    content_path = str(EXT_DIR / "content.js")
    redact_path = str(EXT_DIR / "network_redact.js")
    sensitive_names = [
        "api_key",
        "access_token",
        "card_number",
        "cc-number",
        "cc_number",
        "ccnum",
        "ssn",
        "cvv",
        "cc-csc",
        "cc-exp",
        "mfa_code",
        "totp",
        "verification_code",
    ]
    for name in sensitive_names:
        assert is_secret_field(name=name) is True, f"engine missed {name}"

    script = f"""
const fs = require('fs');
const vm = require('vm');

const redactCtx = vm.createContext({{
  globalThis: {{}},
  URL,
  URLSearchParams,
  TextDecoder,
  TextEncoder,
  Uint8Array,
  Date,
  Set,
  Map,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
}});
redactCtx.globalThis = redactCtx;
vm.runInContext(fs.readFileSync({json.dumps(redact_path)}, 'utf8'), redactCtx);
const {{ redactNetworkSave }} = redactCtx.OpenAdaptNetworkRedact;

const contentSrc = fs.readFileSync({json.dumps(content_path)}, 'utf8');
const secretNameMatch = contentSrc.match(/const SECRET_NAME =\\s*([\\s\\S]*?);/);
const autoMatch = contentSrc.match(/const SECRET_AUTOCOMPLETE =\\s*([\\s\\S]*?);/);
const SECRET_NAME = eval(secretNameMatch[1]);
const SECRET_AUTOCOMPLETE = eval(autoMatch[1]);

const names = {json.dumps(sensitive_names)};
const contentMatches = Object.fromEntries(names.map((n) => [n, SECRET_NAME.test(n)]));
const autoMatches = Object.fromEntries(['cc-number', 'cc-csc', 'cc-exp'].map((a) => [a, SECRET_AUTOCOMPLETE.test(a)]));

const bodyObj = {{ applicant: 'Jane Doe' }};
for (const n of names) bodyObj[n] = 'sensitive-val-' + n;
const rawBytes = new TextEncoder().encode(JSON.stringify(bodyObj));
const redacted = redactNetworkSave({{
  method: 'POST',
  url: 'https://portal.example.com/api/submit',
  type: 'xmlhttprequest',
  tabId: 11,
  frameId: 0,
  timeStamp: Date.now(),
  statusCode: 200,
  requestHeaders: [{{ name: 'Content-Type', value: 'application/json' }}],
  responseHeaders: [],
  requestBody: {{ raw: [{{ bytes: rawBytes }}] }},
}});

console.log(JSON.stringify({{ contentMatches, autoMatches, redactedBody: redacted && redacted.request_body }}));
"""
    out = _run_node(script)
    for name in sensitive_names:
        assert out["contentMatches"][name] is True, f"content.js missed {name}"
        assert out["redactedBody"][name] == "<secret>", (
            f"network_redact.js missed {name}"
        )
    for ac in ("cc-number", "cc-csc", "cc-exp"):
        assert out["autoMatches"][ac] is True, f"content.js autocomplete missed {ac}"
    assert out["redactedBody"]["applicant"] == "Jane Doe"


def test_auth_pause_works_in_recording_mode_and_persists_across_password_to_mfa_navigation() -> (
    None
):
    bg_path = str(EXT_DIR / "background.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

const listeners = {{ onMessage: [], tabsUpdated: [], tabsCreated: [] }};
const tabMessages = [];
const sessionStore = {{}};
const chromeMock = {{
  runtime: {{
    lastError: null,
    getURL: (p) => 'chrome-extension://testext/' + p,
    onMessage: {{ addListener: (fn) => listeners.onMessage.push(fn) }},
    onInstalled: {{ addListener: () => {{}} }},
    connectNative: () => {{ throw new Error('offline'); }},
  }},
  action: {{
    onClicked: {{ addListener: () => {{}} }},
    setBadgeText: () => {{}},
    setBadgeBackgroundColor: () => {{}},
    setTitle: () => {{}},
  }},
  sidePanel: {{ setPanelBehavior: () => Promise.resolve() }},
  storage: {{
    session: {{
      get: (k, cb) => cb && cb(Object.assign({{}}, sessionStore)),
      set: (o, cb) => {{ Object.assign(sessionStore, o); if (cb) cb(); }},
      remove: (k, cb) => {{ delete sessionStore[k]; if (cb) cb(); }},
    }},
  }},
  tabs: {{
    onCreated: {{ addListener: (fn) => listeners.tabsCreated.push(fn) }},
    onUpdated: {{ addListener: (fn) => listeners.tabsUpdated.push(fn) }},
    onRemoved: {{ addListener: () => {{}} }},
    query: (q, cb) => cb([{{ id: 77, windowId: 1 }}]),
    sendMessage: (tabId, msg, cb) => {{ tabMessages.push({{ tabId, msg }}); if (cb) cb(); }},
  }},
  windows: {{
    onRemoved: {{ addListener: () => {{}} }},
    onFocusChanged: {{ addListener: () => {{}} }},
  }},
  webRequest: {{
    onBeforeRequest: {{ addListener: () => {{}} }},
    onBeforeSendHeaders: {{ addListener: () => {{}} }},
    onCompleted: {{ addListener: () => {{}} }},
    onErrorOccurred: {{ addListener: () => {{}} }},
  }},
}};
const ctx = vm.createContext({{
  chrome: chromeMock, globalThis: {{}}, importScripts: () => {{}},
  URL, Date, Map, Set, Promise, setTimeout, clearTimeout,
}});
ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync({json.dumps(bg_path)}, 'utf8'), ctx);

function send(msg, sender) {{
  return new Promise((resolve) => listeners.onMessage[0](msg, sender, resolve));
}}

(async () => {{
  const sp = {{ url: 'chrome-extension://testext/sidepanel.html' }};
  // Enable recording via sidepanel (no active co-pilot run!)
  const recOn = await send({{ type: 'SET_RECORDING', active: true }}, sp);
  const recGet = await send({{ type: 'GET_RECORDING' }}, sp);

  // Content script detects sign-in wall during recording-only mode
  const paused = await send(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'signin', signals: ['password_input'], origin: 'https://sso.example.com' }},
    {{ tab: {{ id: 77 }}, frameId: 0 }}
  );

  // Password -> MFA navigation updates URL & loading -> complete; pause MUST stay active
  listeners.tabsUpdated[0](77, {{ url: 'https://sso.example.com/mfa', status: 'loading' }});
  listeners.tabsUpdated[0](77, {{ status: 'complete' }});
  const afterNav = await send({{ type: 'COPILOT_GET_AUTH_STATE', tabId: 77 }}, sp);

  // Explicit resume clears pause
  const resumed = await send({{ type: 'COPILOT_AUTH_RESUME', tabId: 77 }}, sp);
  const afterResume = await send({{ type: 'COPILOT_GET_AUTH_STATE', tabId: 77 }}, sp);

  console.log(JSON.stringify({{ recOn, recGet, paused, afterNav, resumed, afterResume, tabMessages }}));
}})();
"""
    out = _run_node(script)
    assert out["recOn"]["ok"] is True and out["recOn"]["active"] is True
    assert out["recGet"]["active"] is True
    assert out["paused"]["authPause"]["paused"] is True
    assert out["afterNav"]["authPause"]["paused"] is True
    assert out["resumed"]["resumed"] is True
    assert out["afterResume"]["authPause"] is None


def test_e2e_content_to_background_recording_only_and_sso_popup_auth_pause() -> None:
    """Drive real content.js -> background.js messaging in both recording-only and SSO popup modes."""
    from openadapt_flow.extension_evidence import is_secret_field

    for secret_name in ("code", "sms_code", "Card Number", "API Key"):
        assert is_secret_field(name=secret_name) is True, (
            f"Python ingest must redact {secret_name!r}"
        )

    bg_path = str(EXT_DIR / "background.js")
    content_path = str(EXT_DIR / "content.js")
    sidepanel_path = str(EXT_DIR / "sidepanel.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

const bgListeners = [];
const tabCreatedListeners = [];
const contentListenersByTab = new Map();
const tabMessages = [];

const chromeBg = {{
  runtime: {{
    lastError: null,
    getURL: (p) => 'chrome-extension://testext/' + p,
    onMessage: {{ addListener: (fn) => bgListeners.push(fn) }},
    onInstalled: {{ addListener: () => {{}} }},
    connectNative: () => {{ throw new Error('offline'); }},
  }},
  action: {{
    onClicked: {{ addListener: () => {{}} }},
    setBadgeText: () => {{}},
    setBadgeBackgroundColor: () => {{}},
    setTitle: () => {{}},
  }},
  sidePanel: {{ setPanelBehavior: () => Promise.resolve() }},
  storage: {{
    session: {{
      get: (_k, cb) => cb && cb({{}}),
      set: (_o, cb) => cb && cb(),
      remove: (_k, cb) => cb && cb(),
    }},
  }},
  tabs: {{
    onCreated: {{ addListener: (fn) => tabCreatedListeners.push(fn) }},
    onUpdated: {{ addListener: () => {{}} }},
    onRemoved: {{ addListener: () => {{}} }},
    query: (_q, cb) => cb([{{ id: 10, windowId: 1 }}, {{ id: 99, windowId: 2 }}]),
    get: async (id) => ({{ id, windowId: id === 10 ? 1 : 2 }}),
    sendMessage: (tabId, msg, cb) => {{
      tabMessages.push({{ tabId, msg }});
      const fn = contentListenersByTab.get(tabId);
      if (fn) fn(msg, {{}}, cb || (() => {{}}));
      else if (cb) cb();
    }},
  }},
  windows: {{
    onRemoved: {{ addListener: () => {{}} }},
    onFocusChanged: {{ addListener: () => {{}} }},
  }},
  webRequest: {{
    onBeforeRequest: {{ addListener: () => {{}} }},
    onBeforeSendHeaders: {{ addListener: () => {{}} }},
    onCompleted: {{ addListener: () => {{}} }},
    onErrorOccurred: {{ addListener: () => {{}} }},
  }},
}};

const bgCtx = vm.createContext({{
  chrome: chromeBg,
  globalThis: {{}},
  importScripts: () => {{}},
  URL, Date, Map, Set, Promise, setTimeout, clearTimeout,
}});
bgCtx.globalThis = bgCtx;
vm.runInContext(fs.readFileSync({json.dumps(bg_path)}, 'utf8'), bgCtx);

function sendToBg(msg, sender) {{
  return new Promise((resolve) => bgListeners[0](msg, sender, resolve));
}}

function mountContentTab(tabId, inputs) {{
  let observerStarted = 0;
  let observerStopped = 0;
  class FakeMutationObserver {{
    constructor(cb) {{ this._cb = cb; }}
    observe() {{ observerStarted += 1; }}
    disconnect() {{ observerStopped += 1; }}
  }}
  const contentCtx = vm.createContext({{
    chrome: {{
      runtime: {{
        onMessage: {{ addListener: (fn) => contentListenersByTab.set(tabId, fn) }},
        sendMessage: (msg, cb) => {{
          sendToBg(msg, {{ tab: {{ id: tabId }}, frameId: 0, origin: 'https://sso.example.com' }})
            .then((resp) => {{ if (cb) cb(resp); }});
        }},
      }},
    }},
    window: {{
      addEventListener: () => {{}},
      removeEventListener: () => {{}},
      top: null,
      location: {{ href: 'https://sso.example.com/login', origin: 'https://sso.example.com' }},
    }},
    document: {{
      addEventListener: () => {{}},
      removeEventListener: () => {{}},
      readyState: 'complete',
      documentElement: {{}},
      querySelectorAll: (sel) => (sel === 'input' ? inputs : []),
    }},
    location: {{ href: 'https://sso.example.com/login', origin: 'https://sso.example.com' }},
    MutationObserver: FakeMutationObserver,
    URL, Date, Map, Set, setTimeout, clearTimeout,
  }});
  contentCtx.window.top = contentCtx.window;
  contentCtx.globalThis = contentCtx;
  vm.runInContext(fs.readFileSync({json.dumps(content_path)}, 'utf8'), contentCtx);
  return {{
    ctx: contentCtx,
    get observerStarted() {{ return observerStarted; }},
    get observerStopped() {{ return observerStopped; }},
  }};
}}

function mkInput(attrs) {{
  return {{
    nodeType: 1,
    tagName: 'INPUT',
    type: attrs.type || 'text',
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
  }};
}}

(async () => {{
  const sp = {{ url: 'chrome-extension://testext/sidepanel.html' }};

  // Scenario 1: Recording-only mode (no Co-Pilot run) on tab 10 with password input
  const tab10 = mountContentTab(10, [mkInput({{ type: 'password', name: 'pass' }})]);
  await new Promise((r) => setTimeout(r, 20));
  const c10 = tab10.ctx.__openadaptExtensionContent;
  const secretChecks = {{
    code: c10.isSecretField(mkInput({{ name: 'code' }})),
    smsCode: c10.isSecretField(mkInput({{ name: 'sms_code' }})),
    cardNumberSpace: c10.isSecretField(mkInput({{ 'aria-label': 'Card Number' }})),
    apiKeySpace: c10.isSecretField(mkInput({{ 'aria-label': 'API Key' }})),
  }};

  // User clicks Start Recording in side panel -> background broadcasts SET_RECORDING(true)
  await sendToBg({{ type: 'SET_RECORDING', active: true }}, sp);
  await new Promise((r) => setTimeout(r, 30));
  const recOnlyAuth = await sendToBg({{ type: 'COPILOT_GET_AUTH_STATE', tabId: 10 }}, sp);
  const tab10Paused = c10.authPaused;
  const tab10ObserverStarted = tab10.observerStarted;

  // Scenario 2: Tracked SSO popup tab 99 opened from active run tab 10 (even with recording OFF!)
  await sendToBg({{ type: 'SET_RECORDING', active: false }}, sp);
  await sendToBg({{ type: 'COPILOT_AUTH_RESUME', tabId: 10 }}, sp);
  bgCtx.__openadaptExtensionBackground.setActiveRunTab(10);
  await bgCtx.__openadaptExtensionBackground.trackOpenedChildTab({{
    id: 99,
    openerTabId: 10,
    windowId: 2,
    url: 'https://idp.example.com/oauth',
    active: true,
  }});
  // Mount content.js in tab 99 AFTER trackOpenedChildTab -> CONTENT_READY gets runActive:true
  const tab99 = mountContentTab(99, [mkInput({{ type: 'text', name: 'mfa_code', autocomplete: 'one-time-code' }})]);
  await new Promise((r) => setTimeout(r, 30));
  const popupAuth = await sendToBg({{ type: 'COPILOT_GET_AUTH_STATE', tabId: 99 }}, sp);
  const tab99Paused = tab99.ctx.__openadaptExtensionContent.authPaused;
  const tab99ObserverStarted = tab99.observerStarted;

  // Scenario 3: Verify sidepanel.js keeps Recording badge and polls syncRecordingState
  const sidepanelSrc = fs.readFileSync({json.dumps(sidepanel_path)}, 'utf8');
  const sidepanelKeepsRecording =
    sidepanelSrc.includes("text: 'Recording'") &&
    sidepanelSrc.includes('void syncRecordingState();');

  console.log(JSON.stringify({{
    secretChecks,
    recOnlyPaused: Boolean(recOnlyAuth.authPause && recOnlyAuth.authPause.paused),
    tab10Paused,
    tab10ObserverStarted,
    popupPaused: Boolean(popupAuth.authPause && popupAuth.authPause.paused && popupAuth.authPause.tabId === 99),
    tab99Paused,
    tab99ObserverStarted,
    sidepanelKeepsRecording,
  }}));
}})();
"""
    out = _run_node(script)
    assert out["secretChecks"] == {
        "code": True,
        "smsCode": True,
        "cardNumberSpace": True,
        "apiKeySpace": True,
    }
    assert out["recOnlyPaused"] is True
    assert out["tab10Paused"] is True
    assert out["tab10ObserverStarted"] >= 1
    assert out["popupPaused"] is True
    assert out["tab99Paused"] is True
    assert out["tab99ObserverStarted"] >= 1
    assert out["sidepanelKeepsRecording"] is True


def test_smscode_network_redaction_and_mfa_detection() -> None:
    """P1-C regression: smsCode / userSsn bodies redact and smsCode input triggers MFA pause."""
    redact_path = str(EXT_DIR / "network_redact.js")
    content_path = str(EXT_DIR / "content.js")
    script = f"""
const fs = require('fs');
const vm = require('vm');

const rCtx = {{ console, URL, URLSearchParams, TextDecoder, Buffer }};
rCtx.globalThis = rCtx;
vm.runInNewContext(fs.readFileSync({json.dumps(redact_path)}, 'utf8'), rCtx);
const redactor = rCtx.OpenAdaptNetworkRedact;

const bodyStr = JSON.stringify({{
  smsCode: '482910',
  userSsn: '123-45-6789',
  applicant: 'Jane Doe',
}});
const redacted = redactor.redactNetworkSave({{
  method: 'POST',
  url: 'https://portal.example.com/api/verify',
  requestHeaders: [{{ name: 'content-type', value: 'application/json' }}],
  requestBody: {{ raw: [{{ bytes: Buffer.from(bodyStr, 'utf8') }}] }},
  statusCode: 200,
  t_ms: 1700000000000,
}});

const cCtx = {{
  console,
  URL,
  setTimeout,
  clearTimeout,
  window: {{ addEventListener: () => {{}}, removeEventListener: () => {{}}, top: null }},
  location: {{ href: 'https://portal.example.com/mfa', origin: 'https://portal.example.com' }},
  document: {{
    readyState: 'complete',
    querySelectorAll: (sel) => sel === 'input' ? [{{
      nodeType: 1,
      tagName: 'INPUT',
      type: 'text',
      getAttribute: (k) => (k === 'name' ? 'smsCode' : null),
    }}] : [],
    addEventListener: () => {{}},
    removeEventListener: () => {{}},
  }},
  chrome: {{
    runtime: {{
      onMessage: {{ addListener: () => {{}} }},
      sendMessage: () => {{}},
    }},
  }},
}};
cCtx.window.top = cCtx.window;
cCtx.globalThis = cCtx;
vm.runInNewContext(fs.readFileSync({json.dumps(content_path)}, 'utf8'), cCtx);
const mfaCheck = cCtx.__openadaptExtensionContent.detectAuthInterstitial();

console.log(JSON.stringify({{
  redacted,
  mfaCheck,
}}));
"""
    out = _run_node(script)
    assert out["redacted"] is not None
    payload = out["redacted"]["request_body"]
    assert payload["smsCode"] == "<secret>"
    assert payload["userSsn"] == "<secret>"
    assert payload["applicant"] == "Jane Doe"
    assert "482910" not in json.dumps(out["redacted"])
    assert "123-45-6789" not in json.dumps(out["redacted"])
    assert out["mfaCheck"]["detected"] is True
    assert out["mfaCheck"]["kind"] == "mfa"
    assert "mfa_input" in out["mfaCheck"]["signals"]
