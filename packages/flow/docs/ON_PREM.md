# On-prem (air-gapped) clinic deployment

The deployable form the first healthcare pilot needs: a regulated clinic — e.g.
a Canadian family practice on Accuro over Citrix, under PHIPA — installs
openadapt-flow on **their own** machine so that **PHI never leaves their network
and nothing phones home**. This page is the architecture and the install story.
The runnable scaffold is in [`deploy/on-prem/`](../deploy/on-prem/); the
compliance posture is in
[`deploy/on-prem/COMPLIANCE.md`](../deploy/on-prem/COMPLIANCE.md).

> This is independent of optional hosted control-plane connectivity. There is
> **no** account, telemetry, public cloud model, or hosted-execution dependency
> in this lane. If a step here would require an internet call, it is wrong.

## What runs on the clinic machine

One clinic host (a server or a workstation next to the Citrix client) runs the
whole stack locally:

```
  Clinic host (offline; site firewall denies egress)
  ┌──────────────────────────────────────────────────────────────────┐
  │  jobs/inbox/*.job  ──►  run-queue.sh  ──►  openadapt-flow run      │
  │   (operator drops        (LOCAL queue,      (THE ENGINE:           │
  │    a PHI-free            claims/records/     compile-time bundle →  │
  │    job descriptor)       files jobs)        deterministic replay,   │
  │        │                     │               identity gate, effect  │
  │        │                     │               verify, halt)          │
  │        │                     │                    │                 │
  │        │                     ▼                    ▼ drives GUI       │
  │        │              audit/audit.log      Citrix/RDP/Windows        │
  │        │              (append-only,        session (Accuro) on LAN   │
  │        │               PHI-free,                 │                   │
  │        │               hash-chained)             ▼                   │
  │        │                     │            effects verifier →         │
  │        │                     │            local EMR API/DB/FHIR      │
  │        │                     ▼                                       │
  │        └────────►  runs/<id>/{report.json, REPORT.md,               │
  │                              checkpoints/, templates/*.png}          │
  │                                    │                                 │
  │                    storage_root on a FULL-DISK-ENCRYPTED volume      │
  │                                                                      │
  │   (optional) VLM appliance on a LAN GPU box — no retention           │
  └──────────────────────────────────────────────────────────────────┘
```

Components, and how each maps to what already ships in this repo:

| On-prem component | Backed by | Notes |
|---|---|---|
| **Engine** (compile → replay → identity gate → effect verify → halt) | `openadapt-flow` CLI (`run`/`replay`/`resume`/`certify`/`lint`/`teach`) | Deterministic, `$0`, no model calls by default. |
| **Local runner / scheduler** | `deploy/on-prem/bin/run-queue.sh` + `systemd/*.path` unit | NEW thin wrapper: a directory is the queue; event-driven or polling. No broker, no daemon framework, no network. |
| **Deployment wiring** | `deployment.yaml` → `openadapt_flow.deployment.DeploymentConfig` | Backend URL, system-of-record effect verifier, actuation, durability, policy. Empty file = fully local, zero egress. |
| **PHI scrubbing** | `openadapt_flow/privacy.py` + `openadapt-privacy` (`[privacy]` extra) | `OPENADAPT_FLOW_SCRUB=on` fail-closed; image redaction implied under `on`. |
| **At-rest protection** | Operator full-disk encryption + salted-hash `identity_template` + opt-in AES-256-GCM bundle/checkpoint sealing (`OPENADAPT_BUNDLE_KEY`) | See `docs/phi_at_rest.md`. Bundle JSON and template crops are sealed when encryption is enabled; KMS/key rotation remain operator responsibilities. |
| **Local audit log** | `deploy/on-prem/bin/audit-log.sh` → `audit/audit.log` | Serialized, append-only, hash-chained, PHI-free JSONL index over runs and release changes. |
| **Durable state** | `openadapt_flow/runtime/durable/` (`checkpoints/`, `pending_escalation.json`) | A halted run pauses durably and is resumable (`resume`/`approve`) — locally. |
| **Air-gap attestation** | `deploy/on-prem/bin/verify-airgap.sh` | NEW: proves the software-side no-egress posture; the firewall is the real control. |
| **Optional on-prem VLM** | `docs/deployment/ON_PREM_VLM.md`, `openadapt-flow-vlm-service` | LAN-only GPU box; opt-in; off by default (default install pulls no model). |

