# OpenAdapt platform release manifest

`platform-manifest.json` at the repository root is the single authoritative,
machine-readable statement of what constitutes an OpenAdapt platform release:
which Launcher, Flow, Capture, Privacy, Types, Desktop, and Agent versions
belong together. It also names the selected launcher, customer runner, and
Agent bridge packages, plus the exact frozen Desktop sidecar closure. The
manifest includes release-derived schema versions, dependency compatibility,
release source references, artifact digests, and the release channel.

## Why it lives here

This repository (`OpenAdaptAI/OpenAdapt`) is the launcher/meta-package: it is
the one place that already pins `openadapt-flow` and the other components via
`pyproject.toml`, and it is the integration surface users install. The
platform manifest is therefore generated and versioned here, next to the pins
it must agree with. Other repositories contribute source data only:

- PyPI is the authority for published versions, artifact URLs, and digests.
- Each exact public release tag is the authority for its release source commit
  and tree. This reference is not, by itself, a build-provenance attestation.
- The exact `desktop-vX.Y.Z` tag and its `uv.lock` are the authority for the
  package versions frozen into `openadapt-engine`.
- `https://openadapt.ai/status.json` (maintained in `openadapt-web`,
  `public/status.json`) is the authority for substrate availability, release
  channel, and the public qualification summary.
- `openadapt-flow` holds the versioned qualification evidence packs the
  manifest points at (`public-demo/evidence-packs/*/manifest.json` and the
  effectbench task pack manifest).

## How it is generated

```bash
python scripts/generate_platform_manifest.py
```

The generator reads the real published state from PyPI, exact GitHub release
commits, the Desktop lock, and the live status document. It also reads this
repository's `pyproject.toml`. It derives protocol versions from the exact
selected source files. An optional protocol does not appear until a published
release contains its source. If a source is unreachable, empty, or
inconsistent, generation fails. If a launcher release train is in flight (`pyproject.toml`
ahead of PyPI), pass `--allow-unreleased-launcher`; the manifest still records
the published version.

The launcher release workflow regenerates and validates the manifest after the
new launcher artifacts are visible on PyPI, then commits the exact published
URLs and digests back to `main`. This ordering is intentional: the manifest
cannot truthfully name a launcher release before its immutable artifacts exist.
If publication or reconciliation fails, `main` remains red and the release
workflow opens or updates a failure issue rather than weakening validation.

Regenerate and commit the manifest manually after other component releases.
The daily validator catches component, sidecar, and public-status drift.

The manifest can truthfully describe an incompatible published set. Use the
strict promotion gate only when all required runtime packages exist:

```bash
python scripts/validate_platform_manifest.py \
  --require-compatible --require-network --strict-status
```

The ordinary validator confirms that the recorded dependency result is
correct. It does not rewrite an incompatible result.

An ordered release train does not require a guessed future version in source.
Select exact versions only after PyPI and the release tag contain them:

```bash
python scripts/generate_platform_manifest.py \
  --component-version flow=<published-version> \
  --component-version desktop=<published-version>
```

The generator refuses an input until it can bind the exact PyPI files, hashes,
dependency metadata, release source commit, release tree, and Desktop lock.
The next Flow and Desktop train can use the same contract without a temporary
hard-coded version or a false release claim.

Generate a release display from the manifest. Do not copy version values into
another source file:

```bash
python scripts/render_platform_versions.py
python scripts/render_platform_versions.py --format json
python scripts/render_platform_versions.py --format markdown
```

`docs/platform-compatibility-report.md` is the generated human-readable report.
The offline validator fails if it differs from the machine-readable BOM.

## How it is validated

```bash
python scripts/validate_platform_manifest.py
```

CI runs this on every pull request, on pushes to `main`, and DAILY
(`.github/workflows/platform-manifest.yml`), so drift between the committed
manifest and the actually published artifacts fails loudly.

The workflow also accepts a `component-released` `repository_dispatch` so a
component repository can trigger the check immediately. **No component
repository sends it yet.** The component release workflows have no dispatch
step, so the daily cron is in practice the only automatic detector. A component
release can serve stale digests for up to a day. Wiring the sender needs a token with
`contents: write` on `OpenAdaptAI/OpenAdapt` stored as a secret in each
component repository; the component's own `GITHUB_TOKEN` cannot dispatch
across repositories.

