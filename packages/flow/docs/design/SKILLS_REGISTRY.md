# Verified skills registry

Status: reviewed (coordinator, 2026-10-28). The first stage is implemented:
signed entries, provenance in the workflow, bind, qualify and the run
refusal. It builds on `openadapt pull` (#1173, `openadapt_flow/registry.py`).

## What it is

The registry is a list of skills. Each skill is a compiled program for one
task in one open-source app, shipped with what it needs to be checked on
someone else's copy of that app:

- the program: a compiled bundle, digest-pinned;
- the record check, as a `verify.yaml` template whose instance addresses and
  credentials are left to the user;
- the fault coverage of that check (`check_coverage.json`);
- a verifier score measured blind (below);
- the credentials it needs, by role, never by value;
- the app's license, and what the bundle does and does not contain.

A pulled skill doesn't run until it has been qualified against the user's own
instance. A registry certification is evidence about our test copy, never
about yours.

## The entry

`index.json` keeps schema 1's fields (`id`, `title`, `app`, `path`, `status`,
`certification`, `evidence`, `usage`, `params`, `files`). Schema 2 adds:

| Field | What it holds |
| --- | --- |
| `version` | The skill's version (semver). `pull app/task@1.2.0` pins it; plain `pull` takes the newest. |
| `app_license` | The app's license (for example `GPL-3.0`, `AGPL-3.0`, `Apache-2.0`). |
| `app_versions` | The app versions the skill was qualified on. |
| `contract` | The path of `verify.yaml` inside the bundle. Addresses and credentials are `${VAR}` references. |
| `coverage` | The path of `check_coverage.json`: fault classes applicable, killed and untestable on our copy. |
| `verifier_score` | The blind score: suite id and sha256, author, commit under test, and counts of caught, missed, right clean verdicts and false alarms. |
| `credentials` | The roles the skill needs (the user who runs it, the read-only record-check client). Each is named by the environment variable that will hold it. |
| `contains` | What the bundle holds: program steps, cropped screen templates from a synthetic copy, the contract and evidence. It never holds the app's code. |

A `certified` entry still needs its `certification` record. Separately, any
entry with files needs a `contract` and a `verifier_score`. An entry without
them can be listed, but `pull` labels it "no record check".

## Pull, bind, qualify, run

1. **Pull.** `openadapt pull openemr/register-patient` checks the entry's
   signature against the keys shipped with the package, then downloads the
   files and checks each sha256 and the manifest digest, as today. It then:
   - records the provenance (entry id, version, index, the signed entry's
     sha256, the signer and the published digest) in the workflow itself, so
     the content digest covers it;
   - writes `skill.lock.json` beside the bundle: the entry id, version, index
     URL, file digests and the time;
   - expires any certification the bundle carries
     (`_invalidate_prior_certification`, reason "pulled from the registry;
     qualify on your instance").

   The pulled bundle therefore has its own content digest, distinct from ours.
2. **Bind.** `openadapt-flow skill bind ./register-patient --url
   https://emr.example.org --var RECORD_TOKEN=MY_EMR_READ_TOKEN` writes
   `binding.json`: the user's app address, record-check address, and the
   environment variable that holds each credential. It refuses a literal
   secret: values are only ever variable names.
3. **Qualify.** `openadapt-flow skill qualify ./register-patient --certify
   clinical-write` runs the six qualification cases with the bundled contract,
   resolved through the binding, against the user's instance (a test copy of
   it). Passing certifies the bundle under the user's environment digest.
4. **Run.** `run`, `replay`, `emit-skill` and `emit-mcp` refuse a bundle that
   has a `skill.lock.json` unless it carries a current certification made by
   `skill qualify` for the bound app's environment. The message names the
   command to run. Governed runs refuse an uncertified bundle already. The new
   refusal also covers `replay`, the permissive path, for pulled skills.

The refusal keys off the provenance in the workflow, not the lock file:
deleting `skill.lock.json` changes nothing. Removing the provenance changes
the content digest, which makes the bundle a different, unverified local
bundle, and the fail-closed rules for those apply.

## The blind verifier score

The method is the independent suite on `measure/phase0-independent`
(`benchmark/verification_coverage/independent`), applied per skill:

1. A spec of what must be true after the task, and what must not change, is
   written without reading the generated record check.
2. A blind author, who sees only the spec and a fault brief, writes the fault
   cases. The suite is frozen by sha256 before any run.
3. Scoring is mechanical. A fault case counts as caught when the check does
   not verify it; a clean case counts as right when it does.
4. The entry records the counts and the frozen suite's sha256. `pull --list`
   shows them beside the status.

Each app gets its own suite and fault brief. The registry never shows a score
measured on a different skill or app.

## Seeds

| Skill | App (license) | Record check | Notes |
| --- | --- | --- | --- |
| `openemr/register-patient` | OpenEMR (GPL-3.0) | REST and FHIR | Replaces `openemr/new-referral` (example) with a qualified, scored version |
| `medplum/new-patient` | Medplum (Apache-2.0) | FHIR | Needs #1579 (`focus_on`) and #1581 (form identity) |
| `openimis/claim-intake` | openIMIS (AGPL-3.0) | SQL | Needs an arm64 or Rosetta host (amd64 images) |
| `erpnext/new-customer` or `odoo/new-contact` | ERPNext (GPL-3.0) / Odoo Community (LGPL-3.0) | REST | First non-healthcare entry |
| one more non-healthcare app | to be chosen: a self-hostable OSS app with a documented API | REST or SQL | |

Proprietary apps (Epic, SAP and others) are out of scope without access and
permission.

## Licensing

An entry describes how to use an app. It doesn't redistribute the app:

- no app source, schema files or assets copied from the app's repository;
- screen templates are crops of the app's rendered UI, taken on a synthetic
  copy;
- the fixture that brings up a test copy pulls the app's own published
  images by digest at run time, as `benchmark/openimis_claims` does.

Each entry names the app's license. A crop of an AGPL app's UI is a screenshot
of its output, not its code; this needs one legal review before the first
AGPL entry is published.

## Decisions (review, 2026-10-28)

- **Signatures.** Each entry is signed with the Seals signing (Ed25519ph), and
  the index is to be anchored in Rekor as Seals are. The registry signing key
  and the first anchoring are maintainer actions; until then entries are
  unsigned and `pull` needs `--allow-unsigned`.
- **Where it lives.** A static index in a public repository, with the bundles
  as release assets.
- **Public and private tiers.** `source-policy.yaml` keeps per-system
  read-back recipes private. The public bundle carries the program and a
  minimal contract (app identity, record match keys, identity field, one
  check per typed value). The full contract (frame conditions, exact new set,
  duplicate rules, fault-derived checks) and the fault suites come from a
  private tier fetched with credentials. `verifier_score` publishes only the
  counts and the private suite's sha256. The exact file split is with the
  coordinator.
