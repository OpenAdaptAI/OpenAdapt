# Release Policy

This repository publishes unadmitted candidates from two lanes. Production is
an admission-driven channel with a separate manual derivation step. This
document defines the outputs, ordering, and release-selection rules.

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

This is a distribution hold, not a claim of Production acceptance. A normal
release never creates a Production admission. No release note, manifest, or
installer can state or imply Production acceptance before central activation.

## The two lanes

| Lane | Tag | Trigger | Marked as | Assets |
| --- | --- | --- | --- | --- |
| Engine (Python package) | `vX.Y.Z` | Explicit `Release and PyPI Publish` dispatch from reviewed, green `main` | Regular release ("Latest") | Wheel, sdist, an attested engine-release provenance receipt, PyPI publish attestations, **and a mirrored copy of the matching `desktop-vX.Y.Z` installer set** |
| Native installers | `desktop-vX.Y.Z` | Explicit `Native Installer Release` dispatch from reviewed, green `main` | Published **prerelease candidate** | Candidate installers, platform metadata, SBOM, website manifest, signed build provenance, and `SHA256SUMS` |
| Candidate native channel | `desktop-channel` | Final candidate-index step in the same native dispatch | Published **prerelease index** | The attested, strictly monotonic `openadapt-desktop-channel.json` descriptor |
| Production derived cache | `desktop-production-channel` | Separate manual dispatch after central admission activation | Published **prerelease cache** | One append-only descriptor for each exact active central admission |

The engine lane stays non-prerelease so GitHub's "Latest" pointer names the
newest engine candidate. "Latest" is not the Production default. The native
lane stays prerelease because an artifact does not create admission. The native release workflow requires Developer
ID plus notarization on macOS, Authenticode on Windows, and GitHub OIDC
attestation over the exact Linux DEB and AppImage bytes; see
[docs/RELEASE_CANDIDATE_INSTALLERS.md](docs/RELEASE_CANDIDATE_INSTALLERS.md)
for the verification scope and signing states.

## Which release should I download?

- **Python package / CLI**: install from PyPI (`pip install openadapt-desktop`)
  or take the wheel from the newest `vX.Y.Z` release.
- **Native installer candidate**: use the `vX.Y.Z` engine release selected by the
  attested `openadapt-desktop-verified-release.json` channel index. The index
  binds the matching `desktop-vX.Y.Z` source release and the identical mirrored
  bytes. Authenticate `SHA256SUMS`, then verify its exact inventory. Do not use
  mutable release notes as a release-selection authority.

### The "Latest" installer path

GitHub's `/releases/latest` excludes prereleases by definition, so it always
resolves to an engine release. That link is the one cited in launch material, so
it must not dead-end.

Two mechanisms keep it working. Both run after publication in the same
reviewed-main `native-release.yml` transaction:

1. **`mirror-installers-to-engine-release`** copies the exact attested asset set
   from `desktop-vX.Y.Z` onto `vX.Y.Z`. Before any write, it verifies the exact
   asset inventory against the signed GitHub attestation, workflow, source
   commit, and run attempt.
   `/releases/latest` therefore carries a verified installer, not just a link.
2. **`point-engine-release`** prepends a marker-delimited pointer block at the
   top of the engine release notes:

```
<!-- openadapt-installer-pointer:start -->
...
<!-- openadapt-installer-pointer:end -->
```

The publish job creates the immutable tag and public prerelease once. It does
not create a draft, overwrite an existing release, or replace published native
bytes. The pointer block is rewritten in place, so pointers never accumulate.
If the matching engine release or its attested receipt is missing, the
transaction fails before the platform builds start.

#### Why mirroring does not promote the Production channel

The earlier policy here was "linked, not mirrored", on the reasoning that
putting approximately 757 MB of candidate binaries on the release GitHub labels
"Latest" could be mistaken for Production selection. A notes-only link was not
enough: `/releases/latest` still showed a visitor nothing but a wheel and an
sdist, and that link is what launch material points at. The maturity concern is
addressed directly instead of by withholding the artifact:

- `desktop-vX.Y.Z` **stays a prerelease**. Flipping it to non-prerelease would
  make the native lane GitHub's "Latest" outright, and that remains forbidden.
- Every filename encodes its trust state: `…-developer-id-notarized.dmg`,
  `…-authenticode.msi`, or `…-github-attested.AppImage`.
- The pointer block leads with the required platform trust contracts and gives
  the `sha256sum -c` and `gh attestation verify` commands.
- The mirror job verifies every downloaded byte against `SHA256SUMS`. It then
  requires the signed subject set to equal that complete inventory. It also
  checks the exact GitHub-hosted reviewed-main workflow, source commit, and run
  attempt before upload.
- The engine release gets **assets only**. It never receives the
  `<!-- installer-release -->` marker, so the machine-readable selection rule
  below is unchanged and download-page consumers keep resolving `desktop-v*`.

`desktop-vX.Y.Z` therefore remains the canonical installer release. Its build
provenance, attestations, and supersession notices stay bound to it. `vX.Y.Z`
carries a byte-identical convenience copy.

## Freshness automation

The native lane previously lagged the engine lane because `desktop-v*` tags
were pushed by hand. Three workflows now keep it fresh:

