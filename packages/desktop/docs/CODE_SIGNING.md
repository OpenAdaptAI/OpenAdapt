# Code Signing Runbook (founder activation)

This is the step-by-step guide to move OpenAdapt Desktop installers from
**Beta / unsigned** to **signed and trusted**. It lists exactly what to
buy, which secrets to add, and what each public surface may then truthfully say.

Nothing here changes how the app is built until the secrets exist. The release
workflow already fails **open** to today's ad-hoc/unsigned behaviour when a
credential set is absent, and fails **closed** (the build errors) only when a
set is *partially* configured. You can therefore add one platform at a time.

## How signing is wired (read once)

- `scripts/native_signing.py preflight --platform <macos|windows|linux>` inspects
  the `native-release` environment secrets and emits two values to the workflow:
  - `mode` — the honest signing label (`adhoc`/`unsigned` with no secrets, or
    `developer-id-notarized`/`authenticode` once a complete set is present).
  - `method` — *how* a signed Windows artifact is produced (`pfx` vs
    `trusted-signing`); `pfx`/`adhoc`/`unsigned` otherwise.
- The `mode` is baked into every artifact **filename**:
  `OpenAdapt-Desktop-Beta-v<version>-<os>-<arch>-<signing>.<ext>`.
  This is the honesty mechanism — a download page or trust center can read the
  filename token and never overstate maturity. When you configure macOS
  Developer ID, the macOS asset name flips from `-adhoc-` to
  `-developer-id-notarized-` automatically; no page edit is required beyond the
  claim wording below.
- The launch-smoke test (`scripts/smoke_test_native_installer.py`) already
  installs, launches, and **verifies the signature** for the active mode:
  `codesign`/`spctl`/`stapler` on macOS, `Get-AuthenticodeSignature` on Windows.
  Signed builds add signature verification; unsigned builds keep today's checks.
  Signing is **not** yet a hard release gate, because the secrets do not exist.
- The macOS engine is a PyInstaller one-file sidecar. Developer ID jobs pass
  `APPLE_SIGNING_IDENTITY` into both PyInstaller and Tauri so the embedded
  Python libraries and final launcher share one Team ID under hardened runtime.
  Identity-less prereleases use `tauri.adhoc.conf.json` without hardened
  runtime. The installed-app smoke executes bundled Flow after Tauri's final
  signing pass; a bundle that is signed but cannot load its engine fails.

All secrets below live in the protected **`native-release`** GitHub Actions
environment (Settings → Environments → `native-release` → *Environment secrets*),
or via the CLI:

```bash
gh secret set APPLE_TEAM_ID --env native-release --repo OpenAdaptAI/openadapt-desktop
```

---

## 1. macOS — Developer ID + notarization ($99/yr)

