# Typed business decisions

A business decision is a finite human choice inside a compiled workflow. It is
not a general text prompt. It is not an operational halt action. It does not
prove record identity or prove that a write succeeded.

Use a business decision when the workflow must preserve human authority for a
declared policy choice. Examples include selecting one reviewed disposition or
choosing whether a qualified exception can continue.

The compiler or qualification interface can propose the decision contract. A
person does not have to edit JSON. The certified workflow version must contain
the reviewed contract before production use.

## Contract

`StateKind.BUSINESS_DECISION` carries `BusinessDecisionSpec` version
`openadapt.business-decision/v1`:

- one bounded question;
- one or more authorized roles;
- one declared enum output parameter whose choices equal the option values;
- at least two finite options;
- one exact successor for each option;
- optional local evidence requirements for each option;
- one expiry interval;
- one or more deterministic live-state revalidation predicates, including at
  least one direct affirmative frame check.

Bundle validation requires the output parameter to exist. It also requires the
state transitions to equal the option mapping. A decision state cannot contain
an action, loop, subflow, terminal result, or exception route.

Governed repair cannot change the decision contract or route around it. The
regression gate requires the decision to remain reachable and to continue to
dominate every certified option target and downstream state that it protected.
It also refuses a new decision that was not in the active qualified program.
Changing that control boundary requires a new qualification; it is not a heal.

The decision output has frame scope. A subflow can inherit a parent value. A
loop row can override it. A loop-local answer does not leak into another row or
the parent frame.

## Runtime sequence

1. Flow reaches the decision state and performs no application action.
2. The durable runtime records the exact program cursor and pauses.
3. The customer runner creates a signed request. The request binds the run,
   bundle, workflow contract, state, scope, parameters, decision contract, and
   expiry.
4. Desktop, Cloud, or another authenticated operator route shows only the
   declared options.
5. The route supplies an authenticated principal and its roles to
   `submit_business_decision`. It also supplies the exact required local
   evidence digests and an idempotency key.
6. Flow checks the role, option, expiry, evidence set, local content hashes, and
   idempotency binding. It writes a signed receipt and exact resume authority.
7. Resume authenticates the request and receipt. Flow binds the finite output
   in the current frame only.
8. Flow captures a fresh settled frame and evaluates the option's compiled
   revalidation predicates. A direct `TEXT_PRESENT` or `ANCHOR_RESOLVES`
   predicate must pass. A parameter, the retained answer, an absence check, or
   a Boolean expression that can be true without affirmative live evidence
   cannot authorize continuation.
9. Flow continues only to the successor bound into the signed receipt.
10. A successor action still runs all normal target, identity, policy,
    postcondition, effect, and durable-execution gates.

The request, receipt, and required evidence stay content-addressed inside the
customer run directory. The run report carries digests and local inventory
references. It does not carry the evidence bytes.

The signed receipt is the write-ahead answer authority. If the runner stops
after it writes the receipt but before it writes the answer pointer, restart
recovers that exact receipt. It refuses a different answer and never signs two
answers for one request. The submission lock is an operating-system advisory
lock, so the kernel releases it when a process exits or is killed.

If an unanswered request expires while the same durable pause is still active,
the runner signs a new request. The new request binds the retained predecessor
request by its digest and content hash. The old request stays in the local
inventory for audit, but it is no longer active and cannot accept an answer.
Issuance and submission use the same advisory lock, and submission checks the
active pointer again while it owns that lock. Thus, a late answer cannot race a
renewal and authorize the old request. Historical evidence authenticates the
complete retained renewal chain. A missing, changed, cyclic, or answered
predecessor makes that evidence invalid.

## Trust boundary

`BusinessDecisionPrincipal` is an input from an authenticated operator route.
The engine validates its role against the certified decision contract. The
engine does not turn a command-line role string into authentication.

Desktop or Cloud can construct the principal after its own sign-in and role
checks. A customer-local integration can use an operating-system or enterprise
identity policy. The same finite contract and signed receipt apply to all of
these routes.

A business answer is control authority only:

- It cannot satisfy `identity_armed`.
- It cannot satisfy a postcondition.
- It cannot create an effect-verification verdict.
- It cannot convert screen similarity or a human statement into `VERIFIED`.
- It cannot authorize a target that is not in the certified option mapping.

If the application changes after the person answers, live revalidation halts
before the selected successor action. If the successor is consequential, its
own entity identity and effect contract must still pass.

## Qualification authoring

