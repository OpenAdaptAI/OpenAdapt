# Verified undo coverage (`undo propose`)

How many saving steps get an undo that a test copy shows to work, and why the
rest don't. The method is in
[`docs/AFTER_A_STOP.md`](../../docs/AFTER_A_STOP.md#proposed-undo-steps).

## Method

Live rows (`run.py`, one fixture at a time):

1. Start the synthetic app and its record store in process, on 127.0.0.1.
2. Record the app's canonical demonstration in a real Chromium with network
   capture on (the demo driver's clicks).
3. Compile it. Bind the save's record check as an operator would: the fields
   the save's request carries, counted as new records only.
4. Write a deployment file that declares the app a test copy: a reset URL and
   a `rest` reward contract over the whole record store (`/api/db`).
5. Run `openadapt-flow undo propose` with that file and the recording.

Offline rows read an existing bundle and recording and run nothing
(`--offline`). They answer whether any candidate can be built. They were used
for the OpenEMR and openIMIS fixtures, which are shared with the scoreboard
and were not started for this.

Synthetic data only. No model calls.

## Results

| App | Run | Saving step | Verified undo | Why not, and what would close the gap |
| --- | --- | --- | --- | --- |
| MockMed | live | Save Encounter | MOCKMED_RESULT | MOCKMED_WHY |
| MockLoan | live | Authorize Disbursement | Yes: `DELETE /api/disbursement/{undo_record_id}` (captured traffic, REST convention) | |
| MockLoan, fee-row mode | live | Authorize Disbursement | No | The save also writes a fee row on a second surface, and the `DELETE` leaves it, so the records after the undo don't match. Closes with a recording of the app's own reversal path. |
| OpenEMR fixture, new patient | offline | `step_039` (a click Flow counts as a possible write) | No | No record check is bound to the step, so there is nothing to invert. Closes with a bound record check. |
| OpenEMR fixture, new patient | offline | `step_040` (the save) | No | The save is a browser form post, so no API undo is built. The only control after the save is Cancel. No recording deletes a chart. Closes with a recording of the app's own delete path. |
| openIMIS fixture, claim | offline | `step_015` (the save) | No | The recording has no network capture and no recording deletes a claim. The live fixture wasn't started. Closes with a recording of the app's own delete or cancel path, or a new recording with `--capture-network`. |

TOTALS_LINE

The full reports are in [`results/`](results/): one JSON file per fixture
(each candidate's source, status, basis and reason, and for a live row the
digests and counts of the three snapshots) and `summary.json`.

## Reproduce

```bash
cd packages/flow
PYTHONPATH=../types:. python benchmark/undo_proposal/run.py --out benchmark/undo_proposal/results --fixture mockmed
```

Use `--fixture mockloan` and `--fixture mockloan-fee-row` for the other live
rows. For an offline row:

```bash
PYTHONPATH=../types:. python benchmark/undo_proposal/run.py --out benchmark/undo_proposal/results --offline NAME BUNDLE RECORDING "what the save does"
```
