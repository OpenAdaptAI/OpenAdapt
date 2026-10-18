# OpenEMR referral throughput: parallel worklists

How many synthetic referrals OpenAdapt enters per hour on a local OpenEMR
8.0.0.3 test copy, one worker at a time and with 4 and 8 workers, and where the
time in one entry goes. Every patient, address, phone number (555-01xx) and
email (`example.invalid`) is made up. OpenAdapt isn't affiliated with the
OpenEMR project.

| File | What it is |
| --- | --- |
| `harness.py` | Runs one worklist on N workers against the fixture and judges every item afterwards |
| `profiler.py` | Exclusive-time profiler for one replay (each wrapped call's own time, no double counting) |
| `compute_figures.py` | `collect` copies raw run data into `results/`; `figures` derives `results/figures.json`; `--check` fails when it is stale |
| `results/runs.jsonl` | One line per run: arm, workers, wall time, counts, load, engine commit |
| `results/items.csv` | One row per item: worker, state, judge's verdict, seconds |
| `results/profiles/*.json` | Per-item time buckets and per-step buckets |
| `results/figures.json` | The figures quoted in docs, derived from the files above |

## Task and fixture

The task is the referral program from
[`../openemr_referral_timing/`](../openemr_referral_timing/README.md): 41 steps
that fill and save OpenEMR's New Patient form, compiled with DOM element
identity. The bundle is not in this repository. The engine's own read-back
checks the saved chart through the OpenEMR REST API after every save.

The fixture is [`../openemr_local/`](../openemr_local/) (pinned images, verified
baseline) under its own compose project and ports. Before each run the harness
resets the database to the baseline, adds 8 clerk accounts cloned from the
fixture's actor user, seeds the existing chart Rosa Delgado, and generates M
distinct synthetic referrals.

## What one run measures

- **Wall time** starts before the first worker signs in and ends when the last
  worker finishes its last item, including that item's read-back. Each worker
  signs in once, as its own clerk, in its own browser context.
- **Per item**: close any open chart (OpenEMR keeps the last patient open after
  a save, which moves the form), open a blank New Patient form, replay the
  program, and read the save back. Nothing is skipped between items.
- **Judge**, after the clock stops: the test setup reads every item back from
  the database with SQL. An item is correct when exactly one new chart exists
  for it with 12 of 12 fields matching. Unrun items must have no chart.
- **Entries per hour** = correct items x 3600 / wall time, pooled over rounds.

The arms ran interleaved in rounds (1 worker on 16 items, then 4 workers on 32,
8 workers on 32, and 4 workers with read-back window 1 on 32), so that load
from other work on the shared machine fell on every arm. Each run waited up to
15 minutes for the 1-minute load average to drop below 24 and records the load
it ran under.

The serial figure published earlier (29.7 s median per referral) measured a
different thing: a fresh browser context and sign-in for every referral, plus
the judge's read-back inside the clock. A worklist signs in once per worker,
so its per-item time is lower even with one worker.

## Profiles

`results/profiles/prof-main.json` profiles current `main` with the published
method (fresh context and sign-in per run). `results/profiles/prof-new.json`
profiles this branch as a one-worker worklist. Each wrapped call's own time is
assigned to one bucket, so the buckets add up to the task time.

## Rebuild

```bash
python -m benchmark.openemr_throughput.compute_figures figures --check
```

To re-run (needs podman, the fixture's baseline state, and the compiled
bundle):

```bash
THROUGHPUT_UI_PORT=9911 THROUGHPUT_API_PORT=9910 \
python -m benchmark.openemr_throughput.harness run --bundle BUNDLE \
  --fixture-state STATE --out OUT --tag n4-r1 --workers 4 --items 32
```

## Results

RESULTS_SECTION

## Caveats

- One machine (12 cores, shared with other work) and a local test copy of
  OpenEMR in a 6-CPU podman VM. A production EMR adds network latency, a shared
  database and other users; its concurrency limit may be lower.
- One task and one bundle. Items were independent new patients.
- The clerks share the actor's synthetic password and permissions.
- The browser is Playwright's bundled Chromium. The same bundle halted at a
  scroll step in an installed Google Chrome; that was not investigated here.