## "Nothing leaves your network" — concrete and verifiable

The air-gap is enforced by the clinic firewall; openadapt-flow's job is to (a)
need no egress and (b) let an operator *prove* it. Four defence-in-depth layers:

1. **No egress by default in the software.** The engine is deterministic and
   model-free: `runtime.allow_model_grounding` defaults `false`, and with no
   `OPENADAPT_FLOW_VLM_URL` set the replay makes **zero** outbound calls. There
   is no telemetry, analytics, license check, or update ping anywhere in the run
   path.
2. **Structural egress denial.** The systemd unit sets `IPAddressDeny=any`
   (kernel drops all IP traffic for the runner unless a LAN CIDR is explicitly
   allow-listed); the Docker Compose alternative puts the runner on an
   `internal: true` network (Docker installs no gateway to the internet).
3. **Fail-closed PHI handling.** `OPENADAPT_FLOW_SCRUB=on` makes a missing
   scrubbing capability *abort* rather than write plaintext PHI; `run-queue.sh`
   refuses to start unless it is `on`.
4. **Attestation.** `verify-airgap.sh` scans `onprem.yaml`, the referenced
   `deployment.yaml`, and the environment for any off-LAN URL or cloud API key;
   with `--probe` it actively curls a public canary and **asserts the call
   fails**; with `--audit` it walks the audit-log hash chain. It is the
   operator's repeatable pre-flight and periodic check.

```bash
OPENADAPT_FLOW_SCRUB=on ./deploy/on-prem/bin/verify-airgap.sh \
    --config onprem.yaml --probe --audit
# AIR-GAP ATTESTATION: PASS (no FAIL checks)
```

## How local artifacts are stored and protected

Everything lives under `storage_root` (default `/srv/openadapt`), which the
operator places on a **full-disk-encrypted** volume (LUKS / BitLocker /
FileVault — openadapt-flow never holds the disk key):

- **`bundles/`** — compiled bundles (`workflow.json` + `templates/*.png` +
  `workflow.py`). PHI-at-rest: the identity band is a salted **hash**, not
  plaintext, and identifier-bearing postconditions/landmarks are dropped when
  the `privacy` extra is active. Frames are image PHI.
- **`runs/<id>/`** — `report.json` (the identity/effect **audit trail** — keeps
  literal identifiers on purpose, so it is PHI), the scrubbed shareable
  `REPORT.md`, `checkpoints/` (durable resume state), and step frames.
- **`jobs/`** — the queue (`inbox`/`processing`/`done`/`failed`). Job files are
  **PHI-free** descriptors (bundle path + params-by-reference).
- **`audit/audit.log`** — append-only, hash-chained, PHI-free (see below).
- **`keys/`** — the vendor **public** key for verifying offline updates. Real
  secrets (identity salt, VLM token) live in the OS keychain and are referenced
  by env-var *name*, never stored here.

**Secrets.** Never committed, never in `onprem.yaml`. The salt and any VLM token
are held in the OS keychain / a root-only file and injected as environment
variables (`OPENADAPT_FLOW_IDENTITY_SALT`, `OPENADAPT_FLOW_VLM_TOKEN`).

**Encryption - honest status.** Operator full-disk encryption remains the
deployment baseline. In addition, `Workflow.save(encrypt=True)` and
`OPENADAPT_BUNDLE_KEY` opt into AES-256-GCM containers for `workflow.json`,
template crops, and durable checkpoints; keyed loads decrypt them in memory and
fail on a missing/wrong key or tampering. This is shipped but off by default.
The engine does not provide KMS integration, envelope-key rotation, backup-key
escrow, or hardware-backed key custody; those remain operator responsibilities.

## The local audit log

