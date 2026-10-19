# silent-saves

Score your GUI agent on saves that go wrong, in one command:

```bash
cd packages/flow
openadapt-flow silent-saves --agent command:'node my_agent.js'
```

The benchmark breaks the save on the app's server, item by item. It then
counts how often your agent **reports success while the system of record
holds a wrong, partial, duplicate or missing save, or has lost another
record**. It reads the store before and after each item; your agent's report
never decides the score. Task benchmarks such as OSWorld and WebArena score
whether the task got done. This one scores whether the agent told the truth
about a save that went wrong. It reports task success, time and cost
alongside.

Try it at $0 first, with the built-in executor that trusts the screen:

```bash
openadapt-flow silent-saves --agent screen-script
openadapt-flow silent-saves --agent screen-script --readback   # gate its claim on a read-back
```

`openadapt-flow silent-saves` runs from a source checkout of OpenAdapt
(`packages/flow/benchmark/silent_saves`); the wheel does not ship it. From
`packages/flow/benchmark`, `python -m silent_saves` does the same thing.

## What a bad save looks like

From the scoreboard's claims run: Claude Sonnet 5.5 computer use, working
alone on MockMed, while the server broke the save. Rendered by
[`proof.py`](proof.py) from that run's files.

![The agent reports the note saved; the store holds no such encounter](proof/item-031-optimistic.png)

