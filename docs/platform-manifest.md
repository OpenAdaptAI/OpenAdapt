# OpenAdapt platform release manifest

`platform-manifest.json` at the repository root is the single authoritative,
machine-readable statement of what constitutes an OpenAdapt platform release:
which launcher, flow, capture, and desktop versions belong together, where
their published artifacts live (with sha256 digests), which operating systems
and substrate drivers are supported, what qualification evidence backs the
release, and what release channel it is on.

## Why it lives here

This repository (`OpenAdaptAI/OpenAdapt`) is the launcher/meta-package: it is
the one place that already pins `openadapt-flow` and the other components via
`pyproject.toml`, and it is the integration surface users install. The
platform manifest is therefore generated and versioned here, next to the pins
it must agree with. Other repositories contribute source data only:

- PyPI is the authority for published versions, artifact URLs, and digests.
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

The generator reads the real published state (PyPI JSON API, the live
status.json) plus this repository's `pyproject.toml`. It never invents
numbers: if a source is unreachable, empty, or disagrees with the repository,
it fails loudly. If a launcher release train is in flight (pyproject.toml
ahead of PyPI), pass `--allow-unreleased-launcher`; the manifest still records
the published version.

The launcher release workflow regenerates and validates the manifest after the
new launcher artifacts are visible on PyPI, then commits the exact published
URLs and digests back to `main`. This ordering is intentional: the manifest
cannot truthfully name a launcher release before its immutable artifacts exist.
If publication or reconciliation fails, `main` remains red and the release
workflow opens or updates a failure issue rather than weakening validation.

Regenerate and commit the manifest manually after other component releases;
the daily scheduled validator catches component or public-status drift.

## How it is validated

```bash
python scripts/validate_platform_manifest.py
```

CI runs this on every pull request, on pushes to `main`, and DAILY
(`.github/workflows/platform-manifest.yml`), so drift between the committed
manifest and the actually published artifacts fails loudly.

The workflow also accepts a `component-released` `repository_dispatch` so a
component repository can trigger the check immediately. **No component
repository sends it yet.** `openadapt-flow`, `openadapt-capture`, and
`openadapt-desktop` have no dispatch step in their release workflows, so the
daily cron is in practice the only automatic detector, and a component release
can serve stale digests for up to a day. Wiring the sender needs a token with
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

- Structure: manifest kind, schema major version, required fields, artifacts
  with sha256 digests per component.
- Signature honesty: while `signature.value` is null, `signature.status` must
  read `unsigned (signing infrastructure pending)`. A non-null signature
  value fails validation because no verification path exists yet.
- Repo agreement: openadapt-* compatibility ranges match `pyproject.toml`, and
  the manifest's launcher version is not ahead of `pyproject.toml`'s. A
  `pyproject.toml` ahead of the manifest is the normal in-flight-release state
  and only warns.
- Published-artifact agreement: component versions, artifact filenames, URLs,
  and sha256 digests match PyPI exactly (`--offline` skips this class). A
  manifest version behind PyPI's latest, or naming a release PyPI never
  published, fails; one ahead of `info.version` but already present in
  `releases` is index propagation lag and warns, with digests still verified.
  An unreachable PyPI warns rather than fails — it is not evidence of drift —
  unless `--require-network` is passed.
- Status skew: disagreement with `status.json` versions is a warning by
  default (status.json regenerates on its own cadence in `openadapt-web`);
  `--strict-status` escalates it to a failure.

Not validated yet (planned):

- Cryptographic signature verification (see the signing plan below).
- Desktop OS installer artifacts (MSI/DMG); today only PyPI artifacts exist.

## Schema (v1)

Top-level fields:

| Field | Meaning |
|-------|---------|
| `manifest_kind` | Always `openadapt-platform-release-manifest`. |
| `schema_version` | Semver of this schema; validators reject unknown majors. |
| `generated_at` | UTC timestamp of generation. |
| `release_channel` | Lowercased product lifecycle from status.json (currently `beta`). |
| `components` | `launcher`, `flow`, `capture`, `desktop`: package name, published version, `requires_python`, and artifacts (`type`, `filename`, `url`, `sha256`). |
| `compatibility` | The launcher's supported Python range and its real openadapt-* dependency specifiers, extracted from `pyproject.toml`. |
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
2. Windows Authenticode signing for desktop installers (MSI/EXE) once those
   installers are produced.
3. Apple Developer ID signing plus notarization for macOS app and DMG
   artifacts.

Until step 1 lands, the validator enforces that the manifest claims nothing:
a manifest with a non-null `signature.value` fails validation.

## Consumers

Anything that needs "what is the current OpenAdapt platform release" as data
should read this manifest rather than scraping PyPI or hardcoding versions:
release notes tooling, the website, the desktop updater, and support
tooling. Consumers must check `schema_version` and must treat
`signature.status` as informational until signing ships.
