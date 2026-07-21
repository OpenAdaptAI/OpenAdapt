# PHI at rest — the compiled bundle

A compiled bundle (`workflow.json` + `templates/*.png` + `workflow.py`) is a
**persistent record** produced from a recording of a patient screen. In a
healthcare deployment it is a **HIPAA-designated record** and must be
classified, access-controlled, retention-managed, and **encrypted at rest** by
the operator. This page states the current at-rest posture, what each
remediation does, and the remaining key-management evolution.

See also [PRIVACY.md](PRIVACY.md) (the in-flight / scrubbing map),
[phi_in_transit.md](phi_in_transit.md) (PHI **on the wire** — the win_agent
control channel is now TLS-encrypted with certificate pinning), and the
`openadapt_flow.ir.Workflow` manifest fields.

## What is in a bundle, and how it is protected today

| Artifact | Contents | At-rest protection today |
| --- | --- | --- |
| `workflow.json` identity band | **Salted-hash `identity_template`** — no plaintext name/DOB/MRN (REM-2) | One-way hash + governance guard + operator disk encryption |
| `workflow.json` postconditions | `TEXT_PRESENT` assertions; identifier-bearing ones **dropped** when the Presidio scrub is active (REM-2/GAP-3) | Compile-time scrub (optional) + governance guard |
| `workflow.json` landmarks | `anchor.landmarks[].ocr_text` — nearby ROW text used by the geometry rung, often the patient name; identifier-bearing ones **dropped** when the Presidio scrub is active | Compile-time scrub (optional) + governance guard |
| `workflow.json` labels/typed text | `anchor.ocr_text`, literal `Step.text` — may echo an identifier that is **load-bearing** for replay (the target label / the typed search value) | **Not scrubbed** (scrubbing would break resolution/typing); governance guard flags it; **parameterize** the typed identifier (`entity_ref`) so it is supplied at run time and never stored |
| `templates/*.png` | Pixel crops of the recorded screen — **image PHI** | **Sealed with AES-256-GCM** in an encrypted bundle (`save(encrypt=True)` → `templates/*.png.enc`, no cleartext crop on disk); governance guards + operator disk encryption otherwise |
| `workflow.py` | Human-readable rendering; identity band is now a PHI-free note (REM-2) | Same as `workflow.json` |

### The identity template is NOT encryption

`identity_template` removes **plaintext** PHI: no grep-visible, human-readable,
log-leakable, git-committable identifier remains, and the wrong-patient guard
still runs against the hashes. But a salted hash of a **low-entropy** identifier
(a name, a DOB) is **brute-forceable** by anyone who holds **both** the bundle
and the salt. So the template is a real reduction in exposure, **not a
cryptographic control**.

To raise the bar today, set `OPENADAPT_FLOW_IDENTITY_SALT` at **both** compile
and replay: the salt is then kept **out of the bundle**, so the hashes are
one-way to anyone without the external secret. Manage that secret like any other
(OS keychain / KMS / CI secret).

## Governance (REM-1, shipped)

- `.gitignore` excludes bundle output dirs; the committed
  `docs/showcase-openemr/bundle` is an explicit **synthetic-data** exception.
- Manifest fields on `workflow.json`: `contains_phi`, `phi_scrubbed`,
  `encrypted` — for a compliance inventory.
- Pre-commit / CI guard (`scripts/check_bundle_phi.py`) **blocks** committing a
  bundle whose steps carry a plaintext identity band (always), and — with the
  `privacy` extra installed — an identifier-bearing postcondition / label.

## Shipped: opt-in AEAD encryption (REM-1 crypto)

Encryption-at-rest is now available as an **opt-in** layer
(`openadapt_flow.crypto`), OFF by default so nothing breaks:

