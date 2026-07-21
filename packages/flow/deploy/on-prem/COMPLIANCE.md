# On-prem compliance posture (PHIPA / HIPAA-adjacent)

**Not legal advice, and not a compliance guarantee.** This note describes the
technical substrate openadapt-flow provides for a regulated clinic to run
compiled automations on-premise. Whether a given deployment satisfies PHIPA
(Ontario), PIPEDA, HIPAA, or another regime is a determination for the clinic's
privacy officer and counsel, against their own risk assessment, agreements, and
safeguards. **We provide the software substrate; we do not sign a BAA, and no
part of this repository is a certification.**

## The one-line posture

Everything runs and stays on the clinic's own machine/network. PHI is processed
locally, protected at rest by the operator's full-disk encryption plus in-bundle
identity hashing, logged to a local append-only audit trail, and **nothing is
sent off the clinic network** — no telemetry, no cloud model, no auto-update.

## Data-flow diagram — all local

```
  Clinic network (air-gapped from the internet by the site firewall)
  ┌───────────────────────────────────────────────────────────────────────┐
  │                                                                         │
  │   Operator ──drops──► jobs/inbox/*.job    (PHI-free job descriptor)     │
  │                          │                                              │
  │                          ▼                                              │
  │                    run-queue.sh  ──► openadapt-flow run (the ENGINE)     │
  │                          │                    │                         │
  │             ┌────────────┘                    │ drives GUI              │
  │             ▼                                  ▼                         │
  │      audit/audit.log                 Citrix / RDP / Windows session      │
  │      (append-only, PHI-FREE,          (Accuro etc.) on the LAN          │
  │       hash-chained)                          │                          │
  │                          ┌───────────────────┤                          │
  │                          ▼                    ▼                          │
  │                  runs/<id>/                effects verifier              │
  │                   report.json  ◄── PHI       (local EMR API / DB /       │
  │                   REPORT.md (scrubbed)        FHIR on the LAN)           │
  │                   checkpoints/  ◄── PHI                                  │
  │                   templates/*.png ◄── image PHI                          │
  │                          │                                              │
  │                          ▼                                              │
  │        storage_root on a FULL-DISK-ENCRYPTED volume (LUKS/BitLocker)     │
  │                                                                         │
  │   (OPTIONAL) VLM appliance ◄──identifier crops (PHI, in-flight)──┐       │
  │      GPU box, LAN-only, no retention  ───────────────────────────┘       │
  │                                                                         │
  └───────────────────────────────────────────────────────────────────────┘
        ▲                                                        ▲
        │  NO outbound telemetry. NO cloud model. NO auto-update. │
        └──────────────── site firewall denies egress ───────────┘
```

