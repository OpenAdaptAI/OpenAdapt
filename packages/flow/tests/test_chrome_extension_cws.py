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
    assert "tabs" in manifest["permissions"]
    # Least privilege: unused scripting and redundant activeTab must not be declared.
    assert "scripting" not in manifest["permissions"]
    assert "activeTab" not in manifest["permissions"]


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
    assert "https://openadapt.ai/privacy" in text
    assert "richard@openadapt.ai" in text
    assert "non-secret form values" in text
    assert "### `scripting`" not in text
    assert "### `activeTab`" not in text

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
      let reply = {{ ok: true }};
      if (payload.type === 'COPILOT_AUTH_PAUSED') {{
        reply = {{ ok: true, type: 'COPILOT_AUTH_PAUSED_ACK', paused: true, tab_id: payload.tab_id }};
      }} else if (payload.type === 'COPILOT_AUTH_RESUMED') {{
        if (rejectNextResume) {{
          rejectNextResume = false;
          reply = {{ ok: false, error: 'copilot_auth_resume_rejected' }};
        }} else {{
          reply = {{ ok: true, type: 'COPILOT_AUTH_RESUMED_ACK', paused: false, resumed: true }};
        }}
      }} else if (payload.type === 'COPILOT_GET_STATE') {{
        reply = {{ ok: true, state: {{ run_id: 'run_1', outcome: 'halted_needs_human', halts: [] }} }};
      }}
      setTimeout(() => {{
        for (const fn of msgListeners) fn(reply);
      }}, 5);
    }},
    disconnect: () => {{}},
  }};
}}

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
      query: (q, cb) => cb([]),
      sendMessage: (id, m, cb) => {{ if (cb) cb(); }},
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
  // 1. Missing tab sender is rejected; subframe & idle-tab detections are ignored
  const missingTabResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ origin: 'https://portal.example.com' }}
  );
  const idleResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );

  // Activate run on tab 42
  bgCtx.__openadaptExtensionBackground.setActiveRunTab(42);
  const subframeResp = await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'mfa', signals: ['mfa_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 2, origin: 'https://ad.example.com' }}
  );

  // 2. Top-frame detection on active run tab 42 sends COPILOT_AUTH_PAUSED and records hostAcked=true
  const detectResp = await sendBg(
    {{
      type: 'AUTH_INTERSTITIAL_DETECTED',
      kind: mfaResult.kind,
      signals: mfaResult.signals,
    }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );

  const syncResp = await sendBg(
    {{ type: 'COPILOT_SYNC_STATE' }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 3. Host rejects first resume -> fail closed, authPause MUST remain active
  const rejectedResume = await sendBg(
    {{ type: 'COPILOT_AUTH_RESUME', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  const afterRejectedResume = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 4. Host acks second resume -> clears authPause for tab 42
  const acceptedResume = await sendBg(
    {{ type: 'COPILOT_AUTH_RESUME', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );
  const afterAcceptedResume = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  // 5. Per-tab auth state clears automatically on navigation (tabs.onUpdated)
  await sendBg(
    {{ type: 'AUTH_INTERSTITIAL_DETECTED', kind: 'signin', signals: ['password_input'] }},
    {{ tab: {{ id: 42 }}, frameId: 0, origin: 'https://portal.example.com' }}
  );
  tabUpdatedListeners[0](42, {{ status: 'loading', url: 'https://portal.example.com/dashboard' }});
  const afterNavClear = await sendBg(
    {{ type: 'COPILOT_GET_AUTH_STATE', tabId: 42 }},
    {{ url: 'chrome-extension://testext/sidepanel.html' }}
  );

  console.log(JSON.stringify({{
    valueReadCount,
    mfaResult,
    pwdResult,
    normalResult,
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
    nativeTypes: nativeMessages.map((m) => m.type),
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

    assert out["missingTabResp"]["ok"] is False
    assert out["idleResp"]["ignored"] is True
    assert out["subframeResp"]["ignored"] is True
    assert out["detectAuthPause"]["paused"] is True
    assert out["detectAuthPause"]["hostAcked"] is True
    assert out["detectAuthPause"]["mode"] == "run_paused"
    assert out["syncAuthPause"]["paused"] is True
    assert "AUTH" in out["badgeHistory"]
    # Fail-closed resume: host rejection must NOT clear pause state
    assert out["rejectedResume"]["ok"] is False
    assert out["stillPausedAfterReject"] is True
    # Host-acked resume clears pause state cleanly
    assert out["acceptedResume"]["ok"] is True
    assert out["acceptedResume"]["resumed"] is True
    assert out["afterAcceptedResume"] is None
    assert out["afterNavClear"] is None
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

    stdin_bytes = b"".join(
        [
            encode(
                {
                    "type": "COPILOT_AUTH_PAUSED",
                    "tab_id": 42,
                    "kind": "mfa",
                    "signals": ["mfa_input"],
                    "origin": "https://portal.example.com",
                }
            ),
            encode(
                {
                    "type": "COPILOT_AUTH_PAUSED",
                    "tab_id": -1,
                    "kind": "mfa",
                }
            ),
            encode(
                {
                    "type": "COPILOT_AUTH_RESUMED",
                    "tab_id": 42,
                    "kind": "mfa",
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
    assert len(replies) == 3
    assert replies[0]["ok"] is True
    assert replies[0]["type"] == "COPILOT_AUTH_PAUSED_ACK"
    assert replies[0]["paused"] is True
    assert replies[1]["ok"] is False
    assert replies[1]["error"] == "copilot_auth_pause_rejected"
    assert replies[2]["ok"] is True
    assert replies[2]["type"] == "COPILOT_AUTH_RESUMED_ACK"
    assert replies[2]["resumed"] is True

    auth_state = json.loads(
        (tmp_path / "copilot_auth_state.json").read_text(encoding="utf-8")
    )
    assert auth_state["paused"] is False
    assert auth_state["tab_id"] == 42
    assert auth_state["kind"] == "mfa"


def test_sidepanel_wires_auth_pause_and_popup_return_controls() -> None:
    html = (EXT_DIR / "sidepanel.html").read_text(encoding="utf-8")
    js = (EXT_DIR / "sidepanel.js").read_text(encoding="utf-8")

    assert 'id="auth-pause-container"' in html
    assert 'id="popup-banner-container"' in html
    assert "COPILOT_AUTH_RESUME" in js
    assert "COPILOT_RETURN_TO_PARENT" in js
    assert "I Signed In" in js
    assert "Return to Parent Tab" in js
