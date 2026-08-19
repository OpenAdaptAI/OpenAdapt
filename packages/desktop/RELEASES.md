# Release Policy

This repository publishes from two lanes while the native channel remains
Beta. This document is the source of truth for what each lane produces, which
release to download, and how the lanes converge.

## Active release hold: the current Flow pin is not releasable

`main` pins `openadapt-flow[browser,console]==1.31.0`. That pin is an interim
source pin so the tree builds and tests against the newest published Flow. It
is **not** a releasable pin: the immutable 1.31.0 wheel on PyPI predates the
`openadapt.push-result/v1` contract that Desktop's governed `push` requires, so
a release built on it can only fail closed on every hosted handoff.

Do not tag `desktop-v*`, dispatch the engine release workflow, or publish any
installer while this pin is in force. The hold clears only when all of the
following are true, in order:

1. The next `openadapt-flow` semantic release ships the reviewed push-result
   contract and is published to PyPI.
2. The exact managed Cloud runtime candidate is reviewed, deployed, and
   acknowledged.
3. Desktop updates `pyproject.toml`, `uv.lock`, the frozen-sidecar inventory,
   and its qualification evidence from that accepted release.
4. This section is deleted in the same pull request that lands the new pin.

This is a distribution hold, not a claim of production acceptance. Neither lane
carries a signed qualification-admission record today, so no release note,
manifest, or installer may state or imply production acceptance.

## The two lanes

| Lane | Tag | Trigger | Marked as | Assets |
| --- | --- | --- | --- | --- |
| Engine (Python package) | `vX.Y.Z` | Explicit `Release and PyPI Publish` dispatch from reviewed, green `main` | Regular release ("Latest") | Wheel, sdist, PyPI publish attestations, **and a mirrored copy of the matching `desktop-vX.Y.Z` installer set** |
| Native installers | `desktop-vX.Y.Z` | `desktop-v*` tag push (automated, see below) | Draft, then published **prerelease** | Beta installers, platform metadata, SBOM, website manifest, signed build-provenance identity, and `SHA256SUMS` |

The engine lane stays non-prerelease so GitHub's "Latest" pointer always names
the canonical engine release. The native lane stays prerelease because its
installer surface is Beta. The native release workflow now requires Developer
ID plus notarization on macOS, Authenticode on Windows, and GitHub OIDC
attestation over the exact Linux DEB and AppImage bytes; see
[docs/BETA_NATIVE_INSTALLERS.md](docs/BETA_NATIVE_INSTALLERS.md)
for the verification scope and signing states.

## Which release should I download?

- **Python package / CLI**: install from PyPI (`pip install openadapt-desktop`)
  or take the wheel from the newest `vX.Y.Z` release.
- **Native installers (Beta)**: use the `vX.Y.Z` engine release selected by the
  attested `openadapt-desktop-verified-release.json` channel index. The index
  binds the matching `desktop-vX.Y.Z` source release and the identical mirrored
  bytes. Authenticate `SHA256SUMS`, then verify its exact inventory. Do not use
  mutable release notes as a release-selection authority.

### The "Latest" installer path

GitHub's `/releases/latest` excludes prereleases by definition, so it always
resolves to an engine release. That link is the one cited in launch material, so
it must not dead-end.

Two mechanisms keep it working, both driven from `native-release.yml`:

1. **`mirror-installers-to-engine-release`** copies the exact attested asset set
   from `desktop-vX.Y.Z` onto `vX.Y.Z`. Before any write, it verifies the exact
   asset inventory against the signed GitHub attestation, workflow, source
   commit, run attempt, and successful protected publish job.
   `/releases/latest` therefore carries a verified installer, not just a link.
2. **`point-engine-release`** prepends a marker-delimited pointer block at the
   top of the engine release notes:

```
<!-- openadapt-installer-pointer:start -->
...
<!-- openadapt-installer-pointer:end -->
```

