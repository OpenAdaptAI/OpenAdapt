# Check a write that something else made

Preview, aligned with RFC-0002 (Open Effect Receipt). The receipt format may
still change before the RFC is final.

An agent, a script, or a person says it saved a record. `verify-run` reads
the system of record and decides whether that is true. It uses the same
record checks a production `run` uses (`rest`, `sql`, `fhir`, `graphql`, and
the other kinds in [`EFFECT_KIT.md`](EFFECT_KIT.md)). It makes no AI model
calls.

```bash
# Before the agent starts: read the store so new rows can be told from old ones.
openadapt-flow verify-run --verify verify.yaml --params task.json \
  --capture-baseline before.json

# ... the agent does its work ...

openadapt-flow verify-run --verify verify.yaml --params task.json \
  --run-record agent-run.json --authorization-ref TICKET-1234 \
  --baseline before.json --out share/
```

`verify.yaml` has two parts, each in a shape Flow already reads:

```yaml
schema_version: openadapt.verify/v1
verifier:        # the same keys as the effects: section of deployment.yaml
  kind: rest
  base_url: http://localhost:8080
  records_path: /api/db
effects:         # the same list as effect_contract.yaml
  - kind: record_written
    match:
      patient_id: {param: patient_id}
      note: {param: note}
    expected_count: 1
    count_new_only: true
```

It is the same file `openadapt-flow qualify --verify` reads (see
[`QUALIFY_A_WORKFLOW.md`](QUALIFY_A_WORKFLOW.md)). verify-run ignores the
qualify-only sections (`app`, `save_step`, `inputs`, `campaign`, and
`verifier.session`), expands `record` to the same effect list qualify uses,
and expands `${NAME}` environment references the same way. So the receipt's
`workflow_digest` equals the `verify_config_sha256` qualify reports for the
same file. `max_execution_window_s` (default 3600) is the one key only
verify-run reads.

The three inputs come from different people on purpose:

- `--params` holds the values the task named, such as which patient. They
  come from whoever assigned the task, so the agent can't pick what it is
  checked against.
- `--run-record` is what the agent reports. It must name `executor_kind`
  (`agent`, `script`, `human`, `flow_replay`, or `unknown`). The receipt
  keeps a salted digest of the file and reads nothing else from it except
  `executor_id` and `executor_build`, which it also keeps only as digests.
- `--baseline` is the read taken before the agent started. Without it, a
  contract that counts new rows can't be judged, and the verdict is
  `INDETERMINATE`. A baseline works once: a second run with the same file
  is refused. One older than `max_execution_window_s` (default 3600) counts
  as no baseline.

To check the open record before the write, hold the write for a person, and
refuse a retry after an uncertain result, wrap the write in a guarded save
([`GUARDED_SAVE.md`](GUARDED_SAVE.md)). It reads this same file.

Optional flags:

- `--approver approver.json` names who approved the write, as
  `{kind, id, policy_ref}` with an opaque `id`. If the approver is the
  executor itself, the outcome is `rejected_policy` even when the write
  landed.
- `--remediates RECEIPT_ID` links this receipt to an earlier one it fixes.
- `--correlation-id ID` carries your own ticket or trace id.

Both link ids must be opaque (8 to 128 of `A-Za-z0-9._:-`). They are absent
unless given, and when present they are signed with the rest of the receipt.

## Records outside the target

With a baseline, verify-run also proves that nothing outside the declared
delta changed: no record that the effects don't name was added, removed or
edited, and a pre-existing target changed only in the fields the effects
name. This is the frame check ([`VERIFICATION_COVERAGE.md`](VERIFICATION_COVERAGE.md)).
It reads the same read set the effects use, once after the save, and judges
the whole contract at once, so two effects on one read set never flag each
other's targets. The agent's write can be right while it also edited another
patient's record in place, and only the baseline shows that.

Without a baseline the frame can't be checked, and the run is not
`verified`.

A `frame:` section says how records are compared and who may change them:

```yaml
frame:
  identity_field: pid        # default: id, else the whole record
  ignore_fields: [date]      # fields that change on their own
  target_fields: [phone_home]  # default: the fields the effects name
  attribution:               # optional
    kind: audit_read         # or record_field, with server_set: [...]
    actor_field: user
    record_field: patient_id
    self: [{param: bot_user}]  # the bot's own user or session ids
    read:                    # a second read-only read: the app's audit log
      kind: sql
      sql_driver: pymysql
      sql_query: SELECT id, user, patient_id FROM log WHERE event LIKE 'patient-record-%'
```

