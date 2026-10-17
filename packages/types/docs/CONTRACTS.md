# What the versioned wire contracts may carry

These are the messages that OpenAdapt components exchange: runners, the
Execute API, Cloud, Desktop, and agent connectors. Each one is a Pydantic
model in `openadapt_types` and a file under `openadapt_types/schemas/`. Most
files are JSON Schema. `execute-v1-openapi.json` is an OpenAPI document, and
two files hold test vectors for matching a runner to a plan's requirements.
A test checks that every file in that directory appears in this index.

| Schema files | Python models | What it holds | Details |
|---|---|---|---|
| `control-overlay-frame-v1.json`, `control-overlay-frame-v2.json`, `control-overlay-timeline-v1.json`, `control-overlay-timeline-v2.json` | `ControlOverlayFrameV1`, `ControlOverlayFrameV2`, `ControlOverlayTimelineV1`, `ControlOverlayTimelineV2` | PHI-safe execution overlay states, one at a time or as a timeline | [Control overlays](#control-overlays) |
| `execute-v1-openapi.json` | `ExecuteRequestV1`, `ExecuteStatusV1`, `ExecuteEvidenceReceiptV1`, `ExecuteClient` | Asynchronous qualified execution: request, status, terminal receipt, and signed webhooks | [OpenAdapt Execute v1](#openadapt-execute-v1) |
| `business-decision-task-v1.json`, `business-decision-presentation-v1.json`, `business-decision-delivery-policy-v1.json`, `business-decision-answer-v1.json`, `business-decision-answer-receipt-v1.json` | `BusinessDecisionTaskV1`, `BusinessDecisionPresentationV1`, `BusinessDecisionDeliveryPolicyV1`, `BusinessDecisionAnswerV1`, `BusinessDecisionAnswerReceiptV1` | A signed, finite human choice that the workflow declared before execution | [Business decisions](#business-decisions) |
| `human-decision-task-v1.json`, `human-decision-task-v2.json`, `human-decision-receipt-v1.json` | `HumanDecisionTaskV1`, `HumanDecisionTaskV2`, `HumanDecisionReceiptV1` | A signed, PHI-free projection of one attended runtime pause, and its terminal outcome | |
| `production-admission-registry-state-v1.json` | `ProductionAdmissionRegistryStateV1` | One signed current state for active and revoked Production admissions | [Production admission registry](#production-admission-registry) |
| `production-lifecycle-target-v2.json`, `qualification-release-reference-v2.json`, `production-lifecycle-admission-binding-v2.json` | `ProductionLifecycleTargetV2`, `QualificationReleaseReferenceV2`, `ProductionLifecycleAdmissionBindingV2` | One exact Production target, one immutable release index entry, and the target, release, artifact, digest, authority, and validity fields from one verified admission | |
| `runner-capability-manifest-v1.json`, `execution-requirements-v1.json`, `runner-capability-match-v1.vector.json`, `runner-capability-negative-v1.vector.json` | `RunnerCapabilityManifestV1`, `ExecutionRequirementsV1` | What one runner installation advertises, what one qualified plan requires, and test vectors for matching them | |
| `artifact-ref-v1.json`, `code-capability-manifest-v1.json`, `code-capability-admission-v1.json`, `process-evidence-receipt-v1.json` | `ArtifactRefV1`, `CodeCapabilityManifestV1`, `CodeCapabilityAdmissionEnvelopeV1`, `ProcessEvidenceReceiptV1` | A path-free artifact reference; an exact Python program with locked dependencies, typed I/O, permissions, and verifier bindings; its time-bounded admission; and one signed root over child receipts, human receipts, and the artifact graph | [Process artifacts and code capabilities](#process-artifacts-and-code-capabilities) |
| `reward-contract-v1.json`, `reward-certificate-v1.json`, `reward-evidence-receipt-v1.json` | `RewardContractV1`, `RewardCertificateV1`, `RewardEvidenceReceiptV1` | Training against one verified terminal effect. The receipt is not an Execute Seal | [Reward contracts](#reward-contracts) |
| `authentication-task-v1.json`, `authentication-receipt-v1.json`, `authentication-capture-policy-v1.json` | `AuthenticationTaskContractV1`, `AuthenticationReceiptV1`, `AuthenticationCapturePolicyV1` | A value-free login requirement bound to an existing attended task, its receipt, and the capture-exclusion policy | [Authentication tasks](#authentication-tasks) |
| `authoring-observe-v1.json`, `authoring-command-v1.json`, `authoring-bind-v1.json` | `AuthoringObserveV1`, `AuthoringCommandV1`, `AuthoringBindV1` | The hosted authoring MCP wire: a PHI-safe observe tree, the mailbox command envelope, and bind status | [Authoring MCP wire](#authoring-mcp-wire) |
| `clinic-inbox-v1.json`, `clinic-outbox-v1.json`, `clinic-tool-result-v1.json`, `clinic-mcp-tools-v1.json` | `ClinicInboxV1`, `ClinicOutboxV1`, `ClinicToolResultV1`, `ClinicMcpToolCatalogV1` | The clinic job handoff: an inbox job, an actuation intent, an MCP tool result, and the tool catalog | [Clinic job inbox and MCP tools](#clinic-job-inbox-and-mcp-tools) |
| `approver-identity-v1.json`, `approver-policy-v1.json`, `approval-record-v1.json`, `autonomy-eligibility-v1.json` | `ApproverIdentityV1`, `ApproverPolicyV1`, `ApprovalRecordV1`, `AutonomyEligibilityV1` | Who approved a step (a person, a rule, an agent or a system), which approvers each step class allows, one approval decision for the audit trail and receipt, and the evidence a workflow needs before it runs on exceptions only | [Approvals and read-back](#approvals-and-read-back) |
| `plain-outcome-v1.json`, `plain-progress-v1.json` | `PlainOutcomeV1`, `PlainProgressV1` | One run's result in plain words, or the state of a run that hasn't ended | [Plain results](#plain-results) |

`OracleObservation` and `EffectStrengthV1` have no schema file of their own.
See [Oracle tiers](#oracle-tiers).

## Control overlays

Version 1 remains the control-state contract. Version 2 adds an optional
normalized top-level viewport rectangle, the exact source viewport and DPR, and
an exact observation or decoded-media-frame binding, without changing V1.

Overlay schemas reject unknown fields and contain only closed presentation
labels and canonical statuses. Screenshot payloads, action-target selectors,
accessible names, text and values, typed input, identities, URLs, logs, report
bodies, and user-authored workflow names remain outside this public contract.
V2 may carry only normalized target geometry from a browser top-level CSS
viewport. Native and RDP device-pixel geometry is not part of this V2 schema.

Target geometry never carries locators, accessible names, values, URLs, or
screenshots. A private live observation uses a run/export-scoped HMAC reference
instead of a linkable raw frame hash. Published media uses the exact media
SHA-256 and decoded frame index. A renderer draws tracking only when that
binding matches; it omits the rectangle rather than replaying selectors,
interpolating movement, or inferring a missing target from adjacent events.
The runtime does not guess a future viewer transform. Desktop, Cloud, and media
renderers map the normalized rectangle through their actual content box.
If multiple runtime states land in one decoded media frame, the producer must
coalesce them deterministically; it must not invent extra media frames or
approximate their timing.

## Business decisions

The separate `business-decision-*-v1.json` files define a finite human branch
that the workflow declared before execution. They do not reuse the operational
halt actions. They carry only opaque bindings, option IDs, digests, counts, and
closed status values. The separate presentation artifact classifies each
question and option label as `local_only` or `reviewed_remote_safe`. Remote
delivery requires a positive egress-review digest for every field. The signed
delivery policy binds the exact presentation and review digest. Screenshots,
free-form notes, and live record values stay on the customer runner. An
accepted answer only selects a compiled branch. The next action must still pass
its live-state, identity, policy, and effect contracts.

## Production admission registry

`ProductionAdmissionRegistryStateV1` is the one current signed registry state.
Each admission reference is either `active` or `revoked`. The registry does not
add a second permit, lease, authority file, or revocation history.

A consumer first verifies the registry signature and checks its saved minimum
registry revision. It then hashes and parses the exact referenced admission
bytes. The consumer checks the target, claim, repository, release kind,
artifact set, artifact digests, and artifact authorities against the exact
policy target. It saves the newest verified registry revision. This check
rejects an older active registry after a later signed revocation.

`expires_at: null` means that the admission stays active until the signed
registry revokes it. When an expiry is present, it must follow `not_before`,
and the consumer enforces it at read time. The policy does not cap this expiry.

## OpenAdapt Execute v1

OpenAdapt Execute is the public asynchronous contract for a qualified
workflow. A partner sends an authorized request, receives an execution ID, and
then reads a terminal receipt or receives a signed webhook. The contract does
not expose a runner, customer data, evidence bytes, application recipes, or
Cloud control-plane internals.

The generated OpenAPI document is packaged at
`openadapt_types/schemas/execute-v1-openapi.json`. It defines asynchronous
execution submission, lifecycle status, terminal evidence receipts, and signed
decision and terminal webhooks, plus the opt-in `execution.halted` webhook.
Cloud sends `execution.halted` only to endpoints subscribed to it, next to
`execution.terminal`, when a run stops before any effect. It keeps `waiting_for_reconciliation` as a
lifecycle state and `reconciliation_required` as a terminal outcome. A
`verified` receipt needs an oracle read at tier 2 or 3 (see
[Oracle tiers](#oracle-tiers)).

The package also includes a
small Python client. The reference Python and TypeScript clients are in
[`examples/execute`](../examples/execute/). Use `https://app.openadapt.ai/api` as
the OpenAdapt Cloud base URL. It uses a partner-provisioned bearer token and
the client appends the stable `/v1` paths.
The client requires an HTTPS base URL and rejects redirects before it can send
the bearer token to another endpoint.

```python
from openadapt_types import ExecuteClient

client = ExecuteClient(
    base_url="https://app.openadapt.ai/api",
    bearer_token="partner-provisioned-token",
)
```

The client does not poll forever. A workflow can wait for a human decision or
reconciliation. Use the terminal receipt or signed webhook as the completion
signal.

## Oracle tiers

`verified` and `rolled_back_verified` require an observed effect strength
that maps to Seal oracle tier 2 or 3. Tier 0 is visual / OCR. Tier 1 is a
second-session UI read. Neither can mint production `VERIFIED`.

The partner adapter is `channel` plus `read`. Tiers, the gate, and a
ten-line file oracle are in [ORACLE.md](ORACLE.md).

## Process artifacts and code capabilities

`ArtifactRefV1` identifies an immutable output without putting a local path on
the wire. It binds the content digest, size, media type, producer, storage
boundary, data class, and verifier receipt. A pending artifact cannot carry a
verifier receipt. A terminal artifact state requires one.

`CodeCapabilityManifestV1` describes one exact Python program. It binds the
source archive, lockfile, direct entry point, typed I/O, declared outputs,
permissions, effect contract, oracle contract, and qualification campaign.
The entry point is an argument array. It is never a shell command.

`CodeCapabilityAdmissionEnvelopeV1` grants time-bounded authority to one exact
manifest. `ProcessEvidenceReceiptV1` binds the terminal process outcome to the
child receipts, human receipts, artifact graph, and evidence root. A verified
process requires oracle tier 2 or 3, no uncertain delivery, and no model call
during the run.

These contracts carry identities and digests. Program bytes, customer paths,
secret values, evidence bytes, and oracle recipes stay inside the declared
execution boundary. See [PROCESS_CAPABILITIES.md](PROCESS_CAPABILITIES.md) for
the package and runtime split.

## Reward contracts

`RewardContractV1`, `RewardCertificateV1`, and `RewardEvidenceReceiptV1` let a
trainer optimize against a verified terminal effect and know the bound the
checker carries. The contract binds task, environment, required and forbidden
effect contracts, and the oracle recipe by digest. The certificate carries
`epsilon`, `delta`, `threshold`, the calibration corpus digest, and an expiry
denominated in policy updates. The receipt carries the outcome, the component
vector, the scalar, and the certificate state.

`reconciliation_required` and `failed_platform` are unscored. They carry no
scalar and the contract cannot map them to zero. `certified` is true only at
oracle tier 2 or 3, with a current certificate that names this contract and
clears its `certificate_policy`. `calibration_scope` accepts `synthetic` and
`issuer` accepts `self_signed`, because nothing in this package can check a
production calibration or an issuer identity. Tier 0 and 1 receipts are
`development_only`.

The reward receipt is not an Execute Seal. It has its own schema id and none
of the Seal's fields. It says OpenAdapt verified one episode's terminal
effect. It does not say Flow governed the policy. See [REWARD.md](REWARD.md).

## Authentication tasks

`AuthenticationTaskContractV1` adds authentication semantics to the existing
`human_step` task. It binds the attended-task digest, admitted method classes,
principal class, user-presence rule, MFA rule, verifier, maximum session age,
and source-time capture policy.

`AuthenticationReceiptV1` carries keyed principal and session bindings. It
doesn't carry the values used to create them. It also binds the exact process
execution, step, random challenge, operator authority, verifier evidence, and
capture-exclusion receipt.

The operator's Done action requests verification. It can't prove that login
succeeded. The runner accepts the receipt only after every live binding,
freshness rule, user-presence result, MFA result, and verifier result agrees.

This is a wire contract, not a complete authentication feature. Capture,
Flow, and the operator surface must share the protected interval before a
release can claim the complete path.

## Authoring MCP wire

`AuthoringObserveV1`, `AuthoringCommandV1`, and `AuthoringBindV1` are the
public hosted-authoring contracts. They do not reuse `ComputerState` or
`UINode`. Sharing `ElementRole` is the only computer-state type on this
wire. The projector that drops field values, titles, and screenshots lives
in Capture. These models refuse those keys.

Observe is a PHI-safe projected tree: roles, automation ids, normalized
bounds, and `node_id`. It does not carry `value`, `title`, `screenshot`,
`text`, window titles, URLs, backend pixels, or extra keys. Cap is 200
nodes and 32 KiB. Any backend can advertise `agent_drive`, including
native Windows. RDP and Citrix are screen-only: their observations carry no
tree, and the agent works from the client window's pixels. A producer can
still mark an observation `coach_only` when it won't let an agent drive.

The mailbox envelope is `openadapt.authoring.command/v1`. Hosted `click`
is `{ node_id }` only. Pause results name a param and never a value.
Compile returns `needs_human_admit`, never `VERIFIED`. Bind tokens are
`oab_` plus 43 unreserved characters. Lease secrets are `oals_` plus 64
hex characters. Cloud `oar_` and pairing `oap_` are refused.

Command ids are `cmd_` plus one canonical Crockford ULID. Command times use
RFC 3339 with seconds and an offset. A command can live for at most 900
seconds. `parse_authoring_command` checks the full closed envelope and refuses
it at or after `expires_at`. `client_display` stays in the closed bind result
and bind status. It is not a command-envelope field.

## Clinic job inbox and MCP tools

`ClinicInboxV1`, `ClinicOutboxV1`, and `ClinicToolResultV1` are the public
handoff for a compiled clinic program. They are not a workbench. Schema
packs, extraction, review UI, NL2SQL, and OCR do not belong on this wire.

Inbox fields are `patient_token`, `artifact_path`, `source`, and
`recorded_at`. Identity is the opaque token. A name, MRN, screenshot, or
OCR string has no field. `artifact_path` is a relative POSIX path of
opaque segments; a live filename that could carry a person name is
refused.

Outbox fields are `action`, `template`, and `needs_human`. `template` is
an opaque id of a clinic-defined template, not the typed body. When
`needs_human` is true, `require_actuation_dispatch` refuses. OpenAdapt
types the template only after a human stamp.

The admitted MCP names are `run_harvest`, `run_attach_fax`, and
`run_create_triage_task`. Each returns `VERIFIED`, `HALTED`, or
`RECONCILIATION_REQUIRED`. `is_verified_success` is true only for
`VERIFIED`. There is no success flag a caller can set to launder a halt.
There is no tool that decides urgency or writes follow-up copy.

## Plain results

`plain-outcome-v1.json` describes one run's result for a person or a calling
agent: `result`, `label`, `may_have_changed`, `safe_to_retry`, `proof`,
`next_action`, `technical_code`, and `exact`. The label and next step come
from the registry in `openadapt_types/vocabulary/outcomes.json`, never from
screen text, so they can't carry PHI. `exact` holds only the status codes the
result came from; a value that isn't shaped like a status code is treated as
unrecognized and left out.

The `PlainOutcomeV1` model refuses a result that breaks the safety rule. Only
a result that proves no effect may set `may_have_changed` to false,
`safe_to_retry` needs `may_have_changed` false, and `sealed` proof needs an
Execute terminal outcome. The result and both flags must be the ones the
outcome table gives for the values in `exact`, unless `technical_code` names
a fallback, which only adds caution. The JSON Schema names the rule in
`x-openadapt-safety-rule` but doesn't enforce it, so code in another language
checks itself against `outcome-vectors.json`. `plain-progress-v1.json` covers
a run that hasn't ended; it always has `safe_to_retry` false.
[VOCABULARY.md](VOCABULARY.md) has the full mapping.

## Approvals and read-back

`openadapt_types.approval` is the one contract for who decides a
consequential step, when a step waits, and how a save was checked.

- `ApproverIdentityV1` names the approver: `person`, `rule`, `agent` or
  `system`, with an opaque id and how it was identified (`cloud_user`,
  `unauthenticated`, `policy`, `local_cli`, `signed_webhook`,
  `mcp_session`). A rule pins its rule set by digest.
  `approver_from_legacy` maps the older `decided_by` and `approver_kind`
  fields onto it. A record that says only `automation` or `unknown` is
  refused, because it doesn't say who decided.
- `ApprovalModeV1` says when a step waits: `not_gated`, `each_item`
  (the default, and what an unknown value means), `batch`, or
  `problems_only` (exceptions only). The first three values and
  `problems_only` are the ones Flow already stores.
- `ApproverPolicyV1` says which approver kinds may decide each step class.
  The default is a person for every class. A `clinical_write` step takes a
  non-person approver only when the policy pins the customer's opt-in.
- `ApprovalRecordV1` is one decision as the audit trail and receipt carry
  it. `training_label` is true only when a person decided; rule, agent and
  system decisions are recorded and never learned from.
- `ReadBackTier` is the customer-facing check ladder. Its labels are
  RFC-0002's `verification_tier` values: `independent_readback` (the system
  of record, such as an API, FHIR, a database or a report export, or a
  separate session), `screen_readback` (the acting session or screen read
  it) and `unchecked`. `read_back_tier` maps Flow tiers and the finer
  strength names onto it, weakest wins, and anything unknown maps to
  `unchecked`. `per_item_review_reason` returns a reason for every save
  except a confirmed independent read-back; given the fine `strength`, it
  also refuses a separate-session read (`separate_session_only`), since only
  a system-of-record read is clean. A screen-only or missing check always
  goes to a person.
- `ApproverIdentityV1` carries RFC-0002's `authorization.approver` fields
  (`kind`, opaque `id`, `policy_ref`), plus `auth`, `auth_method` and
  `on_behalf_of`, which say how the approver was identified.
- `AutonomyEligibilityV1` and `AutonomyEligibilityProvider` are the evidence
  a workflow needs before `problems_only`: a pinned calibration artifact and
  an audit miss-rate bound under the target, with at least 1% of clean saves
  still audited. A provider raises `EligibilityUnavailable` when there is
  none.

Nothing in this module approves a step. Callers decide; these types record
and check the decision.
