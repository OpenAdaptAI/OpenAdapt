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

## Integration API

The public engine exports:

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
