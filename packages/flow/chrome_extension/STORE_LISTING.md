# Chrome Web Store Submission Package — OpenAdapt Co-Pilot

This document contains the store listing copy, single-purpose statement,
per-permission justifications, and local-only privacy policy required for
submitting **OpenAdapt Co-Pilot** to the Chrome Web Store.

Build the uploadable `.zip` bundle with:

```bash
python3 scripts/package_chrome_extension.py --out dist/openadapt-copilot-cws.zip
```

---

## 1. Store Listing Copy (User-Facing)

### Name

`OpenAdapt Co-Pilot`

### Short Description (128 / 132 characters)

```text
Local browser co-pilot for OpenAdapt: side-panel run control, popup return, MFA pause, and verified receipts. Zero cloud egress.
```

### Detailed Description

OpenAdapt Co-Pilot sits in Chrome's side panel alongside your work tabs to help
you complete repetitive multi-step web forms accurately while keeping you in
charge of every decision.

What it helps you do:

- **See live progress as you work:** Track auto-filled fields, active steps,
  and completed workflows directly in the Chrome side panel without switching
  windows.
- **Review conflicts before anything changes:** Whenever a portal pre-fills a
  different value than your local dossier or requires a missing field, the
  co-pilot pauses, shows both values side by side, and lets you approve,
  override, or skip with one click.
- **Handle pop-up estimators smoothly:** When a portal opens a separate
  estimator or calculator window, OpenAdapt tracks the child window and returns
  focus cleanly to your parent tab when you finish or click Return.
- **Keep sign-in and 2FA completely yours:** Whenever a site asks for a
  password, passkey, or one-time code, OpenAdapt pauses automatically and never
  reads or stores credentials. Sign in normally with your password manager or
  authenticator, then click Resume.
- **Verify every finished run:** View a tamper-evident local receipt summarizing
  extracted outputs once a workflow completes.

Built for local-first privacy:

- All data stays on your computer and communicates only with your locally
  installed OpenAdapt desktop runtime.
- No cloud servers, no analytics trackers, no ads, and no credential storage.

### Category & Language

- **Category:** Productivity
- **Primary Language:** English (United States)

---

## 2. Single Purpose Statement (Privacy Practices Tab)

> OpenAdapt Co-Pilot assists the user in completing, reviewing, and verifying
> structured browser form workflows through Chrome's side panel while
> communicating exclusively with the user's locally installed OpenAdapt desktop
> runtime.

---

## 3. Per-Permission Justifications (Privacy Practices Tab)

### `sidePanel`

Required to render the OpenAdapt Co-Pilot side panel alongside the user's active
tab so the user can monitor workflow progress, resolve prefill conflicts, resume
after manual sign-in, and inspect verified receipts.

### `activeTab`

Required to inspect and highlight form fields on the tab the user is actively
working on when the user invokes the co-pilot.

### `tabs`

Required to track parent/child pop-up windows (such as external estimators opened
from a quoting workflow), return focus to the parent tab when a child pop-up
closes, and synchronize field highlights with open web tabs.

### `scripting`

Required to highlight fields requiring operator review and verify form element
states inside the active workflow tab.

### `storage`

Required to persist transient co-pilot session state (such as active child
pop-up tab mappings and sign-in pause state) across extension restarts without
storing any credentials or sending data off-device.

### `nativeMessaging`

Required to exchange workflow state, operator conflict decisions, and redacted
local workflow evidence directly with the user's locally installed OpenAdapt
desktop runtime (`com.openadapt.extension`). No network or cloud endpoints are
used.

### `webRequest`

Required in read-only observation mode (non-blocking) to record redacted
metadata about form-submission network responses during an active user-initiated
recording session so the local runtime can verify whether a portal save
succeeded. Request bodies, headers, cookies, tokens, and query secrets are
stripped locally before leaving the extension.

### Host Permissions (`<all_urls>`)

Users run structured form workflows across a wide variety of self-hosted,
internal, and third-party web portals (such as carrier quoting portals, EHRs,
and enterprise web apps) whose domains cannot be enumerated in advance. Host
access is used solely to render field highlights, detect sign-in/MFA walls so
the co-pilot can pause safely, and capture user-initiated workflow evidence for
the local desktop runtime.

### Remote Code Certification

- **Are you using remote code?** **No.** All JavaScript, CSS, and HTML assets
  are bundled inside the extension package. No remote scripts, `eval()`, or
  dynamically fetched code are executed.

---

## 4. Privacy Policy (Zero Cloud Egress)

**Effective Date:** 2026-11-22

1. **Local-Only Architecture:** OpenAdapt Co-Pilot operates exclusively on the
   user's local machine. It communicates only with the user's locally installed
   OpenAdapt desktop application via Chrome Native Messaging. It does **not**
   transmit browsing history, form inputs, screenshots, or telemetry to
   OpenAdapt servers or any third-party cloud service.
2. **Zero Credential Capture:** OpenAdapt Co-Pilot never reads, logs, stores,
   or transmits passwords, passkeys, security tokens, cookies, or one-time MFA
   codes. When a sign-in or multi-factor authentication screen is detected, the
   co-pilot automatically pauses so the user can authenticate directly in their
   own Chrome profile.
3. **Redacted Local Evidence:** During an explicit user-started recording
   session, network response metadata is redacted locally (stripping query
   strings, authorization headers, cookies, and response bodies) before being
   passed to the local OpenAdapt runtime.
4. **No Data Sale or Advertising:** OpenAdapt does not collect, sell, rent, or
   transfer user data to third parties for advertising, analytics, or credit
   decisions.
5. **Contact:** For privacy questions, contact `privacy@openadapt.ai`.

---

## 5. Pre-Publish Verification Checklist

- [ ] `manifest.json` uses `"manifest_version": 3`, `"minimum_chrome_version": "116"`, and `description` <= 132 characters.
- [ ] PNG icons exist at `icons/icon16.png` (16x16), `icons/icon48.png` (48x48), and `icons/icon128.png` (128x128).
- [ ] `python3 scripts/package_chrome_extension.py --out dist/openadapt-copilot-cws.zip` succeeds with zero validation errors.
- [ ] Zip bundle excludes `STORE_LISTING.md`, `README.md`, `native_host/`, tests, and hidden files.
- [ ] Automated test suite (`packages/flow/tests/test_chrome_extension_cws.py`) passes.
- [ ] Store screenshots (1280x800) and promotional tile (440x280) uploaded in the Chrome Web Store Developer Dashboard.
