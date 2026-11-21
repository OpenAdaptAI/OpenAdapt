# Verdict tiers, entity-scoped frames and shadow mode

A whole-database frame stops a correct write whenever any row moves. On a
live app many rows move with no harm: a scheduler's heartbeat, a task queue,
prices or totals the app recomputes after a save, a notification for each
watcher of a record. This page describes how the verifier separates those
from real harm without letting any planted fault through.

## Three tiers

Every check of a write ends in one of these tiers. `WriteResult.tier` names
it, and `to_dict()` returns it with `side_effects`, `enforcement` and
`blocks`.

| Tier | Outcome | What it covers |
| --- | --- | --- |
| `hard` | `REFUTED` | The target is missing, a value is wrong after normalization, a duplicate, a changed or removed row in a business-entity table that the write doesn't declare (including another user's row and an unexcused column of the acting user's row), a late revert, a lost update, any removed row, and anything no row comparison explains. |
| `logged` | `CONFIRMED` | The write holds, and rows moved only outside the entity tables (added or changed, never removed). Each is listed in the frame evidence as `side_effects`: table, change, row count and column names, never keys or values. The same rows go to `openadapt-flow learn` as candidate excuses. |
| `doubt` | `UNCERTAIN` | Timing or knowledge doubt that the verifier's own waiting didn't clear. |
| `clean` | `CONFIRMED` | Nothing else moved. |

A logged pass is still `CONFIRMED`. A harm that slips through the logged
tier is therefore counted as a silent pass by any grader that reads the
outcome. The tier never hides a pass.

### Retry before reporting a doubt

Before a write is judged, the verifier waits for its declared records to
become visible: 0.5 s, then 1, 2 and 4 s, within 8 s in all
(`OPENADAPT_VERIFY_RETRY_S` sets the budget; 0 turns it off). The wait
reads only through database verifiers, so the wait itself can't move a
bookkeeping row. It never decides anything: the full judge runs afterwards
either way. A record that is present stops the wait at once.

The database frame's own settling (`settle_s`, `settle_attempts`) ignores
motion in tables outside the entity tables, so a scheduler that never stops
doesn't keep a write unsettled. Those tables are still diffed against the
baseline in the read that is judged.

## Entity-scoped frames

`frame.database.entity_tables` lists the business-entity tables. When it is
set, the hard checks cover those tables and rows elsewhere are logged. The
tables the frame already names (`views`, `judged_by`, `new_row_fields`,
side records, learned excuses, owner tables, the app profile's rules)
always count as entity tables. When it is unset, every table is judged as
before.

The list is derived from the setup, never written by hand
(`openadapt-flow learn entities`):

- each kind's main table;
- every table a contract's own read selects from, which covers the child
  tables a write adds and the tables a field's location reads through;
- the acting user's table: the one table that every column bound to an
  actor parameter references by foreign key. When it can't be derived, no
  scope is proposed, because another user's row must stay a stop.

On a frame that diffs the whole database, a surface's own check columns
(`pin__*`, `empty__*`, which a generated read computes as 1 or 0) are not
compared on undeclared rows. One can flip with the clock and no write. The
stored columns they are computed from are compared themselves.

## The safety bar

A relaxation (an entity scope, or a learned mask) is admitted only if the
planted faults are all still caught under it
(`openadapt_flow.runtime.effects.admission`):

- wrong row, wrong value, duplicate, sibling business-record edit, late
  revert and lost update;
- for a relaxation that touches the acting user's table, another user's row
  and another column of the actor's own row.

Every class must have at least one case, every case must be caught, and
every case caught before the relaxation must be run again under it.
`fault_gate` names count for the same classes (`wrong_record` is a wrong
row, `unrelated_edit` a sibling edit).

```
openadapt-flow learn entities propose --manifest manifest.yaml --read read.yaml --out scope.json
openadapt-flow learn entities stage scope.json --out candidate/
# run the planted faults against candidate/ and write faults.json
openadapt-flow learn entities apply scope.json --faults faults.json --by "A. Reviewer"
```

`faults.json` is `openadapt.relaxation-faults/v1`: the proposal's
`proposal_sha256` and one `{case, class, caught}` per case. `apply` refuses
results run under another proposal, and writes every contract or none.

## Shadow mode by default

A contract is new to an app until it has been seen to judge real writes
correctly. Until then (`enforcement: shadow`) its verdicts are computed and
recorded in full, but `WriteResult.blocks` is false, so a caller that stops
on a verdict doesn't stop. Each check is appended to the contract's gate log
(`OPENADAPT_GATE_DIR`, by default `<OPENADAPT_LEDGER_DIR>/gates`, else
`~/.openadapt/gates`; names and outcomes only).

After N checks in a row with no stop of a correct write, gating is offered,
and a person turns it on:

```
openadapt-flow gate status contract.yaml
openadapt-flow gate label contract.yaml <write_id> caught      # a real harm: keeps the run
openadapt-flow gate label contract.yaml <write_id> false_stop  # breaks it, and is counted
openadapt-flow gate enable contract.yaml --by "A. Reviewer"    # refused until offered
```

N is `OPENADAPT_GATE_CLEAN_RUNS`, kept between 20 and 50 (default 30). A
stop that nobody has labelled breaks the run, since it may be a false stop.
`OPENADAPT_ENFORCEMENT=gating` gates every contract (the behaviour before
shadow mode), and `shadow` keeps every contract in shadow. Shadow mode
changes only `blocks`. The ledger still holds a refuted attempt, so a retry
is never sent silently.

## Limits

- An edit by another user to a record in an entity table, with no audit
  log or modified-by field to attribute it, still stops the write. Nothing
  in the store tells it apart from the agent editing a sibling record.
- A harm confined to a table outside the entity tables (an agent that
  changes a notification or a queue row) is logged, not stopped. The scope
  is admitted only when the planted faults, which target business records,
  are all caught. It is not a check on side tables.
