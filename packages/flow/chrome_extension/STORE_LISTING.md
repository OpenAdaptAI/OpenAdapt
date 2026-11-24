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

### Short Description (129 / 132 characters)

```text
Local browser co-pilot for OpenAdapt: side-panel control, popup return, sign-in capture pause, and verified receipts. Local-only.
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
  password, passkey, or one-time code on the active run tab, OpenAdapt Co-Pilot
  pauses browser evidence and network capture on that tab, shows a sign-in
  notice in the side panel, and signals the local host. It never reads or
  stores credentials—sign in normally with your password manager or
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
capture after manual sign-in, and inspect verified receipts.

### `storage`

Required to persist transient co-pilot session state (such as active child
pop-up tab mappings and per-tab sign-in pause state) across extension restarts
without storing any credentials or sending data off-device.

### `nativeMessaging`

Required to exchange workflow state, operator conflict decisions, auth
pause/resume acknowledgements, and locally redacted workflow evidence directly
with the user's locally installed OpenAdapt desktop runtime
(`com.openadapt.extension`). No network or cloud endpoints are used.

### `webRequest`

Required in read-only observation mode (non-blocking) only while the user has
explicitly started a local recording session. It observes state-changing form
submission requests (`POST`/`PUT`/`PATCH`/`DELETE`) so the local runtime on the
same computer can verify whether a portal save succeeded. Non-secret request
fields (form-data and JSON payloads) and response status codes are sent **only
to the user's local OpenAdapt runtime on the same computer**. Secret-looking
keys, passwords, authorization headers, cookies, tokens, and query secrets are
redacted locally before leaving the extension; response bodies are never
captured, and nothing is sent to remote or cloud servers.

### Host Permissions (`<all_urls>`)

Users run structured form workflows across a wide variety of self-hosted,
internal, and third-party web portals (such as carrier quoting portals, EHRs,
and enterprise web apps) whose domains cannot be enumerated in advance. Outside
an active user-initiated recording or co-pilot run, the extension performs no
background page scanning, popup tracking, or network capture. Host access is
used solely during an active run or user-invoked side panel session to render
field highlights, detect sign-in/MFA walls on the active run's tab, and relay
locally redacted workflow evidence to the local desktop runtime.

### Remote Code Certification

- **Are you using remote code?** **No.** All JavaScript, CSS, and HTML assets
  are bundled inside the extension package. No remote scripts, `eval()`, or
  dynamically fetched code are executed.

---

## 4. Privacy Policy (Zero Cloud Egress)

**Hosted Privacy Policy URL:** `https://openadapt.ai/privacy-policy`
**Effective Date:** 2026-11-22

1. **Local-Only Architecture:** OpenAdapt Co-Pilot operates exclusively on the
   user's local machine. It communicates only with the user's locally installed
   OpenAdapt desktop application via Chrome Native Messaging
   (`com.openadapt.extension`). It does **not** transmit browsing history, form
   values, screenshots, or telemetry to OpenAdapt servers or any third-party
   cloud service.
2. **What Is Sent Locally During Active Recording:** Only when the user
   explicitly starts a local recording session, non-secret form values and
   non-secret request payloads (such as form-data or JSON fields) from the
   recorded workflow are sent **exclusively to the user's local OpenAdapt
   runtime on the same computer**. Outside an active recording or co-pilot run,
   no page values or network requests are captured.
3. **Zero Credential Capture:** OpenAdapt Co-Pilot never reads, logs, stores,
   or transmits passwords, passkeys, security tokens, cookies, or authorization
   headers, and detects and redacts common one-time-code fields (including
   `verification_code`, `sms_code`, `smsCode`, `totp`, `mfa_code`, `2fa`,
   `one-time-code`, `otp`, `pin`, and `code`). Secret-looking fields, query
   parameters, headers, and request keys are redacted locally before leaving
   the extension, and response bodies are never captured. When a sign-in or
   multi-factor authentication screen is detected on the active run's tab, the
   extension pauses browser evidence and network capture on that tab, displays
   a sign-in notice in the side panel, and sends a structural pause signal (zero
   credentials) to the local host until the user signs in and clicks Resume.
4. **No Data Sale or Advertising:** OpenAdapt does not collect, sell, rent, or
   transfer user data to third parties for advertising, analytics, or credit
   decisions.
5. **Support & Privacy Contact:** `hello@openadapt.ai` (Hosted policy:
   `https://openadapt.ai/privacy-policy`).

---

## 5. Pre-Publish Verification Checklist

- [ ] `manifest.json` uses `"manifest_version": 3`, `"minimum_chrome_version": "116"`, and `description` <= 132 characters.
- [ ] PNG icons exist at `icons/icon16.png` (16x16), `icons/icon48.png` (48x48), and `icons/icon128.png` (128x128).
- [ ] `python3 scripts/package_chrome_extension.py --out dist/openadapt-copilot-cws.zip` succeeds with zero validation errors.
- [ ] Zip bundle excludes `STORE_LISTING.md`, `README.md`, `native_host/`, tests, and hidden files.
- [ ] Automated test suite (`packages/flow/tests/test_chrome_extension_cws.py`) passes.
- [ ] Store screenshots (1280x800) and promotional tile (440x280) uploaded in the Chrome Web Store Developer Dashboard.
