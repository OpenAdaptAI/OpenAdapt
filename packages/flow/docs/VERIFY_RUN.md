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

Some rows belong to a user rather than a record, such as a user's own UI
settings, so no log entry names them by record. With `frame.database` and
an audit log that names the table each entry wrote (`table_field`), such a
change is another user's when their own write entry in the window names the
table and the row is theirs. The tables are the app profile's `own_rows`
tables (OpenEMR's `user_settings` and `recent_patients`), each with its
`owner` (`{column, by}`). `by: username` compares the entry's actor with the
column; `by: user_id` looks the actor's id up in the profile's `users`
table. Table names compare lower-cased and without a schema, so an entry
naming `USER_SETTINGS` or `openemr.user_settings` names `user_settings`.

Some apps write a global default row beside a user's row. The first time
anyone saves a label, OpenEMR's `setUserSetting` also inserts the label's
default, a `user_settings` row with `setting_user = 0` and the same label
and value. The profile declares it on the owner
(`default: {value: "0", same: [setting_label, setting_value]}`), so a
default row with a value nobody saved is never explained. A default
row added in the window is explained only beside one explained row of the
same table with equal `same` columns: another user's row (then the default
is theirs too) or the bot's own row (then it is the bot's bookkeeping). Each
explained row explains one default row. A default row changed or removed,
added beside a row of another label, or added beside a row nothing
explains, stays unexplained.

`attribution.owners` can restate a profile table's owner in a contract, in
the same shape. It can't add one: a table outside the profile's reviewed
bookkeeping tables, or one that an append-only or column rule names (such
as `users_secure`, whose owner is only for the acting user's sign-in
counters), is refused, and so is a different owner for a profile table.
Credentials never get an owner here.

Fail closed:

- Only a row that names no record is judged by its owner: a row whose
  `record_key` (or `record_keys`) value is empty, or a table without that
  column. A row that names a chart is judged by its chart, and another
  entry by the same user doesn't explain it. Without `record_key`, every
  row names itself, and no row is judged by its owner.
- Each entry explains one row, of the kind it wrote (insert, update or
  delete), owned by that entry's actor. A row of another user, or a row whose
  owner changed, is collateral.
- If the bot's own entry names the table in the window, every changed row of
  that table is collateral: the entry names no row, so any of them may be the
  bot's change.
- The bot's own per-user rows stay the profile's bookkeeping when its entry
  names the table, and that entry is not counted as a write the contract
  doesn't cover, because the whole-database diff judged the table row by row. A bot
  entry for such a table with none of its own rows excused still refutes, and
  so does one that names the table in a schema the frame doesn't read. An
  entry's table matches a snapshot table only by its exact name (case
  aside), or by its unqualified name when the entry names no schema.
- That leaves a limit: in such a table, a change the bot makes and reverts
  within the window, such as setting another user's row and setting it
  back, leaves the same table before and after. Its entry is passed over,
  so nothing refutes it, as with any comparison of two snapshots.
- A table with no declared owner, an actor the users table doesn't know, a
  row with no owner, or an audit log that can't be read leaves the change
  unexplained.
- What the log can't show stays a limit: a change the app writes without
  logging it can't be told from another user's.

OpenEMR's `log` table has no table column, so the audit read takes the table
from the statement OpenEMR logs in `comments`, and the write kind from the
end of `event` (`patient-record-update`, `other-insert`). OpenEMR logs a
`user_settings` or `recent_patients` write as an `other` event, which it
records only when the `audit_events_other` global is on; with it off, such a
change has no entry and stays unexplained. An encrypted comment
(`log_comment_encrypt.encrypt = 'Yes'`) yields no table, so its entry
explains nothing. On MariaDB:

```yaml
  attribution:
    kind: audit_read
    actor_field: user
    self: [{param: bot_user}]
    record_field: patient_id
    record_key: pid
    event_field: kind
    write_events: [insert, update, delete, replace]
    insert_events: [insert, replace]
    delete_events: [delete]
    table_field: tbl
    read:
      kind: sql
      sql_driver: pymysql
      sql_query: >-
        SELECT l.id, l.user, l.patient_id,
          SUBSTRING_INDEX(l.event, '-', -1) AS kind,
          CASE WHEN IFNULL(e.encrypt, 'No') = 'Yes' THEN NULL ELSE
            LOWER(REPLACE(REGEXP_SUBSTR(l.comments,
              '(?i)(?<=insert into |update |delete from |replace into )[`a-z0-9_.]+'),
              '`', ''))
          END AS tbl
        FROM log l LEFT JOIN log_comment_encrypt e ON e.log_id = l.id
