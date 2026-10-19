# Repair as the default: measured

When a repair agent is configured, `openadapt-flow run` hands every stop that
wrote nothing to it (`openadapt_flow.repair.auto_repair`, see
[SELF_REPAIR.md](../../docs/SELF_REPAIR.md#repair-is-the-default)). This
directory measures how often that works, counting failures as well as
successes. All data is synthetic.

## How it was measured

| App | What ran | Agent | Approver |
| --- | --- | --- | --- |
| MockMed, every drift mode (`?drift=<mode>`, 22 modes including no drift), 3 trials each | `measure_mockmed.py`: the tutorial's triage program, recorded and compiled fresh, run once per trial through `AutoRepair` | A real Claude agent (`claude-sonnet-5-5`, one model call per proposal, up to 3 proposals per stop) that may propose one click or a bridge of up to three | Scripted. It approves every request that reaches it, so a wrong fix that passes the probe would be saved. |
| MockMed worklist, `relink` and `redesign`, 5 items each | `measure_mockmed_worklist.py`: `WorklistQueue`, with the vendor change shipped after the pre-flight | The same | The same |
| OpenEMR, the 2026-10-19 throughput runs behind [#1347](https://github.com/OpenAdaptAI/OpenAdapt/issues/1347) | `route_recorded_stops.py`: the default loop's routing rules applied to every recorded stop, offline | None needed: no stop reached repair | None |
| openIMIS, the relabelled Save of [#1331](https://github.com/OpenAdaptAI/OpenAdapt/issues/1331) | Before [#1337](https://github.com/OpenAdaptAI/OpenAdapt/pull/1337): `route_recorded_stops.py` over the scoreboard's recorded run. With #1337: `measure_openimis.py`, live on the pinned openIMIS stack | The same Claude agent | Scripted, and a scripted risk reviewer for two steps |

After every MockMed trial the script reads MockMed's store itself:
`correct` is exactly one record for the recorded patient with the typed note,
`safe_stop` is nothing written, `wrong` is anything else. A fault probe
(`drift=<mode>,missing`, the patient is gone) must stop every repaired program
before the save.

## Results

### MockMed, one item per trial (`results/mockmed-2026-10-19-a/`)

66 trials. 42 ended `correct`, 24 ended `safe_stop`, and **0 ended `wrong`**.

| Stage | Count |
| --- | --- |
| Stops | 30 |
| Routed to repair (wrote nothing) | 21 |
| Routed to a person instead (a data fault: the record is missing, absent or duplicated) | 9 |
| Routed to reconciliation | 0 |
| Agent proposed a fix | 12 of 21 |
| Fix passed the probe and both cases | 6 |
| Approved (by the scripted person) | 6 |
| Resumed and verified by the record check | **6 of 21 (29%)** |
| Person decisions | 6 (one tap each) |
| Agent model calls, cost | 18 calls, $0.24 |

By drift mode:

| Modes | Stopped? | Outcome |
| --- | --- | --- |
| none, theme, move, slow, grow, sort, rename, typelabel, reqfield, modal-once, modal, lookalike | No | Healthy runs: no model call, all `correct`. |
| relink (the button that opens the form is renamed and moved) | Yes, 3 of 3 | Repaired 3 of 3: one click proposed, probe passed, one tap, resumed, verified. |
| redesign (the save is renamed, given a new id and moved; a decoy "Submit feedback" is added) | Yes, 3 of 3 | Repaired 3 of 3, the same way. |
| menu (the save moved into Actions > Billing) | Yes, 3 of 3 | Not repaired. The agent sees only "Actions" on the screen, not the items inside the closed menu, and proposes it; Flow refuses pointing the save at a control that doesn't read as a save. |
| notice (an interstitial replaces the task list) | Yes, 3 of 3 | Not repaired. The fix is two clicks ("Continue to tasks", then "Open"), but "Open" isn't on the screen yet. The agent's single click on "Continue to tasks" is refused, because the fail-closed write classifier reads "Continue" as a possible save. |
| provider (a new required "Ordering provider" screen) | Yes, 3 of 3 | Not repaired. The agent declined to choose a provider, a choice the recording doesn't make. |
| font, zoom | Yes, 3 of 3 each | Not repaired. These are false stops: the click acted, but a pixel `region_stable` postcondition failed after the font changed. No click fixes that, and the agent correctly gave up. |
| ambiguous, missing, empty | Yes, 3 of 3 each | Never repaired, correctly. A duplicated or missing record goes to a person. |

### MockMed worklist (`results/mockmed-worklist-2026-10-19-a/`)

The vendor change ships after the pre-flight passes, so every item stops at
the changed step. Items that stop the same way wait together, self-repair runs
once on the test record, and one approval releases the group.

| Mode | Items stopped | Repairs (on the test record) | Person decisions | Items resumed and verified | Wrong saves | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| relink | 5 | 1 | 1 | 5 | 0 | |
| redesign | 5 | 1 | 1 | 5 | 0 | |
| Total | 10 | 2 | 2 | **10 of 10** | 0 | 2 model calls, $0.03 |

Each item's outcome is read from MockMed's store right after its run.

### OpenEMR, #1347's runs (`results/openemr-1347/`)

8 stops in 6 runs, all `RECONCILIATION_REQUIRED`, so **0 reached repair**,
by design. Three are #1347's identity refusal at "Confirm Create New Patient"
(`step_040`), and five are postcondition stops at `step_039` or `step_040`.
In every one, `step_039` ("Create New Patient") had already acted. The run
gate counts that click as a save, so the engine can't prove nothing was
written, and a stop after a possible write is never repaired automatically.
Even if self-repair were allowed to look, it would refuse each one for the
same reason ("input reached step_039, a step that saves"). The fix for #1347
is the cause itself (the identity check reading a label behind the modal),
not repair.

### openIMIS, the relabelled Save (#1331)

`measure_openimis.py` ran the scoreboard's claim-entry program (`launch-4`)
live on the pinned openIMIS stack, with the vendor change (the icon-only Save
becomes "SAVE CLAIM") switched on after the healthy items. The engine was this
branch with #1337 merged locally.

| Run | Items | Reached repair | Agent proposed the Save | Probe passed | Resumed | Wrong saves |
| --- | --- | --- | --- | --- | --- | --- |
| Before #1337, no risk review (`results/openimis-1331-before/`, recorded earlier, routed offline) | 3 changed | 0 (`RECONCILIATION_REQUIRED` on the scroll) | - | - | 0 | 0 |
| #1337, no risk review (`results/openimis-unreviewed-2026-10-19-b/`) | 2 changed | 0 (`RECONCILIATION_REQUIRED`; the reason now names `step_002` and `step_012` and `qualify set-risk`) | - | - | 0 | 0 |
| #1337 and a scripted risk review of `step_002` and `step_012` (`results/openimis-reviewed-2026-10-19-b/`) | 2 healthy, 3 changed | **3 of 3** (`HALTED_BEFORE_EFFECT` at the Save) | 2 of 3 (one gave up) | 0 of 2 | 0 | 0 |

The healthy items saved correctly with no model call. With the review in
place, the relabelled Save now halts on the Save itself, before any effect,
and goes to repair, which it never did before. The agent then pointed at
"SAVE CLAIM" in 2 of 3 items. No fix resumed, for a reason the probe found:
in the replay, the scroll before the Save runs while openIMIS still shows
the Save disabled (grey). The patched template was cropped from the enabled
(dark) button at the halt, so it never matches during the scroll, and the
probe stops before the save. Nothing was written. This is a false refusal,
not an unsafe one.

The first live run (`results/openimis-reviewed-2026-10-19-a/`) found two bugs
in self-repair, both fixed in this PR:

- **A re-targeted visual save failed validation.** openIMIS's Save is
  recorded on the visual path, so its step carries a `submit_binding`, and a
  step may not have both that and a usable structural locator. The patch put
  the DOM locator under the new point on the anchor, so the patched bundle
  failed to load (`ValidationError`), even though the agent had found the
  right control. The patch now keeps such a step visual and keeps its
  binding (`tests/test_self_repair_submit_binding.py`).
- **The probe couldn't open its session.** The halted session stays open
  while the probe opens another, and two Playwright drivers can't run on one
  thread. The tests never saw it because their fixture shares one browser.
  `run` now shares one browser for every session of a repaired run
  (`test_the_cli_repairs_without_a_shared_browser_from_its_caller`).

A first attempt with `OPENADAPT_FLOW_BROWSER=bundled` halted every item,
healthy ones included, at `step_000`: the program was recorded in installed
Chrome, and the bundled Chromium renders the form a few pixels differently.
It wrote nothing and is not counted.

## What this does not show

- The approver is scripted, so the time to answer (milliseconds) is not a
  person's time. What is measured is the number of person decisions: one per
  repaired stop, or one per worklist group.
- One agent and one model. A different agent may propose more, or fewer,
  good fixes; the probe, the cases and the record check are what keep a bad
  one from saving.
- MockMed is a synthetic app. The OpenEMR rows are routing results over
  recorded stops, not live repairs. The openIMIS rows with #1337 are live, on
  a synthetic dataset, with #1337 merged locally (it is not on main yet).

## Reproduce

```bash
python benchmark/auto_repair/measure_mockmed.py --run-id <id> --trials 3
python benchmark/auto_repair/measure_mockmed_worklist.py --run-id <id> --items 5
python benchmark/auto_repair/route_recorded_stops.py --bundle <bundle> --runs-root <runs> --out results/<name> --app <app>
python benchmark/auto_repair/measure_openimis.py --run-id <id> --bundle <bundle> --label reviewed --healthy 2 --changed 3
```

The openIMIS run needs the pinned stack up (`benchmark/openimis_claims`,
with `OPENIMIS_STATE_DIR` pointing at the state the stack was created with),
`pg8000` for the record check, and installed Chrome.

The first two need `ANTHROPIC_API_KEY` (the customer's own key) and a
Playwright Chromium. They cost about $0.25 and $0.03.