Both jobs run on `release: published` for a non-draft `desktop-v*` prerelease,
not on the tag push, for the same reason the supersession notice does:
`publish-draft` creates a **draft**, whose tag page 404s publicly, and neither a
pointer nor a mirror may advertise a URL nobody can open. The pointer block is
rewritten in place, so republishing is idempotent and pointers never accumulate.
If the matching engine release is missing, both jobs fail loudly rather than
leaving "Latest" without an installer.

#### Why mirroring does not promote the Beta channel

The earlier policy here was "linked, not mirrored", on the reasoning that
putting ~757 MB of Beta binaries on the release GitHub labels "Latest" would
overstate their maturity. A notes-only link was not
enough: `/releases/latest` still showed a visitor nothing but a wheel and an
sdist, and that link is what launch material points at. The maturity concern is
addressed directly instead of by withholding the artifact:

- `desktop-vX.Y.Z` **stays a prerelease**. Flipping it to non-prerelease would
  make the native lane GitHub's "Latest" outright, and that remains forbidden.
- Every filename encodes its trust state — `…-developer-id-notarized.dmg`,
  `…-authenticode.msi`, or `…-github-attested.AppImage`.
- The pointer block leads with the required platform trust contracts and gives
  the `sha256sum -c` and `gh attestation verify` commands.
- The mirror job verifies every downloaded byte against `SHA256SUMS`. It then
  requires the signed subject set to equal that complete inventory. It also
  checks the exact GitHub-hosted workflow, tag commit, run attempt, and
  protected publish job before upload.
- The engine release gets **assets only**. It never receives the
  `<!-- installer-release -->` marker, so the machine-readable selection rule
  below is unchanged and download-page consumers keep resolving `desktop-v*`.

`desktop-vX.Y.Z` therefore remains the canonical installer release — build
provenance, attestations, and supersession notices are bound to it — and
`vX.Y.Z` carries a byte-identical convenience copy.

## Freshness automation

The native lane previously lagged the engine lane because `desktop-v*` tags
were pushed by hand. Three workflows now keep it fresh:

1. **Release and PyPI Publish** (`.github/workflows/release.yml`): a maintainer
   explicitly dispatches one semantic release after the intended release train
   has landed. It refuses to publish until the exact main-contained commit has
   successful `Test` and `Build artifacts` push workflows. The recovery operation
   can rebuild and publish a pre-existing, main-contained ref after checking that
   ref's exact CI; ordinary merges never publish packages.
2. **Native Installer Freshness** (`.github/workflows/native-freshness.yml`):
   when that engine release is published, it verifies the stable engine release
   and opens a pull request with the exact deterministic transform of
   `package.json`, `package-lock.json`, `src-tauri/Cargo.toml`,
   `src-tauri/Cargo.lock`, and `src-tauri/tauri.conf.json`. It never writes to
   `main`. After the protected branch merges that pull request, a main-push job
   reconstructs the transform from the immutable `vX.Y.Z` tag. It also verifies
   the current engine release and then creates `desktop-vX.Y.Z`. A manual
   backfill uses the same pull-request path.
3. **Native Installer Release** (`.github/workflows/native-release.yml`):
   the tag push triggers the fail-closed signing
   preflight, the platform build matrix, install/launch/uninstall smoke tests,
   final-byte checksums, attestation, and a **draft** prerelease that a
   maintainer reviews and publishes. The release tag must be on current main,
   differ from the matching engine tag only in the five native version files,
   and advance the highest published native version. The build matrix runs only
   on `desktop-v*` tags, not on ordinary pushes.

When the external controls below are active, each engine release `vX.Y.Z` can
get a matching native prerelease `desktop-vX.Y.Z` from the same reviewed source.

## External activation requirements

The workflow code is not the complete trust boundary. At the time of this
document update, the repository has no main or release-tag ruleset, and the
native signing identities are not configured. The historical `desktop-v0.15.0`
prerelease keeps its original ad-hoc and unsigned labels. Do not describe it as
a trusted release.

