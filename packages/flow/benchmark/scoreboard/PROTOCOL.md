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

On openIMIS the UI change is a script injected into the one running app, so
it affects every client. Runs on the same app must not overlap a UI-change
run. Each openIMIS agent row records the state the app actually served
(`ui_change_on=` in `detail`).

The arms can use different models. openIMIS arm 2 was recorded with
`claude-opus-5-5` for the launch demo, while arm 1 is Sonnet 5.5. The
scoreboard's Model column says which model each row used.

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

Two things to check before quoting an agent's `silent_wrong` count:
- The "reported success" rule is a word match, so review every agent row
  that isn't `correct` by hand. Each item's ledger
  (`raw/<task>/agent/<run>/ledgers/`) keeps the agent's full final message.
- openIMIS also counts a claim saved under a different claim number as a
  wrong save. The insuree's claim count is taken before and after the item.

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
- People asked to approve. In arm 2's live MockMed runs, the parameter
  confirmation, review and admission are scripted: a named test reviewer and
  a registered throwaway admitter key do them. `approvals_asked` counts the
  approvals the flow requires, not a person's time.

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

## Amendment 1 (2026-10-18, after a code review, before any figure was quoted)

Arms 1 and 3 on MockMed and arm 1 on openIMIS were running when these changes
were made:
- The hand-review rule for an agent's `silent_wrong` and openIMIS's insuree
  claim count, both in "Outcome of an item". The insuree count, and the
  per-row record of the UI state the app actually served, apply to runs
  started after this amendment. The openIMIS agent run `2026-10-18-a` had
  already loaded the earlier code, so its non-correct rows get checked by
  hand against the claims table.
- The note that arm 2's approvals are scripted.
- The note that the openIMIS UI change is global.
- The Model column, and the "done correctly, all items" column (correct items
  out of every planned item, paused ones included).

## Amendment 2 (2026-10-19)

- **Hand check.** Each agent run directory may hold `hand_check.json`: one
  entry per row that isn't correct, with the outcome confirmed or not and the
  evidence. The build reports "N of M checked" per agent arm. Every agent row
  that isn't correct has been checked: MockMed 2 (both silent wrong confirmed),
  OpenEMR 10 (all safe stops), openIMIS 1 (a harness timeout, safe stop).
- **One model per table.** openIMIS arm 2 was re-run with `claude-sonnet-5-5`
  (`demo/cua_to_program/results/sb-sonnet-1`), the model arm 1 uses. The
  launch demo's Opus 5.5 run stays under `raw/` with `"in_table": false` in its
  `meta.json`, and the build skips such runs.
