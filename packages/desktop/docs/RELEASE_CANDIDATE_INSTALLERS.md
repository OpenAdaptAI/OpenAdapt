<!-- installer-release -->

# Native Release Candidates

OpenAdapt Desktop native packages are **unadmitted candidates** for the
installed authoring, teaching, and local-pairing companion. They bundle and start the
Python sidecar, connect the Tauri/React cockpit to it over local JSON-lines IPC,
and register the `openadapt://` operating-system handler. The handler accepts
only the fixed `openadapt://connect` action and forwards it to the sidecar's
strict, transactional pairing flow.

The canonical compiler and governed runtime remain in `openadapt-flow`. Each
native installer freezes the exact `openadapt-flow[browser,console]==2.4.0`
runtime and its `playwright==1.61.0` browser automation dependency into the
Desktop sidecar. The `console` extra is what lets an installed application
serve the attended decision console the mobile decision portal relays; the
`browser` extra carries the frozen Playwright driver.
Compile, replay, run, and teach therefore work without a separate Python,
`openadapt-flow`, or `playwright` installation on `PATH`. Desktop starts with no
browser download. The first browser workflow downloads the Chromium revision
pinned by the bundled Playwright runtime unless an approved browser cache is
pre-provisioned; native desktop, RDP, and Citrix workflows never enter that
setup path.

The [mobile decision portal](DECISION_PORTAL.md) shows the current phone
interface, one-use pairing flow, local evidence boundary, hosted outbound lane,
runner revalidation, and typed result contract.

Desktop keeps separately licensed media and vision components outside its MIT
installer. On first use, it downloads the exact release-reviewed component for
the current platform, verifies the pinned URL, byte count, and SHA-256, installs
it into a versioned local cache, and re-verifies every extracted file before
loading it. This applies to the managed FFmpeg 8.1.2 runtime used for capture
encoding and the RapidOCR 1.4.4/OpenCV 5.0.0.93 runtime used for visual
resolution. A partial or drifted download is never activated; rerunning the
operation retries it. Enterprise images can pre-provision the same exact cache
without changing the runtime contract. Developer ID builds carry the narrow
macOS library-validation entitlement required to load that independently
signed, hash-verified OpenCV extension; the manifest and full-file cache audit
remain the admission boundary.

Each Desktop version is one immutable `openadapt-desktop-vX.Y.Z` release in the
OpenAdapt monorepo. It carries the Python wheel and sdist (the same bytes as on
PyPI), these native installers, and the release's own attested channel. The
native version comes from `package.json`, `src-tauri/Cargo.toml`, and
`src-tauri/tauri.conf.json`, with matching entries in both lockfiles; a Desktop
release pull request sets them and the Python version together
(`scripts/prepare_release.py`). When it merges, the Desktop release workflow
builds and smoke-tests every package, attests the exact bytes, attaches
everything to a draft, publishes the wheel and sdist to PyPI, and publishes the
draft once. If any build fails, nothing is published. OpenAdapt's releases are
immutable, so a published release is never edited; a newer release supersedes
an older one by version. This transaction doesn't change the admission-driven
Production channel. The release policy is in
[RELEASES.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/desktop/RELEASES.md).

## Artifact labels

Every new filename includes `Candidate`, the native version, operating system,
architecture, and signing state. The matrix and the signing each platform can
carry:

| Platform | Architectures | Packages | Signing labels |
| --- | --- | --- | --- |
| macOS | Apple Silicon (`arm64`), Intel (`x86_64`) | DMG | `adhoc` today; `developer-id-notarized` once credentials exist |
| Windows | `x86_64` | MSI and NSIS setup executable | `unsigned`, or `authenticode` once SignPath is configured |
| Linux | `x86_64` | DEB and AppImage | `github-attested` exact bytes |

The "Signing in this release" section above states which labels this release
uses. With Authenticode, SignPath signs the MSI and every program file in it,
and the NSIS setup itself; the files the NSIS setup installs are not signed. The build workflow installs
and uninstalls every package on clean hosted runners and verifies the
executable architecture and the declared signing policy: the exact Developer ID
authority and Team ID on macOS when used, and a valid signer and timestamp on
each Windows installer when signed. The workflow launches every installed
application and requires the process to survive a 20-second startup window
(catching launch panics before they ship), and stages the exact tested bytes.
The repository test matrix also checks that only the `openadapt` scheme is
registered and that its handoff is fixed to `connect_uri` without a shell or
general navigation escape hatch. These checks do not replace qualification of
a complete real workflow.

## Integrity and provenance

Release jobs stage the exact post-signing, smoke-tested files and scan that
assembled installer set with Syft to publish a machine-readable CycloneDX JSON
software bill of materials (SBOM). The workflow refuses an empty or malformed
SBOM, includes it and the public build-provenance identity in the sorted
`SHA256SUMS` manifest, verifies the exact inventory, and creates a GitHub
artifact attestation over every named file. It also attests `SHA256SUMS` itself.
The release verifier requires the signed subjects to equal that inventory. It
binds the exact reviewed-main workflow, source commit, published engine tag and
release, run ID, run attempt, and GitHub-hosted runner. Consumers must
authenticate `SHA256SUMS` before they trust its digests:

```bash
gh attestation verify SHA256SUMS \
  --repo OpenAdaptAI/OpenAdapt \
  --cert-identity "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/desktop-release.yml@refs/heads/main" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --deny-self-hosted-runners
sha256sum -c SHA256SUMS
python verify-openadapt-native-release.py --directory . --manifest SHA256SUMS
```

The final helper refuses missing, extra, linked, non-regular, duplicate, or
digest-mismatched files. Each release also carries its own attested
`openadapt-desktop-channel.json`. It binds this release's `SHA256SUMS` and
provenance by hash, states every platform's signing mode, and names the
previous release's channel by hash. Pass it with `--channel` (and the last
channel you accepted with `--previous-channel`) to authenticate the whole chain
before trusting any digest. A download service must select the newest valid
`openadapt-desktop-v*` release this way, never from mutable release-note text.
After a native release succeeds, its channel is the authority for that version.

An attestation binds bytes to a build identity; it does not establish that the
software is secure or functionally complete.

## Signing credentials

The protected `native-release` environment of OpenAdaptAI/OpenAdapt holds the
signing credentials. Every set is complete-or-absent: a partial set fails the
build, and an absent set ships the platform's unsigned label instead.

- Windows Authenticode through SignPath (free for open source projects through
  the SignPath Foundation): `SIGNPATH_API_TOKEN`, `SIGNPATH_ORGANIZATION_ID`,
  `SIGNPATH_PROJECT_SLUG` (`openadapt-desktop`), and
  `SIGNPATH_SIGNING_POLICY_SLUG` (`release-signing`). The pipeline policy is in
  `.signpath/policies/openadapt-desktop/release-signing.yml` at the monorepo
  root.
- macOS Developer ID and notarization, if it is ever configured:
  `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`,
  `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID`. Until then macOS builds are
  ad-hoc signed.
- Linux uses the GitHub OIDC exact-byte attestation above. It has no secret and
  is not described as native-signed.

The updater stays disabled until its independent public/private signing-key
lifecycle and recovery procedure are established.

The signing runbook lists the secrets to add and the claims each public surface
can make. See [CODE_SIGNING.md](CODE_SIGNING.md).