Before the next native tag or release:

1. Add a no-bypass pull-request ruleset for `main`, with the required exact-head
   checks. Require branches to be up to date before merge. Require the
   `Reject a stale native version pull request` check for `native-version/v*`
   pull requests.
2. Add an immutable release-tag ruleset for `v*`. Only the engine release
   identity can create a tag. Do not permit a tag update or deletion.
3. Add an immutable release-tag ruleset for `desktop-v*`. Only the native
   freshness identity can create a tag. Do not permit a tag update or deletion.
4. Configure Apple Developer ID plus notarization and one Windows Authenticode
   identity in the reviewed `native-release` environment.
5. Publish only after the exact tag workflow passes. Then verify the public
   assets, attestation, pointer, mirror, and supersession result.

The `native-release` environment reviewer is an additional publish boundary.
It does not replace the main and tag rulesets.

## Supersession

After a native prerelease is published, CI selects the highest semantic version
from all published, marked `desktop-v*` prereleases. It verifies that release
and edits every lower prerelease to carry a prominent "Superseded by
`desktop-vX.Y.Z` — do not use"
notice at the top of their notes (machine marker:
`<!-- openadapt-superseded-by: desktop-vX.Y.Z -->`). The previous published
installer remains valid while its replacement is a draft. A late publish event
for an older version cannot move the pointer or mirror backward. CI never
deletes releases or assets; superseded assets are retained for provenance and
any deletion is a human decision.

## Machine-readable selection rule (download pages)

Consumers must not select a release from mutable release notes. The release
workflow publishes `openadapt-desktop-verified-release.json` on the matching
stable engine release after every byte and trust check passes. It attests the
index with the release-event workflow identity from protected `main`.

- Fetch the index from the stable engine release channel.
- Verify the index attestation against
  `.github/workflows/native-release.yml@refs/heads/main`.
- Require the closed index schema and the expected repository.
- Require a version that does not decrease from the last accepted index.
- Use only the engine release, native tag, checksum digest, and asset names in
  the verified index.
- Download all named files into an empty directory.
- Authenticate `SHA256SUMS` against the exact `desktop-vX.Y.Z` workflow identity.
- Run `sha256sum -c SHA256SUMS` and
  `verify-openadapt-native-release.py`. The helper refuses an incomplete or
  expanded directory.

The release-note markers remain useful for human notices and historical
supersession. They are not a machine trust boundary.

- `openadapt-desktop-release-manifest.json` is the website-readable index. It
  lists each artifact name, platform, architecture, signing state, and SHA-256,
  plus the checked CycloneDX SBOM. The release workflow recomputes the named
  hashes and validates the complete file set against `SHA256SUMS` before it
  creates the GitHub attestation. The signed subject set must equal the complete
  checksum inventory. The verification path also binds the workflow, source
  commit, run ID, run attempt, and protected publish job. A consumer must still
  verify the checksums and attestation; `github-attested` is an exact-byte Linux
  trust label and is not a native-signing claim.

## Convergence plan

The next native release cannot publish until Apple Developer ID plus
notarization and Windows Authenticode are configured and the Linux exact-byte
attestations verify. After the first complete trusted release proves the full
channel, the repository can simplify the two upload targets:

1. The native build workflow attaches its attested installer assets to the
   canonical `vX.Y.Z` engine release *instead of* also creating a separate
   `desktop-v*` prerelease. (Step 1 is already half-done: `vX.Y.Z` carries the
   assets today via `mirror-installers-to-engine-release`. What remains is
   retiring the second upload target, not adding the first.)
2. The `desktop-v*` prerelease lane retires; existing `desktop-v*` prereleases
   remain as historical, superseded records.
3. The `<!-- installer-release -->` marker moves to `vX.Y.Z` with the assets,
   and the pointer/mirror jobs retire with the lane.

Until then, the freshness automation above keeps the two lanes at the same
version.
