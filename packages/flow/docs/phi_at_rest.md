# PHI at rest: bundles, recordings and runs

A recording, the bundle compiled from it, and the run directory written when
the bundle runs are all persistent records of a patient screen. In a
healthcare deployment each one is a HIPAA-designated record that must be
classified, access-controlled, retention-managed and encrypted at rest. This
page states what Flow encrypts, how, what it costs, and what it still leaves
to the operator.

See also [PRIVACY.md](PRIVACY.md) (what is scrubbed before anything leaves the
machine), [phi_in_transit.md](phi_in_transit.md) (PHI on the wire) and
`openadapt_flow.sealed_set` (the implementation).

## Summary

With a key configured, these are ciphertext on disk:

- every file of a sealed bundle that can hold patient data
  (`openadapt-flow seal`, or `compile` of an encrypted recording);
- every file of a web recording, from the first frame (`record`);
- every file of the run directory of a sealed bundle: the report, step
  screenshots, identity and resolution crops, transition evidence,
  checkpoints, pauses, approvals and undo records.

Flow reads them in memory. The test
`tests/test_sealed_at_rest.py::test_record_compile_and_governed_run_write_no_plaintext`
records a demonstration against the synthetic clinic app, compiles it, runs it
to VERIFIED under the Standard profile and renders the report, while a process
audit hook checks every file the process writes, renames or deletes, temporary
files included. It fails if any of them holds plaintext.

