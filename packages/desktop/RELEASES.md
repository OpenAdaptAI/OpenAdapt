# Release Policy

This repository publishes from two lanes until code signing lands. This
document is the source of truth for what each lane produces, which release to
download, and how the lanes converge.

## The two lanes

| Lane | Tag | Trigger | Marked as | Assets |
| --- | --- | --- | --- | --- |
| Engine (Python package) | `vX.Y.Z` | `python-semantic-release` on every releasable push to `main` | Regular release ("Latest") | Wheel, sdist, and PyPI publish attestations |
| Native installers | `desktop-vX.Y.Z` | `desktop-v*` tag push (automated, see below) | Draft, then published **prerelease** | Experimental DMG (macOS arm64/x86_64), MSI + NSIS (Windows x86_64), DEB + AppImage (Linux x86_64), per-platform metadata JSON, and one `SHA256SUMS` manifest with GitHub artifact attestations |

The engine lane stays non-prerelease so GitHub's "Latest" pointer always names
the canonical engine release. The native lane stays prerelease because its
installers are Experimental scaffold-shell artifacts and are ad-hoc signed or
unsigned until external signing credentials are configured; see
[docs/EXPERIMENTAL_NATIVE_INSTALLERS.md](docs/EXPERIMENTAL_NATIVE_INSTALLERS.md)
for the verification scope and signing states.

## Which release should I download?

- **Python package / CLI**: install from PyPI (`pip install openadapt-desktop`)
  or take the wheel from the newest `vX.Y.Z` release. Engine releases carry no
  installers.
- **Native installers (Experimental)**: use the newest published `desktop-vX.Y.Z`
  prerelease whose notes do not carry a "Superseded" notice. Verify assets with
  `sha256sum -c SHA256SUMS` and `gh attestation verify`.

## Freshness automation

The native lane previously lagged the engine lane because `desktop-v*` tags
were pushed by hand. Two workflows now keep it fresh:

1. **Native Installer Freshness** (`.github/workflows/native-freshness.yml`):
   when an engine release is published (or on manual `workflow_dispatch` with a
   current engine version), it first verifies that the matching engine tag is
   an ancestor of `main` and that the application sources have not advanced.
   It then synchronizes the native version sources (`package.json`,
   `package-lock.json`, `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock`,
   `src-tauri/tauri.conf.json`) to the engine version, commits to `main`, and
   pushes the matching `desktop-vX.Y.Z` tag. It never builds anything itself
   and refuses a historical backfill that would label newer application code
   with an older version.
2. **Experimental Native Release** (`.github/workflows/native-release.yml`):
   unchanged build semantics — the tag push triggers the fail-closed signing
   preflight, the platform build matrix, install/launch/uninstall smoke tests,
   final-byte checksums, attestation, and a **draft** prerelease that a
   maintainer reviews and publishes. The build matrix runs only on `desktop-v*`
   tags, not on ordinary pushes.

As a result every engine release `vX.Y.Z` gets a matching native prerelease
`desktop-vX.Y.Z` built from the same version.

## Supersession

After a native prerelease is published, older published `desktop-v*` prereleases
are edited to carry a prominent "Superseded by `desktop-vX.Y.Z` — do not use"
notice at the top of their notes (machine marker:
`<!-- openadapt-superseded-by: desktop-vX.Y.Z -->`). The previous published
installer remains valid while its replacement is a draft, so the notice always
points at a publicly available replacement. CI never deletes releases or
assets; superseded assets are retained for provenance and any deletion is a
human decision.

## Machine-readable selection rule (download pages)

Consumers that list releases via the GitHub API (for example the
openadapt.ai download page) should select installers from release metadata
alone:

- A native installer release is identified by its tag prefix `desktop-v` and by
  the `<!-- installer-release -->` marker at the top of its notes; its assets
  include the platform installers.
- Recommended rule: offer downloads from the newest non-draft `desktop-v*`
  prerelease whose body contains `<!-- installer-release -->` and does **not**
  contain `<!-- openadapt-superseded-by:`. Label plain `v*` releases
  "CLI/engine only".

## Convergence plan (post-signing)

The two lanes exist because installer signing is credential-gated: publishing
unsigned or ad-hoc-signed binaries as the repository's "Latest" release would
overstate their maturity. Once Apple Developer ID + notarization and Windows
Authenticode credentials are configured (the workflows already fail closed on
partial configuration) and installers build signed:

1. The native build workflow attaches its attested installer assets to the
   canonical `vX.Y.Z` engine release instead of creating a separate
   `desktop-v*` prerelease.
2. The `desktop-v*` prerelease lane retires; existing `desktop-v*` prereleases
   remain as historical, superseded records.
3. The `<!-- installer-release -->` marker moves with the assets, so download
   pages keep working without a selection-rule change.

Until then, the freshness automation above keeps the two lanes at the same
version.
