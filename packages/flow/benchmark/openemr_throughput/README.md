# OpenEMR referral worklist runner

Runs the OpenEMR referral program over a worklist on a local OpenEMR 8.0.0.3
test copy, with 1 to 8 workers, either speed profile, and optional pipelined
read-back, and judges every item from the database afterwards. Every patient,
address, phone number (555-01xx) and email (`example.invalid`) is made up.
OpenAdapt isn't affiliated with the OpenEMR project.

Speed figures are published only through the
scoreboard (`benchmark/scoreboard/`, added in [#1303](https://github.com/OpenAdaptAI/OpenAdapt/pull/1303)) (arm 4, `program_fast`, and arm 6, `parallel`).
This folder holds the runner and the profiler, not results.

| File | What it is |
| --- | --- |
| `harness.py` | Runs one worklist on N workers against the fixture and judges every item afterwards |
| `profiler.py` | Exclusive-time profiler for one replay, deeper than the per-step `timings` in `report.json` |

## What one run does

1. **Setup, not timed.** Reset the database to the fixture's verified
   baseline, add 8 clerk accounts cloned from the fixture's actor user, seed
   the existing chart Rosa Delgado, and generate M distinct synthetic
   referrals. First names have 4 or more letters (see LIMITS.md, "Short
   parameter values").
2. **The worklist.** `run_worklist_pool` over the M referrals on N workers.
   Each worker opens its own browser context and signs in once as its own
   clerk. For each item it claims, it closes any open patient chart (OpenEMR
   keeps the last patient open after a save, which moves the form), opens a
   blank New Patient form, and replays the program with the engine's own
   read-back of the saved chart through the OpenEMR REST API. `--processes`
   runs each worker in its own process.
3. **Judge, not timed.** The test setup reads every item back from the
   database with SQL. An item is correct when exactly one new chart exists
   for it with 12 of 12 fields matching; an unrun item must have no chart.

`--halt-at I` replaces item I with a possible duplicate of the seeded chart;
the engine must halt on it and the pool must stop every worker.
`--speed fast` applies the `fast` profile (`openadapt_flow.runtime.speed`).
`--profile` (one worker) writes an exclusive-time profile per item.

Each run writes `summary.json` (counts, wall time, sign-in time per worker,
host load, engine commit, speed settings) and `items.json` (worker, state,
judge's verdict and seconds per item) into `--out/<tag>`, for the
scoreboard's importer.

## Run

Needs podman, the fixture's baseline state, and the compiled referral bundle
(not in this repository). Timed runs wait until the host's 1-minute load
average is under 12.

```bash
THROUGHPUT_UI_PORT=9911 THROUGHPUT_API_PORT=9910 \
python -m benchmark.openemr_throughput.harness run --bundle BUNDLE \
  --fixture-state STATE --out OUT --results OUT/runs.jsonl \
  --tag n4 --workers 4 --items 32 --speed fast --processes --gate-load 12
```
