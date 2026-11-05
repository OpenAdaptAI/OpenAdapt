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
complete history. The caller must finish the audit/history and search reads
before asserting their completeness.

This adapter evaluates record-level scope. Existing field comparisons still
check the values on the target record. A proven declared transient cleanup
can satisfy scope and delete checks while refuting the literal history
predicate. That independent callable does not itself grant admission.

## Production guarantee

The mandatory targeted layer uses receipt-bound records, the plan's declared
schema-key-bound identifying search at write time and at late recheck, and the
final state. Its evidence schema is `openadapt.targeted-hard-invariants/v1`.
It records receipt, contract and schema commitments, target identity MACs,
search counts and observation times. Raw records, references and actors remain
in memory. It performs no reads or model calls.

Initial evidence uses phase `write`: both searches occurred at write time,
and the later search remains owed. It contains no pre-issuance receipt hash;
the outer issued receipt binds the target commitments and observations.
Phase `late` names the original issued receipt and the actual late search.
Write-phase observations cannot discharge the owed recheck or establish a
final result.

The shared targeted reader authenticates the issued state, plan membership,
receipt, parameters and exact target commitments before producing observations.
The fixed judge consumes those observations; a caller's completeness flag is
not an authority. Both identifying searches must be complete. Each required
record history must be bound to the exact written-version anchor and cover the
ordered post-anchor versions through the recheck. Missing required in-scope
evidence produces a named `not_checked` result and prevents confirmation.

Every receipt names `outside_receipt_intermediate_history` as `UNKNOWN`.
A duplicate created and deleted outside the receipt IDs, or an unrelated
create/delete between the two identifying searches, is outside this guarantee.
That disclosed limitation does not downgrade an otherwise complete in-scope
result. The stricter global historical callable above remains available as an
optional check where the application supplies complete history.

A contract can explicitly limit itself to final-state checks. Its receipt then
also names per-record intermediate history as `UNKNOWN`; unavailable history
never becomes an empty complete history. This is a smaller stated guarantee,
not proof that no intermediate change occurred. The targeted producer supports
record writes and field checks, not delete tasks. A delete task needs separate
authenticated plan authorization before this producer can qualify it.

Known scope and delete failures already adjudicated by the existing frame
remain refutations. An observed removed row is distinct from a missing expected
side row. Changes authoritatively attributed to other actors remain theirs.
Field matching, version binding and legitimate-other edit checks remain
independent requirements. An unknown never authorizes replay of a dispatched
write; reconciliation is read-only.

Transient cleanup requires this run's write receipts to bind the resource
identity, a declared cleanup plan and an explicit final absence read. Actor
equality and absence from a search do not supply that binding. The legacy
Ensure path has no such receipt input, so its cleanup exception refutes with
`own_transient_receipt_binding_missing`. Without authenticated targeted
observations, production checks report a named unknown. A snapshot cannot
establish undeclared search or intermediate-history coverage.

The targeted adapter's synthetic controls are component checks. They do not
qualify a real application or establish wider historical absence. Per-field
matching and the optional global callable's controls are unchanged.

## Canonical Ensure bridge

`Contract.from_ensure` binds the complete Ensure contract and the supplied
parameter names to the canonical verifier. The existing Ensure field and
operation judge consumes one native observation collection. It supplies actual
independently read target IDs to the frame; empty generic selectors never
declare the entire read set. Structured Reference inputs remain structured.
A run-salted commitment binds the exact inputs before baseline reconstruction.

The legacy `capture_baseline` and `verify_ensure` calls begin and consume that
same canonical write. Their live pending authority is excluded from snapshot
data fields, representations and pickle/copy state. A reconstructed or edited
snapshot cannot recover it. Canonical APIs provide the explicit durable path.
Shadow uses the canonical recheck fence for both FHIR and SQL. Reply IDs remain
reporting and grouping hints; they cannot authorize omitted writes or targets.

Every nonfinal result retains an owed read-only reconciliation obligation.
Missing identity remains named UNKNOWN, including when no native read can be
scheduled. An unavailable baseline, or inputs changed after dispatch, cannot
become positive from a later matching row. A refuted attempt remains refuted;
recorded review may authorize a new attempt.

The Ensure adapter retains the full field contract for late comparisons.
Reference evidence is collected through the same native transport before the
original field predicates run. Display text can differ only under those
existing rules; an unresolved target remains UNKNOWN. The exact written
version and body anchor are still authenticated independently of field
comparison. A field comparison cannot repair a changed anchor.

An issued read-only reconciliation deadline is an obligation, including when
the record identity is unresolved. It does not mark the write landed,
provisional, or confirmed. Such an obligation remains visible after a refused
read and cannot authorize a retry.

## Declared native SQL scope

The shared producer also consumes the SQL adapter's existing complete native
query result. It proves the declared projected tuple currently exists; it does
not prove a database schema, a primary key or persistent record history. An
explicit contract identity field remains required when declared. Without one,
evidence labels the commitment `projected_tuple`, rather than a record ID.
Ambiguous column labels, incomplete rows or an unreadable projection refuse.
Per-field matching and the existing expected-count checks remain independent.

A contract that already declares a database frame reuses its settled native
snapshot, schema and row-key commitments for the receipt-named records and late
readback. The producer adds no query. Issued receipt, source, parameters and
row/schema commitments are validated before the common durable read fence.
Existing scope, delete and actor adjudication still apply to every declared
native frame read; a missing in-scope observation prevents confirmation.

When no identifying search was declared, its named check is `not_checked` and
its coverage is `UNKNOWN` outside the guarantee. That undeclared search does
not downgrade complete in-scope reads or become a successful empty search.
Each receipt discloses this limit, along with the absence of intermediate
history. Declared database side-record coverage remains independently checked.
The optional strict global predicates and the FHIR targeted guarantee retain
their own required observations.