Every arrow is inside the clinic network. The only PHI that ever moves between
machines is (a) the GUI/EMR traffic the clinic already runs on its LAN, and (b)
— **only if the operator opts into the VLM appliance** — identifier crops to a
LAN GPU box that does not retain them (see ON_PREM_VLM.md "PHI data-flow
boundary"). The default install has neither a VLM nor any other cross-machine
PHI path beyond the clinic's existing EMR access.

## What is encrypted (and what that means)

| Layer | Control | Real today? |
|---|---|---|
| The disk holding `storage_root` (bundles, runs, audit, PHI frames) | **Operator full-disk encryption** (LUKS / BitLocker / FileVault). openadapt-flow never holds the key. | REAL — this is the primary PHI-at-rest control. Operator-provisioned; `install.sh` reminds but cannot verify it. |
| The identity band inside `workflow.json` | **Salted-hash `identity_template`** — no plaintext name/DOB/MRN; optionally an external `OPENADAPT_FLOW_IDENTITY_SALT` kept out of the bundle. | REAL — see `docs/phi_at_rest.md`. Reduces exposure; a hash of a low-entropy identifier is brute-forceable by a holder of both bundle and salt, so it is **not** a cryptographic seal. |
| Identifier-bearing postconditions / landmarks | **Dropped at compile time** when the Presidio scrub is active. | REAL (requires the `privacy` extra). |
| The shareable `REPORT.md` free text and (opt-in) frames | **Presidio PHI scrubbing / image redaction**, fail-closed under `SCRUB=on`. | REAL (requires `privacy` extra + local NER model). |
| A single **sealed, encrypted bundle container** (AES/age envelope, decrypt-in-memory) | **Opt-in AEAD via `openadapt_flow.crypto`** — `Workflow.save(encrypt=True, key=…)` / `OPENADAPT_BUNDLE_KEY` seals `workflow.json` with **AES-256-GCM** (scrypt-derived key, domain-separated AAD); `encrypted: true` on the manifest + `Workflow.encrypted`. | **REAL (opt-in, shipped)** — see `docs/phi_at_rest.md`. A wrong/missing key or tampered ciphertext fails loud and safe. Unencrypted stays the default, so enable it explicitly for at-rest sealing beyond full-disk encryption. |
| `templates/*.png` (recorded screen crops) | Full-disk encryption + governance guards; opt-in bundle sealing writes only AES-256-GCM `*.png.enc` crops and decrypts them in memory. | REAL but opt-in. Unencrypted bundles still carry plaintext crops and require the storage boundary. |

**Honest bottom line on encryption:** at-rest protection is operator full-disk
encryption + one-way identity hashing + optional scrubbing, **plus opt-in
per-bundle AES-256-GCM sealing** (`OPENADAPT_BUNDLE_KEY`) for `workflow.json`,
template crops, and checkpoints. Enable the per-bundle seal for cryptographic
at-rest protection that does not depend solely on the volume; full-disk
encryption remains the baseline for recordings, reports, logs, and unsealed
bundles.

## Audit-log contents

`audit/audit.log` is newline-delimited JSON, append-only, **PHI-free by
construction**. Per record: UTC timestamp, event (`queued`/`started`/
`verified`/`halted`/`failed`/`resumed`), opaque job id, bundle *basename*, run
directory *path*, process exit code, OS actor, a short operator note, and
`prev_sha` — a sha256 chain over the previous line so silent edits/deletions are
detectable (`verify-airgap.sh --audit`). It records **what ran and the
outcome**, never patient data. The per-step identity/effect detail (which does
touch PHI) lives beside each run in `runs/<id>/report.json` +
`checkpoints/`, under the encrypted volume and your retention policy — the
audit log is the tamper-evident *index* over those, not a copy of them.

Tamper-**evidence**, not tamper-**proof**: a local root can recompute the chain.
For stronger assurance, make the log append-only at the filesystem layer
(`chattr +a` on Linux) and/or export it to a WORM store on the LAN.

## Honest boundaries (what we do NOT provide)

- **No BAA / no legal compliance guarantee.** The substrate is here; the
  agreements, DPIA/PIA, breach procedures, staff training, and sign-off are the
  clinic's.
- **Per-bundle cryptographic seal is opt-in** (AES-256-GCM via
  `OPENADAPT_BUNDLE_KEY`; see the table). It is off by default, does not cover
  recordings or run reports, and does not supply KMS/rotation, so full-disk
  encryption remains the baseline at-rest control.
- **The engine's own safety limits still apply** — the wrong-patient identity
  ladder, unarmed-step gaps, transactional-write caveats, and OCR ceilings in
  `docs/LIMITS.md` are unchanged by running on-prem. On-prem changes *where* the
  data lives, not *what the replay can and cannot guarantee*.
- **Air-gap enforcement is the site's firewall.** Our `internal:true` network,
  systemd `IPAddressDeny=any`, and `verify-airgap.sh` are defence-in-depth and
  attestation — they do not replace a correctly configured network boundary.
- **The optional VLM appliance moves identifier crops in-flight on the LAN.** If
  the clinic's risk assessment forbids that, leave the appliance disabled (the
  default) and the runner is fully local and model-free.
