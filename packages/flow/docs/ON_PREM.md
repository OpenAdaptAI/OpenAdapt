# On-prem (air-gapped) deployment

A regulated organization, such as a medical practice whose records application
runs over Citrix or RDP, installs openadapt-flow on **its own** machine so that
**PHI never leaves its network and nothing phones home**. This page is the
architecture and the install story.
The runnable scaffold is in [`deploy/on-prem/`](../deploy/on-prem/); the
compliance posture is in
[`deploy/on-prem/COMPLIANCE.md`](../deploy/on-prem/COMPLIANCE.md).

> This is independent of optional hosted control-plane connectivity. There is
> **no** account, telemetry, public cloud model, or hosted-execution dependency
> in this lane. If a step here would require an internet call, it is wrong.

## What runs on the site host

One host (a server or a workstation next to the Citrix client) runs the
whole stack locally:

```
  Site host (offline; site firewall denies egress)
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
  │        │              (append-only,        session on the LAN        │
  │        │               PHI-free,                 │                   │
  │        │               hash-chained)             ▼                   │
  │        │                     │            effects verifier →         │
  │        │                     │            local records API/DB/FHIR  │
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

The air-gap is enforced by the site firewall; openadapt-flow's job is to (a)
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

**Encryption.** Operator full-disk encryption remains the deployment baseline.
For an application-sealed production candidate, inject
`OPENADAPT_BUNDLE_KEY` from the customer-controlled secret manager and run
`openadapt-flow seal ./bundle-v2 --out ./bundle-prod`, then certify that exact
destination with
`openadapt-flow certify ./bundle-prod --policy clinical-write`. The command
preserves the source, refuses an existing destination, encrypts `workflow.json`
and template crops with AES-256-GCM, and verifies the resulting integrity
digest. Sealing changes the artifact contract and explicitly expires prior
certification; a versioned qualification campaign must therefore run its cases
and `qualify certify` against the sealed destination. Durable checkpoints use
the same environment key; keyed loads decrypt in memory and fail on a
missing/wrong key or tampering.
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

The engine drives the target application through its configured backend. When
the application is published over Citrix, the automation process runs **on the
site host inside the site network**, and reaches the application over the LAN
via the RDP/pixel or Windows backend (`docs/backends/RDP.md`, the `windows`/`rdp` extras) — the same
path a user's session already uses. The runner needs LAN reachability to
the Citrix/RDP endpoint and to the application's system-of-record API (for effect
verification), and **nothing beyond the LAN**. No component sits between the
site and the internet. On pure-pixel Citrix/RDP substrates the identity ladder
falls back from DOM/a11y structured text to the pixel/OCR tiers — read
`docs/LIMITS.md` for the wrong-patient guarantees and their availability cost on
that substrate before relying on it.

## Offline updates (operator-pulled, signed — never phoned)

The site **never** auto-updates over the internet. Updates are prepared
out-of-band and pulled in by the operator:

1. An engineer builds a signed release (engine wheels / bundle) on a connected
   host and produces a **detached signature**.
2. The operator copies the archive + signature onto the site host on removable
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
regulatory posture and boundaries.

## Unattended service on macOS

`openadapt-flow service run` is a long-running loop for a service manager to
keep alive. Each cycle it runs `bin/run-queue.sh once`, writes an offline Seal
for any finished run that lacks one, folds new Seals into a local batch, and
writes `service/heartbeat.json`. It sets strict no-outbound mode
(`OPENADAPT_NO_OUTBOUND=1`, see [`OFFLINE_SEALS.md`](OFFLINE_SEALS.md)) for
itself, the queue, and every run, and it refuses to start if that variable is
explicitly off unless you pass `--allow-outbound`.

Run the installer as the service account, a dedicated macOS user that does
nothing else. It needs administrator rights (sudo) and stops with a clear
message if they are not granted.

```bash
deploy/on-prem/bin/install-macos.sh --root /Users/Shared/openadapt \
  --openadapt-flow /Users/Shared/openadapt/current/venv/bin/openadapt-flow \
  --session-profile /Users/Shared/openadapt/session.rdp
