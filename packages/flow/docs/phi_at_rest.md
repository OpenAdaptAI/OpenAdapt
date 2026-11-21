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

## Default, opt-out and strict mode

Flow encrypts recordings, bundles and run directories by default, with no
setup. Each one gets its own random data key, wrapped through a per-set
bucket key to this computer's device key and, in an organization, to the
organization's key (see [Key hierarchy](#key-hierarchy)).

| | Default | `OPENADAPT_FLOW_AT_REST=strict` |
| --- | --- | --- |
| Durable checkpoints and attended state | Encrypted | Encrypted |
| Web recordings (`record`, the quickstart and tutorial) | Encrypted | Encrypted |
| Desktop recordings (`record --backend windows\|macos\|linux\|rdp\|citrix`) | Raw capture plaintext while recording; see [Desktop recordings](#desktop-recordings) | Held back before the command runs |
| Cross-window recordings (`record --surface`) | Logged | Held back before the command runs |
| Bundles from `compile`, `induce`, `admit` | Encrypted | Encrypted |
| Run directories (report, step screenshots, crops, frames, evidence) | Encrypted | Encrypted |
| A run with an approval gate, an attended draft, a business-decision state or `--save-healed-to` | Logged for a plaintext bundle; refused for an encrypted one (the default since this release) | Refused before it runs |
| The bundle, skill library and compiled fix of `teach` | Encrypted from encrypted input; otherwise logged | Encrypted |
| The sanitized review copy and approved archive of `push` | Encrypted from encrypted input; otherwise logged | Encrypted |
| The receipt page (`seal.html`) beside an encrypted run | Logged: it embeds the run report | Not written |
| Approval requests, `--record-video`, and the output of `visualize`, `explain`, `emit-skill`, `compose`, `process`, `variants`, `route`, `for-each`, `shadow`, `compile-agent`, `guard`, `bench`, `sanitize`, `redact-report`, `jit`, `--workers` and `induce --report` | Logged | Held back before the command runs |

**Logged** means stderr names the path and one line goes to
`~/.openadapt/plaintext-at-rest.jsonl`; when that line can't be written, the
write stops. OpenAdapt Desktop shows a notice in its window from the same
ledger. Issue [#1751](https://github.com/OpenAdaptAI/OpenAdapt/issues/1751)
tracks the logged artifacts that remain.

A sealed bundle always runs into a sealed run directory, in every mode.

**Turning it off.** `OPENADAPT_ENCRYPT_AT_REST=0`, or `--no-encrypt` on
`record`, `compile`, `run` and `replay`, writes plaintext. So does
`OPENADAPT_FLOW_PLAINTEXT_AT_REST='<reason>'`, which records the reason. Each
process prints a one-line warning and adds a ledger line. Strict mode ignores
all of them. Files encrypted earlier still read under the opt-out, with the
key that encrypted them.

**Healthcare and other regulated deployments:** set
`OPENADAPT_FLOW_AT_REST=strict` and an organization policy
([Organization policy](#organization-policy)). Strict mode runs:

- `record`, `compile`, `induce`, `admit run`, `run`, `replay` and `resume`,
  which encrypt what they write, plaintext input included;
- the other commands listed under
  [Commands on sealed input](#commands-on-sealed-input), on encrypted input
  (every directory they are given must be encrypted);
- the commands that write no recorded data: service health and control, the
  no-outbound allow list, `at-rest status`, key and receipt verification.
  `service run` runs the queue script it is given; the Flow commands the
  script starts inherit strict mode, and the script's other actions are the
  operator's.

Every other command, and qualification trials (`qualify campaign`,
`qualify run-case`, `qualify drills`), is held back before it starts.
`seal` runs on plaintext input too: it writes a sealed set under
`OPENADAPT_BUNDLE_KEY`. A symlink is judged by the directory it points to,
and a directory holding a plaintext `workflow.json`, `events.jsonl` or
`report.json` counts as plaintext wherever it sits. Every entry under an encrypted input is checked when the command starts: a symlink or a directory that can't be read below it refuses the run, and with a key the set is opened again rather than taken from an earlier read. `qualify run` and
`qualify certify` take `--evidence-root`, which is usually a plaintext parent
of sealed runs, so in strict mode run them on encrypted evidence or outside
strict mode. An unknown value of `OPENADAPT_FLOW_AT_REST` or
`OPENADAPT_ENCRYPT_AT_REST` stops the command. `seal` and record checks on
encrypted bundles use `OPENADAPT_BUNDLE_KEY` only.

OpenAdapt Desktop encrypts certified workflows with per-workflow keys in the
system keychain, and still runs Flow with the opt-out recorded under its own
reason.

## Summary

By default, these are ciphertext on disk:

- every file of a web recording that can hold patient data, from the first
  frame: screenshots (`frames/`), typed text and DOM text (`events.jsonl`),
  parameter examples (`meta.json`), network bodies;
- every such file of a compiled bundle: the program, its crops and sidecars;
- every file of a run directory: the report (with its inputs), step
  screenshots, identity and resolution crops, transition evidence,
  checkpoints, pauses, approvals, undo records and `REPORT.md`.

Flow reads them in memory: `replay`, `run`, `resume`, `compile`, `inspect
report-run`, `seal verify`, the receipt page and the quickstart read
encrypted and plaintext folders alike. The test
`tests/test_sealed_at_rest.py::test_record_compile_and_governed_run_write_no_plaintext`
records a demonstration against the synthetic clinic app, compiles it, runs it
to VERIFIED under the Standard profile and renders the report, while a process
audit hook checks every file the process writes, renames or deletes, temporary
files included. It fails if any of them holds plaintext.

What stays readable is listed under [What is not encrypted](#what-is-not-encrypted).

## Key hierarchy

Every new recording, bundle and run directory (sealed-set format version 3)
has three layers of keys:

```
data key (DEK, 256-bit random, one per set)
  wrapped with HPKE (RFC 9180: DHKEM P-256, HKDF-SHA256, AES-256-GCM) to
bucket key pair (P-256, one per set)
  whose private half is wrapped, in a bucket record, to
    the device runtime key    only while the set is being written
    the device viewer key     user presence on hardware
    organization key(s)       P-256 HPKE or RSA-OAEP-3072; required in org mode
    a recovery key            solo mode, optional
```

- **Files.** Each file is a Tink `AES256_GCM_HKDF_1MB` stream (1 MiB
  segments, last segment flagged, so a truncated file fails to open) under a
  payload key derived from the DEK and the set id, with the file's path in
  the set as associated data. `tests/test_stream_aead.py` checks the format
  against Tink itself.
- **Header.** `.openadapt-sealed.json` holds the set id, the key reference
  (bucket id and fingerprint), the DEK wrapped to the bucket's public key,
  the index, and two HMAC-SHA-256 MACs: one under a key derived from the DEK,
  one under a key derived from the bucket's private key. The second stops
  someone who knows the bucket's public key from writing a set this computer
  would accept.
- **Algorithms.** NIST-approved throughout: P-256, HKDF-SHA256, AES-256-GCM,
  RSA-OAEP with SHA-256. All come from the `cryptography` package (50 or
  later). This is not a FIPS 140-3 validated module unless you run it on a
  FIPS-validated OpenSSL provider.

### Where the bucket record lives

| | Solo (default, no policy) | Organization mode (`mode: org`) |
| --- | --- | --- |
| Record | Inside the set's header (`records: inline`), so it travels with backups | `~/.openadapt/keys/buckets/<id>.json` plus a copy in the organization's `key_archive` (`records: archive`); never in the header |
| Wrapped to | Device viewer key (and runtime key while open), recovery key if one exists | Device viewer key (runtime while open, local copy only), organization keys, recovery key if set |
| Lost laptop | Backups open with the recovery key; without one they can't be opened | Every set whose record reached the archive opens with the organization key |
| Crypto-shredding | This disk's copy only: a backup still opens with the recovery key | Every tracked copy: the local record (overwritten, then deleted) and the archive copy |

A bundle keeps its runtime stanza after it is written, because programs
replay unattended. Recordings and runs lose it when they are finalized; from
then on the viewer key (Touch ID or Windows Hello, on hardware) opens them.

### Device keys

| Platform | Provider | Status |
| --- | --- | --- |
| macOS | Secure Enclave runtime key; Secure Enclave viewer key with Touch ID | Interface and test double only; **not built**, pending a check on real hardware |
| Windows | TPM key through the Platform Crypto Provider; Windows Hello viewer key | Interface and test double only; **not built**, pending a check on real hardware |
| Linux | TPM2 key | Interface only; **not built** |
| Everywhere today | A software P-256 key in the OS keychain through `keyring` (macOS Keychain, Windows Credential Manager with DPAPI, Linux Secret Service), else a 0600 file with a warning. `pip install openadapt` includes `keyring`; a bare `openadapt-flow` needs the `keychain` extra | Built; `at-rest status` labels it `software` |

A software key protects against a copied disk, backup or ticket attachment,
not against code running as you. `require_hardware_device_key: true` in the
policy refuses the software key. Next to a keychain-held key,
`~/.openadapt/keys/device-key.store.json` records which entry holds it and a
check value (no key material). If that entry is later missing, locked or
different, Flow fails closed and never makes a new key over it.
`OPENADAPT_FLOW_KEY_STORE` picks the software key's store: `auto`
(default), `keychain` or `file`.

### Organization policy

The organization makes its key in its own KMS or HSM and distributes a policy
file. Flow reads the first one found, and a user file never overrides a
machine one:

1. macOS: the managed preferences profile `ai.openadapt.policy` (MDM);
2. Windows: `HKLM\SOFTWARE\Policies\OpenAdapt`, value `Policy` (Group
   Policy, Intune);
3. Linux: `/etc/openadapt/policy.json`, owned by root and writable by root
   only;
4. `OPENADAPT_POLICY=<file>`. It may set organization mode only when it is
   owned by root (`OPENADAPT_POLICY_TRUST_USER_FILE=1` lifts that for
   development, and status says so).

```json
{
  "schema": "openadapt.ai/policy/v1",
  "org": "example-health",
  "mode": "org",
  "recipients": [
    {"role": "org", "status": "active", "type": "hpke-p256",
     "public_key": "<base64 SPKI>", "fingerprint": "sha256:…",
     "kms_uri": "arn:aws:kms:…"},
    {"role": "org", "status": "retired", "type": "rsa-oaep-3072-sha256",
     "public_key": "<base64 SPKI>"}
  ],
  "key_archive": "file:///Volumes/keys/openadapt/",
  "require_hardware_device_key": false,
  "max_unarchived": 50
}
```

- In organization mode, a missing or unreadable policy, no active
  organization recipient, or no `key_archive` stops recording and running.
- If the archive is unreachable, records stay local as "not yet archived"
  (`at-rest status` counts them), and new sets stop after `max_unarchived`.
- Only a local or mounted path (`file://`) is supported as `key_archive` so
  far; `s3://` and `smb://` refuse.
- OpenAdapt Cloud never distributes the organization's key or holds any key.

A reviewer opens a set with the archive copy of its record in
`~/.openadapt/keys/buckets/` and `OPENADAPT_ORG_KEY_FILE` (a PEM private
key). Unwrapping through the KMS itself (`Decrypt`, `DeriveSharedSecret`) is
not built yet.

### Solo recovery

`openadapt-flow at-rest recovery create` makes a recovery key: 32 random
bytes shown once as a 52-character code, turned into a P-256 key with HPKE's
DeriveKeyPair. Only its public half is stored; it becomes a recipient of
every new set. Open with it on another computer by setting
`OPENADAPT_RECOVERY_CODE`. Without one, a lost or reset computer means its
encrypted runs can't be opened.

### Legacy keys

- `OPENADAPT_BUNDLE_KEY` and `OPENADAPT_RECORDING_KEY` still win outside
  organization mode: Flow writes the version 1 header it always wrote,
  keyed by that passphrase (scrypt N=2^15, r=8, p=1). They are deprecated.
- `~/.openadapt/keys/at-rest.key` (mode 0600), the key earlier versions
  made, still opens what it encrypted, and is still the local passphrase of
  durable checkpoints and attended state, which keep their own containers.
- Version 1 and 2 sets, and plaintext folders, still read.

`openadapt-flow at-rest status` shows the mode, the policy source, the device
keys, the organization fingerprints, the recovery key and the unarchived
count. It prints no key material.

## Crypto-shredding

`openadapt-flow at-rest shred DIR --yes` destroys the set's bucket key: the
local record is overwritten and deleted, and the archive copy deleted. It
also drops the record from the header, and deletes the durable checkpoints
and other stores' ciphertext inside the set. It returns a shred record
(bucket id, time, copies destroyed). The library call is
`openadapt_flow.sealed_set.shred`, for a retention job. In solo mode it
reaches only this disk's copy, and says so.

It does not reach plaintext that leaked earlier (a screenshot pasted into a
ticket, a crash dump), copies someone opened and saved, or a desktop
recording's raw capture session.

## Receipts

The receipt (`seal.json`), its timestamp and log proofs, `receipt.json`,
`receipt.md`, `receipt.png` and the public card (`public-seal.json`) stay
plaintext beside an encrypted run. They hold signatures, outcomes, counts and
digests, never the report's contents.

**Every digest is over the plaintext.** The receipt's `subject_sha256` and
the card's `report_sha256` are the SHA-256 of the plaintext `report.json`
bytes, decrypted in memory. So a receipt is the same whether the run is
encrypted or not, and checking it needs a key that opens the report.
`seal verify` and the receipt page decrypt the report to check it.

The receipt page (`seal.html`) embeds the run report so it can check the
receipt in the browser. Beside an encrypted run it is therefore a plaintext
copy of the report (including the run's inputs): writing it is logged, and
strict mode refuses it.

## Desktop recordings

The desktop recorders capture through openadapt-capture, which writes its
raw session (input database and screen video) in plaintext while recording,
in a private 0700 folder beside the output. When recording stops, Flow
converts it, encrypts the converted recording into the output, and deletes
the raw session and the converted plaintext. Until then, and if the process
is killed before it converts, they are plaintext on disk; deleting them is not
a secure erase. So crypto-shredding can't be claimed for a desktop recording
(NIST SP 800-88r2 3.2.2). Record on an encrypted volume. Encrypting inside the
recorder, before the first write, is not built yet.

## Keys

| Variable | Used for |
| --- | --- |
| `OPENADAPT_POLICY` | A policy file, when no machine policy exists. |
| `OPENADAPT_RECOVERY_CODE` | The solo recovery code, to open sets on another computer. |
| `OPENADAPT_ORG_KEY_FILE` | An organization recipient's PEM private key, for a reviewer. |
| `OPENADAPT_FLOW_KEY_STORE` | Where the software device key is kept: `auto`, `keychain` or `file`. |
| `OPENADAPT_BUNDLE_KEY` | Deprecated: a passphrase for bundles and their run directories (version 1 header). |
| `OPENADAPT_RECORDING_KEY` | Deprecated: the same for recordings. |

Each set is opened once per reader or writer handle, which reuses its keys
for that operation. Independent path-helper reads open the set again; the
module retains no reader or passphrase cache. Caller inputs and derived keys
remain sensitive memory; releasing a handle does not promise erasure. Files
are encrypted whole in memory: a very large file needs its size in memory to
read or write.

## Sealed sets

A sealed bundle, an encrypted recording and an encrypted run directory share
one format, the sealed set:

- `.openadapt-sealed.json` holds the wrapped data key (version 3: wrapped
  to the set's bucket key, see [Key hierarchy](#key-hierarchy); version 1:
  the salt and scrypt parameters) and an index sealed with the data key. The index lists every other file with the
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

`openadapt_flow.sealed_set.BUNDLE_FILES`, `RECORDING_FILES`, `LIBRARY_FILES`
and `SANITIZED_FILES` are the source of truth; a test keeps these tables in
step with them.

### Bundle

| File | Holds | At rest |
| --- | --- | --- |
| `workflow.json` | The program: target labels, typed literals, landmark text, selectors, URLs, effect values, data-source rows, parameter defaults, window titles | Encrypted |
| `templates/*` | Crops of the recorded screen (targets, expected regions, drag ends, identifier crops) and the layout fingerprint | Encrypted |
| `manifest.json` | SHA-256 digests, compiler version, certification provenance | Plaintext (structural) |
| `workflow.py` | A readable rendering of `workflow.json` | Encrypted |
| `compile_notes.json`, `param_hygiene.json`, `param_proposals.json`, `annotations.json`, `api_proposal.json`, `sections.json` | Compiler sidecars; they quote labels, masked or raw example values and screen text | Encrypted |
| `effect_contract.yaml` | A scaffolded effect contract with observed values | Encrypted |
| `undo-lineage.json`, `record-check-binding.json` | Prior digests; record-check coverage | Encrypted |
| `qualification-proposal.json`, `qualification-proposal.draft.json`, `qualification-admission.local-dev.json`, `qualification/*` | Qualification pins, cases and reports | Encrypted |
| `admission-decision.json`, `admission.json`, `admit.json`, `jit_draft.json` | Admission records; some carry read-back values or a login name | Encrypted |
| `teach-authorization/*` | The person-signed teach authorization the program was taught under; a spelled-out input or record value is in it | Encrypted |
| `openadapt-card.json`, `repair/*`, `evidence/*`, `narration.json` | Operator card, repair candidates with anchor text, copied evidence, scrubbed narration | Encrypted |
| Anything else | Unknown | `seal` refuses |

### Recording

| File | Holds | At rest |
| --- | --- | --- |
| `events.jsonl` | Every action: typed text, field labels, structural locators, page URL and title, system-of-record rows when a reader was attached | Encrypted |
| `frames/*` | Before and after screenshots of every action | Encrypted |
| `meta.json` | App URL, parameter example values, window title, identifier region | Encrypted |
| `network.jsonl` | Request URLs, form fields and bodies, response bodies (`--capture-network`) | Encrypted |
| `sor_snapshots.json`, `verify.proposed.yaml` | System-of-record rows read by `record --verify`, and the effects proposed from them | Encrypted |
| `check.json`, `check.proposed.json`, `readback.proposed.yaml`, `record_check.proposed.yaml`, `record_check.proof.json`, `identity.proposed.json`, `effect_contract.yaml` | Checks and proposals derived from the recording | Encrypted |
| `distill.json`, `narration.json`, `.openadapt-approval.json`, `.openadapt-sanitization.json` | Derived reports, scrubbed narration, approvals | Encrypted |
| Anything else | Unknown | Refused |

### Skill library

| File | Holds | At rest |
| --- | --- | --- |
| `skills.json` | Every learned program version (target labels, typed literals, screen text, crop paths) and the execution traces it was learned from | Encrypted |
| Anything else | Unknown | Refused |

### Sanitized review copy

| File | Holds | At rest |
| --- | --- | --- |
| `.openadapt-sanitization.json` | The redaction manifest: file names, hashes and redaction records | Encrypted |
| `.openadapt-approval.json` | The reviewer's approval | Encrypted |
| `*` | The scrubbed copy of each recording or bundle file, before review | Encrypted |

The approved archive beside it (`<name>.approved.zip`) is one AES-256-GCM
container under the same key.

### Durable checkpoints and attended state

In the default mode, under the configured or local key, the durable store
writes its checkpoints, pauses and approvals as `<name>.enc` AES-256-GCM
containers. The attended store does the same for the pause capability and its
history, the decision log (operator and message), program and reconciliation
receipts, and program-transition control frames. Each file's associated data
names its run-relative path, so a file copied over another path does not
decrypt, and once a run's attended state is encrypted a plaintext file in its
place is refused. The external attended journal stores each decision-log
snapshot encrypted and bound to its run and sequence; its hash chain and HMAC
cover the plaintext. A run whose attended files are plaintext from an earlier
version still resumes, and its first encrypted write converts them.
File and journal operations derive their keys with scrypt under the random
salt kept in the run's `.attended_state.encrypted` marker. A conversion
operation reuses its local AEAD across the files it converts; independent
reads and writes derive again, so the cost grows with the number of independent
crypto operations. Each file and journal row still gets its own nonce. Each
file names the salt it was sealed
under, and a file whose salt is not the one in the run's marker is refused, so
a file copied in from another run under the same key does not read. If two
processes convert the same run at once, the one that loses the race to write
the marker reseals its files under the winner's salt. The replayer classifies
a run's outcome with its own checkpoint key, so a run whose key was passed in
code rather than through the environment still reads its sealed evidence.
Once a run's attended state is encrypted,
`OPENADAPT_FLOW_PLAINTEXT_AT_REST` does not turn it back to plaintext: a write
with no key is refused. Containers written by the earlier per-file format
still read.
`tests/test_attended_at_rest.py` runs attended decisions under the same disk
audit hook and checks that no attended file or journal row holds plaintext.

### Run directory

Every file is encrypted except these structural ones: `.durable_run.claim`
(run id and namespace), the empty `*.lock` files, `.attended_action.lease`
(nonce digests and times), `.attended_capability.key` (a random per-run
secret, no recorded data), and the receipt files (`seal.json`,
`seal.anchor.json`, `seal.tsa.json`, `seal.tsr`, `seal.publish.json`,
`public-seal.json`, `receipt.json`, `receipt.md`, `receipt.png`; see
[Receipts](#receipts)). `seal.html`, the receipt page, is an export: it is
plaintext and logged. Undo's prior record values (`undo/prior/*`) are
encrypted with the run. The durable store's checkpoints, pauses and
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
`record --verify` keeps its system-of-record reads inside it, encrypted, and
`record --with-check` marks its check inside it, encrypted.

Web recordings are encrypted under a configured key, and under the local key
in strict mode. `--no-encrypt` records one in plaintext, with a warning and a
ledger line. The desktop recorders (`--backend
windows/macos/linux/rdp/citrix`) write a capture session (a SQLite database
and video) through openadapt-capture, and Flow logs its directory; with
`OPENADAPT_RECORDING_KEY` set, or in strict mode, use the web recorder.

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

`record` (including `--with-check`), `compile`, `induce`,
`scaffold-verifier`, `seal`, `certify`, `qualify`, `admit`, `run`, `replay`,
`approve`, `resume`, `undo`, `visualize` (including a process contract),
`explain`, `emit-skill`, `for-each`, `sections`, `check mark`, `compose`,
`process`, `variants`, `route`, `teach`, `push`, `sanitize`,
`review-sanitized` and `approve-sanitized` read sealed bundles, run
directories and encrypted recordings in memory, given the key, and keep what
they write inside a sealed set sealed. Each reads artifact files through one reader,
`openadapt_flow.artifacts`:

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
- `run`, `replay` and `resume` write a sealed run directory, and `approve`
  writes its approval into it encrypted. A run with an
  approval gate, an attended draft or a business-decision state refuses before
  it acts, because those stores still write plaintext state.
- `undo` keeps its approval, claim, parameters and result in the sealed run
  directory; the undo run is a sealed set of its own inside it.
- `for-each` of a sealed bundle writes the looped bundle sealed, under the
  same key rules as `compile`.
- `emit-skill` copies a sealed bundle into the skill folder as ciphertext.
  `visualize` and `emit-skill` write their page and `SKILL.md` outside the
  sealed set: by default they name it on stderr and in the ledger, and strict
  mode refuses them before they run.
- `sections` and `check mark` write `sections.json` and `check.json` back
  into the sealed bundle or recording, encrypted.
- `compose` copies sealed child bundles into the composition as ciphertext.
  `process`, `variants` and `route` read sealed child bundles and their
  admission envelopes in place. The parent file each writes
  (`composition.json`, `process-contract.json`, `variant-set.json`) holds
  names, digests and pointers; by default it is named on stderr and in the
  ledger, and strict mode refuses the command before it runs.
- `teach` of a sealed run, fix recording or bundle, and every `teach` in
  strict mode, writes the taught bundle sealed (crops, program and repair
  candidate), under the same key rules as `compile`. Its skill library
  (`<out>.skills`, or `--library`) is a sealed set of its own, and the fix
  recording compiles into `_teach_compiled`, a sealed bundle inside the
  recording that the recording's index skips. A library or `--out` directory
  that already holds plaintext is refused, so nothing learned from encrypted
  input lands beside plaintext. An `--out` or library that is, contains, or
  is inside any input (by real path or by inode, so a symlink alias counts)
  is refused before anything is written.
- `push` and `sanitize` of an encrypted recording or bundle, and every
  `push` in strict mode, write the sanitized review copy as a sealed set of
  kind `sanitized`, staged in a private (0700) directory beside it and removed
  if anything fails. Scrubbing, the second-pass check and the approval rescan
  run on the decrypted bytes in memory and are exactly as strict as for
  plaintext. `review-sanitized` shows the original and the copy from memory
  and writes a reviewer's redactions back encrypted; `approve-sanitized`
  encrypts the approved archive; the upload decrypts that archive in memory and
  sends the exact approved bytes.

Refusals never quote what they read. A validation error's quoted input value
is dropped from every refusal the CLI prints. With sealed input or in strict
mode, a `teach` or `push` refusal names only the error's type, unless its text
was built only from the command line (the paths you passed, such as an `--out`
that overlaps an input or a run that came from another bundle). An unexpected
error prints its type instead of a traceback, and a success line names a skill
or workflow only by a short hash.

Without the key, each of these refuses before it reads anything and names the
variable to set. Any other command given a sealed path refuses with a message
that lists the commands above, rather than decrypt to disk. A bare
`Workflow.save()` into a sealed bundle keeps it sealed.

## What is not encrypted

Outside strict mode, the artifacts the [table](#default-opt-out-and-strict-mode)
marks as logged. Also:

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
- Structural files: `manifest.json`, the sealed-set headers (with the public
  recipients), the run directory's claim, lock and lease files, and the
  receipt files listed under [Receipts](#receipts).
- The receipt page (`seal.html`) and the quickstart's summary page
  (`index.html`). The summary page leaves out an encrypted run's
  screenshots; the receipt page embeds the report.
- A desktop recording's raw capture session while it records, as above.
- The admission root's `admission-pending.json` and
  `admission-decision.json`.
- Bundles encrypted with the older `Workflow.save(encrypt=True)` library call
  and no `seal`: only `workflow.json` and `templates/` are encrypted there.
- Attended continuation (`execute_attended_action`, the Desktop and
  console path) of an encrypted run directory is not built yet. A run
  directory from before this release keeps running in plaintext (logged),
  with its attended state in its own encrypted containers.
- Commands outside the list above, `repair` among them, refuse sealed input
  rather than decrypt it to disk. Each says so before it reads anything.

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
