# Code Signing Runbook (founder activation)

This guide adds platform trust to OpenAdapt Desktop release candidates. It
lists which secrets to add and what each public surface can then truthfully
say. Code signing does not create a Production admission.

Native releases ship without paid signing today: macOS DMGs are ad-hoc signed
and Windows installers are unsigned, and every file name, the release notes,
the website manifest, and the release channel say so. Windows signing comes
from SignPath, which signs open source projects free of charge through the
SignPath Foundation. Every credential set is complete-or-absent; a partial set
fails the build.

## How signing is wired (read once)

- `scripts/native_signing.py preflight --platform <macos|windows|linux>` inspects
  the `native-release` environment secrets and emits two values to the workflow:
  - `mode` — the honest signing label (`adhoc`/`unsigned` for local and CI
    builds, or `developer-id-notarized`/`authenticode` for native releases).
  - `method` — *how* a signed artifact is produced (`signpath` on Windows,
    `developer-id` on macOS); `adhoc`/`unsigned` otherwise.
- The `mode` is baked into every artifact **filename**:
  `OpenAdapt-Desktop-Candidate-v<version>-<os>-<arch>-<signing>.<ext>`.
  This is the honesty mechanism — a download page or trust center can read the
  filename token and never overstate maturity. When you configure macOS
  Developer ID, the macOS asset name flips from `-adhoc-` to
  `-developer-id-notarized-` automatically; no page edit is required beyond the
  claim wording below.
- The launch-smoke test (`scripts/smoke_test_native_installer.py`) already
  installs, launches, and **verifies the signature** for the active mode:
  `codesign`/`spctl`/`stapler` on macOS, `Get-AuthenticodeSignature` on Windows.
  With SignPath, the MSI is checked in full (installer and installed program);
  the NSIS setup is checked as an installer only
  (`--installer-only-signature`), because SignPath cannot sign inside it.
- The macOS engine is a PyInstaller one-file sidecar. Developer ID jobs pass
  `APPLE_SIGNING_IDENTITY` into both PyInstaller and Tauri so the embedded
  Python libraries and final launcher share one Team ID under hardened runtime.
  Identity-less CI builds use `tauri.adhoc.conf.json` without hardened runtime.
  The installed-app smoke executes bundled Flow after Tauri's final
  signing pass; a bundle that is signed but cannot load its engine fails.

All secrets below live in the protected **`native-release`** GitHub Actions
environment (Settings → Environments → `native-release` → *Environment secrets*),
or via the CLI:

