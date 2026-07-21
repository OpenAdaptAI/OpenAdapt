# BYOC Connector (bring-your-own-cloud)

The BYOC Connector lets **OpenAdapt Cloud manage governed runs that execute
inside the customer's own environment** — their VM, VPC, or on-prem host. The
customer owns the Windows licensing and the data boundary: the PHI-bearing
compiled bundle and run report stay in *their* storage, and only PHI-free
status/halt metadata ever flows back to the control plane.

It is the tractable path to "hosted across every substrate": desktop / RDP /
Citrix-over-RDP runs the managed browser runner cannot host still get a managed
control plane, a governed engine, and an audit trail — without a single PHI byte
or pixel leaving the customer perimeter.

## Why pull, not push

The managed lane PUSHES work (`enqueueRun -> fetch(runner)`). A BYOC customer
runs the data plane behind their own firewall with **no inbound hole**, so we
cannot push. The Connector inverts this: it makes only **outbound HTTPS**
connections to the control plane and PULLS jobs down them (the shape of a GitHub
self-hosted runner or a Citrix Cloud Connector). **The customer opens zero
inbound ports.**

## The loop (all outbound)

```
register -> POST /api/connector/register   enroll once, get a per-connector token
poll     -> POST /api/connector/poll        long-poll; lease the next queued job
execute  -> openadapt-flow run ...          the governed admission gate + Replayer,
                                             against the CUSTOMER'S own storage
callback -> POST /api/internal/run-callback  PHI-free status/metrics
ack      -> POST /api/connector/ack          release the lease (done|failed)
```

## Install and run

The Connector ships with the engine (`openadapt-flow`), so a dispatched run is
the *same* fail-closed `openadapt-flow run` you would run locally.

```bash
pip install openadapt-flow

# 1. Enroll this machine once (mints + persists a per-connector token, 0600).
openadapt-flow connector enroll \
  --control-plane-url https://app.openadapt.ai \
  --enrollment-secret "$BYOC_ENROLLMENT_SECRET" \
  --org-id org_your_clinic \
  --profile /opt/openadapt/deployment.yaml \
  --storage-backend local \
  --storage-root /srv/openadapt        # a full-disk-encrypted customer volume

# 2. Run the daemon (poll -> execute -> report -> ack, until interrupted).
openadapt-flow connector run
```

Enrollment persists to `~/.openadapt/connector.toml` (mode 0600 — it holds the
token). A bare `connector run` reads it back. Every flag also resolves from an
env var (`CONTROL_PLANE_URL`, `BYOC_CONNECTOR_TOKEN`, `BYOC_ORG_ID`, ...).

## Data boundary and safety

* **Down (to the Connector):** a PHI-free job *descriptor* only — run/workflow
  ids, an opaque customer-storage *reference* (never our signed URL), PHI-free
  params, a secret *reference* (never a value), the org's resolved governed
  policy (safety + grounding), a run-scoped callback token, and the immutable
  bundle binding.
* **Up (to the control plane):** PHI-free status/metrics + a storage *path* into
  the customer's own store. **Never** the report body, screenshots, OCR text, or
  a patient identifier.
* **The bundle and report bytes** are read from / written to the **customer's own
  storage** (`--storage-root`, a local encrypted volume). Our control plane holds
  no URL to them and signs no access.

**Fail-closed everywhere.** A dispatch is refused (reported `failed`, no GUI
touched) when it is missing the governed safety policy, missing the run-scoped
callback token, carries an our-owned signed URL, or when the org enabled a
grounding rung whose API key env is not set on this machine. The governed `run`
itself refuses any bundle that is not certified, identity-armed, effect-verified,
and encrypted — those engine gates are unchanged, so identity checks, effect
verification, and halt-don't-guess all remain intact.

## Enabling the lane (control plane)

The lane is off by default. An operator enables it with
`BYOC_ENABLED=true` **and** a configured `BYOC_ENROLLMENT_SECRET`, and flips the
org's `deployment_kind` to `byoc`. See openadapt-cloud `src/lib/byocLane.ts`.

## What works today vs. what remains for production

**Works end to end (this release):**

* enrollment -> per-connector token -> outbound poll/lease;
* governed local execution via `openadapt-flow run` (fail-closed admission +
  Replayer) against the customer's own `local` storage;
* PHI-free status/halt/metrics callback with the run-scoped token + bundle
  binding the control plane verifies;
* cross-org isolation (a Connector only ever leases its own org's jobs);
* the resolved safety + grounding policy delivered and applied fail-closed.

**Remaining for production:**

* **Hardened auth.** Enrollment is a single org-shared secret today; production
  wants per-org (ideally short-lived, rotating) enrollment tokens, plus
  mTLS/private-link between the Connector and the control plane.
* **Full policy materialization.** The delivered safety block governs *dispatch*
  and the two fail-closed gates above; materializing every safety key into the
  engine's runtime config (so e.g. the delivered grounding endpoint is the one
  the Replayer dials) is the same cross-runner "runner-side consumption" half the
  Modal and Windows runners have not finished either. The operator's local
  `deployment.yaml` (`--config`) is authoritative for the engine posture today.
* **S3 / Azure Blob customer storage.** Only `local` is built into the engine
  Connector; the operator-reference agent (openadapt-cloud `connector/agent.py`)
  carries S3/Azure backends that need a customer cloud to exercise.
* **Autoscaling / HA.** One Connector process, one lease at a time; production
  wants supervised restart, multiple Connectors per org, and a server-side lease
  reaper for a Connector that dies mid-run.
* **Customer-facing install/packaging.** A signed installer / container image and
  an operator runbook (partly in openadapt-cloud `deploy/byoc/`).