- **Bundle.** `Workflow.save(bundle_dir, encrypt=True, key=…)` seals the
  serialized `workflow.json` with **AES-256-GCM** and writes it as
  `workflow.json.enc` (no plaintext `workflow.json` on disk), **and seals every
  `templates/*.png` image crop the same way** — each becomes
  `templates/*.png.enc` (under a distinct `TEMPLATE_AAD` domain so a crop
  ciphertext can't be swapped for the workflow-json one) with its plaintext
  removed, so an encrypted bundle leaves **no cleartext PHI-bearing screenshot**
  on disk. `Workflow.load(bundle_dir, key=…)` decrypts both `workflow.json` and
  the crops **in memory** (the resolver reads a decrypted crop via
  `Workflow.decrypted_template(rel)`; nothing is rewritten as cleartext). The
  passphrase comes from the `key` argument or the **`OPENADAPT_BUNDLE_KEY`**
  environment variable; a per-bundle random salt + scrypt KDF stretches it to the
  256-bit data key.
- **Durable checkpoints.** `CheckpointStore(run_dir, key=…)` (wired through
  `Replayer(checkpoint_key=…)` and `resume(…, key=…)`) seals every checkpoint /
  pending-escalation / run-manifest / Phase-2 interpreter checkpoint the same
  way (`…​.json.enc`), so a resumable run's persisted params + effect contracts
  are ciphertext at rest.
- **Integrity preserved.** The schema-v2 manifest (content digest + per-asset
  SHA-256 + provenance) is sealed over the **plaintext** content *before*
  encryption — including the template crops, whose digests stay over the
  **plaintext** PNG — so a decrypted load still verifies integrity end-to-end
  (the crop check runs against the decrypted bytes in memory). The
  `encrypted: true` manifest flag is now live (mirrored on `Workflow.encrypted`)
  and now covers **both** `workflow.json` and the crops, and the `manifest.json`
  sidecar stays plaintext so a compliance inventory can read it without the key.
- **Fails loud + safe.** A **wrong or missing key**, or a **tampered ciphertext**
  (a flipped byte breaks the GCM tag — of `workflow.json` *or* a crop), raises
  `crypto.DecryptionError` / `crypto.MissingKeyError`; a **missing** or
  **swapped** crop ciphertext raises `BundleIntegrityError`. Never a partial or
  silent load.

**Scope / done:** both `workflow.json` **and** the `templates/*.png` image crops
are now sealed into AES-256-GCM containers in an encrypted bundle — no cleartext
PHI-bearing screenshot is left on disk. (The COMPLIANCE.md at-rest line for
`templates/` can therefore flip from "operator-disk-encryption only" to "sealed";
that file is owned by a separate PR.)

The live resolver consumes the keyed load's in-memory decrypted crops and does
not materialize plaintext PNGs on disk. Deployment-time **key management** (OS
keychain / KMS / envelope keys, escrow, and rotation) is still the operator's
responsibility: the passphrase is supplied via environment or library argument.
This provides the AEAD substrate, not a KMS.

### Future key-management design (envelope keys / whole container)

1. **Sealed container.** Serialize `workflow.json` + `templates/` into a single
   encrypted container (e.g. an [age](https://github.com/FiloSottile/age) /
   libsodium sealed archive). The bundle on disk is ciphertext; nothing readable
   without the key.
2. **Envelope keys.** A per-bundle data key (DEK) encrypts the container; the
   DEK is wrapped by a deployment key (KEK) held in the operator's KMS / OS
   keychain. Rotating the KEK re-wraps DEKs without re-encrypting bundles.
3. **Decrypt only in memory at replay.** `Workflow.load` gains a decrypt path
   that unwraps the DEK from the configured key provider and materializes the
   bundle **in memory only**; plaintext never lands on disk.
4. **Manifest.** `encrypted: true` plus the key id / wrapping metadata (never the
   key). The governance guard treats an `encrypted: true` bundle as opaque.
5. **Fail closed.** A compliance-pinned deployment (`OPENADAPT_FLOW_SCRUB=on`)
   refuses to write an **un**encrypted bundle once the key provider is
   configured.

Even with bundle sealing enabled, treat every bundle as PHI, keep it off public
or unmanaged shared storage and git, and use **full-disk encryption** on the
machines that hold it.
