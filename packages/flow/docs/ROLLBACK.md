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
| `ROLLED_BACK` | Flow undid every change the run made, and a read of the whole system of record shows each of the run's rows back as it was and nothing else changed. |
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

If that read fails, or the audit log that attribution reads can't be read, the
run is refused before it starts: nothing after a stop could be proven.

### After a stop

1. **Fresh read.** Flow reads the store again and compares it with the first
   read. If nothing changed, nothing landed, but only if the [change ledger](AFTER_A_STOP.md#the-change-ledger)
   agrees: a confirmed write the read doesn't show (in an allow-listed
   table, a column the profile leaves out, or outside the one collection
   read) ends `RECONCILIATION_REQUIRED`, and an uncertain write counts as
   absent only when a fresh read of its own record says so. A step that
   declares no effect and has no compensation (pressing Enter in a form
   field) is judged by the whole-database reads and the audit log together:
   only when every write the run's user logged is a row the read shows
   changed. A write to a table the read keeps append-only (an allow-listed
   table, or one the app profile keeps append-only, such as a disclosure
   log) is then still caught by its log entry; a write the app doesn't log
   there, or logs under an event `write_events` doesn't list, isn't. With
   the audit log, the same holds for the whole run: a write by the run's
   user the log shows and the reads don't account for (from any step, in
   any table) never ends "nothing landed" or `ROLLED_BACK`. Only a
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
5. **Only the run's own changes.** A compensation runs only while every row
   that changed since the first read is positively the run's own (see
   [Other users](#other-users)). Flow checks that at the stop, reads the
   whole store again before each compensation and after it, and stops at
   the first row that isn't: another user's write anywhere, a row
   attribution can't vouch for, a run row that moved since the last read,
   or a row the compensation changed that the run didn't write. Then it
   ends `RECONCILIATION_REQUIRED` with the diff; nothing of anyone else's
   is compensated away. Without `rollback.attribution`, nothing is
   compensated (except on a test copy the deployment declares only the run
   writes: `test_copy.declared` and `rollback.exclusive`).
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
   apart from the app's own bookkeeping (`rollback.profile`):
   - Nothing else differs, and every write in the change ledger was
     compensated or shown absent: `ROLLED_BACK`. For a whole-database read,
     the verifier's own `diff_database` (`openadapt_flow.verifier`) must
     agree: it may see no more rows moved in a table than a void kept
     there. A kept void row must have changed since the stop. The audit
     log must show no other user's write to a run row while the
     compensations ran.
   - Anything else: `RECONCILIATION_REQUIRED`. `report.json` holds a summary
     with counts and column names only. `<run-dir>/rollback/diff.json`
     (owner-only) holds every added, removed and changed row, with the values
     before and after. When another user wrote to the run's rows while the
     rollback ran, `rollback/late.json` holds those rows as the stop left
     them.

The halt message and `REPORT.md` show what happened to each step:

```text
Rolled back: the system of record matches the read from before the run.
  every change was compensated and the system of record matches the read from before the run
  step `step_010`: compensated with `undo-step_010-network-1` (every undo step ran and an independent read of the system of record confirmed its declared effects)
```

## Other users

A rollback isn't a transaction: a colleague can save something while the run
is going. So a rollback runs only on a store that holds nothing but the run's
own changes, and the app's audit log (`rollback.attribution`, the same shape
as `frame.attribution` in `verify.yaml`, `kind: audit_read`) must show that
each changed row is the run's:

- **The app's audit log** (`audit_read`): the log holds the run's own write
  for the row's record and table, and no other user's write to the row or
  to its record (the same patient, in any table). The log's record field
  must name one kind of record in every table (a patient, as OpenEMR's log
  does): `record_key`, or `record_keys` per table. Rollback refuses an audit
  attribution without it, or without `event_field`, `write_events`,
  `insert_events` and `table_field`. Entries count only as the frame counts
  them: write events, for the row's own table; a colleague viewing a record
  is not a write. The run's logged inserts must account for its new rows:
  two new rows on one record and table need two of its inserts. This rests
  on the app logging every user's write under the record it touches.
- **No last-writer field.** A server-set modified-by value (`record_field`)
  shows only the last writer: another user's edit between two of the run's
  writes to a row (a save and a later step that also writes) would be
  hidden. Rollback refuses it.
- **Silence is never "the run's":** a row with no record value, in a table
  the audit log doesn't cover, or one that was removed (the first read keeps no values to name it by), goes to a person.
  So does everything when the audit log can't be read.

Any other user's write during the run, anywhere in the store, sends the
rollback to a person, with the diff. That is fail-closed on a busy app: it
makes rollback a tool for a quiet store, or a moment when only the run is
writing. Without attribution, nothing is compensated. "The run's own user"
is the account: the bot account must be used by the run alone, since
anything signed in as it counts as the run.

**What a rollback can't see.** Flow reads the whole store before and after
each compensation, but not during one. A colleague's write to the run's
record while a compensation runs can be lost with it: an edit to a run row
the compensation then deletes, a new row the app's delete takes with the
record, or an edit a restore overwrites. An audit log shows such a write
afterward (`RECONCILIATION_REQUIRED`); it doesn't prevent it.

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
    allow_tables: []                # reviewed tables the app writes on its own, beyond the profile
  allow_delete: false               # the default: no hard deletes
  exclusive: false                  # true only for a declared test copy no one else writes
  profile: openemr                  # the app's own bookkeeping (app_profiles)
  actor_names: [openadapt_bot]      # who the run signs in as, for the profile
  attribution:                      # OpenEMR's audit log (VERIFY_RUN.md, Records outside the target)
    kind: audit_read                # required: the app's audit log
    actor_field: user
    self: [openadapt_bot]
    record_field: patient_id
    record_key: pid
    event_field: kind
    write_events: [insert, update, delete, replace]   # every write kind the log names
    insert_events: [insert, replace]
    delete_events: [delete]
    table_field: tbl
    read:                           # the table and write kind, derived from the log
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
- **It isn't a transaction.** Any other user's write during the run sends
  the rollback to a person; without attribution, nothing is compensated.
- **A save that removes a row isn't rolled back** outside a test copy: the
  removed row can't be shown to be the run's.
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
- **Single web runs only, for now.** A desktop, RDP, worklist-pool or
  durable (pausable) run with rollback on is refused before it starts. The
  Standard and Regulated execution profiles require durable runs, so
  rollback doesn't run under them yet.
- **Database-numbered ids move on.** A delete compensation can't wind back
  an id counter (MySQL `AUTO_INCREMENT`, SQLite `AUTOINCREMENT`); each read
  accepts a counter that moved, with no row left behind, by no more than the
  rows the run added to that table.
- **A rolled-back run isn't resumed.** Durable runs are refused with
  rollback on; and as a second guard, once a compensation has started,
  `resume`, the attended console and its relay all refuse the run.
- **Another user's own UI state counts too.** Per-user rows such as
  OpenEMR's recent-patient list or sign-in counters move when a colleague
  works; the app profile excuses only the acting user's own rows, so the
  rollback goes to a person.
- **The idempotency key stays used.** A rolled-back run doesn't free its key
  for a retry; a person starts the next run.
