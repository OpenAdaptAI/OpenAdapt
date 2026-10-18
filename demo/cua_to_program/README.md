# CUA to program: openIMIS claim entry

A computer-use agent (Claude, through the API) enters one insurance claim in
openIMIS. OpenAdapt records that run through the JIT wrapper
(`openadapt_flow.jit.compile_agent`) and compiles it into a program. The
program then enters the next claims with no model calls, and each save is
read back from the openIMIS database.

**Status: demo profile, not admitted for production.** The standard run gate
refuses these openIMIS forms today (reasons below), so the program runs under
the demo profile with a system-of-record check attached. Every number here is
measured on one Mac and is in `results/`. All data is synthetic.

## Reproduce

```bash
ANTHROPIC_API_KEY=... demo/cua_to_program/run.sh
```

`run.sh` starts the pinned openIMIS reference stack, creates the synthetic
policyholders, records one Claude run, compiles it, runs the batch, and
renders the videos. It spends about $0.15 on the API. It needs Docker (the
openIMIS images are linux/amd64; on Apple Silicon run them under Rosetta, for
example `colima start --vm-type vz --vz-rosetta`), Python 3.12, uv, ffmpeg and
about 10 GB of free disk.

## What each step does

| Step | Command | What it does |
|---|---|---|
| Stack | `scripts/openimis_claims_demo.py up` | openIMIS 25.10 from digest-pinned images, loopback only. The AGPL compose file is fetched at run time and checked against pinned hashes. Nothing from it is stored here. |
| Bootstrap | `pipeline.py bootstrap` | 11 synthetic policyholders with active policies, and a read-only Postgres role that can only `SELECT` four tables. |
| 1. Agent | `pipeline.py record --agent claude` | `claude-opus-5-5` with the `computer_toolset_20260801` tool enters one claim. Every action goes through the JIT `CaptureComputer`. `agent_ledger.json` keeps each model call's time, tokens and list-price cost. |
| 2. Admission | `pipeline.py admit` | Confirms the parameters, records a review, and asks the standard gate for the attended canary. The gate refuses (see `admission.json`). Nothing runs. |
| 2. Compile | `pipeline.py compile` | Compiles the same recording with the standard compiler and the confirmed parameters (`insurance_no`, `claim_no`, `explanation`). The compiler mines the record check from the claim table's before and after snapshots. |
| 3. Batch | `pipeline.py batch` | Runs the program for each claim under the demo profile. Sign-in is unmeasured setup. A claim that stops with nothing written (the read-back finds no row) is retried, up to 3 attempts. Every attempt is a row in `batch.csv`. |
| Figures | `summarize.py` | Derives every reported figure from the raw files into `summary.json`. |
| Video | `render.py` | Three panels drawn from the screen recordings and the measured counters. Sped-up stretches are labelled. |

## The record check

After the save, the program reads the claims table as a read-only role
(`openimis_app.CLAIMS_SQL`) and requires all of the following:

- exactly one claim with this run's claim number, insurance number and
  explanation, in status Entered (2), at facility VIHOS001, with diagnosis A389;
- the claim number equals the run's `claim_no` parameter;
- the insurance number equals the run's `insurance_no` parameter;
- the explanation equals the run's `explanation` parameter.

The phrase used in the video is "read back from the database". No seal is
produced, so nothing here is called cryptographically verified.

## Why it is not admitted

The JIT path admits a program only after an attended canary on the standard
profile. For this program the standard gate refuses with `clinical-write`
certification violations: key presses and unlabelled clicks need a reviewed
risk classification from a qualification campaign (`results/*/admission.json`).
Along the way we also found and fixed or split out these engine gaps:

| Gap | Where it is |
|---|---|
| JIT `WebApp` had no sign-in hook; the capture took after-frames before slow lookups finished | Fixed here: `WebApp.setup`, `WebApp.capture_settle_timeout_s` (tests in `tests/test_jit_webapp_setup.py`) |
| A mined record check matched every earlier record with the same constant fields, so it refuted every claim after the first | Fixed here: confirmed parameters join the selector (`effect_mining.py`, tests) |
| The run gate ignored a submit binding as the identity of an icon-only Save | Follow-up PR #1274 (draft, needs safety review) |
| Guarded focusing clicks refused id-less fields on new-record forms | Follow-up PR #1275 (draft, needs safety review) |
| The service option list closes before the DOM identity probe, so the program halts at step 11 on part of the claims | Open; the halt is correct, and the fix is to wait for the list to stay open |
| Parameter hygiene misses a parameter value that OCR misreads (`0` read as `O`), which compiles into an on-screen check | Open |
| Postconditions get compiled from transient screens (`Loading...`, text read mid-typing) | Open; `capture_settle_timeout_s` reduces it |
| A fetch cut off by a page navigation never finishes in Playwright, so page quiescence waits forever | Worked around in `openimis_app.setup`; open in the engine |

## Files

- `results/<run>/agent_run.json`, `agent_ledger.json`: the agent run.
- `results/<run>/program.json`: the compiled steps, parameters and record check.
- `results/<run>/admission.json`: the standard gate's answer.
- `results/<run>/batch.csv`: one row per attempt.
- `results/<run>/summary.json`: the figures.
- `results/<run>/pilot-*.csv`: earlier measured runs with other settings.
- `results/launch-{1,2,3,5}/`: earlier agent runs whose programs halted more often (kept for the spend total and as evidence).
