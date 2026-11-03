# Cross-system reconciliation for prior authorization and claims

Design note, 2026-11-02. Status: design. The usage and the measured results
are added below once they exist.

## The question

One prior authorization or one claim lives in three systems of record:

- the **EHR**, where the order and the clinic's copy of the decision live
  (read over FHIR);
- the **clearinghouse**, which carries the X12 traffic: the 278 for a prior
  authorization, the 837 and its 277 claim status for a claim;
- the **payer**, which from January 1, 2027 must expose prior authorization
  over FHIR (CMS-0057-F, the Da Vinci PAS, CRD and DTR profiles).

Each of those systems can show its own records as consistent while the work
has gone wrong between them. The reconciler takes one unit of work and asks
one question: **do the three systems agree on it?**

It reports these breaks:

| Break | What the systems show |
|---|---|
| `not_received` | The EHR shows the request submitted, but the payer or the clearinghouse has no record of it. |
| `mismatch` | The record arrived, but its service code, service date or member differs from the EHR's. |
| `not_written_back` | The payer decided, but the EHR has no matching copy of the decision (no auth number, or a different outcome). |
| `duplicate` | One unit of work has more than one submission at the payer or the clearinghouse. |

## Built on the verifier, not beside it

There is one engine. The reconciler adds no new judgment code for "does this
store show this record":

- **Each leg is an `openadapt.verify/v1` contract.** Its `verifier:` section is
  the wiring `openadapt_flow.verifier` already reads (`kind: fhir` for the EHR
  and the payer), built with `deployment.build_effect_verifier`, so token
  handling, pagination limits, origin checks and the fail-closed reads are the
  same code.
- **Each check is an `Effect`** (`record_written` with `expected_count: 1`, and
  `field_equals`), judged by the same `judge_records` that `verify_write`
  uses, with the same null and number rules.
- **The clearinghouse leg is a new `EffectVerifier` substrate, `x12`,**
  registered through the existing plugin seam
  (`register_verifier_factory`). It reads a folder of X12 files (the
  clearinghouse's inbound mirror), parses each 277 or 278 transaction into a
  flat record, and hands the records to `judge_records`. A file it can't
  parse completely, or whose envelope counts and control numbers don't
  match, makes the whole read unreadable, never a partial set.
- **The outcome vocabulary is `openadapt_flow.verifier.Outcome`**, and each
  leg's coverage is a `verifier.Scope`. A reconciliation is `CONFIRMED` only
  when every check on every leg is `CONFIRMED`.

What differs from `verify_write` is that there is no write in the window: the
reconciler reads the current state of three stores. So it uses only effects
that make sense without a baseline. A leg that declares `count_new_only` or
`exact_new_set` is refused when the spec loads. Each leg's `Scope` says this
in `not_covered`: the reconciler shows what the stores hold at `read_at`, not
who wrote it or when.

## One unit of work

A reconcile spec (`openadapt.reconcile/v1`) names the legs and how the unit's
identity crosses them:

```yaml
schema_version: openadapt.reconcile/v1
unit: prior_authorization
anchor: ehr_request            # the EHR record the others must agree with
legs:
  ehr_request:   {role: ehr,           verifier: {kind: fhir, ...}, match: {...}}
  clearinghouse: {role: clearinghouse, verifier: {kind: x12, ...},  match: {...}}
  payer:         {role: payer,         verifier: {kind: fhir, ...}, match: {...}}
  ehr_decision:  {role: writeback,     verifier: {kind: fhir, ...}, match: {...}}
compare: [service_code, service_date, member_id]
decision: {from: payer, fields: [auth_number, outcome]}
expected_within_s: {clearinghouse: 3600, payer: 259200, ehr_decision: 86400}
```

The caller passes only the unit's identity (`params`: the order id, the
submission id). The reconciler:

1. Reads the **anchor** leg. Exactly one record must match the identity. Zero
   is `UNCERTAIN` (nothing to reconcile against), two or more is a
   `duplicate` in the EHR. The anchor's fields (`service_code`,
   `service_date`, `member_id`, the submission time) become the expected
   values for the other legs, so the payer and the clearinghouse are judged
   against what the clinic ordered, not against what they returned.
2. Reads each other leg by identity only (`record_written`,
   `expected_count: 1`). Zero records is `not_received`; two or more is
   `duplicate`.
3. On the one matched record, checks each `compare` field (`field_equals`).
   A differing field is a `mismatch` naming the field.
4. When the payer shows a decision, binds its `decision.fields` as expected
   values and checks the write-back leg the same way. A missing or
   different copy is `not_written_back`.

## Verdicts and time

Every break carries the outcome of the check behind it:

- `REFUTED`: the store affirmatively contradicts agreement. A wrong code, a
  second submission, or a record that is absent after its deadline.
- `UNCERTAIN`: the store couldn't be read, read too weakly, or shows a record
  absent while it may still arrive. Absence inside `expected_within_s` (counted
  from the anchor's submission time) is `UNCERTAIN`, not a break: a 278 can
  lag the PAS submission, and a payer has up to 72 hours (expedited) or 7
  calendar days (standard) to decide under CMS-0057-F. A missing or
  unreadable submission time makes absence `UNCERTAIN` at any age.
- `CONFIRMED` checks produce no break.

The reconciliation's outcome is the worst of its checks: any `REFUTED` makes
it `REFUTED`, else any `UNCERTAIN` makes it `UNCERTAIN`. Fail closed: a leg
that raises, times out or returns a partial read is `UNCERTAIN` and names the
leg in `scope.unread`.

## Evidence

The JSON result holds closed fields only, as `WriteResult.to_dict()` does:
leg names, break kinds, field names, record references (`Claim/123`, an X12
file name and its ST control number), counts, read times and reasons. It
holds no member ids, names or codes. A person follows the record references
to the systems themselves.

## What it doesn't cover

- Who made a record, or when: it reads the stores' current state.
- Service lines the spec doesn't name. FHIR paths take the first list element
  unless filtered (`item[sequence=2]...`), so a multi-line request needs a
  field per line.
- Stores it doesn't read: a payer portal, faxes, a second clearinghouse.
- Attachments and DTR questionnaire answers. CRD and DTR shape the request
  before it is sent. The reconciler checks what was sent and decided, not
  whether the documentation was complete.

## How it is tested

Public systems and synthetic data only:

- **Payer:** the HL7 Da Vinci PAS reference implementation
  (`HL7-DaVinci/prior-auth`, Apache-2.0), built from source and run locally
  in a container. No code from it is copied into this package.
- **EHR:** a local Medplum (Apache-2.0) with synthetic patients, and Epic's
  public FHIR sandbox for a few read-only calls within its terms. Oracle
  Health is not used: its terms forbid benchmarking without approval.
- **Clearinghouse:** X12 277 and 278 files written for these tests, with
  synthetic members.

The fault set is pre-registered: the fixture manifest and its sha256 are
committed before the run. It plants dropped submissions (at the
clearinghouse and at the payer), code, date and member mismatches, missed
write-backs and duplicates, and adds clean units, units still inside their
deadline, and look-alike decoys. It measures catches (a planted break found,
with the right kind and leg) and false breaks (a `REFUTED` break on a clean
or in-flight unit).
