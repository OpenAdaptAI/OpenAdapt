# Code as action against the screenshot loop

Measured 2026-10-18 with `claude-sonnet-5-5` at effort `medium`, on the
operator's own key, in the compile-agent harness. Every arm runs through the
same harness, save gate, JIT capture and compiler; only what the agent sees
and how it acts differ:

- **code**: `ClaudeAgent(vision="code")`, one `run` tool that takes a script
  ([docs/CODE_AS_ACTION.md](../../docs/CODE_AS_ACTION.md)).
- **every_turn**: Claude's computer toolset (`computer_toolset_20260801`), a
  screenshot after every action. The screenshot loop.
- **selective** (MockMed only): the structural listing after every action, a
  screenshot on request (compile-agent's default).

Correct is decided by reading the system of record back, never by the agent's
own report. Synthetic data only.

**Load.** The host (12 cores) was shared with other sessions; its 1-minute
load was between 115 and 420 at the start and end of every run, so every row is marked
`under_load: true` and every time below was taken under load. Tokens, model
calls, actions and cost do not depend on load.

## MockMed (5 trials per arm, `results/mockmed/`)

Task: "Open the referral for Jane Sample, start a Triage encounter, and save
this note: {note}", with a distinct note per trial, arms in rotating order,
MockMed reset before each run. Correct = the record check passed (exactly one
new encounter, on patient p1, type Triage, with the note).

| median per run | code | selective | every_turn |
| --- | ---: | ---: | ---: |
| Correct (read back) | 5 / 5 | 5 / 5 | 5 / 5 |
| Model calls | 6 | 5 | 5 |
| Actions | 6 | 6 | 13 |
| Prompt tokens | 12,738 | 10,332 | 73,303 |
| Output tokens | 350 | 359 | 533 |
| Cost | $0.012 | $0.014 | $0.075 |
| Model time | 5.9 s | 5.8 s | 9.7 s |
| Run time (under load) | 21.5 s | 24.0 s | 26.0 s |
| Screenshots sent | 0 | 1 | 14 |

On a five-step task, code as action and the structural listing are about the
same; both read about a sixth of the screenshot loop's tokens and cost about
a sixth as much. The program compiled from a code-arm run was admitted and
replayed twice: VERIFIED both times, 0 model calls (`replays.jsonl`).

## OpenEMR referral (8 referrals per arm, `results/openemr/`)

The referral task from [openemr_referral_timing](../openemr_referral_timing/):
register a referred, made-up patient from 12 structured fields in OpenEMR's
New Patient form (it sits in an iframe; a duplicate-check dialog opens in a
second one), and do not create a duplicate. r6 matches an existing chart's
name with a different birth date: correct means no new chart. Sign-in and the
blank form are set up before the clock starts. One block per arm from a reset
fixture (pinned image, `realrun` state), referrals r1 to r8 in order. Correct
= SQL read-back by the test setup: exactly one new chart with 12 of 12 fields
matching (r6: no new chart).

| median per run | code | every_turn |
| --- | ---: | ---: |
| Correct (read back) | 8 / 8 | 6 / 8 |
| Model calls | 12 | 14 |
| Actions | 36 | 56 |
| Prompt tokens | 90,777 | 619,498 |
| Output tokens | 1,024 | 2,028 |
| Cost | $0.055 | $0.332 |
| Model time | 18.5 s | 48.6 s |
| Run time (under load) | 45.4 s | 75.2 s |
| Screenshots sent | 2 | 56 |

- The code agent read 6.8 times fewer prompt tokens, cost 6 times less, and
  took 36 actions to the screenshot loop's 56. It asked for a screenshot in
  every run (1 to 4 each), mostly to find a section below the fold. Both
  stopped correctly on r6.
- The two screenshot-loop misses were stopped by the save gate before any
  save: Enter on the "Contact" button (a `<button>` in a form submits it) and
  a Ctrl+End shortcut. The gate refuses an action it can't rule out as a save
  when the record identity isn't around it. Neither arm wrote a wrong or
  duplicate chart.
- In the earlier referral timing study (its own harness, no save gate), a
  `claude-sonnet-5-5` screenshot agent saved 62 of 63 at a median $0.083 and
  49 actions. The screenshot arm here cost more per run; it is a different
  harness and toolset (`computer_toolset_20260801`, a screenshot after every
  action), so the two are not a matched comparison.
- The gate's identity for this task is "Create New Patient" in the scope of
  the save (the confirm dialog): a new patient's identity is typed, not
  shown, so this checks that the save happens in the new-patient dialog. It
  is weaker than an MRN check and is the same for both arms.

**Trace and program.** For each code run the recording was compiled and
checked with `check_trace_against_ir`. In 4 of 8 runs the first version of
the check reported 2 steps missing; in each, they were a pair of clicks that
opened and closed the form's Contact section, which distill drops as a dead
end. The check now reads `distill.json` and reports such steps as distilled.
Two more code runs with the fixed check (`results/openemr-distill-check/`,
r4 and r7, from a reset fixture): both correct with 12 of 12 fields, both
pass, and r4 shows one step distilled as a dead end. That r4 run also shows
the spread: 26 model calls, 8 failed lines, 8 screenshots and $0.148, against
11 calls and $0.048 for r4 in the main block.

**Set aside.** `results/openemr/superseded.jsonl` holds three runs from
blocks stopped to fix save-gate bugs that refused the screenshot agent (see
`SUPERSEDED.md`). They are not in the tables.

**Redacted.** During these runs the page URL the tool printed still had its
query string, so OpenEMR's per-session `token_main` (a local test fixture,
since stopped) reached the model and the rows. The committed rows replace it
with `REDACTED`, and the tool now prints URLs without query strings.

## Spend

`results/spend.json` lists every paid run, including four debugging runs
whose rows were not kept. Total: see the file; the cap was $15.

## Reproduce

```bash
PYTHONPATH=. python benchmark/code_as_action/measure_mockmed.py --env-file .env --trials 5
# OpenEMR: a running local fixture (benchmark/openemr_local) and its state dir.
DOCKER_HOST=... OPENEMR_FIXTURE_STATE=<state dir> OPENEMR_FIXTURE_LOCK=<lock> PYTHONPATH=. \
  python benchmark/code_as_action/measure_openemr.py --env-file .env --reset --arms code
python benchmark/code_as_action/summarize.py
```