```

It installs:

| Item | What it does |
|---|---|
| `ai.openadapt.flow-service` (LaunchAgent) | Starts when the service account signs in. `KeepAlive` restarts it whenever it exits, at most every 10 s. |
| `ai.openadapt.flow-health` (LaunchAgent) | Every 60 s runs `service health --restart-on-fail`, which calls `launchctl kickstart -k` when the service is down, stale or stuck. |
| `ai.openadapt.flow-boot-check` (LaunchDaemon, runs as the service account) | At each boot, waits for the service account to sign in and opens a notice and sends alerts if it does not (see [After a restart](#after-a-restart)). |
| `ai.openadapt.flow-pf` (LaunchDaemon) and `/etc/pf.anchors/ai.openadapt.flow` | Strict mode only: outbound rules for the service account (see [Firewall rules](#firewall-rules)). |

A LaunchAgent runs in the user's login session, which a GUI or RDP client
window needs. Use `pmset` to stop the machine sleeping during run windows.
`install-macos.sh --uninstall` removes the agents, the daemons and the
anchor. `--render-only DIR` writes every file to `DIR` and applies nothing.

### The allow list

Strict no-outbound mode blocks the LAN too. The allow list starts empty, so
only this machine is reachable. `--session-profile` reads the host (and the
gateway, when one is in use) from a FreeRDP session file or a `.rdp` file,
adds only those addresses to `ROOT/no-outbound-allow.json`, and prints each
one:

```text
no-outbound allow list: deriving from /Users/Shared/openadapt/session.rdp
added: 10.20.0.15/32 port 3389  (session host, from /Users/Shared/openadapt/session.rdp)
```

To add or remove an address by hand, then reload the firewall rules:

```bash
openadapt-flow no-outbound allow --file /Users/Shared/openadapt/no-outbound-allow.json add 10.20.0.40 --port 5432
openadapt-flow no-outbound allow --file /Users/Shared/openadapt/no-outbound-allow.json remove 10.20.0.40
deploy/on-prem/bin/install-macos.sh --refresh-pf --root /Users/Shared/openadapt
```

See [`OFFLINE_SEALS.md`](OFFLINE_SEALS.md#the-allow-list) for the file format.

### After a restart

The default is **no automatic login**. After a restart the service does not
run until a person signs in to the service account; queued jobs wait in
`jobs/inbox/`.

The boot check runs before anyone signs in. It records the restart in
`service/events.jsonl` and waits. If the service account has no session after
`reboot.alert_after_s` (300 s by default), it opens a notice in
`service/notices/` and sends an alert through each configured channel. When
someone signs in it records how long the service waited.

- **Local-only installs** (no alert channels): the notice and the events are
  still written. The console counts open notices in its notification, so the
  OpenAdapt Desktop tray shows them, and `service health` warns on
  `service_notices`. List and acknowledge them with
  `openadapt-flow service notices --root ROOT [--ack ID]`.
- **Alert channels** are opt-in, in `ROOT/service/service.json`. An alert
  carries only the install name, the event, the time and a fixed sentence:

  ```json
  {
    "schema": "openadapt.onprem-service-settings/v1",
    "name": "records-host-2",
    "reboot": {"alert_after_s": 300, "auto_login": false},
    "alerts": {"channels": [
      {"kind": "webhook", "name": "ops", "url": "http://10.20.0.9:8080/openadapt"},
      {"kind": "command", "argv": ["/usr/local/bin/site-alert"]}
    ]}
  }
  ```

  A `webhook` gets the alert as a JSON `POST`; a `command` gets it on
  standard input. In strict mode a webhook host must be on the allow list,
  like any other peer. A failed send is recorded in `service/events.jsonl`.

**Opting into automatic login.** A site that must resume without a person
can turn on automatic login for the service account:

1. Run the installer with `--auto-login`, or set `"auto_login": true` under
   `reboot` in `service/service.json`. This records the choice; the boot
   check then alerts if the automatic login has not produced a session in
   time.
2. Turn on automatic login in macOS: System Settings > Users & Groups >
   Automatically log in as, or `sudo sysadminctl -autologin set -userName
   <account>`. macOS does not allow automatic login while FileVault is on.
   Weigh that against the site's disk-encryption policy before you choose it.

A mode that runs without any desktop session (no sign-in at all) is on the
roadmap. It is not available today.

### Firewall rules

In strict mode the installer adds `pf` rules by default. They block every
outbound TCP and UDP packet from sockets owned by the service account, except
to loopback and to the allow list. The kernel enforces them for every process
the account runs, including the remote-desktop client and other children
that the in-process hook cannot see.

```text
pass out quick on lo0 all
pass out quick proto tcp from any to 10.20.0.15 port 3389 user svc-records keep state
block return out log quick proto { tcp udp } from any to any user svc-records
```

The installer renders the anchor from the allow list
(`openadapt-flow service pf render`), checks it
(`openadapt-flow service pf check`, then `pfctl -nf`), installs it at
`/etc/pf.anchors/ai.openadapt.flow`, adds the two anchor lines to
`/etc/pf.conf` (after a backup to `/etc/pf.conf.openadapt-backup`), and loads
it. The `ai.openadapt.flow-pf` daemon re-enables pf and reloads
`/etc/pf.conf` at each boot. The result is recorded in
`service/firewall.json`, and `service health` warns when strict mode runs
without the rules.

This needs administrator rights at install. Without them the installer stops.
`--no-pf` installs without the rules; the health check then reports it.

Limits: `pf` matches the account only for TCP and UDP, so other protocols
(ICMP, for example) are not covered, and the rules apply to the service
account only. The site firewall stays the primary control. Connection
sampling (`service egress-audit`) stays on as the audit record.

### Health

```bash
openadapt-flow service health --root /Users/Shared/openadapt
```

It prints every check as JSON and exits 1 if any check fails:

| Check | Fails when |
|---|---|
| `heartbeat`, `running`, `fresh` | No heartbeat, the service process is gone or stopped, or the last heartbeat is older than 3 poll intervals (at least 60 s) |
| `stuck_jobs` | A job has sat in `jobs/processing/` longer than `--stuck-after` (1 h) |
| `seal_key` | The Seal key is readable by group or others |

It warns, without failing, on `failed_jobs`, `unsealed_runs`, a last cycle
with errors, a service running with `--allow-outbound`,
`blocked_outbound` (connection attempts the no-outbound hook refused),
`service_notices` (for example, a restart that waited for a sign-in), and
`firewall` (strict mode without the `pf` rules).

### Outbound-traffic audit

```bash
openadapt-flow service egress-audit --root /Users/Shared/openadapt --samples 30 --interval 2
```

The audit finds the service's process and all its descendants (`ps`), then
samples their open sockets (`lsof -nP -i`) the requested number of times. Its
verdict is `clean` only if no sampled socket had a peer off this machine or
outside the allow list (`ROOT/no-outbound-allow.json` and
`OPENADAPT_NO_OUTBOUND_ALLOW`). It also reports listeners bound to all
interfaces and counts the refusals in `audit/outbound-blocked.jsonl`. The
report is written to `audit/egress-audit-<time>.json` and sealed with the
local key. The command exits 0 only for `clean`.

What the evidence covers: the in-process hook refuses outbound connections
from the service's Python processes before any packet is sent; the audit
covers every process in the tree, including FreeRDP and other child
processes, at each sample. A connection that opens and closes between samples
in a non-Python child is not seen by the audit. The site firewall remains the
primary control. On macOS, the `pf` rules the installer adds in strict mode
close that gap for TCP and UDP (see [Firewall rules](#firewall-rules)).

### Windows notes

The same commands run on Windows; only the service manager differs.

- **Service.** Wrap `openadapt-flow service run --root C:\OpenAdapt` with a
  service wrapper such as NSSM, or a Task Scheduler task set to run at logon
  and restart on failure. Use a logon session if the run drives a GUI or RDP
  client window. Set `OPENADAPT_NO_OUTBOUND=1` and `OPENADAPT_FLOW_SCRUB=on`
  in the service environment.
- **Restart.** `service restart` runs `Restart-Service -Name <label>`; name
  the service `ai.openadapt.flow-service` or pass `--label`. Configure service
  recovery to restart on the first, second and later failures.
- **Health.** Schedule `service health --root C:\OpenAdapt --restart-on-fail`
  every minute.
- **Egress audit.** `service egress-audit` needs `ps` and `lsof` and reports
  `unavailable` on Windows. Use `Get-NetTCPConnection -OwningProcess <pid>`
  and `Get-NetUDPEndpoint` for the service's process tree instead, and add a
  Windows Defender Firewall outbound block rule for the service's
  `python.exe` and its child programs.
- **Run queue.** `run-queue.sh` needs bash (Git Bash or WSL). A native
  PowerShell queue is not part of this change.

## Related

- `docs/LIMITS.md` — what compiled replay does and does not guarantee (on-prem
  changes *where* the data lives, not these limits).
- `docs/PRIVACY.md` / `docs/phi_at_rest.md` — the PHI touchpoint + at-rest maps.
- `docs/deployment/ON_PREM_VLM.md` — the optional LAN-only VLM appliance.
- `docs/backends/RDP.md` — the Citrix/RDP pixel path.
