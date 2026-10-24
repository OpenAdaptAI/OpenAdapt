# Why each measured stop happened, and why repair did not resume it

Every stop in this directory's results, classified by root cause. The
evidence for each row is in `results/causes/*.csv`, written by
`stop_causes.py` from the runs' own `report.json` and, when self-repair ran,
`self-repair/self-repair.json`:

- `stop` is what the halted step's error says (a rule over the message);
- `blocker` is what kept repair from resuming the item;
- `unsettled` lists the steps whose effect the transaction classifier could
  not settle, when the run ended `RECONCILIATION_REQUIRED`.

The root cause sometimes needs what the benchmark knows about the change it
made (which drift mode, which injected fault). That column is filled in
here, by hand, from the benchmark's labels and the linked issues.

A **false stop** is a stop on a run where nothing was wrong: the screen was
correct and the record would have been, or was, saved correctly.

## Summary

71 stops: 30 MockMed trials, 12 MockMed worklist runs, 8 OpenEMR runs, 8
openIMIS runs and 13 silent-saves items.

| Root cause | Stops | False stops | Resumed before | Fix |
| --- | --- | --- | --- | --- |
| A control was renamed or moved | 24 | 0 | 19 | none for the 19; the other 5 (openIMIS) never reached repair: write classification, below |
| An interstitial screen, dismissed by a "Continue" button | 13 | 0 | 0 | dismiss classified from evidence, not the label; the agent sees the screen after each click |
| A save moved behind a closed menu | 3 | 0 | 0 | the agent sees the screen after each click |
| A pixel or mined-text postcondition failed on a correct screen | 9 | 9 | 0 | semantic postconditions |
| An identity check read a label behind a modal (#1347) | 3 | 3 | 0 | the identity check itself (#1347), not repair |
| A repaired target was cropped in a transient state | 3 | 0 | 0 | a template that holds across the control's enabled and disabled states |
| A correct stop: a data fault or a new business choice | 16 | 0 | n/a | none: these go to a person |

On top of the root cause, 13 stops (all 8 OpenEMR and 5 openIMIS) could not
reach repair at all because an earlier step had acted without proof that it
wrote nothing (**write classification**):

| App | Steps the classifier could not settle | Why |
| --- | --- | --- |
| OpenEMR referral | `step_002` ... `step_037`: 8 × Control+a, 4 × Enter | Unreviewed special keys. #1366 classifies 11 of 12 on a recording made with network capture; `step_007` keeps a background `POST` in its window. |
| OpenEMR referral | `step_039`, "Create New Patient" (self-repair's own check) | The label reads as a write, but the click opens OpenEMR's duplicate check; "Confirm Create New Patient" (`step_040`) is the save. The workflow therefore counts two saves. |
| openIMIS claim | `step_002` (Tab), `step_012` (a picker) | Unreviewed. A person's `qualify set-risk` settles them (#1337); with that review all 3 stops reached repair. |

## MockMed, one item per trial (`results/causes/mockmed-2026-10-19-a.csv`)

| Mode | Stops | `stop` | `blocker` | Root cause | False stop |
| --- | --- | --- | --- | --- | --- |
| relink | 3 | `target:moved` | `none:resumed` | renamed and moved | no |
| redesign | 3 | `target:geometry_only` | `none:resumed` | renamed and moved | no |
| menu | 3 | `target:geometry_only` | `patch:proposed_control_not_a_save` | save behind a closed menu: the agent sees only "Actions" and proposes it as the save | no |
| notice | 3 | `target:not_found` | `patch:proposed_control_write_shaped` | interstitial: the agent proposes "Continue to tasks", which is refused because its label reads as a write, and "Open" is not on the screen yet | no |
| font | 3 | `postcondition:region_only` | `agent:gave_up` | `region_stable` after the font changed; the click had acted correctly | **yes** |
| zoom | 3 | `postcondition:region_only` | `agent:gave_up` | `region_stable` at 125%; fixed on main by the page-scale check (#1360) | **yes** |
| provider | 3 | `postcondition:text_and_region` | `agent:gave_up` | a new required "Ordering provider" choice the recording never made | no (correct stop) |
| ambiguous | 3 | `target:ambiguous` | `person:not_a_missing_target` | two copies of the referral | no (correct stop) |
| missing | 3 | `identity:mismatch` | `person:rejected_policy` | the referral is gone | no (correct stop) |
| empty | 3 | `identity:unreadable` | `person:rejected_policy` | no referrals | no (correct stop) |

## MockMed worklist (`results/causes/mockmed-worklist-2026-10-19-a.csv`)

12 rows: 10 items and the 2 test-record runs self-repair made, all
`relink` or `redesign`. The 2 repairs resumed; the other 10 rows are items
held in their group and released by its one approval (`blocker` `not_run`).
All 10 items ended correct.

## OpenEMR referral, #1347's runs (`results/causes/openemr-1347.csv`)

| Runs | Halted at | `stop` | Root cause | False stop |
| --- | --- | --- | --- | --- |
| `fast1` and `std1` item 26 | `step_039` | `postcondition:text` ("No matches were found." absent) | a real possible duplicate: both runs reached the same synthetic patient, whose name had been used before (#1349, fixed by #1420) | no (correct stop) |
| `fast2` 16, `par8b` 7, `std2` 8 | `step_040` | `identity:mismatch` | the identity check read "Emergency Contact:" behind the modal (#1347) | **yes** |
| `par8b` 2, `par8w1b` 9 and 20 | `step_040`, after the save | `postcondition:text_and_region` | the save's check used tab-strip text and a pixel region mined from the recording; #1402 replaced them with the chart heading | **yes** |

Every one also ended `RECONCILIATION_REQUIRED` for the write-classification
reason above, so none reached repair. The three stops after the save are not
repair targets anyway: input reached the save.

## openIMIS, the relabelled Save (#1331)

| Run | Stops | `stop` | `blocker` | Root cause |
| --- | --- | --- | --- | --- |
| before #1337 (`openimis-1331-before.csv`) | 3 | `target:scroll_budget` | `reconciliation:effect_not_settled` (`step_002`, `step_012`) | relabelled Save, and write classification |
| #1337, no review (`openimis-unreviewed-2026-10-19-b.csv`) | 2 | `target:not_found` | the same | relabelled Save, and write classification |
| #1337 and a review (`openimis-reviewed-2026-10-19-b.csv`) | 3 | `target:not_found` | 2 × `probe:stopped_before_save`, 1 × `agent:gave_up` | the agent's "SAVE CLAIM" was cropped while enabled; in the probe the scroll before it runs while the Save is still disabled, so the template never matches |

## Silent-saves 1.1.0 UI-change blocks (`results/causes/silent-saves-2026-10-19-a.csv`)

| Items | Stops | Root cause |
| --- | --- | --- |
| 46-55 (`notice`) | 10 | interstitial and "Continue", as in MockMed |
| 76 (`relink`) | 1 | renamed and moved; repaired once, items 77-85 ran the repaired program |
| 30 (`zoom`, `wrong_record` fault) and 80 (`relink`, `optimistic` fault) | 2 | correct stops: the server's own faults |

The zoom block's other 9 items did not stop (page-scale check).

## Reproduce

```bash
python benchmark/auto_repair/stop_causes.py \
    --runs-root ~/.cache/openadapt-auto-repair/mockmed-2026-10-19-a \
    --out results/causes/mockmed-2026-10-19-a.csv
```

The run directories are local caches (`~/.cache/openadapt-auto-repair/`,
the scoreboard's OpenEMR runs); the CSVs here are what they held on
2026-10-20.