`set_business_decision()` is the scriptable authoring boundary for Desktop,
CLI, API, and qualification-agent clients. The caller supplies the typed
`BusinessDecisionSpec`, an exact graph and state, and, for a new node, one
unambiguous insertion point. Flow derives the only admitted transition shape,
creates the finite enum output parameter, validates the complete workflow, and
invalidates the prior certification.

The API does not submit an answer and does not grant runtime authority. It
refuses an insertion that can silently redirect more than one path. It also
refuses a decision that changes an existing output parameter into an
incompatible contract. A client can therefore offer a guided editor without
requiring a person to edit the workflow manifest.

The CLI uses the same boundary:

```text
openadapt-flow qualify business-decision BUNDLE --input DECISION.json
openadapt-flow qualify business-decision BUNDLE --check
```

`DECISION.json` uses `openadapt.business-decision-authoring/v1`. It names one
graph, one state, the optional unambiguous insertion state, and one exact
`BusinessDecisionSpec`. A qualification agent can prepare this file. A person
can review it in a client before the client calls the command. The command
derives the transitions, creates the finite output parameter, validates the
complete workflow, saves the new qualification revision, and invalidates the
prior certification. It does not submit a runtime answer.

## Reviewed judgment cases

Qualification can retain the institutional knowledge that explains when a
human must choose a branch. `set_judgment_cases()` stores one local
`openadapt.judgment-case-set/v1` contract. The same contract is available from:

```text
openadapt-flow qualify judgment-cases BUNDLE --input CASES.json
openadapt-flow qualify judgment-cases BUNDLE --check
```

Each case binds:

- a closed typed fact schema;
- the exact workflow and decision contract;
- local evidence and optional review-note hashes;
- bounded reviewer provenance;
- one treatment: `automatic_rule`, `human_node`, or
  `more_evidence_required`.

The evidence paths are local relative paths. Flow reads and hashes their bytes
during certification. The exact case and evidence contract becomes part of the
certification evidence digest. A changed case, review note, or evidence digest
invalidates the match.

An `automatic_rule` case names a reviewed rule identifier and one finite
decision option. It requires a reciprocal contrasting case. This is a coverage
check only. Flow never converts the facts or a review note into executable
policy. The rule must be authored and qualified through the normal program
path.

A `human_node` case keeps the decision as a permanent runtime human choice. A
`more_evidence_required` case refuses certification. Thus, a single historical
answer never becomes production policy automatically.

## Operational halts are separate

An operational halt means that Flow cannot prove a runtime condition. The
attended surface can offer actions such as Continue, Reject, Teach, Escalate,
or Reconcile. These actions resolve the halt under their existing capability
and revalidation contracts.

A business decision represents a declared branch in the business workflow. It
has a finite answer schema and role policy before execution. Do not translate a
generic halt into a business decision, and do not use a business decision to
bypass a halt.

Flow does not issue a generic attended Continue/Skip/Teach capability for a
business-decision pause. The finite business-decision request is the only
continuation authority for that state. A workflow that needs a decline or
escalation path must declare it as an option and map it to an explicit state.

Phone delivery for operational halts is described in
[`DECISION_DELIVERY.md`](DECISION_DELIVERY.md). A mobile business-decision view
can use the same delivery infrastructure, but it must preserve the distinct
business-decision request and receipt schema.

### Portable mobile projection

The qualification project stores one optional
`openadapt.qualified-business-decision-delivery/v1` binding for each decision
that can reach a phone. The binding contains only reviewed static presentation
copy and exact policy references. It contains no key, token, screenshot, OCR
text, record value, or person identifier.

`set_business_decision_deliveries()` is the scriptable authoring boundary for
Desktop, CLI, API, and qualification-agent clients. It binds the exact graph,
state, decision-contract digest, mobile context cards, finite option detail,
role mapping, authenticated route, key IDs, and relay-capability digest. The
operation increments the qualification revision and removes the old
certification. Certification refuses a stale decision binding.

At an active durable pause,
`build_qualified_business_decision_cloud_relay()` reads that binding from the
certified bundle. It combines it with deployment-owned key bytes, tenant and
runner identities, and the runner token. It then creates and validates the
presentation, signed delivery policy, signed portable task, and Cloud relay.
The caller does not construct these internal contracts for each run.

The build refuses when the certification cannot be reproduced, the active
request differs from the qualified decision, a key ID changed, or the decision
has protected local evidence. A remote answer still grants only one finite
branch choice. The runner performs live state checks before any successor
action.

`openadapt_flow.interop.business_decision` projects a local signed request into
the separate `openadapt.business-decision-task/v1` contract from
`openadapt-types`. The projection carries opaque tenant, runner, run, pause,
request, bundle, workflow, decision, role-policy, presentation, relay, expiry,
and idempotency bindings. It carries finite option IDs and opaque successor
digests. It does not carry the question, option labels, role names, values,
screenshots, OCR text, or record identifiers.

