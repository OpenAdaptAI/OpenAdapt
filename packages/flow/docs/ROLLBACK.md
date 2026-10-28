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
| `ROLLED_BACK` | Flow undid every change, and a read of the whole system of record matches the read taken before the run. |
| `HALTED_BEFORE_EFFECT` | Nothing landed: right after the stop, the whole system of record already matches the read from before the run. |
| `RECONCILIATION_REQUIRED` | It doesn't match. The report says what differs, and a private file holds the rows a person needs to repair it. |

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
   read. If they're equal, nothing landed, and the read itself proves it.
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
4. **Each compensation runs in a fresh session** through the normal replayer.
   Identity checks, the risk gate, the deployment's save-approval gate and the
   compensation's own effect check all apply. In the default approval mode, a
   person still answers each compensation's card. A web run gets a fresh
   session: cookies and storage are cleared, and the browser goes back to the
   entry URL. When Flow is attached to the operator's own browser, it never
   clears it, so only API-only compensations run there.
5. **Final read.** Flow compares the whole store with the read from before the
   run:
   - Equal: `ROLLED_BACK`.
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

OPENEMR_RESULTS

## What this doesn't do

- **It undoes only what a person approved.** A step without an approved
  compensation stops the rollback, and the store is reported as it is.
- **It isn't a transaction.** Another user's write between the first and
  final reads makes the store differ, and the run ends
  `RECONCILIATION_REQUIRED`. That is the safe answer, not a rollback of their
  write.
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