What stays readable is listed under [What is not encrypted](#what-is-not-encrypted).

## Keys

| Variable | Used for |
| --- | --- |
| `OPENADAPT_BUNDLE_KEY` | Sealed bundles and their run directories. Also turns on encrypted web recording by default. |
| `OPENADAPT_RECORDING_KEY` | Recordings. When it is unset, recordings use `OPENADAPT_BUNDLE_KEY`. |

The key is a passphrase. Flow stretches it with scrypt (N=2^15, r=8, p=1)
over a random per-directory salt, once per directory, and seals each file with
AES-256-GCM under the derived key, a fresh random 96-bit nonce and the file's
path in the associated data. Both primitives come from the `cryptography`
package. Key management (KMS, escrow, rotation) is the operator's: Flow reads
the passphrase from the environment and never writes it.

## Sealed sets

A sealed bundle, an encrypted recording and an encrypted run directory share
one format, the sealed set:

- `.openadapt-sealed.json` holds the salt, the scrypt parameters and an index
  sealed with the derived key. The index lists every other file with the
  SHA-256 of its bytes on disk. The header also repeats the index digest
  (`set_digest`) for an inventory that has no key; Flow trusts only the copy
  it recomputes from the authenticated index.
- Each file that can hold patient data is stored as `<name>.enc`.
- Opening the set authenticates the index, refuses any file the index does not
  list and any listed file that is missing, and checks each file's hash before
  decrypting it. A file cannot be added, dropped, swapped for another file,
  replaced with an older ciphertext of itself, or moved to another set.
- Writing into a set refuses a file the inventory below does not know, rather
  than passing it through.
- A recording that stopped before it finished has no index, and every reader
  refuses it as unfinished.

`openadapt-flow seal ./bundle --out ./bundle-sealed` prints the content digest
(the workflow and its plaintext crops) and the sealed file-set digest:

```bash
export OPENADAPT_BUNDLE_KEY='<inject from your secret manager>'
openadapt-flow seal ./bundle-v2 --out ./bundle-prod
openadapt-flow certify ./bundle-prod --policy clinical-write
```

`seal` never modifies the source and never overwrites a destination. It reads
the source into memory, encrypts it into a private staging directory, reopens
the result as a reader would, and publishes it with an atomic no-replace
rename. Because encryption changes the artifact contract, sealing expires any
prior certification; certify the sealed destination.

## Inventory

`openadapt_flow.sealed_set.BUNDLE_FILES` and `RECORDING_FILES` are the source
of truth; a test keeps this table in step with them.

### Bundle

| File | Holds | At rest |
| --- | --- | --- |
| `workflow.json` | The program: target labels, typed literals, landmark text, selectors, URLs, effect values, data-source rows, parameter defaults, window titles | Encrypted |
| `templates/*` | Crops of the recorded screen (targets, expected regions, drag ends, identifier crops) and the layout fingerprint | Encrypted |
| `manifest.json` | SHA-256 digests, compiler version, certification provenance | Plaintext (structural) |
| `workflow.py` | A readable rendering of `workflow.json` | Encrypted |
| `compile_notes.json`, `param_hygiene.json`, `param_proposals.json`, `annotations.json`, `api_proposal.json` | Compiler sidecars; they quote labels, masked or raw example values and screen text | Encrypted |
| `effect_contract.yaml` | A scaffolded effect contract with observed values | Encrypted |
| `undo-lineage.json`, `record-check-binding.json` | Prior digests; record-check coverage | Encrypted |
| `qualification-proposal.json`, `qualification-proposal.draft.json`, `qualification-admission.local-dev.json`, `qualification/*` | Qualification pins, cases and reports | Encrypted |
| `admission-decision.json`, `admission.json`, `admit.json`, `jit_draft.json` | Admission records; some carry read-back values or a login name | Encrypted |
| `openadapt-card.json`, `repair/*`, `evidence/*` | Operator card, repair candidates with anchor text, copied evidence | Encrypted |
| Anything else | Unknown | `seal` refuses |

### Recording

| File | Holds | At rest |
| --- | --- | --- |
| `events.jsonl` | Every action: typed text, field labels, structural locators, page URL and title, system-of-record rows when a reader was attached | Encrypted |
| `frames/*` | Before and after screenshots of every action | Encrypted |
| `meta.json` | App URL, parameter example values, window title, identifier region | Encrypted |
| `network.jsonl` | Request URLs, form fields and bodies, response bodies (`--capture-network`) | Encrypted |
| `sor_snapshots.json`, `verify.proposed.yaml` | System-of-record rows read by `record --verify`, and the effects proposed from them | Encrypted |
| `check.json`, `record_check.proposed.yaml`, `record_check.proof.json`, `identity.proposed.json`, `effect_contract.yaml` | Checks and proposals derived from the recording | Encrypted |
| `distill.json`, `.openadapt-approval.json`, `.openadapt-sanitization.json` | Derived reports and approvals | Encrypted |
| Anything else | Unknown | Refused |

### Run directory of a sealed bundle

Every file is encrypted except these structural ones: `.durable_run.claim`
(run id and namespace), the empty `*.lock` files, `.attended_action.lease`
(nonce digests and times) and `.attended_capability.key` (a random per-run
secret, no recorded data). The durable store's checkpoints, pauses and
approvals are sealed under the same key with their own AES-256-GCM containers,
and the run set adopts them as ciphertext. A file written in plaintext by any
other component makes the run refuse to seal its index.

## Recording encrypted from the start

```bash
export OPENADAPT_RECORDING_KEY='<inject from your secret manager>'
openadapt-flow record --backend web --url https://app.example --out ./rec
```

The web recorder seals every frame, event line, the metadata and the network
capture in memory and writes only ciphertext, from the first frame. The event
log is resealed whole on each append, so the file on disk is always one
complete ciphertext. The recording is finalized (indexed) when it finishes.
`record --verify` keeps its system-of-record reads inside it, encrypted;
`record --with-check` says it cannot mark a check in an encrypted recording
yet and marks nothing.

Encryption is on by default where it is safe: when `OPENADAPT_BUNDLE_KEY` is
set, a deployment that seals its bundles also encrypts its web recordings.
`--no-encrypt` opts out. The desktop recorders (`--backend
windows/macos/linux/rdp/citrix`) write a raw capture session (a SQLite
database and video) through openadapt-capture, which Flow cannot encrypt yet.
With `OPENADAPT_RECORDING_KEY` set they refuse to record; with only the bundle
key set they record in plaintext as before.

### Cost

Measured on 2026-10-19 with `benchmark/at_rest/run.py` (results in
[`benchmark/at_rest/results.json`](../benchmark/at_rest/results.json),
summary in [`benchmark/at_rest/RESULTS.md`](../benchmark/at_rest/RESULTS.md)):

| | Plaintext | Encrypted |
| --- | --- | --- |
| Persist one 1280x800 frame (median) | 0.12 ms | 0.25 ms |
| Record one action: two frames, one event (median) | 5.2 ms | 5.8 ms |
| Same, actions 136-150 of 150 (median) | 5.1 ms | 6.1 ms |
| Key derivation, once per recording | none | 183 ms |
| Disk for a 150-action recording (26 MB) | | +62.6 KB (0.24%) |

The disk overhead is 44 bytes per file plus the sealed index (one SHA-256 per
file). The settle wait was disabled for these numbers; with it, a real action
takes tens to hundreds of milliseconds, so encryption adds about 1% or less.

## Commands on sealed input

`record`, `compile`, `induce`, `scaffold-verifier`, `seal`, `certify`,
`qualify`, `admit`, `run`, `replay`, `resume` and `undo` read sealed bundles
and encrypted recordings in memory, given the key, and keep what they write
sealed:

- `compile` of an encrypted recording writes a sealed bundle, under
  `OPENADAPT_BUNDLE_KEY` or, when that is unset, the recording's key. It
  refuses an existing plaintext output directory.
- `induce` of encrypted inputs writes a sealed program. `--report` and
  `--probe-on` refuse, because they write outside a sealed set.
- `scaffold-verifier` writes its drafts inside the encrypted source. `--out`
  and `--prove` refuse.
- `qualify` reseals the bundle; `--out` must be inside it.
- `admit` compiles into sealed bundles and merges crops in memory. The
  pending and decision records at the admission root stay plaintext; they hold
  digests, counts, the workflow name and the decider.
- `run`, `replay` and `resume` write a sealed run directory. A run with an
  approval gate, an attended draft or a business-decision state refuses before
  it acts, because those stores still write plaintext state.
- `undo` keeps its approval, claim, parameters and result in the sealed run
  directory; the undo run is a sealed set of its own inside it.

Without the key, each of these refuses before it reads anything and names the
variable to set. Any other command given a sealed path refuses with a message
that lists the commands above, rather than decrypt to disk. A bare
`Workflow.save()` into a sealed bundle keeps it sealed.

## What is not encrypted

- Data in memory. Decrypted frames, crops and the workflow live in process
  memory while a command runs, and can reach swap or a crash dump. Use
  encrypted swap and full-disk encryption.
- The application itself. The browser profile and cache of the app being
  recorded or driven, and the app's own storage, are outside Flow.
- Terminal output. Commands print what they always printed (paths, outcomes,
  `induce` summaries); redirecting it to a file writes plaintext.
- Names and sizes. File names (`templates/step_003.png.enc`), file counts and
  ciphertext sizes show the number of steps and frames and roughly how large
  each image is.
- Structural files: `manifest.json`, the sealed-set headers, and the run
  directory's claim, lock and lease files.
- Desktop recordings, as above.
- The admission root's `admission-pending.json` and
  `admission-decision.json`.
- Bundles encrypted with the older `Workflow.save(encrypt=True)` library call
  and no `seal`: only `workflow.json` and `templates/` are encrypted there.
- The offline Seal of a run (`local-seal`) is not written for an encrypted
  run directory yet; the run says so on stderr.
- Commands outside the list above, such as `visualize`, `explain`, `teach`,
  `emit-skill` and `push`, refuse sealed input rather than read it.

## The identity template is not encryption

`identity_template` removes plaintext identifiers from `workflow.json`: no
readable, greppable name, DOB or MRN remains, and the wrong-patient check
still runs against the hashes. But a salted hash of a low-entropy identifier
can be brute-forced by anyone holding the bundle and the salt. Set
`OPENADAPT_FLOW_IDENTITY_SALT` at both compile and replay to keep the salt out
of the bundle. In a sealed bundle the template is encrypted as well.

## Governance

- `.gitignore` excludes bundle output directories; the committed
  `docs/showcase-openemr/bundle` is a synthetic-data exception.
- `workflow.json` carries `contains_phi`, `phi_scrubbed` and `encrypted` for a
  compliance inventory; `manifest.json` mirrors `encrypted` without the key.
- `scripts/check_bundle_phi.py` blocks committing a bundle with a plaintext
  identity band, and, with the `privacy` extra, identifier-bearing
  postconditions and labels.
- Profiles: Regulated requires an application-sealed bundle and refuses before
  actuation without one; Standard with a sealed bundle requires encrypted
  durable state.

Even with everything above, treat every bundle, recording and run directory as
PHI, keep it off public or unmanaged storage and out of git, and use full-disk
encryption on the machines that hold it.