```

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

## Lost updates

Two snapshots can't see a lost update. The agent reads a record, another
user saves a change to it, and the agent then saves its stale copy over
theirs. After the save the record looks like "the agent changed the phone,
the address is unchanged", which is also what a correct update looks like.
Only the record's versions show the second save.

`frame.versions` names a read that returns one row per version of a record,
and the fields of a row that name the record, the version and, where the app
records it, the author:

```yaml
frame:
  identity_field: id
  versions:
    read:                     # read-only, the same shape as `verifier:`
      kind: sql
      sql_driver: psycopg2
      sql_connect_args: {host: 127.0.0.1, port: 5432, user: reader, dbname: hapi}
      sql_password_env: HAPI_DB_PASSWORD
      sql_query: >-
        SELECT r.res_type || '/' || r.fhir_id AS id, v.res_ver::text AS ver
        FROM hfj_res_ver v JOIN hfj_resource r ON r.res_id = v.res_id
    record_field: id          # the version row's record
    target_field: id          # the target's field in the read set (default: identity_field)
    version_field: ver
    # author_field: user_id   # when the app records who saved each version
    # self: [{param: bot_user_id}]
```

An app that logs each save with its user can name the author instead. EspoCRM's
`action_history_record` logs every API save; a lead has no version column:

```yaml
  versions:
    read:
      kind: sql
      sql_driver: pymysql
      sql_connect_args: {host: 127.0.0.1, port: 3306, user: reader, database: espocrm}
      sql_password_env: ESPOCRM_DB_RO_PASSWORD
      sql_query: >-
        SELECT CAST(CONCAT(LOWER(target_type), ':', target_id) AS CHAR) AS id,
          CAST(number AS CHAR) AS ver, CAST(user_id AS CHAR) AS author
        FROM action_history_record WHERE action <> 'read' AND deleted = 0
    record_field: id            # equals the read set's id (lead:<id>)
    version_field: ver
    author_field: author
    self: [{param: bot_user_id}]
```

The versions are read before the write and after it. Every record that a
confirmed write effect names and that existed before the write is a target.
The versions of a target that are new in the window must be the agent's:

- With `author_field`, every new version's author must be one of `self`. A
  version by anyone else is refuted as a lost update. A version with no
  author is not checked. The read must be a history that only grows: every
  version read before the write must still be there after it, or the check
  is not checked. A read of only the current version and its last author
  would hide the other user's save.
- Without it, versions must be whole numbers that step by one per save of
  that record (HAPI's `res_ver`, a `version_number` column). At most one
  step may happen. Two or more steps are refuted: step mode assumes the
  agent saves each record once per run, so a second step is someone else's.
  An agent that saves the same record twice (correcting its own write) needs
  `author_field`. A version that went back is refuted. A version
  that isn't a whole number (an opaque ETag) is not checked: without authors
  the saves can't be counted. Prefer a history read, one row per version. A
  read of only the current version counts steps as the difference, which
  trusts the app's numbering: an app that numbers a save from the client's
  copy (two saves from version 1 both write version 2) hides a lost update
  there, while a history read shows the two rows as `duplicate_version`.
  The numbers must be per record and
  contiguous: a global sequence, such as an audit table's row id, also moves
  for other records' saves, so a correct update would be refuted. Use
  `author_field` with such a sequence.

Anything else stops the run, never a pass:

- a versions read that fails before or after the write;
- a target that existed but has no version, or a target record without the
  field that names it;
- a version that reads as null, before or after (`null_version`);
- two rows of one record with the same version (`duplicate_version`): a
  save could hide behind another;
- a target whose record changed in the window with no new version
  (`no_new_version`): the app saved without a version row, so who saved
  can't be told. A target that didn't change (already right, nothing
  written) needs no new version; this relies on the read recording every
  save, since a change reverted by a save the read misses looks unchanged.

A record created in the window has no earlier version to overwrite and isn't
judged. Whether a target existed is told by `frame.identity_field` (default
`id`), never by `target_field`'s value after the write: a target that existed
with no version under its current `target_field` value is `missing`. Only targets are judged: another user's save to an unrelated record
leaves this check as it was (the frame and its attribution judge that
record, as before). The versions are read before the records at the
baseline, so a save between the two reads shows in the record baseline. The
window starts at the baseline: a copy the agent read before `begin_write` is
not covered.

The baseline keeps versions as keyed MACs of the record and the version;
only a whole-number version's value is kept, to count steps. The receipt's
`evidence.frame.versions` holds counts and a problem code (`another_author`,
`extra_steps`, `went_back`, `no_author`, `not_a_number`, `null_version`,
`duplicate_version`, `no_new_version`, `history_changed`, `missing`,
`no_target_id`, `unreadable`, `self_unresolved`), never ids or values. A
refuted check refutes the frame (`checked.frame: refuted`); one that can't be
judged leaves the frame `not_checked`.

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
runs the check against a test copy of the system of record. It writes nine
kinds of bad save to the copy's store, and the check reads each one through
its own read: a wrong value in each declared field, the values on a
look-alike record, an edit to another record, an edit in another table, a
row in another table for the saved record, a partial write, a duplicate, no
write, and a write that lands after the check's timeout. A read whose
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
  refuse_not_applicable: false    # true for an EMR: refuse a trimmed copy
  min_store_tables: 0             # the fewest tables the test copy must hold
```

A check is admitted only when every fault class of every part it claims was
tested and fully killed: one missed class refuses it, whatever
`min_coverage` says. Under a policy, `checked.coverage` in the receipt holds
the totals, the record's digest, `policy` (`met`, or why not, such as
`coverage_for_another_check` or `coverage_class_missed:other_table`) and
`classes`, each claimed class's result. `verified` requires `met`. A changed check
needs a new coverage record. Without `admission:` nothing about coverage is
required or stated.

