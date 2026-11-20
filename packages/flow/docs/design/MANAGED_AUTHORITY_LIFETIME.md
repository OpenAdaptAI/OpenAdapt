# Managed authority lifetime

A managed program dispatch needs two different kinds of authority. Its local
verification binds what may run. Its remote one-use delivery permit bounds when
one input may start. Neither a prior successful run nor a historical receipt
authorizes another input.

## Dispatch scope

`jit.dispatch_authority.verify_program_dispatch` verifies a real signed
admission, the bound read-back configuration, current public keys and principal
registry, and the local run gate. It returns a `ManagedDispatchBinding` carrying
an opaque, process-local proof. Serialized envelopes never contain that proof.

The proof binds the exact authorization, run id, dispatch-binding digest,
resolved bundle path and content, workflow, input values and worklists, policy,
admission, read-back configuration, trust registry, remote authority origin and
dispatch session. The shared managed-envelope parser rejects unknown raw
authorization fields before they can disappear into normalized historical
models. This also covers structured API-step and write-approval child models.
Declared string aliases and normal omitted defaults remain supported; duplicate
or misspelled aliases refuse. Public `Replayer.run`, `deployment.build_replayer` and CLI
execution use this same binding. A public runtime call claims it once, before
backend callbacks. A second call must verify a new binding. Changing runner-local
safety settings clears the proof and requires verification again.

For a Python deployment, obtain the binding through the shared verifier before
constructing the replayer:

```python
from openadapt_flow.jit.dispatch_authority import verify_program_dispatch

verified_binding = verify_program_dispatch(
    dispatch_binding,
    bundle_dir=bundle,
    workflow=workflow,
    params=params,
    worklists=worklists,
    policy_source=policy_source,
    effect_verifier=effect_verifier,
    key_dir=public_trust_directory,
)
# Pass verified_binding as managed_dispatch_binding to Replayer or
# deployment.build_replayer, with the same inputs and policy.
```

A durable resume reconstructs this proof from retained inputs and current
admission/trust data. It also supplies the actual reopened target origin. An
absent or different live target refuses; the admission's declared origin cannot
stand in for observing the target that will receive input.

## Input lifetime

Live delivery accepts only `openadapt.production-delivery-permit-artifact/v3` and payload
`openadapt.production-delivery-permit-payload/v3`. The signed payload adds one
exclusive UTC `delivery_not_after` deadline, bounded by registry expiry and
`registry_checked_at + 60 seconds`. Its signature domain is
`openadapt.production-delivery-permit-payload.v3\0`.

The receiver requires an exact retained pending permit, a matching live owner
and protected progress, and current dispatch scope. It then samples its trusted
clock immediately before the input call. That sample must be before both the
signed deadline and the exact registry expiry, at or after issuance and the
registry check, and strictly less than 60 seconds after that check. Preparation, local trust
reads and API form checks happen before this final sample. A denied input does
not fetch another permit or mark itself delivered.

Desktop calls, initial runs and durable continuation use this boundary. The
built-in REST and form actuators invoke it for each actual request. Intermediate
form writes and the final save each receive their own one-use permit and retain
their acknowledgment before proceeding. Managed adapters without this
per-request contract refuse before execution. Managed execution uses the exact
built-in actuator types; deployment subclasses must use the local execution
contract until a separately qualified boundary is available. REST redirects,
configured retries, authentication resends and response resend hooks are refused. The deadline authorizes input
start, not operating-system or server completion. Already issued grants cannot
promise instant revocation without another round trip.

Cloud derives the deadline from the real dispatch session, runner lease,
qualification, connector credential, product/Flow release and registry expiry,
and the registry-check age limit. Its atomic issuer rechecks these conditions
after database locks. These producer checks and the receiver check cover
different time windows; neither replaces the other.

## Completion and crash evidence

Receipts remain signed v2 artifacts binding the exact permit digest, claim and
sequence. A valid v3-linked receipt requires
`issued_at <= delivered_at < delivery_not_after` and delivery before registry
expiry. Generic artifact parsing is historical and has no current-clock gate.
A timely receipt arriving after the deadline remains evidence of that completed
input. A later input still needs a fresh permit.

Completion that cannot prove those bounds produces no valid receipt. The ACK
route returns HTTP409 with a closed
`openadapt.production-delivery-uncertainty-observation/v1` body. Flow validates
its exact run/session/permit/claim/sequence, retains its verbatim raw bytes as
nonauthorizing audit evidence, leaves the pending permit and cursor unchanged,
and fences further grants. It closes with reconciliation required. Record
read-back alone cannot replace the missing managed receipt.

The protected pending permit is the managed API write-ahead, persisted before
every actual request. Folded-step checkpoints can advance local progress; the
live sequence does not create a competing operator pause. A failure writes its
ordinary uncertain pause. A crash after grant or write but before acknowledgment
leaves the retained pending grant, prevents another grant or re-dispatch, and
requires reconciliation. Local API execution retains its existing provisional
pause mechanism.

## Defect class and evidence

The defect class is temporal scope and attribution of execution authority.
Known siblings are ambient admission caches across public entry points, changed
policy/inputs/trust/session after verification, durable resume in another
process, delayed permit responses, preparation crossing a deadline, API writes
missing acknowledgments, and a crash between writes in an API sequence.
Stateful generated invalidation traces exercise the public runtime and
deployment entry points; CLI and resume controls exercise their shared proof.
Receiver tests use actual HTTPS responses with planted delays and boundary
controls. Independent Python parsing checks an actual Cloud-generated synthetic
v3 artifact and receipt; a separate live-clock check refuses that historical
permit.

These are synthetic qualification and protocol tests. They do not establish a
successful hosted run on a held-out real application. The engine contains no
application-specific branch. The durable result is an attributable, bounded
write with retained evidence, or a refusal that cannot become VERIFIED.

## Validation

The sequential targeted run on 2026-11-20 passed 410 tests with one skip.
It included dispatch scope and stateful invalidation traces, actual CLI replay,
durable resume, signed multi-request API delivery and process-crash controls,
real HTTPS permit/ACK delays, continuation lock/read delays, and the existing
API, durable, and import-surface regressions. Ruff formatting/lint, mypy for 11
changed source modules, the file-size ratchet and generated review brief checks
passed. The complete package suite is left to CI; this local run is targeted.

After integration with current main, five CLI approval/authority helper bodies
were moved to `cli_program_dispatch`; their `__main__` wrappers and dynamic
admission-mode callback remain. An AST comparison found all five bodies
unchanged, and the run included the existing approval-mode, admission-pipeline
and intake regressions. The CLI shrank by 99 lines; its budget was lowered.