The mobile view resolves its static reviewed question and option copy from the
exact presentation artifact that the signed delivery policy names. Each text
field is either local-only or remote-safe with a positive egress-review digest.
Projection compares the exact copy and its shared content digest with the
authenticated Flow request before it signs a task. A remote task is valid only
when all visible text has the one reviewed egress binding named by the policy.
The delivery policy also binds the allowed opaque roles, authenticated routes,
answer signing-key IDs, exact authentication profile, relay capability, and
expiry. These fields are qualification output. A caller cannot select them when
it projects a task.

The projection replaces the local run and pause identifiers with keyed opaque
aliases. The remote task does not carry local role names, live record values,
or free-text runtime data. If the decision needs protected local evidence, the
task is local-answer-only. A remote answer includes one option ID and one
idempotency key. The authenticated route adds the principal, mapped role,
authentication profile, route reference, and authentication-context reference
before it signs the answer.

The customer runner verifies both signatures, maps the opaque role back through
the qualification-owned role map, and calls Flow's normal business-decision
submission API. Flow derives its local idempotency binding from the exact signed
portable answer, its client idempotency key, and the task scope. A new signed
answer envelope cannot claim a receipt for an earlier answer. The mobile answer
receipt can report that Flow retained the exact answer and will revalidate the
live application. A retained receipt stays available after the answer window
expires; expiry stops a new answer but does not remove accepted audit evidence.
The portable receipt uses a separate schema from Flow's durable local receipt.
It cannot report `VERIFIED`; only the later execution and effect receipt can
prove the business result.

## Integration API

The public engine exports:

- `project_portable_business_decision_task()` to verify the reviewed
  presentation and signed delivery policy before it creates a remote-safe
  task;
- `admit_portable_business_decision_answer()` to authenticate and map one
  portable finite answer into Flow's local submission models;
- `project_recorded_business_decision_answer_receipt()` to confirm that Flow
  retained an answer and return a signed, non-success portable receipt;
- `BusinessDecisionStore.read_active_request()` to read and authenticate the
  active request;
- `BusinessDecisionStore.retain_evidence()` to retain local evidence by hash;
- `submit_business_decision()` to submit one finite answer with an
  authenticated principal;
- `BusinessDecisionStore.authenticate_evidence()` to verify retained decision
  evidence before it changes restored parameters.

The API is the correct integration boundary for Desktop, Cloud, and
customer-controlled operator services. A future CLI must consume a trusted
principal from a configured local identity policy. It must not let a user
self-assert an authorized production role.

### Customer-runner transport attestations

The customer runner can call
`create_runner_business_decision_signature_attestation()` after it verifies the
portable task and qualification-policy signatures. The helper binds that check
to the exact task, presentation, policy, Cloud role policy, tenant, runner, and
authenticated runner bearer. Cloud can then reject a changed or stale
registration envelope.

After Flow records the answer, the runner can call
`create_runner_business_decision_receipt_attestation()`. This helper first
verifies the portable receipt signature. It then binds the receipt digest and
answer ID to the same authenticated runner route.

These transport attestations do not replace Flow admission. The runner still
verifies the exact task and Cloud answer again before it submits the local
decision. The runner still performs fresh live-state revalidation before any
selected successor action.

`BusinessDecisionCloudRelay` connects these boundaries for one paused run. It:

1. verifies and registers the exact qualified task;
2. polls one answer leased to the same tenant and runner;
3. verifies the Cloud answer and maps its opaque role through the local role
   map;
4. stores the answer in Flow's durable business-decision journal;
5. returns the signed portable receipt and runner transport attestation.

`BusinessDecisionSupervisor` owns the same transport for a directory that can
contain many paused runs. It publishes each unanswered qualified task, polls
the runner queue once, and routes a leased answer by the exact task ID,
revision, and digest. A task key that names two local runs is refused. An answer
that names no current local task is not applied to another run.

The supervisor records the answer only. It does not resume the program and it
does not actuate the application. The customer runner can then start the normal
durable continuation. That continuation consumes the signed answer and performs
the fresh state, identity, action, postcondition, and effect checks.

An uncertain receipt request does not repeat the decision. Flow retains the
same local answer. The runner can submit the same receipt again when Cloud
redelivers the signed answer. The receipt still means only `answer_recorded` and
`recorded_pending_revalidation`. It does not mean that a successor action ran or
that the business effect is `VERIFIED`.
