<!-- installer-release -->

# Experimental Native Installers

OpenAdapt Desktop native packages are the **Experimental installed authoring,
teaching, and local-pairing companion** for OpenAdapt. They bundle and start the
Python sidecar, connect the Tauri/React cockpit to it over local JSON-lines IPC,
and register the `openadapt://` operating-system handler. The handler accepts
only the fixed `openadapt://connect` action and forwards it to the sidecar's
strict, transactional pairing flow.

The canonical compiler and governed runtime remain in `openadapt-flow`.
Compile, replay, run, and teach operations currently require a separately
installed `openadapt-flow` on `PATH`; it is not frozen into these installers.

Native releases use a distinct `desktop-vX.Y.Z` tag and prerelease channel. The
native version comes from `package.json`, `src-tauri/Cargo.toml`, and
`src-tauri/tauri.conf.json`; the Native Installer Freshness workflow
synchronizes those sources to each published engine release and pushes the
matching `desktop-vX.Y.Z` tag, so the native prerelease number mirrors the
engine release it was built from. When a newer native prerelease is published,
older native prereleases receive a prominent "Superseded — do not use" notice;
their assets are retained for provenance, and any deletion is a maintainer
decision made outside CI. The full two-lane release policy and its planned
convergence into a single release after code signing lands are documented in
[RELEASES.md](https://github.com/OpenAdaptAI/openadapt-desktop/blob/main/RELEASES.md).

## Artifact labels

Every filename includes `Experimental`, the native version, operating system,
architecture, and signing state. The initial matrix is:

| Platform | Architectures | Packages | Signing labels |
| --- | --- | --- | --- |
| macOS | Apple Silicon (`arm64`), Intel (`x86_64`) | DMG | `adhoc` or `developer-id-notarized` |
| Windows | `x86_64` | MSI and NSIS setup executable | `unsigned` or `authenticode` |
| Linux | `x86_64` | DEB and AppImage | `unsigned` plus GitHub provenance |

The build workflow installs and uninstalls every package on clean hosted
runners, verifies executable architecture and the declared signing policy, and
stages the exact tested bytes. The repository test matrix also checks that only
the `openadapt` scheme is registered and that its handoff is fixed to
`connect_uri` without a shell or general navigation escape hatch. These checks
do not replace qualification of a complete real workflow.

## Integrity and provenance

Release jobs stage the exact post-signing, smoke-tested files, generate one
sorted `SHA256SUMS` manifest, verify it, and create GitHub artifact attestations
over that manifest. Consumers can verify downloaded assets with:

```bash
sha256sum -c SHA256SUMS
for artifact in OpenAdapt-Desktop-Experimental-*; do
  gh attestation verify "$artifact" --repo OpenAdaptAI/openadapt-desktop
done
```

An attestation binds bytes to a build identity; it does not establish that the
software is secure or functionally complete.

## External signing requirements

The protected `native-release` GitHub environment may provide complete signing
credential sets. Partial sets fail the build instead of falling back silently.

- macOS Developer ID and notarization: `APPLE_CERTIFICATE`,
  `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
  `APPLE_PASSWORD`, and `APPLE_TEAM_ID`.
- Windows Authenticode: `WINDOWS_CERTIFICATE`,
  `WINDOWS_CERTIFICATE_PASSWORD`, and `WINDOWS_CERTIFICATE_THUMBPRINT`. The
  certificate must support code signing; Tauri uses SHA-256 and an RFC 3161
  timestamp service.
- Linux AppImage GPG is intentionally disabled until the workflow pins an
  external AppImage signature validator and publishes the corresponding public
  key fingerprint through an authenticated channel. AppImage does not
  self-verify; DEB/RPM repository metadata signing is also a separate boundary.

When no complete credential set is configured, the prerelease remains explicit
about ad-hoc or unsigned status. The updater stays disabled until its independent
public/private signing-key lifecycle and recovery procedure are established.