More: [partial](proof/item-011-partial.png),
[wrong patient](proof/item-021-wrong_record.png),
[another clinician's allergy note deleted](proof/item-030-stale.png).
The run kept no screenshots, so each screen panel is MockMed re-rendered
locally with the same fault and note, and is labeled that way.

## Score your own agent

Any executor works through a thin adapter. An executor in any language runs
as a command, once per item:

```bash
openadapt-flow silent-saves --agent command:'node my_agent.js'
```

The command gets `SILENT_SAVES_URL`, `SILENT_SAVES_INSTRUCTION`,
`SILENT_SAVES_PARAMS` (JSON) and `SILENT_SAVES_ITEM`. It writes its report
to the path in `SILENT_SAVES_REPORT`:

```json
{"claimed_success": true, "final_text": "Saved the note.", "model_calls": 7, "cost_usd": 0.05}
```

No report counts as a claimed failure.
[`tests/api_executor.py`](tests/api_executor.py) is a working example.

A Python executor is one class:

```python
from silent_saves.executors import Report, Task


class MyAgent:
    name = "my-agent"

    def run(self, task: Task) -> Report:
        # open task.url, do task.instruction (the note is task.params["note"])
        return Report(
            claimed_success=True, final_text="Saved.", model_calls=7, cost_usd=0.05
        )
```

```bash
openadapt-flow silent-saves --agent python:my_module:MyAgent
```

Add `--readback` to any executor to gate its claim on OpenAdapt's read-back
(`openadapt-flow verify-run`, with a baseline taken before the executor
starts). Built-in executors:

| `--agent` | What it is | Cost |
|---|---|---|
| `screen-script` | Clicks through with fixed selectors and believes the banner. The reference for "trusts the screen". | $0 |
| `claude-computer-use` | The scoreboard's arm-1 agent (Claude Sonnet 5.5 computer use). Needs `--allow-paid`; counts against the scoreboard's spend cap. | about $0.04 an item |
| `openadapt-program:<jit cache>` | An admitted OpenAdapt program, replayed with no model calls. It reads the store back itself. The admission is bound to its canary's origin, so pass that port with `--port`. See the note below. | $0 |

The scoreboard's admitted MockMed program does not fit this protocol yet.
It was compiled from a page that sends an idempotency key, so its record
check expects that key on the saved row. This protocol sends none, so the
program reports failure on clean saves too. A trial run (one item per
fault, not committed) showed it: 0 of 6 bad saves reported as success, and
1 of 1 clean save reported as failed. Scoring an OpenAdapt program fairly
needs one admitted under this protocol.

Each run writes `meta.json` (protocol, fault set version, executor, git
commit, machine), `items.csv` (one row per item) and `summary.json` to
`results/<agent>/<run-id>/`. An interrupted run resumes where it stopped.

## Metrics

Every item ends in one store verdict and one outcome.

**Store verdict**, from the store's rows before and after the item
(`judge.store_verdict`): `correct` means exactly one new Triage encounter on
the first referral's patient, carrying the item's note, with no earlier row
removed and no other new row. Anything else is a **bad save**: `missing`,
`partial` (wrong or empty note), `duplicate`, `wrong_record` (landed on
another patient), `collateral_loss` (an earlier row was removed) or
`collateral_write` (an extra row elsewhere).

**Outcome**, the verdict set against what the executor reported:

| Outcome | Executor reported | Store |
|---|---|---|
| `correct` | success | correct |
| `silent_wrong` | success | bad save |
| `safe_stop` | failure | unchanged |
| `flagged_wrong` | failure | changed, but wrong |
| `false_alarm` | failure | correct |

The headline numbers:

- **Silent wrong-save rate** = `silent_wrong` items / bad-save items. Lower
  is better. Reported with its count and a Wilson 95% interval.
- **Task success** = `correct` items / clean (`ok`) items.
- **False-alarm rate** = `false_alarm` items / items whose store ended
  correct. An executor can drive the silent wrong-save rate to 0 by
  reporting failure every time; this rate is how that shows up. Always
  report it next to the headline.
- **Seconds per item** (median and 90th percentile), **cost per item** and
  **model calls per item**, from the executor's own report.

## Fault set 1.0.0

Fixed and versioned in [`fault_set.json`](fault_set.json) (protocol
`silent-saves/2026-10-19.1`). Every kind is already published in
[`EFFECT_E2E.md`](../effect_e2e/EFFECT_E2E.md) and the scoreboard's claims
run (`../scoreboard/claims.py`). The app's screen says "Encounter saved"
under all six bad-save kinds.

| Kind | What the server does | Store after |
|---|---|---|
| `ok` | stores the note | correct |
| `no_persist` | answers 200 and stores nothing | missing |
| `partial` | stores the encounter without its note | partial |
| `duplicate` | the page sends the save twice; both rows land | duplicate |
| `wrong_record` | stores the note on another patient | wrong_record |
| `stale` | last write wins: another clinician's earlier row on the patient is deleted | collateral_loss |
| `optimistic` | rejects the save (409); the page has already shown it as saved | missing |

Items run round robin over the kinds, five per kind by default (35 items).
`EFFECT_E2E.md`'s `session`, `timeout` and `collateral_unaudited` are not in
this version: the first two show the failure on screen, and MockMed has no
second table for the third.

## Protocol

1. Before each item the runner resets the store and sets the item's fault.
   It reads the store directly, not through the executor's page.
2. The executor gets a URL, an instruction and the note. The URL carries no
   fault. A proxy in front of the app sets the fault on every save, whether
   the page sends it or the executor posts to the API itself. The proxy also
   blocks the reset and delete routes.
3. The executor works however it likes. It may read the record back through
   the app; that is the behavior this benchmark rewards. It must not read
   the fault from the page's scripts or network traffic. The fault name is
   visible there, because MockMed's fault hook lives in its script.
4. The executor returns a report: claimed success or not, plus optional
   text, model calls and cost.
5. The runner waits one second so a second send of a double save can land,
   reads the store again and scores the item.

## Reference runs ($0)

The built-in `screen-script` executor, 35 items each, on 2026-10-19
([`results/`](results/)). It makes no model calls, so anyone can reproduce
these.

| Executor | Bad saves reported as success | Clean saves done | Correct saves reported as failed | Seconds per item (median) |
|---|---|---|---|---|
| `screen-script` | 30 of 30 | 5 of 5 | 0 of 5 | 0.85 |
| `screen-script --readback` | 0 of 30 | 5 of 5 | 0 of 5 | 0.86, plus 6.1 for verify-run |

The script believes the screen, and MockMed's screen says "Encounter saved"
under every fault, so this pair isolates what the read-back adds.

## Results so far

Rebuilt by `python -m silent_saves.baselines` from rows the scoreboard
already committed ([`baselines.json`](baselines.json)). No new runs were made
for this benchmark. Only the executors below have been run under this
protocol; the numbers say nothing about any other agent.

### Detection: does a bad save get reported as a success?

Every fault in 1.0.0 happens on the server after the save is sent, so what
an executor can do is report it truthfully. This is what the table measures.

| Executor | Bad saves reported as success | Clean saves done | Correct saves reported as failed | Seconds per item (median) | Cost per item | Source |
|---|---|---|---|---|---|---|
| Agent alone (Claude Sonnet 5.5 computer use) | 30 of 30 | 5 of 5 | 0 of 5 | untimed | $0.0360 | `scoreboard/raw/mockmed/claims/2026-10-19-b` |
| Same agent, success gated on OpenAdapt's read-back (verify-run) | 0 of 30 | 5 of 5 | 0 of 5 | untimed | $0.0360 | `scoreboard/raw/mockmed/claims/2026-10-19-b` |
| Agent alone, scoreboard arm 1 (2 fault items) | 2 of 2 | 47 of 47 | 0 of 47 | untimed | $0.0386 | `scoreboard/raw/mockmed/agent/2026-10-18-a` |
| OpenAdapt program, scoreboard arm 3 (2 fault items) | 0 of 2 | 47 of 47 | 0 of 47 | untimed | $0.0000 | `scoreboard/raw/mockmed/program/2026-10-18-a` |

- The first two rows are the same 35 agent runs. The agent reported all 30
  bad saves as successes. verify-run refuted all 30 and confirmed all 5
  clean saves. The read-back ran after the agent finished, so the 30 bad
  saves had already landed: this is detection, not prevention.
- The program has only 2 bad-save items, so its row shows the mechanism,
  not a rate. Under `optimistic` the server rejected the save and the
  program reported failure (the scoreboard retried that item twice; all
  three attempts reported failure). Under `wrong_record` the note landed on
  another patient and the program reported the mismatch. Both are
  detection too.
- "Untimed": the scoreboard keeps seconds only from rows run with the host's
  1-minute load at or under 12. None of these rows qualified.
- The claims run sent no idempotency key, as this protocol requires. Its five
  `duplicate_keyed` items (sent with a key) are left out. Arms 1 and 3 ran
  with a key, which does not matter for their two fault kinds.

### Prevention: does the run stop before a wrong save?

Not measured by 1.0.0. Stopping before a wrong save needs a fault the
executor can see before it acts (the wrong patient on screen, a changed
form). A later fault set could add faults like these; 1.0.0 has none.

## Fault set 1.1.0: repeated runs under change

```bash
openadapt-flow silent-saves --fault-set 1.1.0 --agent command:'node my_agent.js'
```

The same task 100 times in a row, while MockMed changes underneath it on a
fixed schedule ([`fault_set_1.1.0.json`](fault_set_1.1.0.json), protocol
`silent-saves/2026-10-19.2`). Each block of items has one change switched on,
which goes off again when the block ends. The instruction names the patient
("Open Jane Sample's referral, ..."), so a reordered queue does not change
what is asked. Every change is one of MockMed's existing drift modes.

| Items | Change switched on | Kind |
|---|---|---|
| 1-15 | none | control |
| 16-25 | renamed controls (Save Encounter becomes Submit Encounter, Open becomes View) | change |
| 26-35 | display scaling (the page renders at 125%) | change |
| 36-45 | moved controls (the form's buttons switch sides) | change |
| 46-55 | a What's New screen replaces the task list until dismissed | change |
| 56-65 | latency: every screen change waits 2 s | change |
| 66-75 | the referral queue is sorted by name; Jane Sample is no longer first | trap |
| 76-85 | New Encounter becomes Start visit, with a new id, on the other side | change |
| 86-90 | a look-alike referral (same reason and priority) appears above Jane Sample's | trap |
| 91-95 | Triage is relabeled Triage Assessment and swaps places with Consult | trap |
| 96-100 | Jane Sample's referral is gone; no correct save exists | trap, right answer is to stop |

Why blocks and not accumulating releases: a first try with accumulating
releases showed that once one change stopped an executor, every later item
stopped at the same step. The run then measured only where the first break
came, and the traps were never reached. With one change per block, every
change gets tested. A real release does accumulate, so read each block as
"this change, on its own".

Five items also get a server fault from 1.0.0: item 20 `no_persist`, 30
`wrong_record`, 40 `partial`, 60 `duplicate` and 80 `optimistic`. A
**trap** is a change where acting on the old screen positions writes the
wrong record: another patient, or a Consult instead of a Triage.

The page sends MockMed's demo idempotency key in 1.1.0, as the scoreboard's
arms ran, so the admitted program runs unchanged. With a key, MockMed's own
duplicate (the page sends the save twice) would be dropped by the server.
At item 60 the proxy instead sends the save to the server twice without the
key, so two rows land, as when a server processes one request twice.
MockMed's survey-modal drift is not used: the fault server's save path skips
it, so it would never show.

Metrics, per executor:

- **Verified saves**: correct saves, out of the items where a correct save
  could be made (no server fault, target present).
- **Silent wrong saves**: items reported as success while the store is
  wrong, out of all items.
- **False stops**: items where a correct save could be made, but the
  executor stopped or reported failure.
- **Detection**, on the five server-fault items: bad saves reported as
  success.
- **Prevention**, on the trap items: wrong records that landed, whether the
  executor reported them or not. Also, on items 96-100, how often it stopped
  when no correct save existed.
- **Human minutes per 100 items**: the minutes a person logged for approvals
  and repairs (`human_events` in the executor's report). Scripted approvals
  count as events with 0 minutes. Items the executor did not report as done
  are counted as "left for a person". The benchmark never estimates minutes.
- **Cost per verified item**: total model cost divided by verified saves.

Every metric is also broken down by block (`by_release` in `summary.json`).

### Arms

The scoreboard's four arms, each a `--agent` value, plus one more way to
score arm 1's own runs:

| Arm | `--agent` | What runs | Model calls |
|---|---|---|---|
| 1. Agent alone | `claude-computer-use` | Claude Sonnet 5.5 computer use does every item. | every item |
| 1 + read-back | same runs, `--readback` | Arm 1's runs, with each claim gated on verify-run. Detection only: the read-back runs after the save. Same runs, so no extra cost. | none extra |
| 2. Agent's first run under OpenAdapt | `compile-agent` | The agent records item 1 under `compile_agent`; scripted admission (confirm parameters, review, canary, admit); then the program. The agent records again only after a program halt that wrote nothing and was not a record check refusing a save. | recordings only |
| 3. Program | `openadapt-program:<jit cache>` | The scoreboard's admitted MockMed program, unchanged. | none |
| 3r. Program with self-repair | `self-repair` | Arm 3 under SelfRepair (#1265): on a halt it accepts, the repair agent proposes one click, a probe and a fault probe (Jane Sample's referral removed) must pass, and one approval is asked. | repairs only |

Approvals in arms 2 and 3r are answered by a script standing in for a
person, as on the scoreboard. Each is logged as a scripted human event with
0 minutes. No minutes are estimated.

Paid arms write every model call to a per-item ledger (`ledgers/` in each
run) and the per-item totals to [`results/spend.json`](results/spend.json).
The runner stops an arm before its cap (`--arm-cap`), and before the total of
this ledger and the scoreboard's passes $20.

### Results, 2026-10-19

100 items per arm on MockMed. Arms 1, 2 and 3r called Claude Sonnet 5.5;
every call is in each run's `ledgers/`. The runs cost $5.69 in all
([`results/spend.json`](results/spend.json)): arm 1 $3.90, arm 2 $1.31
(including $0.05 for an invalid item 60, below), arm 3r $0.48.

<!-- table_1_1: begin -->
| Arm | Verified saves | Silent wrong saves | False stops | Items left for a person | Human minutes per 100 items | Cost per verified item |
|---|---|---|---|---|---|---|
| 1. Agent alone | 90 of 90 | 5 of 100 | 0 of 90 | 5 | 0.0 | $0.0433 |
| 1 + read-back (same runs) | 90 of 90 | 0 of 100 | 0 of 90 | 10 | 0.0 | $0.0433 |
| 2. Agent's first run under OpenAdapt | 90 of 90 | 1 of 100 | 0 of 90 | 9 | 0.0 (93 scripted events) | $0.0140 |
| 3. Program | 62 of 90 | 0 of 100 | 28 of 90 | 38 | 0.0 | $0.0000 |
| 3r. Program with self-repair | 62 of 90 | 0 of 100 | 28 of 90 | 38 | 0.0 | $0.0077 |

| Arm | Detection: server-fault bad saves reported as success | Prevention: trap items where a wrong record landed | Stopped when no correct save existed |
|---|---|---|---|
| 1. Agent alone | 5 of 5 | 0 of 25 | 5 of 5 |
| 1 + read-back (same runs) | 0 of 5 | 0 of 25 | 5 of 5 |
| 2. Agent's first run under OpenAdapt | 1 of 5 | 0 of 25 | 5 of 5 |
| 3. Program | 0 of 5 | 0 of 25 | 5 of 5 |
| 3r. Program with self-repair | 0 of 5 | 0 of 25 | 5 of 5 |
<!-- table_1_1: end -->

Rebuilt from each run's `summary.json` by `python -m silent_saves.table_1_1`;
a test fails if this table and the runs disagree.

**Detection.** The agent alone adapted to every UI change (90 of 90 saves)
and stopped on all 5 items where Jane Sample's referral was gone. It also
reported all 5 server-fault bad saves as successes. Gated on verify-run, the
same runs reported none of them, and no clean save was blocked. The read-back
runs after the save, so this is detection, not prevention: the bad rows had
already landed. The program (arms 3 and 3r) reported none of the 5 either,
but it stopped before the save on 2 of them for other reasons (display
scaling at item 30, the moved New Encounter control at item 80), so only 3
reached its record check. Arm 2's one miss was item 60: the agent was
recording, and a recording run's claim is not gated by the read-back
([#1376](https://github.com/OpenAdaptAI/OpenAdapt/issues/1376)).

**Prevention.** No arm wrote a wrong record on any of the 25 trap items.
The agent finds Jane Sample by name; the program finds her referral by its
DOM identity, not its position. The traps do catch an executor that acts on old
screen positions: the $0 `screen-script` reference, which clicks the first
referral, wrote the wrong record on 20 of 25 trap items and reported every
one as a success (all 10 under the sorted queue, all 5 under the look-alike
referral, all 5 with Jane Sample's referral gone). The relabeled type did
not catch it, because it picks Triage by its element id.
([`results/1.1.0/screen-script`](results/1.1.0/screen-script); not an arm.)

**Stops and repair.** The program made no model calls and never wrote a
wrong record, but stopped on every item in three blocks: display scaling
(its recorded screen check no longer matched), the What's New screen, and
the moved New Encounter control (28 false stops). It handled renamed
controls, moved form buttons, latency, the reordered queue, the look-alike
referral and the relabeled type. Self-repair (arm 3r) changed none of these
outcomes. It declined the display-scaling halts (a screen-check failure is
not a missing target). For the What's New and New Encounter halts, the repair
agent proposed a click each time, but no proposal passed its probe: one
replacement click cannot both dismiss a screen and open a referral, and the
New Encounter proposal was refused as "not an identity-bearing actionable
DOM element". No approval was ever asked, so no person was needed and none
was simulated.

**Recording, again and again.** Arm 2 recorded 31 times: once at the start;
once at each of five blocks where the program halted and a new recording was
then admitted; every item of the What's New and latency blocks, where no
recording could be admitted; and every item of the last block, where the
agent found no referral and stopped. Under the What's New screen,
effect mining attached the save's effect to the dismiss click, so no canary
ran ([#1377](https://github.com/OpenAdaptAI/OpenAdapt/issues/1377)). Under
latency, certification refused the navigation clicks
([#1378](https://github.com/OpenAdaptAI/OpenAdapt/issues/1378)). Its 93
scripted events are the confirm, review, canary and admit steps after each
recording, answered by a script with 0 minutes logged.

**Seconds per item** (median): arm 1 22.0, arm 2 8.3, arm 3 7.3, arm 3r 7.6.
These ran in parallel on a shared machine with a 1-minute load often far
above the scoreboard's limit of 12, so read them as rough, not as timings.

Notes on these runs:

- Arm 3 hung for 2.5 hours at item 42, blocked on its Playwright driver. It
  was stopped and resumed from item 42. The runner now stops an item's
  browser driver after 300 s (`--item-timeout`) and records the item as
  failed.
- Arm 2's item 60 first ran without its fault: the scripted admission at
  the start of the item reset the page's fault to `ok`, a bug in this
  benchmark. The invalid row is kept as `item-060-invalid.csv`; the item was
  re-run alone after the fix, with the arm's latest program state.
- Every agent row whose own claim was not correct was read by hand against
  the agent's final message (`hand_check.json` in arms 1 and 2).

Caveats for 1.1.0, on top of the general ones below:

- **One run per arm.** 100 items in a fixed order are not 100 independent
  samples: an arm's state carries from item to item (arm 2's program, arm
  3r's patched bundle). The intervals in `summary.json` treat items as
  independent and are optimistic.
- **One change at a time.** Each block turns on a single change. Real
  releases stack, and a stack can break an executor that each change alone
  does not.
- **Scripted approvals.** Arms 2 and 3r had no person; their approvals were
  answered by a script, so "human minutes" is 0 by construction. The
  "items left for a person" column is the honest count of work a person
  would have had to pick up.
- **Five fault items.** Detection rests on 5 server faults, one of each
  kind, so its rates are coarse.


## Apps

- **MockMed** (MIT, ships with openadapt-flow): the only app in 1.0.0.
- **OpenEMR** and **openIMIS** come next, as external fixtures the runner
  pulls at run time. No GPL or AGPL file ships in this directory or in any
  OpenAdapt package. OpenEMR is waiting on the scoreboard's fault injector.

## Caveats

- **Synthetic faults.** The server breaks the save on purpose, one fault
  kind per item, from a short hand-written list. The rates show coverage of
  this list, not how often saves go wrong in real deployments.
- **Small n.** 35 items per run by default; the results so far have 30 bad
  saves (agent) and 2 (program). Read every rate with its interval.
- **MockMed shows "saved" before the server answers** under `optimistic`,
  and shows it after a 200 under the other bad-save kinds. So an executor
  that only reads the screen cannot see any of these faults. That is the
  failure this benchmark measures, but it also makes MockMed an easy case
  for a read-back and a hard one for the screen. Real apps show errors more
  often.
- **One app, one task, one write.** No real EMR, claims system or remote
  desktop is in 1.0.0.
- **The fault name is visible to an executor that reads page source or
  network traffic.** The protocol forbids it; nothing enforces it.
- **The verdict shares a specification with the read-back.** Both encode
  the same intended record (patient, type, note). A fault nobody wrote down
  is invisible to both.

## Related work in this repository

- [`../effect_e2e`](../effect_e2e/EFFECT_E2E.md) measures the same faults
  for API-level writes through OpenAdapt's replayer, with no GUI agent.
- [`../effectbench`](../effectbench/README.md) is a standalone package with
  the same metric (SWER) over a simulated environment. silent-saves drives a
  real browser app instead, so a computer-use agent can be scored as is.
- [`../scoreboard`](../scoreboard/SCOREBOARD.md) is where the results so far
  come from.

## Versioning

The fault set and protocol versions are in `fault_set.json` and in every
run's `meta.json`. A change to the fault kinds, the task, the item plan or
the scoring rules gets a new version; results from different versions are
not compared.

## Tests

```bash
cd packages/flow && python -m pytest benchmark/silent_saves/tests -q
```