Each change outside the declared delta is attributed. A change that the
audit log (`audit_read`) or the record's own modified-by field with a new
version (`record_field`) attributes to another user is a concurrent edit: it
is listed in `evidence.frame.concurrent_count` and does not count against the
save. A change by one of the bot's own ids, or one nothing explains, is
collateral, and the outcome is `reconciliation_required`. With `audit_read`,
a log entry by the bot for a record no effect names is collateral too, even
when the read shows no change.

Only evidence the bot can't write counts:

- `audit_read` reads the app's own audit log or change feed.
- `record_field` reads fields the app sets on the server. List them in
  `server_set` (they must include `actor_field` and any `version_field`);
  that list is the per-app statement that no user or API client can write
  them. A field the run itself writes, or that a run parameter names, is
  never evidence: attribution that uses one is refused, and every change
  stays unattributed (`evidence.frame.attribution: attribution_refused`).
- The run's own ids (`self`, and the executor's id from the run record) are
  never "another user".
- Another user's change to a record the save declares is never excused. The
  saved value may no longer stand, so the outcome is
  `reconciliation_required` (`evidence.frame.target_changed_by_other`). Only
  changes outside the declared delta can be concurrent edits.

Without `attribution`, every change outside the delta is collateral. A
change is not proof that the executor made it: another user may have edited
the record during the run. A person checks the record before anything is
retried or undone. Values compare by type. A `Decimal` or `datetime` read
from a database equals the JSON form the baseline stored it in, while two
strings compare exactly, so `"02139"` changed to `"2139"` is a change.

When the frame is judged, the per-effect collateral rules
(`forbid_collateral_loss`, `forbid_collateral_change`) apply only where the
file sets them explicitly; the frame covers the rest. Without a baseline,
`forbid_collateral_change` is turned on as before, though it can't refute
without a baseline either.

## What the receipt says was checked

The receipt's `checked` field names each part of the claim behind
`verified` and how it came out:

```json
"checked": {"delta": "held", "frame": "held", "source": "instruction"}
```

- `delta`: the declared records, read from the system of record.
- `frame`: nothing else in the read set changed.
- `source`: the inputs match their source. `instruction` means the inputs
  were the task's own parameters (`--params`), with no document behind them.

With `--provenance params.provenance.json --source referral.pdf`, the source
part is a round trip: for each value with a document source, a second,
deterministic reading of the cited document must equal the field the store
returned. A value misread before the save and saved faithfully passes the
delta and fails here. `evidence.source` names the fields that differ and the
extractors used, never the values.

Each part is `held`, `refuted` or `not_checked`. `verified` requires `delta`
and `frame` to be `held` and `source` to be `held` or `instruction`. The
field is inside `receipt_sha256`, so a Seal signs it. `evidence.frame` holds
the counts behind the frame part (records compared, collateral, concurrent)
and the digest of the `frame:` section. A receipt issued before the field
existed has neither and still checks.

## Admission: a check shown to catch its faults

A record check proves little until it has been seen to fail. The fault gate
(`openadapt_flow/fault_gate.py`, [`VERIFICATION_COVERAGE.md`](VERIFICATION_COVERAGE.md))
runs the check against a test copy of the system of record. It writes seven
kinds of bad save to the copy's store, and the check reads each one through
its own read: a wrong value in each declared field, the values on a
look-alike record, an edit to another record, a partial write, a duplicate,
no write, and a write that lands after the check's timeout. A read whose
scope can't see a fault fails the gate, not only a judge that misses it.

The gate writes `check_coverage.json` (`openadapt.check-coverage/v1`): faults
killed over faults applicable, per class, bound to the check by its digest
(the effects, the read and the frame; not where the store is). An
`admission:` section makes verify-run require it:

```yaml
admission:
  coverage: check_coverage.json   # relative to verify.yaml
  min_coverage: 1.0               # default: every applicable fault caught
  require_classes: [unrelated_edit]
  refuse_test_copies: [recorded_states]   # the default
```

