# Enterprise architecture and security boundary

This document answers the first questions a security reviewer should ask. It
describes shipped controls and explicit gaps; it is not a compliance
certification, legal advice, a BAA, or an SLA.

## Deployment lanes

| Lane | Execution location | Network posture | Current status |
| --- | --- | --- | --- |
| Local evaluation | Operator workstation; bundled browser app or an explicitly supplied URL | No model or telemetry egress by default. The target URL is still network traffic when it is remote. | **Beta** on the reference browser path. |
| On-prem / air-gapped | Customer-controlled host and optional LAN VLM appliance | Site firewall is the primary boundary; systemd/Docker denial and `verify-airgap.sh` add defence in depth. | **Beta deployment foundation** with signed offline update and atomic rollback; see [`ON_PREM.md`](ON_PREM.md). |
| Hosted control-plane connection | Execution remains local; `login`, governed `push`, and `report-break` make explicit HTTPS requests | Opt-in only. Artifact egress requires a verified derivative and trusted destination. | **Live beta; operated outside this repository.** |
| Hosted execution | Runner/control-plane implementation is outside this engine repository | Engine installation alone does not move execution. | Consult the deployed service status and substrate matrix. |

## Components and data flows

| Component | Sees screenshots or identifiers? | Can transmit them? | Persists them? |
| --- | --- | --- | --- |
| Browser/desktop recorder | Yes: full frames, input events, typed values, element/window metadata. | No default egress. An explicit hosted `push` is a separate action. | Yes, in the recording directory. Password and declared secret fields are redacted on the browser path; desktop secret redaction is not yet supported. |
| Compiler | Yes: recording frames and event text. | No. | Yes, as bundle IR and target crops. Treat every bundle as sensitive. Opt-in AES-256-GCM seals workflow JSON and template crops. |
| Local replayer | Yes: live screenshots, bundle targets, parameters, postconditions, and optional identity evidence. | Only to the configured target/backend/effect endpoint, or to an explicitly enabled VLM. No telemetry/model egress is enabled by default. | Yes, `report.json`, `REPORT.md`, step/heal frames, and optional durable checkpoints. |
| System-of-record verifier | Sees configured identifiers/expected effects needed to query REST, FHIR, or a document store. | Yes, to the operator-configured endpoint. | The engine records verifier outcomes and contract hashes; the remote system owns its own logs. |
| Optional VLM appliance | May see target crops, full screenshots, intents, OCR, expected state, and identifier crops. | The runner sends them only when model grounding is explicitly enabled and a VLM URL is configured. | The service contract is no retention; the MLX development backend uses private temporary files and deletes them in `finally`. See [`deployment/ON_PREM_VLM.md`](deployment/ON_PREM_VLM.md). |
| Hosted ingest API | Receives only an explicit, approved deterministic archive. The server gets the exact archive hash/size, scrubber policy/version, coverage, unresolved-finding count, approval provenance, and separate runtime-semantics state in `openadapt.sanitization/v1`. | HTTPS to the recognized OpenAdapt origin or an exact-allowlisted customer origin. | Source artifacts remain local. Text/images are supported; unknown and unsupported types fail before approval. Privacy approval is not executability proof. See [`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md). |
| Hosted break reporting | Receives a PHI-minimal diagnostic derived from `report.json`, not the recording. | HTTPS only when invoked or enabled as a post-run hook. | Automatic payloads omit all intent/reason/error free text and include only hashes, status, resolver rung, and numeric metrics. |

The field-level PHI inventory is maintained in [`PRIVACY.md`](PRIVACY.md).

## Credentials and secrets

| Secret | Supported source | Important boundary |
| --- | --- | --- |
| Workflow input secret | `OPENADAPT_FLOW_SECRET_<FIELD>` at replay | Browser password/declared-secret values are not written to recording frames/events/bundle. Pixel/desktop secret redaction is deferred and the CLI refuses `--secret` there. |
| Bundle/checkpoint key | `OPENADAPT_BUNDLE_KEY` or an explicit library argument | The repository supplies AEAD, not KMS/key rotation. Use a customer-controlled keychain/KMS injection path. |
| Identity salt | `OPENADAPT_FLOW_IDENTITY_SALT` | Keep the same external salt available at compile and replay; do not store it in the bundle. |
| VLM bearer token | `OPENADAPT_FLOW_VLM_TOKEN` / service configuration | Bind loopback by default; require TLS termination plus auth before a non-loopback deployment. |
| Windows agent token and certificate pin | Deployment/backend configuration, preferably injected at runtime | The channel refuses plaintext to a non-loopback host; the short-lived self-signed certificate is pinned independently of bearer authorization. |
| FHIR/REST access token | Deployment configuration model | Do not commit populated deployment files. Current configuration loading does not itself provide a vault. |
| Hosted ingest token | CLI argument, `OPENADAPT_INGEST_TOKEN`, OS keychain, then existing mode-0600 config migration read | New plaintext config storage requires explicit `login --allow-plaintext-token`; prefer the `hosted` extra/keychain or environment injection. |

## Audit and cryptographic guarantees

- `report.json` is the machine audit artifact and intentionally may retain PHI
  needed for identity/effect review. `REPORT.md` is the shareable derivative and
  is scrubbed when the privacy capability is active; `SCRUB=on` fails closed.
- Bundle manifests carry content and per-asset SHA-256 digests. The fail-closed
  `run` gate re-verifies them and can pin a digest/compiler version. A digest is
  integrity evidence, not signer identity.
- Opt-in AES-256-GCM with scrypt-derived keys authenticates encrypted bundle JSON,
  template crops, and durable checkpoint payloads. Key management and rotation
  remain deployment responsibilities.
- Durable approvals record approver, time, resolution, workflow, run directory,
  and bundle version. The local record is not currently signed by an enterprise
  identity provider; do not describe it as non-repudiation.
- Sanitized-artifact approval freezes a deterministic archive and binds its
  exact SHA-256/size to reviewer, time, policy, scrubber, coverage, and per-file
  provenance. Changing the derivative deletes the frozen archive and invalidates
  approval. The reviewer record is local provenance, not an IdP signature or
  non-repudiation guarantee.
- The on-prem audit index is SHA-256 hash-chained and detects ordinary edits or
  deletion. A local root user can recompute it. Use an append-only filesystem or
  customer-controlled WORM/SIEM export for stronger assurance.
- Offline release updates verify signed archives, install into a fresh
  blue/green environment, run smoke and air-gap checks, and atomically switch
  the active release. Rollback restores the recorded previous release. Signer
  trust and customer-site rehearsal remain operator responsibilities.

## Model-assisted repair

Model access is off by default. Enabling it permits screenshots to leave the
runner for the configured model endpoint. A model suggestion enters at the
bottom of the resolution ladder and does not bypass identity, risk,
postcondition, effect, or policy checks. False rescues remain a documented
failure class; regulated deployments should use an on-prem endpoint, no
retention, explicit egress allow-lists, and a policy that halts consequential
low-confidence actions.

There is no separate "regulated build" in this repository that physically
removes every network-capable module. The on-prem posture is achieved through
configuration, the fail-closed run gate, OS/container network denial, and site
firewall policy. If procurement requires compile-time exclusion, treat that as
an unmet requirement rather than inferring it from the deployment scaffold.

## Security-review checklist

Before a consequential deployment, require evidence for all of the following:

- Every entity-sensitive and write action is identity-armed.
- Every write declares an idempotent system effect and has a configured,
  independent verifier.
- The selected policy passes and the fail-closed `run --dry-run` gate admits the
  exact sealed bundle digest.
- Scrubbing is pinned `on`, bundle/checkpoint encryption is enabled, keys come
  from a managed store, and the storage volume is encrypted.
- Network allow-lists include only the target, system of record, and optional
  on-prem model endpoint; public egress is denied and tested.
- Screenshot/report retention, access control, deletion, incident response, and
  backup handling are customer-approved.
- Update artifacts, signer identity, rollback behavior, and disaster recovery
  are tested in the customer environment rather than accepted from a runbook.
- Backend-specific evidence covers the real OS, DPI, remote-display protocol,
  target application, and identity ambiguity rate.

See [`LIMITS.md`](LIMITS.md), [`phi_at_rest.md`](phi_at_rest.md),
[`phi_in_transit.md`](phi_in_transit.md), and
[`VERIFICATION.md`](VERIFICATION.md) for the underlying evidence and gaps.
