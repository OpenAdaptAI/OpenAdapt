# Deterministic hard invariants

`openadapt_flow.hard_invariants.judge_hard_invariants` evaluates three named
predicates independently of field matching:

- `declared_scope`: no observed changes outside the declared task scope.
- `at_most_one`: at most one distinct record carries the business key across
  the baseline, current search and observed history, including deleted records.
- `no_undeclared_deletes`: no observed delete outside a declared operation.

Each result has a fixed reason code and is `held`, `refuted` or `not_checked`.
A known violation refutes even if another read is incomplete. Zero observed
violations establish a predicate only when its reader explicitly reports
complete observations. Current search counts alone do not prove history.
Evidence contains the three results, counts and `model_calls: 0`.

`openadapt_flow.ensure_invariants.judge_ensure_invariants` adapts FHIR
ensure-record observations. It uses the contract's existing identity rules,
actor attribution, correction proof, bookkeeping proof and declared transient
cleanup proof.
History bodies and their prior versions contribute business-key records;
an audit-free sweep, unlogged write or missing history cannot establish
complete history. Window versions must carry readable timestamps and version
IDs, and current read-back must bind to the unique last version. The canonical
read-back helper's legacy skip when history is absent is not a success witness.
The caller must finish the audit/history and search reads before asserting
their completeness.

Unknown attribution cannot prove an own scope or delete violation: the affected
predicate remains `not_checked`. Independently proven own violations still
refute it. Historical business-key cardinality describes the literal store,
including other actors' records; it does not assign blame to the agent.

This adapter evaluates record-level scope. Existing field comparisons still
check the values on the target record. A proven declared transient cleanup
can satisfy scope and delete checks while refuting the literal history
predicate. The callable returns evidence independently and does not change
ensure-record admission, verify-run receipts or per-field comparisons.
