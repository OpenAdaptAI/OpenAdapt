# Scoreboard protocol

Version 2026-10-18.1. Written before the first scoreboard run. Changes are
appended as dated amendments, never edited in place.

The scoreboard is the one benchmark that the README, the website and any paper
quote. A figure is quotable only if `python -m scoreboard.build` produces it
from rows committed under `raw/`.

## Question

For the same task on the same app, how do these compare: a computer-use agent
working alone, the agent's first run under OpenAdapt, and the programs
OpenAdapt compiles from it?
- Is each item right, as read back from the system of record?
- What goes wrong silently?
- What does each arm cost, and how long does it take?

## Tasks

All data is synthetic.

| Task | App | Unit of work | Read-back |
|---|---|---|---|
| `mockmed` | MockMed, bundled with openadapt-flow, run in-process | Open the first referral, start a Triage encounter, save a note (the note varies per item) | `GET /api/db` on the app's store: exactly one record, patient p1, type Triage, note = the item's note |
| `openimis` | openIMIS 25.10 reference stack (`benchmark/openimis_claims`), digest-pinned images under Rosetta | Enter one health-facility claim: insurance number, claim number, diagnosis, service, explanation; save | A read-only Postgres role reads `tblClaim`: exactly one claim with this claim number, insuree, explanation, status Entered, facility VIHOS001, diagnosis A389 |
| `openemr_referral` | OpenEMR test copy (podman fixture, `benchmark/openemr_referral_timing`) | Register a referred patient | Direct SQL on the fixture: one new chart with 12 of 12 fields, or none for the duplicate check |

MockMed is the CI-size smoke task. OpenEMR rows are imported from its own
timing study. They are exactly that study's published set (its PROTOCOL.md and
CAVEATS.md apply), until the study's harness moves into this repository.

## Arms

One row per arm, added as each arm lands:

1. `agent`: the agent alone. Claude Sonnet 5.5 (`claude-sonnet-5-5`) with the
   `computer_toolset_20260801` tool and effort `medium` drives a plain
   Playwright browser (`agent.PlainComputer`). Nothing records or checks it.
   Per-item spend cap $0.50 to $0.75.
2. `first_run`: the agent's first run under OpenAdapt. It is recorded by the
   JIT wrapper (`openadapt_flow.jit.compile_agent`), read back, compiled, and
   taken through admission (confirm parameters, review, attended canary,
   admit). When compile-agent (#1277) lands, this arm moves to it.
3. `program`: the compiled program driving the UI, with no model calls.
4. `program_fast`: the throughput fast path (from the throughput session).
5. `program_api`: the program with the API step (#1269).
6. `parallel`: arm 4 or 5 with N parallel workers.
7. `code_agent`: the code-as-action authoring agent's first run.

The program's execution profile is recorded per run:
- MockMed's program is admitted and runs on the standard (governed) profile.
- openIMIS's program is not admitted: the standard gate refuses these forms
  today (`raw/openimis/first_run/*/items.csv`, and
  `demo/cua_to_program/results/<run>/admission.json`). It runs on the demo
  profile with the same read-only SQL record check attached.

## Items and disruptions

A batch is 100 items, numbered from 1, run in order on one app instance. Each
item has its own values (`tasks/*.py`).

| Item | Disruption | Right answer |
|---|---|---|
| 30 (MockMed) | `optimistic`: the server rejects the save, the screen says saved | stop or flag; nothing saved |
| 40 (MockMed) | `wrong_record`: the save lands on another patient | flag the wrong record |
| 50 onward | UI change. MockMed: buttons renamed (`?drift=rename`). openIMIS: the icon-only Save becomes "Save claim" | save correctly, or stop without a wrong record |

The UI change stays on from item 50 to the end of the batch, as a vendor
release would.

A program arm retries an item up to 3 times, but only when the read-back shows
nothing was written. Every attempt is a row. If an item after the UI change
stops on every attempt, the batch pauses for a person, and the remaining items
are recorded as `paused`.

Repair arms (self-repair #1265, pre-approved repairs #1294) join as separate
rows when they land.

## Outcome of an item

The outcome is decided by the read-back after the item, never by what the arm
reported (`schema.py`):

- `correct`: the intended record, exactly once, with the intended values.
- `silent_wrong`: the arm reported success and the read-back disagrees
  (missing, duplicate, or wrong).
- `safe_stop`: the arm stopped or said it could not finish, and nothing wrong
  landed.
- `flagged_wrong`: a wrong record landed and the arm flagged it.
- `paused`: not attempted.

The agent "reports success" when it ends its turn normally and its last
message does not say it was unable to finish (`run.FAILURE_WORDS`). A program
reports success when its run ends `VERIFIED`, or, on the demo profile,
`COMPLETED_UNVERIFIED` with every record check confirmed.

## Metrics per arm

Computed over the final attempt of each item. All are in `scoreboard.json`.

- Correct by read-back, on items without an injected fault.
- Silent wrong saves.
- Safe stops: on normal items (a cost) and on fault items (the right answer).
- Flagged wrong saves, and paused items.
- Model calls, tokens and list-price cost per item, from the API usage.
- Task seconds per item: the arm's own work, first action to done. Sign-in
  and opening the blank form are excluded for every arm.
- Wall seconds per item: browser start to read-back.
- Median and p90 of both, over correct items, from timed rows only.
- Items per hour: correct timed items, divided by the wall time of all timed
  attempts.
- People asked to approve.

## Timing

A row is timed only if the 1-minute host load average is at or under 12 (the
core count) at the start and at the end of the item. Rows over that are kept
for correctness and labelled untimed. Timed runs wait for a quiet host.

The OpenEMR rows use that study's own start gate (load under 12 before each
run).

## Spend

Claude API spend is capped at $30 in total, on one key, which is never printed
or stored. `raw/spend.json` is the running ledger. It starts from the $0.577
spent recording the launch-demo agent runs, and every agent item adds its
list-price cost.

## Reproduce

```bash
cd packages/flow/benchmark
python -m scoreboard.run first_run mockmed --run-id <id>
python -m scoreboard.run program   mockmed --run-id <id> --n 100 --pause-after-change-halt
python -m scoreboard.run agent     mockmed --run-id <id> --n 100
python -m scoreboard.run agent     openimis --run-id <id> --n 50 --tag <2-5 letters>
python -m scoreboard.importers openimis-demo --run-id <demo run>   # arms 2 and 3
python -m scoreboard.importers openemr
python -m scoreboard.build
```

openIMIS arms 2 and 3 come from `demo/cua_to_program` (`run.sh`), which records
the agent through the JIT wrapper, compiles the program, and runs the batch.

## Data

`raw/<task>/<arm>/<run id>/` holds `items.csv` (one row per attempt), a
`meta.json` (engine commit, protocol version, model, source and its SHA-256 for
imports) and, for agent arms, one ledger per item with every model call's
tokens and cost. Screenshots and videos are not committed. Raw data is
committed before any number is quoted.
