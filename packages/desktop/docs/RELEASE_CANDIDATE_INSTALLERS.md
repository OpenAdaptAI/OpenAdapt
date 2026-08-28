<!-- installer-release -->

# Native Release Candidates

OpenAdapt Desktop native packages are **unadmitted candidates** for the
installed authoring, teaching, and local-pairing companion. They bundle and start the
Python sidecar, connect the Tauri/React cockpit to it over local JSON-lines IPC,
and register the `openadapt://` operating-system handler. The handler accepts
only the fixed `openadapt://connect` action and forwards it to the sidecar's
strict, transactional pairing flow.

The canonical compiler and governed runtime remain in `openadapt-flow`. Each
native installer freezes the exact `openadapt-flow[browser,console]==1.31.0`
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

Native releases use a distinct `desktop-vX.Y.Z` tag and prerelease channel. The
native version comes from `package.json`, `src-tauri/Cargo.toml`, and
`src-tauri/tauri.conf.json`, with matching entries in both lockfiles. The
semantic-release commit usually updates all five files. Native Installer
Freshness checks them. If they don't match, it opens a protected-main pull
request. The freshness workflow never creates a tag or release. A maintainer
then dispatches the Native Installer Release workflow from reviewed `main`.
That transaction verifies the engine receipt, creates the matching
`desktop-vX.Y.Z` tag, publishes and verifies the installers, mirrors them to the
engine release, and updates the signed candidate channel. This transaction
doesn't change the admission-driven Production channel.
All lower native prereleases receive a prominent "Superseded: do not use"
notice. Their assets remain for provenance. The full two-lane release policy
and its planned convergence into a single release are documented in
[RELEASES.md](https://github.com/OpenAdaptAI/openadapt-desktop/blob/main/RELEASES.md).

## Artifact labels

Every new filename includes `Candidate`, the native version, operating system,
architecture, and signing state. The initial matrix is:

| Platform | Architectures | Packages | Signing labels |
| --- | --- | --- | --- |
| macOS | Apple Silicon (`arm64`), Intel (`x86_64`) | DMG | `developer-id-notarized` required |
| Windows | `x86_64` | MSI and NSIS setup executable | `authenticode` required |
| Linux | `x86_64` | DEB and AppImage | `github-attested` exact bytes required |

The release workflow refuses ad-hoc or unsigned platform metadata. The build
workflow installs and uninstalls every package on clean hosted runners. It
verifies the executable architecture and the declared signing policy. macOS
verification requires the exact configured Developer ID authority and Team ID.
Windows verification requires a valid signer and a timestamp certificate on
each installer, the installed executable, and the NSIS uninstaller. The workflow
launches every installed application and requires the process to survive a
20-second startup window (catching launch panics before they ship), and
stages the exact tested bytes. The repository test matrix also checks that only
the `openadapt` scheme is registered and that its handoff is fixed to
`connect_uri` without a shell or general navigation escape hatch. These checks
do not replace qualification of a complete real workflow.

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
  --repo OpenAdaptAI/openadapt-desktop \
  --cert-identity "https://github.com/OpenAdaptAI/openadapt-desktop/.github/workflows/native-release.yml@refs/heads/main" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --deny-self-hosted-runners
sha256sum -c SHA256SUMS
python verify-openadapt-native-release.py --directory . --manifest SHA256SUMS
```

The final helper refuses missing, extra, linked, non-regular, duplicate, or
digest-mismatched files. The canonical engine release also publishes an
attested `openadapt-desktop-verified-release.json`. The `desktop-channel`
release carries the attested, strictly monotonic
`openadapt-desktop-channel.json` candidate index. This closed chain
identifies the exact native tag, engine release, source commits, workflow run,
checksum digest, and complete asset set. A download service must verify this
index attestation. It must not select a release from mutable release-note text.

An attestation binds bytes to a build identity; it does not establish that the
software is secure or functionally complete.

## External signing requirements

The protected `native-release` GitHub environment must provide complete macOS
and Windows signing credential sets. A missing or partial set fails the build.

- macOS Developer ID and notarization: `APPLE_CERTIFICATE`,
  `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
  `APPLE_PASSWORD`, and `APPLE_TEAM_ID`.
- Windows Authenticode, either an importable certificate (`WINDOWS_CERTIFICATE`,
  `WINDOWS_CERTIFICATE_PASSWORD`, `WINDOWS_CERTIFICATE_THUMBPRINT`; Tauri uses
  SHA-256 and an RFC 3161 timestamp) **or** Azure Trusted Signing
  (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
  `TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT`,
  `TRUSTED_SIGNING_CERTIFICATE_PROFILE`), which is the cheaper, token-free option
  for a startup. Configure one set, not both. Both produce a publicly trusted,
  timestamped `authenticode` artifact.
- Linux uses the required GitHub OIDC exact-byte attestation above. It has no
  founder-managed secret and is not described as native-signed.

When either native credential set is absent, the release stops. Historical
prereleases keep their original trust labels. The updater stays disabled until
its independent public/private signing-key lifecycle and recovery procedure are
established.

Before a release, the repository must also have a no-bypass pull-request
ruleset for `main` and immutable release-tag rules for both `v*` and
`desktop-v*`. The engine workflow can create `v*`. The explicitly dispatched
native release workflow can create `desktop-v*`. Neither release identity can
update or delete a tag. The historical `desktop-v0.15.0` prerelease doesn't
satisfy the new trust contract.

The founder activation runbook lists the exact certificates to buy, their costs,
the secrets to add, and the claims each public surface can make. See
[CODE_SIGNING.md](CODE_SIGNING.md).