```bash
gh secret set SIGNPATH_API_TOKEN --env native-release --repo OpenAdaptAI/OpenAdapt
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

## 2. Windows — Authenticode through SignPath (free for open source)

Apply to the [SignPath Foundation](https://signpath.org/) for an open source
code signing certificate. Every component OpenAdapt Desktop ships must be under
an OSI-approved license; the managed FFmpeg runtime is an LGPL-2.1-or-later
build (`--disable-gpl --disable-nonfree`), downloaded on first use rather than
bundled.

In SignPath:

1. Create the project `openadapt-desktop` and link the predefined GitHub.com
   trusted build system to it. Install the SignPath GitHub App on
   OpenAdaptAI/OpenAdapt so SignPath can read the build's audit data.
2. Add the artifact configuration in
   `.signpath/artifact-configurations/openadapt-desktop-installers.xml` (at the
   monorepo root) and make it the project's default. GitHub uploads the
   installers as one ZIP, so one request signs a release: it deep-signs the MSI
   (every PE file inside it, then the MSI) and signs the NSIS `*-setup.exe`.
3. Create the signing policy `release-signing`. Its pipeline policy is
   `.signpath/policies/openadapt-desktop/release-signing.yml` at the monorepo
   root: GitHub-hosted runners only, no re-runs. The workflow itself runs only
   from `main`, and the `native-release` environment deploys only from `main`.
4. Create an API token for a user with submitter rights on that policy.

Then add these four secrets to the `native-release` environment of
OpenAdaptAI/OpenAdapt (all four, or none):

| Secret | Value |
| --- | --- |
| `SIGNPATH_API_TOKEN` | the submitter's API token |
| `SIGNPATH_ORGANIZATION_ID` | the SignPath organization ID |
| `SIGNPATH_PROJECT_SLUG` | `openadapt-desktop` |
| `SIGNPATH_SIGNING_POLICY_SLUG` | `release-signing` |

The Windows job then builds the installers, uploads them, submits one signing
request with `signpath/github-action-submit-signing-request` (pinned by commit),
waits for it, checks that each returned installer carries a valid Authenticode
signature, and smoke-tests the signed files: the MSI install must yield a
signed `openadapt-desktop.exe`, and the NSIS setup must be signed itself. It
publishes them as `authenticode`. Without the secrets it publishes the same
installers as `unsigned`.

SignPath cannot open an NSIS archive, so the program files the NSIS setup
installs and its uninstaller are not signed. The MSI is the fully signed
Windows download. Signing the NSIS payload would need a second request before
Tauri bundles, with a second approval per release.

## 3. Linux — exact-byte GitHub attestation

DEB and AppImage do not share one native trust format. A detached GPG signature
would also require a separate authenticated public-key channel. The production
release therefore uses GitHub's OIDC artifact attestation as the Linux trust
boundary. The release job attests the complete `SHA256SUMS` subject set. It then
runs `gh attestation verify` on the checksummed provenance file and requires the
signed subjects to equal the complete release inventory before upload.

This state is named `github-attested`. It is not called native-signed. It needs
no founder-managed signing secret. GitHub issues the short-lived OIDC identity
for the pinned release workflow. The verifier also binds the reviewed main
commit, run ID, run attempt, and GitHub-hosted runner.
`SHA256SUMS` and the website manifest bind the same exact bytes for offline hash
checks.

This workflow control is not sufficient without repository controls. Configure
a no-bypass pull-request ruleset for `main`. Configure immutable creation rules
for both `v*` and `desktop-v*` tags before a release. Only the engine release
workflow can create `v*`. Only the explicitly dispatched native release
workflow can create `desktop-v*`. Neither identity can update or delete a tag. The historical
`desktop-v0.15.0` prerelease is ad-hoc/unsigned and does not satisfy this trust
contract.

---

## What each surface can truthfully claim

Only claim a state after the corresponding secret set is live and a trusted
release has actually built. The artifact filename token is the source of truth.

| Surface | With no secrets | After macOS Developer ID | After Windows Authenticode | Linux OIDC attestation |
| --- | --- | --- | --- | --- |
| /download page | "Unsigned: macOS asks you to confirm on first open; Windows SmartScreen warns before install." | "**Signed and notarized by Apple** on macOS — opens without a Gatekeeper override." | "The Windows **MSI and the program it installs are signed** with a trusted Authenticode certificate (SignPath)." | "Linux DEB and AppImage downloads have **GitHub OIDC attestations over the exact bytes**." |
| Trust center | Every artifact's file name and the release channel state its signing mode. | Add: "macOS DMGs pass Apple notarization (`spctl` accepted, ticket stapled)." | Add: "Windows installers carry a valid, timestamped Authenticode signature." | Add: "Linux packages pass `gh attestation verify` against this repository." |
| README Limits and COMPLIANCE.md release state | Describe only the latest published artifact set. | Update the note after the first trusted release. | Update the note after the first trusted release. | Update the note after the first trusted release. |

The README Limits, `docs/COMPLIANCE.md`, and
`docs/RELEASE_CANDIDATE_INSTALLERS.md` describe the signing state. Update their
per-platform wording when each platform's first **trusted** release ships (not
when the secrets are merely added). The download page needs no
code change to detect signing — it reads the `-<signing>-` token in the asset
name — only the human-readable claim wording changes.

## Verify a signed release locally

```bash
# First authenticate the checksum manifest and its reviewed-main workflow.
gh attestation verify SHA256SUMS \
  --repo OpenAdaptAI/OpenAdapt \
  --cert-identity "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/desktop-native-release.yml@refs/heads/main" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --deny-self-hosted-runners

# Then check every digest and reject a missing, extra, linked, or non-regular file.
sha256sum -c SHA256SUMS
python verify-openadapt-native-release.py \
  --directory . \
  --manifest SHA256SUMS

# macOS: notarization accepted + ticket stapled
spctl --assess --type open --context context:primary-signature -v <asset>.dmg
xcrun stapler validate <asset>.dmg

# Windows (PowerShell): valid, timestamped, publicly trusted Authenticode chain
Get-AuthenticodeSignature <asset>.msi |
  Format-List Status, SignerCertificate, TimeStamperCertificate
```