`audit/audit.log` is the tamper-evident **index** over the runs: newline-
delimited JSON, append-only, PHI-free. Each record carries UTC timestamp, run or
release event (`queued`/`started`/`verified`/`halted`/`failed`/`resumed` and
prepared/completed update, rollback, or migration), an opaque job id,
the bundle basename, the run-dir path, the process exit code, the OS actor, an
operator note, and `prev_sha` — a sha256 chain linking it to the previous line,
so any silent edit or deletion breaks every subsequent hash and is caught by
`verify-airgap.sh --audit`. The per-step PHI detail stays in each
`runs/<id>/report.json` under the encrypted volume; the audit log records *that*
a run happened and *how it ended*, never patient data. For stronger assurance,
make the file append-only at the OS layer (`chattr +a`) or export it to a LAN
WORM store — the hash chain is tamper-*evidence*, not tamper-*proof*.

## Where the automation sits relative to the Citrix client

The engine drives the target application through its configured backend. In a
Citrix/Accuro clinic the automation process runs **on the clinic host inside the
clinic network**, and reaches the application over the LAN via the RDP/pixel or
Windows backend (`docs/backends/RDP.md`, the `windows`/`rdp` extras) — the same
path a clinician's session already uses. The runner needs LAN reachability to
the Citrix/RDP endpoint and to the local EMR's system-of-record API (for effect
verification), and **nothing beyond the LAN**. No component sits between the
clinic and the internet. On pure-pixel Citrix/RDP substrates the identity ladder
falls back from DOM/a11y structured text to the pixel/OCR tiers — read
`docs/LIMITS.md` for the wrong-patient guarantees and their availability cost on
that substrate before relying on it.

## Offline updates (operator-pulled, signed — never phoned)

The clinic **never** auto-updates over the internet. Updates are prepared
out-of-band and pulled in by the operator:

1. An engineer builds a signed release (engine wheels / bundle) on a connected
   host and produces a **detached signature**.
2. The operator copies the archive + signature onto the clinic host on removable
   media, and points `onprem.yaml:updates` at them.
3. `install.sh --update` verifies the signature against the **pinned vendor
   public key** in `keys/`, installs into a fresh blue/green venv, runs the
   smoke test + `verify-airgap.sh`, atomically flips the runner over, and records
   prepared/completed events in the audit log. A checksum may detect transport
   corruption, but never substitutes for the required signature.

The update and rollback paths are implemented. Release archives are extracted
through bounded traversal/link/device checks, version metadata must match the
bundled wheel, and release state changes are serialized. Existing installations
using the earlier single `venv/` layout are migrated automatically; that venv is
retained as the first rollback target. See the complete operator procedure in
[`deploy/on-prem/UPDATE.md`](../deploy/on-prem/UPDATE.md).

## Install (summary)

```bash
cd deploy/on-prem
cp onprem.example.yaml onprem.yaml                 # edit storage_root, paths
# offline wheelhouse built off-site: pip download 'openadapt-flow[privacy]' -d wheels
sudo ./install.sh --config onprem.yaml --wheelhouse ./wheels --systemd
sudo systemctl enable --now openadapt-flow-runner.path
OPENADAPT_FLOW_SCRUB=on ./bin/verify-airgap.sh --config onprem.yaml --probe
```

Containers instead of systemd: `docker compose -f docker-compose.yml config`
(validate), then `docker compose up -d runner`. See
[`deploy/on-prem/README.md`](../deploy/on-prem/README.md) and
[`deploy/on-prem/UPDATE.md`](../deploy/on-prem/UPDATE.md) for the runnable
package and update/rollback runbook, and
[`deploy/on-prem/COMPLIANCE.md`](../deploy/on-prem/COMPLIANCE.md) for the
PHIPA/HIPAA-adjacent posture and boundaries.

## Related

- `docs/LIMITS.md` — what compiled replay does and does not guarantee (on-prem
  changes *where* the data lives, not these limits).
- `docs/PRIVACY.md` / `docs/phi_at_rest.md` — the PHI touchpoint + at-rest maps.
- `docs/deployment/ON_PREM_VLM.md` — the optional LAN-only VLM appliance.
- `docs/backends/RDP.md` — the Citrix/RDP pixel path.
