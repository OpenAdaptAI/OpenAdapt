# `deploy/on-prem/` — air-gapped clinic deployment package

The deployable form a regulated clinic installs so that **PHI never leaves their
network and nothing phones home**. A single clinic machine/server runs the
openadapt-flow engine + a local run queue + PHI scrubbing + local encrypted
storage + a local audit log, fully offline.

Start with **[../../docs/ON_PREM.md](../../docs/ON_PREM.md)** (architecture +
install story) and **[COMPLIANCE.md](COMPLIANCE.md)** (posture + data-flow).

## Contents

| File | What | REAL vs STUB |
|---|---|---|
| `onprem.example.yaml` | Install config (storage roots, audit path, egress posture, offline-update pointers). Copy to `onprem.yaml`. | REAL (data file) |
| `install.sh` | Stand up storage layout + first versioned release (offline wheelhouse) + systemd units, then run the air-gap gate. Also `--update` (atomic, verified, rollback-able) and `--rollback`. | REAL for layout/release/systemd/gate/update/rollback; STUB only for FDE provisioning |
| `bin/lib-release.sh` | Release manager: pinned-key signature verification, optional checksum, bounded extraction, serialized atomic activation, legacy migration, smoke check, and rollback. | REAL |
| `bin/safe-extract-release.py` | Rejects traversal, links, devices, duplicates, and oversized release archives before build. | REAL |
| `bin/run-queue.sh` | Local, directory-based run queue. Claims jobs, runs `openadapt-flow run`, records outcomes, files done/failed. Fail-closed on `SCRUB!=on`. | REAL (thin wrapper over the shipped CLI) |
| `bin/audit-log.sh` | Append-only, hash-chained, PHI-free audit writer (run + release-lifecycle events). | REAL |
| `bin/verify-airgap.sh` | Asserts no outbound path: config/env/deployment URL scan, optional active egress probe, optional audit-chain walk. | REAL (best-effort; firewall is primary) |
| `bin/test-update.sh` | Hermetic offline proof of signed update, refusal paths, legacy migration, data preservation, rollback/recovery, locks, and audit-chain serialization. | REAL (43 baseline assertions; 45 when local GPG key generation is available) |
| `UPDATE.md` | Offline update + rollback runbook: build a signed release, apply it atomically, revert instantly. | REAL (doc) |
| `systemd/openadapt-flow-runner.service` + `.path` | Event-driven runner unit with kernel-level egress denial (`IPAddressDeny=any`). | REAL (Linux/systemd) |
| `docker-compose.yml` + `Dockerfile` | Container alternative on an `internal:true` (egress-blocked) network; optional LAN-only VLM appliance profile. | REAL topology; STUB image build (needs offline `./wheels`) |
| `COMPLIANCE.md` | PHIPA/HIPAA-adjacent posture, data-flow diagram, what's encrypted, boundaries. | REAL (doc; non-legal) |

## 60-second install (systemd host)

```bash
cp onprem.example.yaml onprem.yaml            # edit storage_root etc.
sudo ./install.sh --config onprem.yaml --wheelhouse ./wheels --systemd
sudo systemctl enable --now openadapt-flow-runner.path

# place a compiled bundle + a deployment.yaml under storage_root, then:
cat > /srv/openadapt/jobs/inbox/triage-0001.job <<'EOF'
bundle=/srv/openadapt/bundles/vitals-triage
params=patient_ref=PT-INTERNAL-42;note=Reviewed
EOF
```

The runner picks up the job, runs it through the deterministic engine, appends a
PHI-free record to `audit/audit.log`, and files the job under `jobs/done` (exit
0) or `jobs/failed` (a fail-safe halt). Re-run the gate any time:

```bash
OPENADAPT_FLOW_SCRUB=on ./bin/verify-airgap.sh --config onprem.yaml --probe --audit
```

## Update & rollback

Patch the box offline; revert instantly if a release misbehaves. Full runbook in
**[UPDATE.md](UPDATE.md)**.

```bash
# apply a staged, signed release (verify -> build -> smoke -> atomic flip)
sudo ./install.sh --update --config onprem.yaml

# revert to the previous release instantly (symlink swap, no rebuild)
sudo ./install.sh --rollback --config onprem.yaml
```

Releases live in root-owned `releases/<version>/`; the service account cannot
modify release code or the activation pointers. `current` is an atomic symlink
the systemd unit runs through. An update is **never** made live until its
signature verifies against the pinned vendor key, its optional checksum matches,
its archive passes bounded extraction, and it passes a smoke check. Customer data
(`bundles/ runs/ jobs/ audit/ keys/`) remains outside the release tree.

## Non-negotiables

- **`OPENADAPT_FLOW_SCRUB=on`** — fail-closed PHI scrubbing. `run-queue.sh`
  refuses to start otherwise.
- **`storage_root` on a full-disk-encrypted volume** — the baseline at-rest
  control; optional bundle/checkpoint sealing is additional (see COMPLIANCE.md).
- **No cloud keys, no off-LAN URLs** — `verify-airgap.sh` must PASS before PHI.
- Offline, operator-pulled, signed updates only — never auto-update. Updates are
  verified + smoke-tested before an atomic flip and are instantly rollback-able.