1. **Release and PyPI Publish** (`.github/workflows/release.yml`): a maintainer
   explicitly dispatches one semantic release after the intended release train
   has landed. It refuses to publish until the exact main-contained commit has
   successful `Test` and `Build artifacts` push workflows. It writes and attests
   `openadapt-desktop-engine-release-provenance.json` after the release commit,
   tag, public release, wheel, and sdist exist. The recovery operation
   can rebuild and publish a pre-existing, main-contained ref after checking that
   ref's exact CI; ordinary merges never publish packages.
2. **Native Installer Freshness** (`.github/workflows/native-freshness.yml`):
   when that engine release is published, it verifies the published engine release
   and opens a pull request with the exact deterministic transform of
   `package.json`, `package-lock.json`, `src-tauri/Cargo.toml`,
   `src-tauri/Cargo.lock`, and `src-tauri/tauri.conf.json`. It never writes to
   `main`, create a native tag, or publish a release. A manual backfill uses the
   same pull-request path.
3. **Native Installer Release** (`.github/workflows/native-release.yml`):
   a maintainer dispatches the workflow from reviewed `main` with the exact
   version. The workflow verifies current main, the five-file version transform,
   the published engine release, and its attested receipt before it starts the
   fail-closed signing preflight. The same transaction builds, smoke-tests,
   attests, publishes, mirrors, writes the verified index, updates the
   monotonic candidate channel, updates the pointer, and marks older
   prereleases superseded. Only this workflow creates `desktop-vX.Y.Z`. It
   never changes the Production channel.

4. **Production channel admission**
   (`.github/workflows/production-channel.yml`): a maintainer supplies one
   immutable current `OpenAdaptAI/.github` commit after the canonical ledger
   activates the exact release. The workflow runs the canonical validator,
   requires exact version and artifact equality with the unchanged published
   candidate, and then appends one attested derived-cache descriptor. The
   descriptor records the central commit, admission ID, admission digest,
   expiry, and complete artifact inventory. It is not a second authority.

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
3. Add an immutable release-tag ruleset for `desktop-v*`. Only the explicitly
   dispatched native release identity can create a tag. Do not permit a tag
   update or deletion.
4. Configure Apple Developer ID plus notarization and one Windows Authenticode
   identity in the reviewed `native-release` environment.
5. Permit the reviewed `native-release` environment on `main`, with no admin
   bypass. Publish only after its approval. Then verify the public assets,
   attestation, pointer, mirror, channel, and supersession result.

The `native-release` environment reviewer is an additional publish boundary.
It does not replace the main and tag rulesets.

Production activation is a separate action after candidate publication:

1. Add one exact, evidence-backed Desktop admission to the canonical
   `OpenAdaptAI/.github` ledger. The canonical validator must select it as the
   active latest Desktop admission.
2. Protect the `production-release` environment on Desktop `main`, with no
   admin bypass. It can cache an active admission. It cannot create one.
3. Dispatch `production-channel.yml` with `operation=promote` and the exact
   current central commit. The workflow refuses any version, source, artifact,
   digest, expiry, or central-current mismatch.
4. Verify the appended cache attestation and the next scheduled drift check.

Until these steps pass, the candidate is not actively admitted. A failed
promotion doesn't change or remove a prior cache.

## Supersession

After the verified index and candidate channel are published, the same transaction edits
every lower marked `desktop-v*` prerelease to carry a prominent "Superseded by
`desktop-vX.Y.Z`: do not use"
notice at the top of its notes (machine marker:
`<!-- openadapt-superseded-by: desktop-vX.Y.Z -->`). CI never
deletes releases or assets; superseded assets are retained for provenance and
any deletion is a human decision.

## Machine-readable selection rule (download pages)

Candidate consumers must not select a release from mutable release notes. Fetch the
attested `openadapt-desktop-channel.json` asset from the `desktop-channel`
release. It binds the selected `openadapt-desktop-verified-release.json` on the
matching engine release. The channel must strictly advance from its prior
descriptor.

- Fetch and attest the channel against
  `.github/workflows/native-release.yml@refs/heads/main`.
- Follow its hash-bound verified-index URL.
- Verify the index attestation against
  `.github/workflows/native-release.yml@refs/heads/main`.
- Require the closed index schema and the expected repository.
- Require a version that does not decrease from the last accepted index.
- Use only the engine release, native tag, checksum digest, and asset names in
  the verified index.
- Download all named files into an empty directory.
- Authenticate `SHA256SUMS` against
  `.github/workflows/native-release.yml@refs/heads/main`.
- Run `sha256sum -c SHA256SUMS` and
  `verify-openadapt-native-release.py`. The helper refuses an incomplete or
  expanded directory.

The candidate descriptor does not select a Production release. A Production
consumer first fetches one immutable current `OpenAdaptAI/.github` commit, runs
its canonical validator, and selects the active latest Desktop admission. It
then fetches the deterministic cache asset whose filename contains that central
commit and admission digest. The consumer requires exact central commit, admission,
expiry, release, artifact, verified-index, and engine-receipt equality. If the
admission is absent, expired, revoked, or different, Desktop has no Production
default. It never falls back to an older admission or to PyPI/GitHub "Latest".

The scheduled drift check applies this same rule. A normal release can publish
a newer candidate without changing Production. A central admission change
fails the check until the exact derived cache exists. A failed promotion leaves
all prior cache assets unchanged.

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

The next native candidate cannot publish until Apple Developer ID plus
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