Detection is not repair. Both the cron and a dispatched run validate and file
an issue; regenerating and committing the manifest remains a human step.

### Why the schedule is daily and why false reds are not tolerated

This manifest went stale once, and both causes are worth stating because they
are properties of the check rather than of the manifest:

1. **The staleness originates in another repository.** `openadapt-flow`
   released 1.24.0 on 2026-07-27; nothing was committed here, so only the
   schedule could notice. It was weekly, so the public raw.githubusercontent
   copy could advertise superseded sha256 digests for up to seven days.
   The schedule is now daily and the job is stdlib-only (no install, no cache),
   costing seconds a day.
2. **The check failed benignly after every release, so a real failure was
   invisible.** Two conditions were classified as errors when they are normal:
   the semantic-release version commit leaves `pyproject.toml` ahead of the
   not-yet-reconciled manifest, and PyPI's `info.version` lags an upload by
   minutes. Both now warn, while every digest, URL, and filename comparison
   stays fatal. A guard that cries wolf at every release gets ignored, and
   that is exactly what happened.

A failed scheduled or dispatched run also files (or comments on) a
`platform-manifest.json has drifted` issue, so drift has an owner rather than
a stale red dot on a repository nobody has open.

`tests/test_platform_manifest_drift.py` proves the guard FAILS on a simulated
future release, a tampered digest, a tampered URL, and a version PyPI never
published — and that it does NOT fail on the two transient release-time
conditions above.

Validated today:

- Structure: manifest kind, schema major version, UTC generation time,
  generation inputs, seven required published components, four runtime units,
  the complete dependency graph, exact schema-source bindings, operating
  systems, substrates, evidence objects, and artifact SHA-256 digests.
- Signature honesty: while `signature.value` is null, `signature.status` must
  read `unsigned (signing infrastructure pending)`. A non-null signature
  value fails validation because no verification path exists yet.
- Repo agreement: openadapt-* compatibility ranges match `pyproject.toml`, and
  the manifest's launcher version is not ahead of `pyproject.toml`'s. A
  `pyproject.toml` ahead of the manifest is the normal in-flight-release state
  and only warns.
- Published-artifact agreement: component versions, Python ranges, dependency
  ranges and markers, and the exact artifact filename/type/URL/SHA-256 set
  match version-specific PyPI metadata (`--offline` skips this class). A
  manifest version behind PyPI's latest, or naming a release PyPI never
  published, fails; one ahead of `info.version` but already present in
  `releases` is index propagation lag and warns, with digests still verified.
  An exact version supplied through `--component-version` can remain behind
  the latest release; the validator records a warning and still verifies all
  selected files and source provenance.
  An unreachable PyPI warns rather than fails — it is not evidence of drift —
  unless `--require-network` is passed.
- Release source references: every component binds its exact public release
  ref, commit, and tree. Network validation resolves each ref again and
  refuses a mismatch.
- Schema agreement: schema versions and source-file hashes are derived again
  from the exact selected release commits. A future schema cannot appear in
  the BOM before its published source exists.
- Evidence agreement: each repository evidence path is bound to an exact Git
  blob or tree and verified against the selected release tree.
- Status skew: disagreement with `status.json` versions is a warning by
  default, because that file lives in another repository and a release here
  must not be blocked by an edit nobody in this repo can make;
  `--strict-status` escalates it to a failure and also fails when the status
  source is unreachable. Operating-system and substrate-table drift always
  fails when the source is reachable. The warning is **not**
  self-healing: `status.json` is hand-maintained in `openadapt-web` and
  nothing generates it from PyPI or from this manifest.
  `openadapt-web` runs a daily guard
  (`scripts/check_published_version_claims.mjs`) that fails on exactly this,
  but that detects after the fact and still needs a person to open the
  corrective pull request. Skew reported here means someone must edit
  `public/status.json` and `data/published-version-claims.json` in
  `openadapt-web`.
