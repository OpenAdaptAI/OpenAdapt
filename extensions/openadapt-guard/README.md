# OpenAdapt Guard (Chrome Extension — Manifest V3)

Verify web form saves with local read-back checks and offline-verifiable Ed25519 signed receipts. Zero telemetry, zero remote code, zero data egress.

## What It Does

1. **Arm on demand (`activeTab`)**: Click the OpenAdapt Guard toolbar icon and click **Arm Guard on This Tab**. No broad `<all_urls>` host permissions are requested.
2. **Fail-closed privacy capture**: On form submit, captures intended values for visible non-sensitive fields only — automatically excluding `input[type="password"]`, `input[type="hidden"]`, `input[type="file"]`, credit-card `autocomplete="cc-*"` fields, and sensitive token/secret/CSRF field names.
3. **Deterministic read-back check**: Waits for the page DOM to settle, reads back visible persisted values and visible error banners (`[role="alert"]`, `.error`, `.alert-danger`, etc.), and computes a deterministic `PASS`, `MISMATCH`, or `ERROR_BANNER` verdict.
4. **Local Ed25519 signed receipts**: Signs the verification payload locally using WebCrypto Ed25519 (keypair persisted only in `chrome.storage.local`). Verify the signature in one click or export the receipt JSON for offline auditing.

## 2-Click Load Unpacked (Chrome)

1. Open `chrome://extensions` and toggle **Developer mode** on (top right).
2. Click **Load unpacked** and select this directory (`extensions/openadapt-guard`).

## Run Automated Tests

Requires Node 22+ (built-in `node --test` and WebCrypto Ed25519):

```bash
node --test extensions/openadapt-guard/tests/guard.test.mjs
```

---

## Chrome Web Store Permission Justifications

| Permission | Plain-English Justification |
| --- | --- |
| `activeTab` | Grants temporary access to the single tab the user is currently viewing only after the user clicks the OpenAdapt Guard toolbar icon and clicks **Arm Guard on This Tab**. No background access to other tabs or browsing history. |
| `scripting` | Injects the local, bundled `content.js` form-verification script into the active tab when the user clicks **Arm Guard on This Tab**. No remote code is ever downloaded or executed. |
| `storage` | Persists the user's locally generated Ed25519 signing keypair and the latest sanitized form verification receipt in `chrome.storage.local` on the user's own device. |

**Host Permissions:** None (`host_permissions` is intentionally omitted).

---

## Privacy Policy Draft

**Effective Date:** 2026-11-22

OpenAdapt Guard is a local-only browser extension built by OpenAdaptAI.

1. **Zero Data Collection & Zero Egress:** OpenAdapt Guard does not transmit, sell, or share any user data, form values, browsing history, or telemetry to OpenAdaptAI or any third party. The extension makes zero network requests.
2. **Fail-Closed Sensitive Field Exclusion:** Password inputs (`type="password"`), hidden inputs (`type="hidden"`), file inputs, payment/credit-card autocomplete fields, and fields whose names or IDs match common secret/token/CSRF patterns are never read or stored.
3. **Local-Only Storage:** The locally generated Ed25519 keypair and the most recent sanitized verification receipt are stored solely in Chrome's local extension storage (`chrome.storage.local`) on your device. Removing the extension or clearing extension storage permanently deletes this data.
4. **User-Controlled Export:** Signed receipt JSON files are exported to your local disk only when you explicitly click **Export Signed Receipt JSON**.
5. **Contact:** Questions regarding this privacy policy can be directed to `contact@openadapt.ai` or `https://github.com/OpenAdaptAI/OpenAdapt/issues`.