`openadapt-flow scaffold-verifier --db app.sqlite --fault-gate` runs the gate
on a copy of the database and keeps the check a draft when a fault survives.
Without `--fault-gate` it writes the coverage of its recorded-state test,
which can't test a wrong value written over the right record or a late
write, so that record is incomplete and refused by default.

## Writes that land later

With `frame.database`, the database is read twice after the save,
`frame.database.settle_s` seconds apart (default 1). When the two reads
differ, the frame is not checked. A write the app makes later than the
second read, such as a background job, a trigger-driven integration or a
delayed duplicate side record, lands after the verdict, and that check
doesn't see it.

A live app moves on its own: a screen that polls, a scheduler tick. Set
`frame.database.settle_attempts` (default 1) to take up to that many more
reads, each `settle_s` after the last, until two in a row match. Two reads
also match when they differ only by rows added to append-only tables, which
the frame excuses anyway. Any other difference, including an edited row of
an append-only table, keeps it waiting. The earlier read of the matching
pair is judged. The budget is `settle_attempts * settle_s` seconds; when no
pair matches within it, the frame is not checked.
`evidence.frame.database.settle_attempts` says how many reads it took. The
same budget applies after the save and at a recheck.

Set `frame.database.recheck_after_s` to read the database once more after a
verified save:

```yaml
frame:
  database:
    dialect: mysql
    read: {...}
    settle_s: 1
    recheck_after_s: 300   # pick it for the app's own jobs; there is no default
```

When it is set and the run is verified:

- The receipt has `recheck_due_at` (whole-second UTC): `recheck_after_s`
  after the second read, never early (the read time is truncated to the
  second, so one second is added before rounding up). A caller can schedule
  the recheck without waiting on it.
- A verified receipt that carries `recheck_due_at` is provisional until the
  recheck runs: a write that lands in the window can still refute it.
- `evidence.frame.recheck` says what the recheck compares
  (`database_since_post_save`), when the post-save read was taken, and a
  salted digest of the state it compares with.
- The caller keeps that state privately (`WriteResult.recheck`). Through
  `begin_write` and `verify_write`, it is saved as `recheck.json` in the
  write's folder, beside `baseline.json` (mode 0600); `RecheckState.save`
  writes it elsewhere. It holds the run's baseline and its
  post-save snapshot as keyed MACs (no values, as the baseline does), the
  audit entries seen then as keyed hashes, what the run's frame reported as
  collateral (row key MACs and entry hashes), and the run's salt.
- `verify_write` records each state it issues under the verifier's state
  directory (`OPENADAPT_VERIFIER_STATE_DIR`, in `rechecks/`, named by the
  state's digest). A state's salt and digests can be recomputed by whoever
  can write the file, so a state without that record is never used: as the
  write being rechecked it gives `UNCERTAIN`, and as an excuse it is
  refused. As with snapshots, the writer being judged must not be able to
  write the state directory.

`openadapt_flow.verifier.recheck` reads the whole database at or after the
due time (twice, `settle_s` apart) and judges every change since the
post-save read with the frame's own rules: the profile's bookkeeping, and a
write that the audit log attributes to another actor.

Later writes under the same contract by the same actor can be passed as
`excused_by`, with any outcome, so the caller can keep working on the next
item while the window runs. A later write accounts only for its own window,
the time between its baseline and its post-save read, where its own frame
judged every change:

- A row that changed in that window, and still holds what that write's
  post-save read saw, is the later write's. A row that matches its
  selector but lands after its window (a delayed duplicate, or a save that
  landed late), or a row changed again since, is judged like any other.
- A removal counts when that window recorded it, matched by the row's link
  MAC: a per-host keyed hash of the row key, kept only when
  `recheck_after_s` is set.
- An audit entry logged in that window is the later write's.
- When the later write was not verified and the change is one its frame
  reported as collateral, or its delta failed, the change may be what
  refuted it, and the harm may be the earlier write's. It doesn't refute
  the earlier write, but the recheck gives `UNCERTAIN`: reconcile both.

| Since the post-save read | Recheck `outcome` |
|---|---|
| Nothing moved that these rules don't explain | `CONFIRMED`: the verdict stands |
| A row moved that nothing explains | `REFUTED`: the earlier `verified` no longer stands (late) |
| A change lies in a later write's window and may be what refuted it | `UNCERTAIN` |
| The database couldn't be read or was still moving, or the state wasn't issued here or doesn't match its receipt, the contract, the actor or the params | `UNCERTAIN`: never a pass |

A recheck before the due time raises `RecheckNotDue`; `wait=True` sleeps
until then instead. Limits:

- A row of a table without a primary key (its rows are keyed by a content
  fingerprint) is never excused by a later write's window.
- A later write under another contract doesn't excuse anything.
- A later write whose database never settled after its save has no state,
  so it can't be passed as `excused_by`. Reconcile it first.

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
`correlation_id` when given, and `recheck_due_at` when a verified run
schedules a recheck),
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