- **Arm 3r, self-repair.** The openIMIS batch from item 45, with each item
  run through `SelfRepair` (#1265) under the demo profile, no signed repair
  policy (#1294) and no fault probe (`repair_arm.py`). A repair approval, if
  one is asked, is answered by the script standing in for a person, as in arm
  2. The program is the one arm 3 uses (`launch-4`).

## Amendment 3 (2026-10-19): the agent's claims versus the read-back

A separate experiment, not an arm (`claims.py`, `raw/mockmed/claims/`).
Claude Sonnet 5.5 computer use works alone on MockMed while the fault server
breaks the save: `no_persist`, `partial`, `duplicate`, `wrong_record`,
`stale` (a concurrent clinician's record is lost: collateral loss), and
`optimistic`. There are 5 items per fault and 5 clean controls (`ok`). Per
item:
- The agent's claim is the agent arm's rule (`run.reported_success`). Every
  claimed success is hand-checked against the ledger (`hand_check.json`).
- `openadapt-flow verify-run` judges the write from `GET /api/db`, with a
  baseline captured before the agent starts. The contract is one new
  `record_written` for p1, Triage, this item's note, with collateral loss
  forbidden (the default).
- Ground truth compares the store before and after directly.

A bad save is any item whose ground truth is not `correct`. verify-run
"catches" a bad save when its outcome is not `verified`. A false alarm is a
clean save that verify-run does not verify.

Run `2026-10-19-a` was discarded before any figure was read: verify-run
refused its run record, a config error. Its spend is in the ledger. In run
`2026-10-19-b`, items 16 to 20 opened the page with `idempotency=demo`, so
their double send was de-duplicated. They are relabelled `duplicate_keyed`
(clean saves; see `meta.json`), and items 36 to 40 are the duplicate fault
without a key. OpenEMR's fixture has no fault injection, so the experiment
is MockMed only.

## Amendment 4 (2026-10-19): OpenEMR worklist runs, and arm 6

- **Worklist runs.** Arms 3, 4 and 6 on OpenEMR were also run through the
  throughput harness (`benchmark/openemr_throughput`, #1296, engine
  b82131e5). It runs 26 referrals as one worklist, after one sign-in per
  worker. Its worklist stops at the first item that needs a person, and the
  items not started are `paused`. Each run is listed on its own line under
  the table, with its engine, beside the October study's run (engine
  0c642bc0). The harness's own figure, correct saves over the run's wall
  time, is listed as "worklist throughput".
- **26 items, not 64.** The harness's made-up surnames repeat every 26
  items. Item 26 matches item 0, OpenEMR correctly shows a possible
  duplicate, and the run stops. The first two 64-item runs stopped exactly
  there. They are kept outside `raw/` and not imported.
- **Arm 6 is self-loaded.** Eight workers load the host themselves (mean
  load 42 and 171), so no arm 6 row can pass the per-item rule. Arm 6 is
  gated on the start only: the load is under 12 when the run starts. The
  mean load during the run is recorded, and its figure is labelled
  "self-loaded throughput", never timed per item. It is reported beside arm
  4's single-worker worklist figure.
- **Engine of an import.** An imported run's `meta.json` names the engine
  that made the rows (the study's engine tag, or the batch's own record),
  not the importer's checkout.

## Amendment 5 (2026-10-19): the quiet window overlapped other work

Another session ran headless MockMed walks, pytest and vitest from about
10:00 to 11:45, overlapping the OpenEMR worklist runs (10:50 to 11:26).
Each row carries the 1-minute load measured at its start and end, and the
`timed` flag follows it. No row with a recorded load over 12 is marked
timed:
- arm 3: 8 of 9 rows timed (loads 7.3 to 11.0);
- arm 4: 4 of 17 rows timed (loads 10.2 to 15.2);
- arm 6: none (self-loaded; Amendment 4).

## Amendment 6 (2026-10-19): arm 5, the API step

Arm 5 comes from the #1269 harness (`benchmark/openemr_api_tier/scoreboard_arm.py`,
engine 93c32e9f) on its own private copy of the pinned OpenEMR fixture (ports
9811/9810). There is one bundle, compiled from a recording with network
capture. Its save step's API step was proposed by the compiler and admitted
under the harness's name `benchmark-fixture`; no person admitted it.
- The API step replaces all 41 GUI steps with one POST to
  `new_comprehensive_save.php`. Its session token (`csrf_token_form`) is read
  from the live form in the replaying browser's own session, and the
  duplicate search runs first as a GET pre-check.
- There are five blocks of four referrals, with API steps on (arm
  `program_api`) and off (arm `program`, run `api-tier-2026-10-19`).
  The two settings are interleaved, the order alternates by block, the
  database is reset per block, and every run is gated at load 12.
- The possible-duplicate referral (r6) is fault `duplicate_check`: the API
  step's pre-check refuses, the GUI steps run, and they stop with no chart.
  That is a safe stop.
- Task seconds for arm 5 are the engine's time; wall seconds include the
  new browser context, sign-in and the read-back.

## Amendment 7 (2026-10-19): claims versus the read-back on OpenEMR

The OpenEMR counterpart of Amendment 3 (`claims_openemr.py`, run
`raw/openemr_referral/claims/2026-10-19-a`).
- **Faults.** OpenEMR itself and OpenAdapt are unchanged. The faults are
  injected only in the fixture's database:
  - by a trigger installed before the agent starts: `partial` blanks the
    home phone, `wrong_value` changes the postal code;
  - by a poller once the new chart is written: `duplicate` copies it,
    `no_persist` deletes it 3 s later, `collateral_loss` deletes another
    chart, `collateral_edit` changes another chart's phone.
- **Setup.** The baseline is restored before every item and after the last
  one. One existing chart is seeded through OpenEMR's patient API, because
  the baseline has none.
- **Plumbing checks before the run, with no agent.** Each fault was tested
  with a direct SQL insert: the trigger or poller acted, and the ground
  truth named it. The one-item smoke run `smoke-2026-10-19` (a clean save,
  before the seeded chart existed) is kept and marked `in_table: false`.
- **verify-run.** It reads OpenEMR's patient API over loopback HTTP on the
  UI port, with the fixture's read-only oracle client. The REST verifier
  has no TLS switch for the self-signed HTTPS port. The token goes to the
  subprocess in an environment variable and is never stored.
- **Fault not reached.** Item 928 (`collateral_loss`): the agent stopped to
  ask for Birth Sex, a required field the instruction doesn't name. No chart
  was written, so the poller never fired. It is counted as "fault not
  reached", and item 936 was run as the fifth `collateral_loss` item.
- **Display.** A claims run is shown in the table only when its `meta.json`
  says `complete: true`, set when every planned item has a row.

## Amendment 8 (2026-10-19): arm 7 timed pass

Arm 7 (`code_agent`) ran 5 MockMed items and 5 OpenEMR items (721 to 725)
from the `claude/scoreboard-arm7` checkout (#1307, engine a5627748),
during a window the other sessions were asked to keep quiet. All 10 were
correct by read-back. The load stayed between 11 and 32: another session's
`silent-saves` benchmark ran throughout. So only 1 MockMed row is timed and
no OpenEMR row is. On OpenEMR the agent's program compiled, but the standard
gate refused its canary, so it is not admitted. The two fixes the run needed
(the `signal` import, and a free port for the MockMed server) and moving its
JIT cache out of the source tree are on #1307. The time columns now show n,
the number of timed rows behind each median.

## Amendment 9 (2026-10-19): collateral change, re-judged

verify-run's collateral check caught a non-target record that vanished, but
not one that changed: the 5 OpenEMR `collateral_edit` items were `verified`
(#1373). The fix is #1375 (`Effect.forbid_collateral_change`, applied by
verify-run). `claims_rejudge.py` re-judged the 5 `collateral_edit` items,
plus the 5 clean saves as a false-alarm control, with #1375's verify-run
(9850b627). The original run kept no records, so each item's database
state was rebuilt without the agent: baseline restored, existing chart
seeded, the item's chart saved through the API, and the same edit applied.
Result: 5 of 5 caught, and 5 of 5 clean saves verified. The run is labelled
a re-judgement on rebuilt state, not an agent run.

The control found a bug in #1375 before it shipped: a baseline stores
records as canonical JSON, so nested values came back with their keys in
another order and read as changed. #1375 now compares values in canonical
form, with a test. It also found a scoreboard bug: a rerun of an item could
read the receipt an earlier run left behind. Every claims script now uses a
fresh directory per item and reads a receipt only after verify-run exits 0
or 1. Every row of the earlier claims runs has exit 0 or 1 and a fresh
directory, so those runs were not affected.

## Amendment 10 (2026-10-25): arm 7 with a read-back on every save, and the program it compiles

Written while arm 7 was on item 25, before any result was summed. Code as
action became the default first-run agent,
and run 1 now reads every save back from the system of record before the
agent goes on (`compile_agent.harness.record_readback`; packages/flow
docs/CODE_AS_ACTION.md). Arm 7 measures that agent, and arm 3 measures the
program compiled from it:

- MockMed items 21 to 40 (20 items, the protocol's own schedule, so item 30
  is the `optimistic` fault and item 40 the `wrong_record` fault). Run id
  `2026-10-25-rb` for both arms.
- Arm 7: `run code_agent mockmed --start 21 --n 40 --keep-program`. Each item
  is its own first run. The MockMed server runs on the fixed origin, because
  item 21's admitted program is kept for arm 3.
- Arm 3: `run program mockmed --start 21 --n 40 --program-from
  code-2026-10-25-rb`, the program compiled and admitted from arm 7's item 21.
- The outcome rules are unchanged. The read-back the agent sees is the
  agent's evidence, not the scoreboard's: the scoreboard still reads the store
  on its own after each item.
- Rows are timed only under the protocol's load rule.
- Arm 7 now follows the runner's `--no-faults` switch like arms 1 and 3
  (faults on by default). Its earlier runs were items 1 to 5, before any
  fault item, so they are unaffected.

## Amendment 11 (2026-10-25, after Amendment 10's run): arm 7 reports what its read-back saw

In Amendment 10's run, item 30 (the save the server drops while the screen
says "saved") came out `silent_wrong` by the word rule. The agent did not
claim the save: its final message says the save "did not take effect" and
that the read-back showed nothing was saved (`raw/mockmed/code_agent/
2026-10-25-rb/ledgers/item-030.json`), but none of `FAILURE_WORDS` is in it.
By hand review the item is a safe stop (`HAND_REVIEW.md` next to the run).
Amendment 10's rows keep the word rule's verdict.

From this amendment on, arm 7 reports success only when the word rule passes
and its own read-back after the last save shows the save landed. The read-back
is part of the arm (OpenAdapt reads every save back during run 1), so this is
what the arm reports, not the scoreboard's judgement: the scoreboard still
reads the store on its own after each item.

## Amendment 12 (2026-10-25, written after the runs it describes): arm 7 on the OpenEMR referral with read-back

A real-app check of the read-back, not a scored batch. Arm 7 on the
`realrun` OpenEMR fixture (pinned image, ports 9301/9300), items 1 to 5
(`2026-10-25-rb`), then item 6 (`2026-10-25-rb-admit`) after a recorder fix.
Amendment 11's success rule applies.

- Items 1 to 6: `correct`; each save's read-back showed the named chart
  (matched by email and last name) landed.
- Admission refused all 6 before the canary ("a write step lacks an
  independent read-back"). Items 1 to 5: the confirm-dialog save step had no
  system-of-record change bound, because the chart lands after the recorder's
  post-save read. The recorder now waits for the change after a save the gate
  let through (`Recorder.expect_write`); item 6 and a scripted replay show
  the save step bound (`record_written`). The remaining refusal is the
  "Create New Patient" click before it: write-shaped, it opens the
  duplicate-check dialog and writes nothing, so it has no read-back. Not
  fixed here: binding a "nothing was written here" read-back to such a step
  changes what admission accepts.

## Amendment 13 (2026-10-25, after review): the read-back's rules changed after Amendments 10 to 12's runs

After review, run 1's read-back counts only a record created during run 1 as
the named one (any change to or removal of a starting record stops the run),
stops on a second named record (a duplicate, counted even without record
ids), treats a save with no new record as unconfirmed and stops the next save
if it lands late, stops when a read fails, and calls a save `landed` only
when the new record holds every input value (`compile_agent.harness.
RecordReadBack`). Amendments 10 to 12's runs used the earlier rules. Not
re-run; by the rules, their verdicts are the same: every item made one save,
on MockMed's empty store or next to OpenEMR records it left untouched; item
40's save created a record on another patient (wrong record either way) and
item 30's showed no new record (unconfirmed, the earlier "no change").