Under a policy, `checked.coverage` in the receipt holds the totals, the
record's digest and `policy` (`met`, or why not, such as
`coverage_for_another_check`). `verified` requires `met`. A changed check
needs a new coverage record. Without `admission:` nothing about coverage is
required or stated.

`openadapt-flow scaffold-verifier --db app.sqlite --fault-gate` runs the gate
on a copy of the database and keeps the check a draft when a fault survives.
Without `--fault-gate` it writes the coverage of its recorded-state test,
which can't test a wrong value written over the right record or a late
write, so that record is incomplete and refused by default.

## Verdicts

| What the store shows | `effect_verdict` | `outcome` |
|---|---|---|
| The declared record, exactly once, through a system-of-record read, and nothing else in the read set changed | `CONFIRMED` | `verified` |
| The declared record, but no baseline, so the frame can't be checked | `CONFIRMED` | `reconciliation_required` |
| The same, but the executor approved its own write | `CONFIRMED` | `rejected_policy` |
| Nothing that matches, and nothing else in the read set moved | `REFUTED` | `halted_before_effect` |
| A duplicate, a wrong value, only some of the effects, or a record outside the delta that was added, vanished or changed | `REFUTED` | `reconciliation_required` |
| The store could not be read, no baseline, or a separate-session read | `INDETERMINATE` or `CONFIRMED` | `reconciliation_required` |

Only a read through the system of record produces `verified`. A screen
read-back (`onscreen`) is refused before anything runs.

`verification_tier` on the receipt says how the effects were checked
(RFC-0002, weakest effect wins):

- `independent_readback`: every effect was read from the system of record
  without the UI (Flow evidence tier 1).
- `screen_readback`: at least one effect was read through a UI session,
  including a separate signed-in session (Flow tier 2).
- `unchecked`: at least one effect was not read.

Only `independent_readback` can yield `verified`.

`checked_how` names the same read's check level
([`READBACK_LADDER.md`](READBACK_LADDER.md)): `system_of_record` (tier 1),
`separate_session` (tier 2), `reopened` (tier 3, the acting session),
`same_screen` (tier 4), or `unchecked`. It must agree with
`verification_tier`: only `system_of_record` (and `counterparty_confirmed`)
goes with `independent_readback`. It is inside the signed receipt and in
`evidence.readback`. A receipt issued before the field existed has neither
and still checks.

## The receipt

`share/effect-receipt.json` holds `receipt` and `evidence`. `receipt` has
every field of the Execute receipt (`openadapt.execute-evidence-receipt/v1`)
plus `executor_kind`, `executor_ref_sha256`, `effect_verdict`, and
`verification_tier`, and `checked_how` (and `remediates` and
`correlation_id` when given),
under the schema `openadapt.effect-receipt/v1`. `evidence.readback` lists
each effect's contract hash, verdict, and counts. `receipt.evidence_digest`
is the SHA-256 of `evidence`. Neither part holds record values, URLs, or the
agent's own text.

Every digest whose input could hold customer data (the run record, the
authorization reference, the baseline, and each bound parameter inside an
effect's contract hash) is an HMAC-SHA256 keyed with a 256-bit salt drawn
for this run (`openadapt.run-salt/v1`). `evidence.salting.salt_commitment`
is the SHA-256 of the salt. The salt itself stays in a private file: the
baseline, or `share/effect-run.private.json` when there is no baseline. Each
REST read carries `X-OpenAdapt-Execution-Id` and a `traceparent` header so
the store's own logs can be matched to the run.

`evidence.authorization.approver` keeps a placeholder shape until OpenAdapt
types defines approver identity.

The file is unsigned. An issuer signs it with
`openadapt.effect-seal-signature/v1`, over its own domain, so a verifier
built for Execute Seals refuses an effect receipt instead of passing it.
Check a signed file offline against keys you pinned:

```bash
openadapt-flow verify-run --check signed.json --keys seal-keys.json
```

The check reports the signature (`verified`) and the outcome
(`verified_outcome`) separately. It exits 0 only when both hold. A genuine
signature over `halted_before_effect` still exits 1 unless you pass
`--accept-outcome halted_before_effect` (repeatable). An unsigned file is
never a pass.

The baseline and run files hold raw records or the salt. They are written
with mode 0600 and never go into the receipt. Delete them when you no longer
need them.
