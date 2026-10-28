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

If that read fails, the rollback isn't armed. The run goes ahead unchanged,
and the report says so.

### After a stop

1. **Fresh read.** Flow reads the store again and compares it with the first
   read. Each row that changed is the run's, another user's, or nobody's
   (see [Other users](#other-users)). If no row is the run's, nothing
   landed, and the read itself proves it.
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
5. **Conflicts.** If another user changed a record the run wrote, after the
   run wrote it, Flow compensates nothing and ends `RECONCILIATION_REQUIRED`
   with the diff, so their change isn't overwritten. The same happens if a
   record the run touched changes between the stop and the first
   compensation.
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
   apart from rows another user changed:
   - Nothing else differs: `ROLLED_BACK`. For a whole-database read with
     nothing set aside, the verifier's own `diff_database`
     (`openadapt_flow.verifier`) must agree, or the run isn't called rolled
     back.
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

- A row another user changed, that the run didn't touch, is set aside. The
  report counts it (`other_actor_rows`).
- A row nothing attributes counts as the run's. A rollback never sets aside a
  change it can't explain, so without attribution, any other write in the
  window makes the run `RECONCILIATION_REQUIRED`.
- A record the run wrote that another user also changed is a conflict
  (`conflicts`). Nothing is compensated.

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
  ignore_columns:
    "*": [modified_at]              # columns that change on their own
  allow_delete: false               # the default: no hard deletes
  attribution:                      # optional: who changed a row
    kind: record_field
    actor_field: modified_by
    self: [openadapt_bot]
    server_set: [modified_by]
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
| Stop after the save, with the proposal not approved | `RECONCILIATION_REQUIRED` | The record stays; `diff.json` names it |

`rollback qualify` passed both halt points (before and after the save).
None of the runs made a model call.


## What this doesn't do

- **It undoes only what a person approved.** A step without an approved
  compensation stops the rollback, and the store is reported as it is.
- **It isn't a transaction.** Without attribution, another user's write
  between the first and final reads ends the run `RECONCILIATION_REQUIRED`.
  With it, their writes are set aside, but a record both of you changed is
  left for a person.
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
- **Web runs only, for now.** Desktop and RDP runs don't arm a rollback.
- **The idempotency key stays used.** A rolled-back run doesn't free its key
  for a retry; a person starts the next run.