- Native sidecar agreement: the Desktop sidecar's resolved OpenAdapt package
  versions and lock digest match the exact `desktop-vX.Y.Z` tag's `uv.lock`.
- Compatibility honesty: the validator recomputes every dependency edge among
  the seven selected components. Optional-extra markers remain visible. The
  Desktop edges resolve against its exact sidecar lock; other edges resolve
  against the selected platform versions.
- Promotion: `--require-compatible` fails while any selected dependency edge
  falls outside its published package range.

Not validated yet (planned):

- Cryptographic signature verification (see the signing plan below).
- Desktop installer signature and notarization verification. Desktop release
  jobs verify their own installer digests and attestations, but this manifest
  does not fetch or verify those large files yet.

## Schema (v1)

Top-level fields:

| Field | Meaning |
|-------|---------|
| `manifest_kind` | Always `openadapt-platform-release-manifest`. |
| `schema_version` | Semver of this schema; validators reject unknown majors. |
| `generated_at` | UTC timestamp of generation. |
| `generation` | SHA-256 bindings for the generator and report renderer. |
| `release_channel` | Lowercased product lifecycle from status.json (currently `beta`). |
| `release_selection` | Latest-published selection or exact published version inputs supplied for an ordered release train. |
| `components` | `launcher`, `flow`, `capture`, `privacy`, `types`, `desktop`, and `agent`: package name, published version, Python range, dependency constraints and markers, release source commit/tree, and the exact artifact set. |
| `runtime_units` | Launcher, runner, Desktop, and Agent deployment views. Non-locked units name selected packages. The Desktop sidecar names the exact lock-resolved closure. |
| `dependency_edges` | Every selected-component dependency edge, its activation marker, version constraint, resolution scope, and exact selected or locked target. |
| `schema_compatibility` | Explicit accepted versions derived from exact selected source files, with source commit, blob, and SHA-256 bindings. Optional schemas are omitted until published. |
| `compatibility` | The launcher's supported Python range and its OpenAdapt dependency specifiers, extracted from `pyproject.toml`. |
| `compatibility_status` | `dependency-compatible` or `dependency-incompatible`, plus the exact failed package edges. The generator computes this field. |
| `supported_os` | Operating systems the launcher supports. |
| `substrate_drivers` | Substrate table (name, public label, delivery) read from status.json. |
| `qualification_evidence` | Stable evidence IDs pointing at the public status document and the flow evidence-pack manifests. |
| `signature` | See below. |

## Signing plan

The `signature` block is present in the schema from day one, but it is
honestly null:

```json
{
  "algorithm": null,
  "value": null,
  "status": "unsigned (signing infrastructure pending)",
  "plan": "docs/platform-manifest.md#signing-plan"
}
```

We deliberately do not fake a signature or stand up theater around one. The
intended rollout, in order:

1. Sigstore (cosign, keyless via the GitHub Actions OIDC identity) signing of
   `platform-manifest.json` itself at release time, with the verification
   step added to `validate_platform_manifest.py` before any signature is
   ever emitted. Note that PyPI publish attestations (PEP 740, also
   sigstore-backed) already exist for recent `openadapt-desktop` uploads and
   provide per-artifact provenance independent of this manifest.
2. Windows Authenticode signing for the MSI and EXE installers after the
   signing credentials are active.
3. Apple Developer ID signing plus notarization for the macOS app and DMG
   artifacts after the Apple credentials are active.

The current Desktop release workflow supports Authenticode and Developer ID
credentials. The current public installer set still uses ad-hoc macOS signing
and unsigned Windows and Linux packages. The platform promotion gate must stay
separate from a claim that the installers have trusted OS signatures.

Until step 1 lands, the validator enforces that the manifest claims nothing:
a manifest with a non-null `signature.value` fails validation.

## Consumers

Anything that needs "what is the current OpenAdapt platform release" as data
should read this manifest rather than scraping PyPI or hardcoding versions:
release notes tooling, the website, the desktop updater, and support
tooling. Consumers must check `schema_version` and must treat
`signature.status` as informational until signing ships.
