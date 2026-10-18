# OpenEMR referral timing: OpenAdapt and a computer-use agent

Raw data and derived figures for the timed referral comparison published on
openadapt.ai, run on 14 and 15 October 2026. Every figure the website, docs or
blog cite for this comparison must be in [`figures.json`](figures.json), which
[`compute_figures.py`](compute_figures.py) derives from [`runs.csv`](runs.csv).

| File | What it is |
| --- | --- |
| `runs.csv` | One row per timed run, 424 rows, every phase and count |
| `figures.json` | The figures, derived from `runs.csv` |
| `compute_figures.py` | Derives `figures.json`; `--check` fails when it is stale |
| `PROTOCOL.md` | The protocol, written before the first timed run, and its one amendment |
| `CAVEATS.md` | Known limits of the run |

All patients, clinicians, addresses, phone numbers (555-01xx) and email
addresses (`example.invalid`) are made up. The rows hold timings, counts and
costs only. The `dir` column points at per-run evidence (screenshots, screen
recordings, engine reports, agent transcripts) that is kept outside the
repository.

`runs.csv` is the harness's `data/runs.csv` with two changes: `dir` was made
relative to the harness folder, and line endings were normalized from CRLF to
LF. That is why its sha256 differs from the original file; every value is the
same.

## What was measured

The task registers a referred patient in a local OpenEMR 8.0.0.3 test copy. Eight
synthetic referrals: seven new patients and one possible duplicate of an
existing patient (r6), where the correct result is no new chart.

- **OpenAdapt** (`replay-dom` in `runs.csv`): the referral program compiled with
  DOM element identity, replayed by engine commit 0c642bc0. 0 model calls.
- **Agent** (`agent-sonnet`): claude-sonnet-5-5 with a capped computer-use
  harness, effort `medium`, list-price token costs.
- `replay-main` is the same task from the 10 October bundle on the same engine.
  It is in the data and in `figures.json` (`openadapt_main_bundle`), and is not
  the published OpenAdapt figure.

A block is one fixture reset followed by the eight referrals back to back. Arms
were interleaved in time. **Warm** means the browser process was already running;
the clock starts at a new browser context and covers sign-in, opening the New
Patient form, the screen-recorder attach, the task and the read-back.

A run is **correct** when the test setup's own database read-back finds exactly
one new chart with 12 of 12 fields matching, or, for r6, no new chart. The arm
under test never judges itself.

## Published figures

The published set (`sets.published`) is warm runs in blocks 2 to 10. Block 1 is
left out because another process competed for the CPU during it. Blocks 11 to 15
exist only for OpenAdapt; they were run later, paired with a different engine
build, without the agent.

| Figure | OpenAdapt | Agent |
| --- | --- | --- |
| New-patient referrals saved and read back 12 of 12 | 62 of 63 | 62 of 63 |
| Possible duplicate: no new chart | 9 of 9 | 9 of 9 |
| Median time, correct runs (n = 62 each) | 29.7 s | 36.5 s |
| p10 to p90 | 26.8 to 32.5 s | 32.4 to 46.1 s |
| Steps or actions per correct run | 41 in every run | median 49 (42 to 76) |
| Screens sent to a model, median | 0 | 10 |
| Model cost per run | $0 | median $0.083 ($0.02 to $0.20) |

OpenAdapt's median was 6.8 s lower (bootstrap 95% CI 5.3 to 9.5 s), 18.6% less
time than the agent's. The one miss in each arm saved nothing:

- OpenAdapt, block 9, r4: the engine stopped before saving because the duplicate
  search did not show the expected "No matches were found." text.
- Agent, block 6, r5: the model reported that its browser tool was unavailable
  and stopped without entering data.

With block 1 included (blocks 1 to 10): 69 of 70 correct for both, medians 29.7 s
(OpenAdapt) and 37.6 s (agent). The `replay-main` bundle: 62 of 63, median 38.6 s
(blocks 2 to 10).

## Rebuild the figures

```bash
python packages/flow/benchmark/openemr_referral_timing/compute_figures.py
```

The script uses only the standard library and a fixed bootstrap seed, so the
output is identical on every machine. A published figure that is not in
`figures.json` has no source and should not be published.
