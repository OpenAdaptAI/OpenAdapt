# Compensating rollback

A stop isn't a rollback. A run that saved at step 3 and stopped at step 8
leaves step 3's record in the application. [AFTER_A_STOP.md](AFTER_A_STOP.md)
lists what changed and lets a person run an undo by hand. This page covers
the step after that: Flow puts the system of record back on its own, and
proves it did.

Rollback is off unless a deployment turns it on. When it is on, a stopped run
ends in one of three ways:

| Outcome | Meaning |
| --- | --- |
| `ROLLED_BACK` | Flow undid every change the run made, and a read of the whole system of record shows each of the run's rows back as it was and nothing else changed, apart from rows another user changed. |
| `HALTED_BEFORE_EFFECT` | Nothing landed: right after the stop, nothing the run did is in the system of record. |
| `RECONCILIATION_REQUIRED` | Anything else. The report says what differs, and a private file holds the rows a person needs to repair it. |

Flow never reports a partial rollback as done.

**Undo, or a person, before every write.** With rollback on, a save that no
approved compensation can undo (none approved, or only a `delete` the
deployment doesn't allow) waits for a person's approval before it runs. In
`problems_only` it goes to a person like an each-item save (code `each_item`,
detail `no_approved_compensation`). `each_item` already asks a person about
every save. A run with no way to ask a person (`not_gated`, or no approval
mode) is refused before it starts, and the refusal names those saves.

The code is in `openadapt_flow/rollback.py`, `rollback_state.py` and
`rollback_qualify.py`. The tests are in `tests/test_rollback.py` and
`tests/test_rollback_qualify.py`.

## How it works

### Before the run

Flow reads the whole system of record:

- With `rollback.database`, every table and column except the reviewed
  `allow_tables`. This uses the same settings and reader as `frame.database`
  in `verify.yaml`.
- Without it, the one collection the deployment's `effects` verifier reads.
  The report says `scope: collection`, because rows in other places aren't
  seen.

If that read fails, the run is refused before it starts: nothing after a stop
could be proven.

### After a stop

1. **Fresh read.** Flow reads the store again and compares it with the first
   read. Each row that changed is the run's, another user's, or nobody's
   (see [Other users](#other-users)). If no row is the run's, nothing
   landed, but only if the [change ledger](AFTER_A_STOP.md#the-change-ledger)
   agrees: a confirmed write the read doesn't show (in an allow-listed
   table, a column the profile leaves out, or outside the one collection
   read) ends `RECONCILIATION_REQUIRED`, and an uncertain write counts as
   absent only when a fresh read of its own record says so. Only a
   whole-database read proves the run's effects absent; a collection read
   never overrides the run's own uncertainty.
2. **Compensate, latest change first.** For each change in the run's
   [change ledger](AFTER_A_STOP.md#the-change-ledger), Flow uses the
   compensation a person approved for that step. That is either a declared
   undo, or a proposal accepted with `undo accept` after it passed on a test
   copy. Flow never uses a proposal no person approved.
3. **Uncertain writes.** If a write's delivery was uncertain, or its effect
   wasn't confirmed, Flow compensates it only when a fresh read shows the
   effect is there:
   - For an undo that finds its record: exactly one new matching record.
   - For an undo that restores old values: the record holds the run's new
     values.
   - Otherwise: every declared effect is confirmed.

   If the effect is absent, there is nothing to undo. If the read can't
   tell, the rollback stops there. Flow doesn't compensate on a guess.
4. **Kinds.** Each compensation declares what it does: `discard_draft`,
   `restore` (old values back), `void` (the record is voided, cancelled or
   made inactive, and stays), or `delete`. A compensation that declares no
   kind counts as `delete`. Many record systems forbid deleting a chart for
   audit or legal reasons, so a `delete` runs only when the deployment sets
   `rollback.allow_delete: true`. `rollback qualify` tries the least
   destructive kind first and never tries a delete the deployment doesn't
   allow.
5. **Conflicts.** If another user changed any row the run could have
   written, in any table, or wrote anything about a record those rows
   belong to (the same patient, in another table), Flow compensates nothing
   and ends `RECONCILIATION_REQUIRED` with the diff, so their change isn't
   overwritten. The same happens if a row the run touched changes between
   the stop and the first compensation. Without `rollback.attribution`,
   another user's edit can't be told from the run's, so Flow compensates
   nothing at all (except on a test copy the deployment declares only the
   run writes: `test_copy.declared` and `rollback.exclusive`).
6. **Each compensation runs in a fresh session** through the normal replayer.
   Identity checks, the risk gate, the deployment's save-approval gate and the
   compensation's own effect check all apply. In the default approval mode, a
   person still answers each compensation's card. A web run gets a fresh
   session: cookies and storage are cleared, and the browser goes back to the
   entry URL. When Flow is attached to the operator's own browser, it never
   clears it, so only API-only compensations run there.
7. **Final read.** Flow compares the whole store with the read from before the
   run. Every row the run touched must be back as it was. A `void`
   compensation may leave the run's own record, by its id, in the tables it
   declares (`retains`), and nowhere else. Every other row must match too,
   apart from rows another user changed and the app's own bookkeeping
   (`rollback.profile`):
   - Nothing else differs, and every write in the change ledger was
     compensated or shown absent: `ROLLED_BACK`. For a whole-database read,
     the verifier's own `diff_database` (`openadapt_flow.verifier`) must
     agree: it may see changes only in tables where a row was set aside or
     kept by a void, or the run isn't called rolled back. A kept void row
     must have changed since the stop. Every row set aside as another
     user's must be exactly as the stop left it, and the audit log must
     show no other user's write to a run row while the compensations ran;
     otherwise a person checks that their change isn't lost.
   - Anything else: `RECONCILIATION_REQUIRED`. `report.json` holds a summary
     with counts and column names only. `<run-dir>/rollback/diff.json`
     (owner-only) holds every added, removed and changed row, with the values
     before and after.

The halt message and `REPORT.md` show what happened to each step:

```text
Rolled back: the system of record matches the read from before the run.
  every change was compensated and the system of record matches the read from before the run
  step `step_010`: compensated with `undo-step_010-network-1` (every undo step ran and an independent read of the system of record confirmed its declared effects)
```

## Other users

A rollback isn't a transaction: a colleague can save something while the run
is going. With `rollback.attribution` (the same shape as `frame.attribution`
in `verify.yaml`), Flow learns who changed each row, from the app's audit log
or a modified-by field only the server sets:

- With the app's audit log (`audit_read`), a row another user changed, that
  the run didn't touch, is set aside. The report counts it
  (`other_actor_rows`). A row added is set aside only when the log holds
  that user's insert (`insert_events`): a later edit never makes a row the
  run created theirs. A row removed is never set aside (the first read
  keeps no values to name it by).
- The log's record field must name one kind of record in every table (a
  patient, as OpenEMR's log does): `record_key`, or `record_keys` per
  table. Rollback refuses an audit attribution without it, or without
  `event_field`, `write_events`, `insert_events` and `table_field`. A log of
  per-table row ids makes unrelated writes conflicts.
- Audit entries count only as the frame counts them: write events
  (`event_field`, `write_events`), for the row's own table (`table_field`).
  A colleague viewing a record never explains a change to it.
- A row nothing attributes counts as the run's. A rollback never sets aside a
  change it can't explain.
- A row the run could have written that another user also changed is a
  conflict (`conflicts`). Nothing is compensated.
- A modified-by field (`record_field`) knows only the last writer, so it
  never sets a row aside: any row another user wrote during the run is a
  conflict, even an unrelated one. It suits a quiet store; on a busy one,
  use the app's audit log. It also can't see a colleague's edit to a row
  while a compensation deletes it; the audit log can.
- Without attribution, nothing is compensated (see step 5).

## Turn it on

```yaml
rollback:
  enabled: true
  database:                 # optional; without it, the effects collection
    dialect: mysql
    read:
      sql_driver: pymysql
      sql_connect_args: {host: 127.0.0.1, user: readonly, database: app}
      sql_password_env: APP_READONLY_PASSWORD
    allow_tables: [log, sessions]   # tables the app writes on its own
  allow_delete: false               # the default: no hard deletes
  exclusive: false                  # true only for a declared test copy no one else writes
  profile: openemr                  # the app's own bookkeeping (app_profiles)
  actor_names: [openadapt_bot]      # who the run signs in as, for the profile
  attribution:                      # who changed a row (needed to compensate)
    kind: audit_read                # the app's audit log; record_field: last writer
    read:                           # a second read-only read, as in verify.yaml
      kind: sql
      sql_driver: pymysql
      sql_query: SELECT id, user, event, tbl, pid FROM log
    actor_field: user
    self: [openadapt_bot]
    record_field: pid
    record_key: pid
    event_field: event
    write_events: [insert, update, delete]
    insert_events: [insert]
    delete_events: [delete]
    table_field: tbl
```

Both lists are a person's reviewed decision for one app. The report records
them.

## Qualify it on a test copy

Before production, `rollback qualify` stops the run on purpose, before and
after every save, on a declared test copy. It uses the same gate as
`induce --probe-on` and `undo propose`:

```bash
openadapt-flow rollback qualify bundle --config test-copy.yaml --recording rec --from-recording delete-recording-bundle --report qualify.json
openadapt-flow undo accept bundle --undo <undo-id>
```

Each halt point is: a reset, a read of the whole store, the steps up to the
halt point, then the same `roll_back` procedure a production stop runs. So
the procedure that passes qualification is the one that runs in production.

- **Before each save:** the steps that lead to the save ran, but the save
  didn't. Once the earlier saves are compensated, the store must match the
  first read. An app that keeps a draft of a filled form shows up here.
- **After each save:** the compensations for this save and every earlier one
  run, latest first. The store must match the first read.

If a saving step has no approved compensation, Flow tries candidates from
[`undo propose`](AFTER_A_STOP.md#proposed-undo-steps): another recording,
captured traffic, or the field's old value. The first candidate that passes
its after-save halt point is written into the bundle as a proposal, with the
halt point as its evidence. A person approves it once with `undo accept`.
A candidate that fails is reported with the reason and never written.

## Measured

`benchmark/rollback/` holds the harnesses and results.

**MockMed, live:** a real browser, the in-process app declared a test copy,
and three `openadapt-flow replay` runs, each made to stop by one added step.

| Case | Outcome | Store afterwards |
| --- | --- | --- |
| Stop after the save, with the qualified compensation approved | `ROLLED_BACK` | Empty, as before the run |
| Stop before the save (a filled form) | `HALTED_BEFORE_EFFECT` (nothing landed) | Empty |
| The compensation not approved, and no person to ask (`replay`) | Refused before the run: the save can't be undone | Empty; nothing ran |

`rollback qualify` passed both halt points (before and after the save).
None of the runs made a model call.

**OpenEMR, live:** the pinned local fixture (its own stack), a real browser,
the scoreboard's New Patient program, and the whole `openemr` database read
through a SELECT-only user. The program filled 28 steps of the form and
stopped before the save. Read with only a copied allow-list of log and session
tables, the rollback ended `RECONCILIATION_REQUIRED` and named exactly what
else moved: 2 `background_services` rows (`next_run`) and 4 `users` rows
(`last_updated`, `uuid`), bookkeeping OpenEMR does on its own while a user is
signed in. That is the case the app profile below is for: a column nobody
listed shows up by name rather than being assumed harmless.

Not measured on OpenEMR: a stop after the save. On this stack the recorded
program stops at its own scroll step (step 28 of 35), before the save, and the
fixture ships with chart deletion turned off (`allow_pat_delete`), so there is
no compensation to run.

## The app's own bookkeeping: one profile per app

Every app writes some rows on its own while a person is signed in: logs,
session state, ids. A rollback must not count those as the run's, and must not
excuse anything else. `rollback.profile` names the app's versioned profile in
`openadapt_flow.app_profiles` (`openemr`), the same profile the verifier's
frame and the RL grader load, so all three agree on what "the app's own"
means. Every table it names is still read and compared; a rule only says which
changes in that table are the app's own:

| Rule | Which changes are the app's own |
| --- | --- |
| `append_only` | New rows. An existing row that changes or goes shows as a difference. |
| `columns` | Changes in the named columns of an existing row, narrowed to a backfill from empty, or to the acting user's own row, where the rule says so. |
| `own_rows` | The acting user's own rows (per-user UI state); everyone else's count. |

The read applies the rules row by row, exactly as the verifier's
`diff_database` does. Rules about "the acting user" need
`rollback.actor_names`, the sign-in names the run acts as; without them,
those rules excuse nothing.

A profile can't excuse patient data: a rule may name only a reviewed
bookkeeping table, and loading refuses anything else. The report records the
profile as `app/version` (`profile_id`).

## What this doesn't do

- **It undoes only what a person approved.** A step without an approved
  compensation stops the rollback, and the store is reported as it is.
- **It isn't a transaction.** Without attribution nothing is compensated.
  With it, other users' writes are set aside, but a row both of you changed
  is left for a person.
- **No hard delete by default.** A `delete` compensation runs only with
  `rollback.allow_delete: true`. A `void` keeps the run's record, voided,
  only in the tables its compensation declares.
- **The collection read sees one collection.** Without `rollback.database`,
  a side record in another table isn't compared. The report says
  `scope: collection`.
- **Tables over `max_rows` are fingerprinted.** A change shows that the
  table moved, but not which rows.
- **No rollback in a workflow with a loop,** or when a step ran more than
  once. A compensation runs with the run's base parameters.
- **Single web runs only, for now.** A desktop, RDP or worklist-pool run
  with rollback on is refused before it starts.
- **A rolled-back run isn't resumed.** A run that paused for a person and
  was rolled back is over: `resume` refuses it, because its earlier writes
  are undone. Start a new run.
- **The idempotency key stays used.** A rolled-back run doesn't free its key
  for a retry; a person starts the next run.
