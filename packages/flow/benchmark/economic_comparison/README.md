# Economic comparison: OpenAdapt against competent alternatives

This directory compares OpenAdapt with three alternatives a competent team
could build today. It counts every person's minute and the total cost over
10, 100 and 1,000 items, not only the per-run speed. Every number in the
tables comes from the raw rows listed under [Raw results](#raw-results).
People's time is priced from stated assumptions
([`assumptions.json`](assumptions.json)), never measured.

Measured on 2026-10-24. Synthetic data only.

## Short answer

On these two tasks OpenAdapt does not win the economics.

- **OpenEMR referral intake (55 items, one app change).** The agent with
  structured tools and REST read-back beat OpenAdapt's production
  configuration on most rows:
  - automated items: 49 vs 41;
  - false stops: 5 vs 9 of 35;
  - median time: 24 s vs 119 s an item;
  - operator time: 0.91 vs 2.15 minutes an item;
  - projected total for 1,000 items: $606 vs $1,158.

  OpenAdapt was better only on model cost ($0 vs $0.05 an item). Neither arm
  made a silent wrong save (0 of 55 each).
- **MockMed fault suite (100 items, 10 UI changes, 5 server faults).** Both
  competent alternatives verified 90 of 90 items with 0 silent wrong saves.
  The agent-written script cost $0.26 in model calls for all 100 items,
  including four repairs of its own. OpenAdapt's program verified 71 of 90
  with 0 silent wrong saves and 19 false stops. The 1,000-item totals are
  within about 30% of each other ($104 to $135). OpenAdapt is lowest in
  dollars and highest in people minutes.
- **The silent-wrong advantage holds only against an agent that trusts the
  screen.** The screenshot-only computer-use agent reported all 5 server-side
  bad saves as saved. Every arm that reads the record back reported none of
  them as saved, whether it was OpenAdapt or not.

## The arms

Every arm gets the same task text, the same app and the same read access to
the system of record that OpenAdapt's record check uses: REST on OpenEMR,
`GET /api/db` on MockMed. No arm gets the fault, and no arm judges itself. On
OpenEMR, the oracle is a direct SQL read; on MockMed, it is the store's rows
before and after each item.

| # | Arm | What it is | Model |
|---|---|---|---|
| 1 | Agent with structured tools | Reads the page's controls (DOM and accessibility view, not screenshots), acts on them by reference, and reads the record before and after the save. It is told to claim success only when exactly one correct record landed. On OpenEMR: [`lifecycle/arms/agent_tools.py`](../lifecycle/arms/agent_tools.py). On MockMed: `AgentTools` in [`silent_saves/agent_arms.py`](../silent_saves/agent_arms.py). | Claude Sonnet 5.5 |
| 2 | Agent-written script | A coding agent writes a Playwright script with the same checks: a duplicate guard before the save and a read-back after it. It dry-runs the script with the save blocked at the network layer, then submits it. Items then run with no model call. When the script breaks, the agent fixes it against the live page and the item runs again. On OpenEMR: [`lifecycle/arms/agent_script.py`](../lifecycle/arms/agent_script.py). On MockMed: `AgentScript` in [`silent_saves/agent_arms.py`](../silent_saves/agent_arms.py). | Claude Sonnet 5.5 |
| 3 | OpenAdapt, as a customer would run it | On OpenEMR: the Regulated profile, `problems_only` approval, durable runs, pipelined read-back, self-repair for a relabeled save button, and the governed run gate on the sealed bundle. This is the production-lifecycle session's arm, `lifecycle/production_arm.py`, run on its branch's engine. The API step is refused on this form (see below), so the GUI save runs. On MockMed: the admitted program with read-back and no model calls. The MockMed program does not run the Regulated profile. | none |
| 4 | Computer-use agent, screenshots only | The scoreboard's reference agent. Measured on MockMed only (committed runs of 2026-10-19). | Claude Sonnet 5.5 |

A fifth MockMed row runs arm 4's own runs with each claim gated on
OpenAdapt's read-back (`verify-run`). That is the cheapest way to add the
read-back to an existing agent.

### The API step

The 3.33 s API-path figure was measured without approval gates or durable
runs. On current main the compiler refuses the API step for this form:
OpenEMR validates the date of birth with a script rule the API step can't
evaluate, and another write happens during the form span. So OpenAdapt's
gated OpenEMR numbers below are the GUI path's
([`../lifecycle/results/api_tier/`](../lifecycle/results/api_tier/)).

## OpenEMR referral intake

Seed 7, 55 referrals over 10 simulated days, workload
`lifecycle-workload/2026-10-24.1`. About a third of the referrals carry an
exception: duplicates, resends, shared surnames and dates of birth, a missing
date of birth, malformed fields, other clerks, a forced logout, slow and
failed saves. One app change: the save button "Confirm Create New Patient"
becomes "Save new patient" from item 26 on. Local OpenEMR 8.0.0.3. Host
1-minute load was about 5 to 20 during these runs.

| Arm | Items | Right in the end | Automated (no hand entry) | Silent wrong | False stops (of clean items) | Median s an item | Model $ an item | Operator min an item | After the app change | Model $ in total |
|---|---|---|---|---|---|---|---|---|---|---|
| 3. OpenAdapt, production configuration | 55 | 53 | 41 | 0 | 9 of 35 | 118.5 | 0 | 2.15 | 7 of 30 later items false-stopped; 7.5 min of maintenance; first verified item 2 items after the change | 0 |
| 3 on current main | 27 (stopped) | 27 | 4 | 0 | 19 of 20 | 226.9 | 0 | 4.56 | not reached | 0 |
| 1. Agent with tools | 55 | 55 | 49 | 0 | 5 of 35 | 24.3 | 0.050 | 0.91 | no stop in items 26-42; 6 stops in items 43-54 (a stuck confirm popup) | 2.77 |
| 2. Agent-written script | 16 (hung) | 16 | 7 | 0 | 7 of 12 | 5.3 | 0 | 2.44 | not reached | 0.86 |
| By hand (reference) | | | 0 | | | 210 | 0 | 3.50 | | 0 |

"Right in the end" counts items the oracle judges right, whoever did them.
Items an arm stopped on were entered by hand by the scripted operator, and
that time is in "Operator min an item". The silent-wrong 95% CI for 0 of 55
is 0 to 6.5%.

What happened to each arm:

- **OpenAdapt.** It stopped on every possible-duplicate listing. That is
  right for real duplicates, and a false stop for new patients who share a
  surname or a date of birth with an existing chart (9 false stops). On
  item 26, the first after the relabel, it stopped after the save had
  landed (`RECONCILIATION_REQUIRED`), so a person reconciled it. A person
  approved the proposed relabel repair once, and later items ran on the
  repaired program. `problems_only` still asked a person to approve the save
  click on 30 items (20 s each in the cost model).
- **OpenAdapt on current main** created no chart by itself in 27 items. Its
  duplicate check stopped on every listing.
- **Agent with tools.** It checked the record before saving and judged the
  duplicate listings itself. It stopped on all 10 items where stopping was
  right: existing charts, resends, another clerk's registration, an
  ambiguous match and missing dates of birth. It handled the relabel with
  no stop. From item 43 on it stopped 6
  times: the confirm popup did not close after a click, and the agent
  reported "not saved" rather than saving twice. None of those items left a
  chart, so they are false stops, not wrong saves.
- **Agent-written script.** Its first script stopped on every
  possible-duplicate listing (5 items). It also timed out on the title field
  on 4 items; the agent fixed it 4 times. After item 16 the run made no
  progress for about 2 hours and was stopped to free the shared fixture
  (ledger row `stopped: hung`). No stack was captured, so the cause is not
  known; every wait in the agent's script had a timeout, which points at a
  harness call with none (such as `page.evaluate` in OpenEMR's tab shell).
  Its numbers are 16 items before the app change, and are not comparable to
  the full runs. The hand-written script and the screenshot agent were not
  run on OpenEMR. `lifecycle.arms.compare` now bounds every item
  (`LIFECYCLE_ITEM_TIMEOUT_S`, default 900 s). On a timeout it writes every
  thread's stack to `stacks.txt`, kills the arm's browser driver, records
  the item as an error and gives the arm a fresh browser. If the arm still
  does not return, the process exits after 3 more minutes. The remaining
  OpenEMR arms are queued for the fixture as `baselines-rest`.

### Totals for OpenEMR

Setup + items + app changes (4 per 1,000 items) + cleanup of silent wrongs.
People time is in the brackets at x0.5 and x2 of the assumed rates.

| Arm | 10 items | 100 items | 1,000 items | People minutes for 1,000 |
|---|---|---|---|---|
| 3. OpenAdapt, production configuration | $72 [36-144] | $171 [86-340] | $1,158 [584-2,307] | 2,228 |
| 3 on current main | $84 | $290 | $2,351 | 4,608 |
| 1. Agent with tools | $105 [53-210] | $151 [78-296] | $606 [329-1,161] | 969 |
| 2. Agent-written script (16 items only) | $130 | $239 | $1,337 | 2,508 |
| By hand | $18 | $175 | $1,750 | 3,500 |

At 10 items, entering every referral by hand costs least. From 100 items on,
the agent with tools costs least; that holds at x0.5 and x2. OpenAdapt's
production configuration removes about 36% of the people time of doing
everything by hand, and the agent with tools about 72%.

## MockMed fault suite

[`silent_saves`](../silent_saves/README.md) fault set 1.1.0: 100 runs of
"open Jane Sample's referral, start a Triage encounter, save this note". Ten
blocks each switch one UI change on: renamed controls, display scaling, moved
buttons, a What's New screen, latency, a reordered queue, a moved New
Encounter control, a look-alike referral, a relabeled type and a missing
referral. Five items get a server-side bad save. The store is read before and
after every item.

| Arm | Verified (of 90) | Silent wrong (of 100) | False stops | Server faults reported saved (of 5) | Stopped when no save existed (of 5) | Model $ for 100 items | Seconds an item, control block |
|---|---|---|---|---|---|---|---|
| 1. Agent with tools | 90 | 0 | 0 | 0 | 5 | 1.45 | 13.7 |
| 2. Agent-written script | 90 | 0 | 0 | 0 | 5 | 0.26 | 0.8 |
| 3. OpenAdapt program (current main) | 71 | 0 | 19 | 0 | 5 | 0 | 7.3 |
| 3r. OpenAdapt program with self-repair (engine of 2026-10-19) | 62 | 0 | 28 | 0 | 5 | 0.48 | 7.4 |
| 4. Computer-use agent, screenshots only | 90 | 5 | 0 | 5 | 5 | 3.90 | 22.5 |
| 4 with OpenAdapt read-back | 90 | 0 | 0 | 0 | 5 | 3.90 | 22.5 + 1.2 |

The silent-wrong 95% CI for 0 of 100 is 0 to 3.7%. Seconds come from a
separate 15-item control-block pass at normal load
(`<arm>/2026-10-24-timing/`). The full runs ran at host loads of 77 to 374
and their times are not used. Rows 3r and 4 are committed runs of
2026-10-19.

Where each arm spent people time, per UI change, in minutes over a steady
item (+fix: a person fixed the program, script or prompt because the arm had
not recovered by the end of the block):

| Change | Agent with tools | Agent script | OpenAdapt program | Screenshot agent |
|---|---|---|---|---|
| What's New screen | 0 | 10 (one fix reviewed) | 52 +fix (re-teach) | 0 |
| New Encounter moved and renamed | 0 | 10 (one fix reviewed) | 49 +fix (re-teach) | 0 |
| Look-alike referral above the target | 0 | 10 (one fix reviewed) | 0 | 0 |
| Target referral gone (right answer: stop) | 10 | 40 (3 fix attempts reviewed; it kept stopping) | 10 | 10 |
| The other six changes | 0 | 0 | 0 | 0 |

The OpenAdapt program stopped on every item under two changes: the What's
New screen and the moved New Encounter control. Its self-repair did not
propose an admissible fix for either.

### Totals for MockMed

| Arm | 10 items | 100 items | 1,000 items | People minutes for 1,000 |
|---|---|---|---|---|
| 1. Agent with tools | $100 | $104 | $135 | 102 |
| 2. Agent-written script | $51 | $56 | $111 | 96 |
| 3. OpenAdapt program | $61 | $65 | $104 | 134 |
| 3r. with self-repair (2026-10-19) | $61 | $66 | $110 | 148 |
| 4. Screenshot agent | $51 | $56 | $110 | 69, plus 10 silent wrongs |
| 4 with OpenAdapt read-back | $51 | $56 | $111 | 72 |

On MockMed the totals are dominated by setup (an engineer's or operator's
hour) and by assumed server faults (10 per 1,000 items). Setup and faults
cost about the same in every arm. The screenshot agent's 10 silent wrongs in
1,000 items are charged only the lower-bound cleanup: a reconcile plus a
re-entry each. Their harm is not priced.

## Where OpenAdapt loses and where it wins

OpenAdapt loses on:

- Verified and automated items, on both apps: 41 vs 49 of 55 on OpenEMR,
  and 71 vs 90 of 90 on MockMed.
- False stops: 9 vs 5 on OpenEMR, and 19 vs 0 on MockMed.
- Seconds an item on OpenEMR: 119 vs 24.
- Operator minutes and total cost from 100 items on, on OpenEMR.
- People time per UI change on MockMed, under the two changes it could not
  repair.

OpenAdapt wins on:

- Model cost an item: $0 vs $0.013 to $0.05 for the agent arms. At 1,000
  items, model cost is $13 to $50 and so a small share of every total.
- Silent wrong saves compared with an agent that trusts the screen: 0 vs 5
  of 5 server faults. This is not a win over agents that read the record
  back: they also had 0.
- Stopping when no correct save exists: 5 of 5, the same as every
  read-back arm.

## Assumptions

All in [`assumptions.json`](assumptions.json):

- **Labor:** operator $30 an hour and engineer $100 an hour.
- **Setup people time:**
  - OpenAdapt: 22 operator minutes (teach, review, admit), plus 30 engineer
    minutes to install it and write the record check.
  - Agent with tools: 60 engineer minutes.
  - Agent script: 30 engineer minutes.
  - Screenshot agent: 30 engineer minutes.
- **Per-touch operator time (cost model V1):**
  - stop: 90 s;
  - reconcile: 120 s;
  - repair review: 120 s;
  - manual entry: 180 s on OpenEMR and 60 s on MockMed;
  - interrupt: 30 s per request.
- **Maintenance people time:**
  - an engineer reviews each agent-made script fix: 10 min;
  - an engineer fixes a script by hand: 30 min;
  - an operator re-teaches OpenAdapt after a change it could not handle:
    22 min;
  - an engineer adjusts an agent's prompt: 15 min.
- **Volume:** 4 UI changes and 10 server-side bad saves per 1,000 items.

No operator timing here is from a real person. Change any number in
`assumptions.json` and run `python -m economic_comparison.totals`.

## Limits

- One run per arm, one task per app, a scripted operator and synthetic
  data. Small samples: read the confidence intervals.
- The OpenEMR runs for arm 3 and arms 1 and 2 used the same seeded stream on
  two copies of the same pinned fixture, a few hours apart.
- Arm 3 on OpenEMR runs the engine of the `measure/production-lifecycle`
  branch (duplicate-list check, relabel repair). Current main is the
  "3 on main" row.
- The agent-written script on OpenEMR hung after 16 items. The hand-written
  script and the screenshot agent were not run on OpenEMR. A stronger
  authoring model or more engineer time might give a better script. That
  was not measured.
- The MockMed OpenAdapt program is not the Regulated configuration. It has
  no Regulated MockMed admission.
- The MockMed program's first pass broke on items 16-18 and 83-100. Item 83
  hit the 300 s watchdog under host load 374 and left the browser driver
  unusable for the items after it; items 16-18 hit a busy durable authority.
  Those 21 items were rerun in a fresh process. The first pass is kept as
  `items.first-pass.csv`.
- Model spend for this comparison: $6.05 of the $30 cap, ledger
  [`../lifecycle/results/spend.json`](../lifecycle/results/spend.json).

## Raw results

| What | Where |
|---|---|
| OpenEMR, arms 1 and 2 | [`../lifecycle/results/economics-2026-10-24/`](../lifecycle/results/economics-2026-10-24/) (`<arm>/ledger.jsonl`, `workload.jsonl`, `plan.json`) |
| OpenEMR, arm 3 | [`../lifecycle/results/production-2026-10-24/`](../lifecycle/results/production-2026-10-24/) (`after/`, `before/`) |
| MockMed, new runs | `../silent_saves/results/1.1.0/{agent-tools,agent-script,openadapt-program}/2026-10-24*/` (the agent's scripts are in `agent-script/2026-10-24/scripts/`, and the agent's tool traces in `agent-tools/2026-10-24/traces/`) |
| MockMed, earlier runs | `../silent_saves/results/1.1.0/{claude-computer-use+readback,self-repair}/2026-10-19/` |
| Totals | [`results/totals.json`](results/totals.json), [`results/TABLE.md`](results/TABLE.md) |

## Reproduce

```bash
cd packages/flow/benchmark
python -m economic_comparison.totals    # recompute every table from the raw rows

# MockMed (no fixture)
python -m silent_saves --fault-set 1.1.0 --agent python:silent_saves.agent_arms:AgentTools --out <dir>
python -m silent_saves --fault-set 1.1.0 --agent python:silent_saves.agent_arms:AgentScript --out <dir>

# OpenEMR (needs the fixture lease; see ~/oa/scratch/fixture-lease/README.md)
python -m lifecycle.apps up
python -m lifecycle.arms.compare --out <dir> --seed 7 --items 55 --days 10 \
    --drift relabel_save@26 --arms-json economic_comparison/openemr_arms.json
```