**Buy:** [Apple Developer Program](https://developer.apple.com/programs/) —
**US$99 / year** (individual or organization). An organization membership needs
a D-U-N-S number and takes a few days to verify; the individual tier activates
immediately.

**Produce the certificate:**
1. In the Apple Developer portal, create a **Developer ID Application**
   certificate (not "Apple Distribution" — that is for the App Store).
2. Download it, open in Keychain Access, and **export** the certificate *with its
   private key* as a `.p12`, setting an export password.
3. Base64-encode it for the secret:
   `base64 -i DeveloperID.p12 | pbcopy` (macOS) — the encoded string is
   `APPLE_CERTIFICATE`.
4. Create an **app-specific password** at <https://account.apple.com> →
   Sign-In and Security → App-Specific Passwords. This is `APPLE_PASSWORD`
   (used only for notarization, not your Apple ID login password).
5. Read the **Team ID** from Membership details (10 characters) → `APPLE_TEAM_ID`.

**Add these six secrets** to the `native-release` environment:

| Secret | Value |
| --- | --- |
| `APPLE_CERTIFICATE` | base64 of the Developer ID Application `.p12` |
| `APPLE_CERTIFICATE_PASSWORD` | the `.p12` export password |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Org (TEAMID)` |
| `APPLE_ID` | your Apple Developer account email |
| `APPLE_PASSWORD` | the app-specific password from step 4 |
| `APPLE_TEAM_ID` | the 10-character Team ID |

On the next `desktop-v*` tag the macOS jobs import the cert into an ephemeral
keychain, build a **Developer ID signed** DMG, submit it to Apple's notary
service, staple the ticket, and the smoke test asserts `spctl` acceptance and a
stapled ticket. Assets ship as `-macos-arm64-developer-id-notarized-*.dmg`.

---

## 2. Windows — Authenticode

Since June 2023 the CA/Browser Forum requires the private key of every publicly
trusted OV/EV code-signing certificate to live on FIPS-140 hardware, so a
classic "download a `.pfx` and sign in CI" flow is no longer available for
public trust. There are two supported paths; **option A is recommended for a
startup** because it is the cheapest legitimate option and needs no hardware
token or CI HSM plumbing.

### Option A — Azure Trusted Signing (recommended, ~US$9.99/mo)

**Buy:** an Azure subscription + a **Trusted Signing** (a.k.a. *Azure Artifact
Signing*, formerly *Azure Code Signing*) account. **Basic plan ≈ US$9.99 /
month** for up to 5,000 signatures (then $0.005 each). Microsoft operates the
publicly trusted CA and mints a fresh, short-lived, timestamped certificate per
signature — instant SmartScreen reputation with no USB token.

**Eligibility note (important for a young startup):** organization onboarding
historically required the legal entity to be **3+ years old**. If the company is
younger, sign up under the **individual developer** tier (identity-validated via
Microsoft Entra Verified ID) until the org-onboarding path is available to newer
entities. Confirm current eligibility on the
[Trusted Signing docs](https://learn.microsoft.com/azure/trusted-signing/)
before purchasing.

**Set up:**
1. Create a Trusted Signing account and a **certificate profile** (choose
   *Public Trust*). Note the account **endpoint** region URI (e.g.
   `https://eus.codesigning.azure.net/`), the **account name**, and the
   **certificate profile name**.
2. Create an Entra **service principal** (app registration + client secret) and
   grant it the **Trusted Signing Certificate Profile Signer** role on the
   account. Record its tenant ID, client ID, and client secret.

**Add these six secrets** to `native-release`:

| Secret | Value |
| --- | --- |
| `AZURE_TENANT_ID` | service principal tenant ID |
| `AZURE_CLIENT_ID` | service principal application (client) ID |
| `AZURE_CLIENT_SECRET` | service principal client secret |
| `TRUSTED_SIGNING_ENDPOINT` | account region URI, e.g. `https://eus.codesigning.azure.net/` |
| `TRUSTED_SIGNING_ACCOUNT` | Trusted Signing account name |
| `TRUSTED_SIGNING_CERTIFICATE_PROFILE` | certificate profile name |

The workflow then installs `trusted-signing-cli`, points Tauri's Windows
`signCommand` at it, and produces a publicly trusted, timestamped Authenticode
MSI/NSIS. The smoke test asserts the signature status is **Valid** (it does not
pin a thumbprint, because Trusted Signing rotates certificates per signature).
Assets ship as `-windows-x86_64-authenticode-*`.

### Option B — importable PFX / EV certificate (only if you already have one)

Use this **only** for an enterprise-internal certificate whose `.pfx` you
control, or a legacy exportable certificate. For public trust, EV certificates
run **~US$249–US$700 / year** (Sectigo EV ≈ $279/yr via resellers; DigiCert EV
≈ $560–$700/yr) and require FIPS hardware — a USB token (breaks unattended CI)
or a cloud HSM such as **DigiCert KeyLocker** (adds cost, and the key is *not*
exportable to a `.pfx`, so it is **not** compatible with the `pfx` path below and
would need a KeyLocker `signtool` integration instead). Given the price and the
hardware constraint, prefer Option A.

If you do have an importable `.pfx`:

| Secret | Value |
| --- | --- |
| `WINDOWS_CERTIFICATE` | base64 of the code-signing `.pfx` |
| `WINDOWS_CERTIFICATE_PASSWORD` | the `.pfx` password |
| `WINDOWS_CERTIFICATE_THUMBPRINT` | 40-hex SHA-1 thumbprint of the signing cert |

> Configure **either** the Azure set **or** the PFX set, never both — the
> preflight rejects an ambiguous mix.

---

## 3. Linux — GPG (free, deferred by design)

GPG is free (`gpg --full-generate-key`). Linux package signing is **intentionally
disabled** in `native_signing.py` today: AppImage does not self-verify, and DEB
signing is a separate repository-metadata boundary, so shipping a signature
without a *pinned external validator and an authenticated public-key channel*
would be signing-theatre. The secret names are reserved
(`LINUX_GPG_PRIVATE_KEY`, `LINUX_GPG_KEY_ID`, `LINUX_GPG_PASSPHRASE`,
`LINUX_GPG_FINGERPRINT`) and the preflight fails closed if any are set, to
prevent accidental half-signing.

**To activate later** (a deliberate follow-up, not part of this readiness step):
publish the public key fingerprint over an authenticated channel (e.g. the
website + `SHA256SUMS` attestation), add a pinned AppImage signature validator to
the smoke test, and add `gpg` to the Linux `SIGNING_MODES` in
`scripts/native_release.py`. Until then Linux ships `unsigned` **plus** GitHub
build provenance (attestation over `SHA256SUMS`), which is the honest state.

---

## What each surface can truthfully claim

Only claim a state after the corresponding secret set is live and a signed
release has actually built. The artifact filename token is the source of truth.

| Surface | With no secrets (today) | After macOS Developer ID | After Windows Authenticode | After Linux GPG |
| --- | --- | --- | --- | --- |
| /download page | "Beta. macOS builds are ad-hoc signed; Windows/Linux are unsigned." | "**Signed and notarized by Apple** on macOS — opens without a Gatekeeper override." | "**Signed with a trusted Authenticode certificate** on Windows — no SmartScreen 'unknown publisher' warning." | "Linux packages are **GPG-signed**; verify against our published key." |
| Trust center | "Ad-hoc/unsigned; verify via `SHA256SUMS` + GitHub attestation." | Add: "macOS DMGs pass Apple notarization (`spctl` accepted, ticket stapled)." | Add: "Windows installers carry a valid, timestamped Authenticode signature." | Add: "Linux packages carry a detached GPG signature; fingerprint published." |
| README honesty note | "Native packages remain Beta and unsigned." | Update the note per platform as each lands. |  |  |

The README signing note and `docs/BETA_NATIVE_INSTALLERS.md` both point
here; update their per-platform wording when each platform's first **signed**
release ships (not when the secrets are merely added). The download page needs no
code change to detect signing — it reads the `-<signing>-` token in the asset
name — only the human-readable claim wording changes.

## Verify a signed release locally

```bash
# Bytes match the attested manifest
sha256sum -c SHA256SUMS
gh attestation verify OpenAdapt-Desktop-Beta-* --repo OpenAdaptAI/openadapt-desktop

# macOS: notarization accepted + ticket stapled
spctl --assess --type open --context context:primary-signature -v <asset>.dmg
xcrun stapler validate <asset>.dmg

# Windows (PowerShell): valid, publicly trusted Authenticode chain
Get-AuthenticodeSignature <asset>.msi | Format-List Status, SignerCertificate
```
