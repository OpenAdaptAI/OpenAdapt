# Cross-system reconciliation for prior authorization and claims

Design note, 2026-11-02. The measured results are in
[`benchmark/reconcile/RESULTS.md`](../benchmark/reconcile/RESULTS.md).

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

A reconcile spec (`openadapt.reconcile/v1`) lists the legs in the order they
are read. The first is the **anchor**: the EHR's record of the request. The
shipped example is `benchmark/reconcile/prior_auth.reconcile.yaml`; in
outline:

```yaml
schema_version: openadapt.reconcile/v1
unit: prior_authorization
anchor: ehr
legs:
  ehr:                       # the request as the clinic made it
    role: ehr
    verifier: {kind: fhir, resource_type: Claim, ...}
    identity: {submission_id: {param: submission_id}, use: preauthorization}
    provides: {member_id: member_id, service_code: service_code,
               service_date: service_date, submitted_at: submitted_at}
  payer:                     # the PAS Claim the payer holds
    role: payer
    verifier: {kind: fhir, ...}
    identity: {submission_id: {param: submission_id}}
    compare: {service_code: {param: ehr.service_code}, ...}
    duplicate_key: {service_code: ..., service_date: ..., status: active}
    provides: {claim_id: id}
    due: {after: ehr.submitted_at, within_s: 604800}
  payer_decision: ...        # the ClaimResponse: outcome, auth number, time
  clearinghouse:             # the 278, which must match the order and the decision
    role: clearinghouse
    verifier: {kind: x12, options: {root: ..., transaction: "278"}}
    identity: {trace: {param: submission_id}}
    compare: {member_id: ..., service_code: ..., service_date: ...,
              auth_number: {param: payer_decision.auth_number},
              decision: {param: payer_decision.review_action}}
    group_by: [trace, purpose]   # a pended review, then the final one
    latest_by: sent_at
    distinct_by: [auth_number]   # two auth numbers are two decisions
  writeback:                 # the clinic's copy of the decision
    role: writeback
    compare: {outcome: {param: payer_decision.outcome}, auth_number: ...}
    required_when: {param: payer_decision.outcome, values: [complete, partial]}
    due: {after: payer_decision.decided_at, within_s: 86400}
```

The caller passes only the unit's identity (`submission_id`). For each leg
in order, the reconciler:

1. Reads the store once. A leg whose `identity`, `duplicate_key`, search or
   due time needs a value an earlier leg didn't give is not read, and is
   `UNCERTAIN`.
2. Judges `record_written` (`expected_count: 1`) on the `identity`. On the
   anchor, zero records is `UNCERTAIN` (nothing to reconcile against). On
   another leg, zero is `not_received` (`not_written_back` for the
   write-back) and two or more is `duplicate`.
3. Judges `record_written` on the `duplicate_key`: more than one is a
   `duplicate`, even under different identities (the same service for the
   same member and date, submitted twice).
4. On the one matched record, judges `field_equals` for each `compare` field.
   A differing field is a `mismatch` naming the field. A compared value an
   earlier leg read as empty binds to null, so a denial's missing auth
   number must be missing on the other side too. A compared value no earlier
   leg gave leaves that field unchecked and the leg `UNCERTAIN`.
5. Passes the matched record's `provides` fields to later legs as
   `<leg>.<name>`.

`required_when` makes a record due only once a value says so: a write-back
is due once the payer has decided. When it isn't due, its absence is no
break, but a record that is there is still compared, so a stale "approved"
copy in the EHR while the payer pends is a `not_written_back` break.

`group_by` and `latest_by` read a store that keeps every status of a unit (a
277 acknowledgement, then the 277 that it was paid) as one unit's history:
only the records with the latest `latest_by` in each group are judged.
Marks compare as instants (zoned ISO 8601 times) or as one sender's X12
`CCYYMMDDHHMM[SS]` clock; a group whose marks are missing, carry no time
zone, or mix the two is kept whole. A redelivered file ties at the latest
and still counts twice, and so do two decisions stamped in the same
second: a false `duplicate`, never a hidden one. A group whose
records carry two different values of a `distinct_by` field (two payer
claim numbers under one trace) is two submissions and is kept whole, so a
resubmission is still a `duplicate`.

The decision is compared as a code, not inferred from an auth number: the
payer's review action (X12 HCR01: A1 certified, A3 not certified, A4
pended) must equal the 278's and the EHR copy's, so a denial and a pend
without auth numbers aren't taken for agreement.

## Verdicts and time

Every break carries the outcome of the check behind it:

- `REFUTED`: the store affirmatively contradicts agreement. A wrong code, a
  second submission, or a record that is absent after its deadline.
- `UNCERTAIN`: the store couldn't be read, read too weakly, or shows a record
  absent while it may still arrive. Absence before the leg's `due` time
  (`within_s` after the time in the `after` value, such as the anchor's
  submission time) is an `UNCERTAIN` break: a 278 can
  lag the PAS submission, and a payer has up to 72 hours (expedited) or 7
  calendar days (standard) to decide under CMS-0057-F. A missing or
  unreadable submission time makes absence `UNCERTAIN` at any age.
- `CONFIRMED` checks produce no break.

A write-back copy that differs from the decision is `UNCERTAIN` inside its
due time (the EHR may still update it) and `REFUTED` after.

The reconciliation's outcome is the worst of its checks: any `REFUTED` makes
it `REFUTED`, else any `UNCERTAIN` makes it `UNCERTAIN`. Fail closed: a leg
that raises, times out or returns a partial read is `UNCERTAIN` and names the
leg in `scope.unread`.

## Evidence

The JSON result holds closed fields only, as `WriteResult.to_dict()` does:
leg names, break kinds, field names, record references (`Claim/123`, or an
X12 unit's place `x12:f<file index>:ISA13.GS06.ST02.n`), counts, read times and
reasons. It holds no member ids, names, codes or dates, and no file names.
A record whose id repeats one of its own values (the PAS reference
implementation's ClaimResponse id is its auth number) is referenced by type
only, `ClaimResponse/…`.
Reasons that come from a store have quoted values and URL queries replaced
with `…`. A person follows the record references
to the systems themselves.

## What it doesn't cover

- Who made a record, or when: it reads the stores' current state.
- Service lines the spec doesn't name. FHIR paths take the first list element
  unless filtered (`item[sequence=2]...`), so a multi-line request needs a
  field per line.
- Stores it doesn't read: a payer portal, faxes, a second clearinghouse.
- Which payer Claim a decision answers. The PAS reference implementation's
  decision is found by the submission identifier it echoes, not by its
  `request` reference (which it writes malformed on an update). A payer
  that hides a superseded Claim from search but keeps its ClaimResponse
  could have the old claim's decision judged for the unit.
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
