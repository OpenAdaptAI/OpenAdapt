# Crystallize on MockMed: selective vision against a screenshot every turn

Measured 2026-10-18 with `measure.py` in this folder, engine commit
`2d5e4c15`, model `claude-opus-5-5` at effort `medium`, on the operator's own
key. MockMed with its transactional system of record (`?fault=ok`), 1280x800,
synthetic data only.

The task: "Open the referral for Jane Sample, start a Triage encounter, and
save this note: {note}". Each trial runs both arms on a freshly reset MockMed,
in alternating order, with a distinct note per trial:

- **selective**: the harness sends the page's structural listing; the model
  can ask for a screenshot, and gets one after the save (the gate).
- **every_turn**: Claude's computer toolset (`computer_toolset_20260801`),
  with a screenshot after every action.

Both arms run through the same harness, capture and compiler, with prompt
caching on (`cache_control` at the top level) in both.

## Results (10 trials per arm, `results/`)

| | selective | every turn | every turn / selective |
| --- | ---: | ---: | ---: |
| Run 1 passed its record check | 10 / 10 | 10 / 10 | |
| Model calls (median) | 5 | 5 | 1.0 |
| Actions performed | 6 | 14 | |
| Screenshots sent to the model | 1 | 15 | 15 |
| Prompt tokens per run (median) | 10,332 | 76,053 | **7.36** |
| Output tokens per run (median) | 418 | 576 | 1.38 |
| Cost per run (median) | $0.028 | $0.148 | **5.29** |
| Model time per run (median) | 8.9 s | 11.6 s | 1.29 |
| Run 1 wall time (median, all trials) | 41.6 s | 52.6 s | 1.26 |
| Run 1 wall time, trials 1 to 3 (host load under 200) | 14.6 to 17.6 s | 36.4 to 58.8 s | |

"Prompt tokens" counts every input token the model read: uncached input,
cache writes and cache reads. Cost uses $4 per million input tokens, $20
output, $5 cache write and $0.20 cache read. Token counts were nearly identical
across trials (selective 10,332 every time; every turn 76,053 to 76,079), so
the medians are representative.

Run 1 wall time includes the recording (a before and after frame and a
system-of-record read for every action) and the compile. The host was shared
with other jobs, and its one-minute load average rose from 60 to 480 during
the run (`host_load_1m` in each row), so wall time is noisy. Model time, the
time spent waiting on the API, is less affected.

### Later runs

One admitted program from each arm was replayed five times with new notes:
**10 of 10 VERIFIED, 0 model calls, 0 tokens**, and every verify-run receipt
VERIFIED. Replay wall time was 15.9 to 37.3 s (median 36.0 s) at a host load
of 390 to 480. In the pilot, at lower load, the replays of the program
compiled from the selective run took 5.6 and 6.3 s.

### Spend

$1.77 for the 20 runs above. With the pilot ($0.17), the identical-inputs
runs ($0.12) and one aborted run (under $0.18), the total was under $2.30.

## Other files

- `results/pilot/`: one trial per arm and two replays per arm, run first to
  check the harness and the cost. After the pilot, the fields `images_sent`
  and `refused_actions` were renamed or removed in the code; the pilot rows were
  edited to match (`images_in_requests`), with no value changed.
- `results/identical-inputs/runs.jsonl`: six runs that used the same note in
  every trial. Later runs read earlier runs' screenshots from the prompt cache,
  so the every-turn arm cost $0.026 instead of $0.148 at the same token count.
  Real tasks have different inputs, so the main run uses a distinct note per
  trial. Kept to show the effect.

## Rows

`results/runs.jsonl` has one row per run 1: the arm, the trial, the order,
the note, the host load before and after, and `run1` with the per-call token
usage and time, the harness's screenshot counts, the gate decision, and the
record check. `results/replays.jsonl` has one row per later run.
`results/summary.json` is computed from them by `measure.py`.

## Reproduce

```bash
PYTHONPATH=. python benchmark/crystallize/measure.py \
  --env-file /path/to/.env --trials 10 --replays 5 --max-total-usd 15
```

`--env-file` holds `ANTHROPIC_API_KEY=...`; the key is read into the process
and never printed. The script stops before a run that could pass
`--max-total-usd`.

## What this does and does not show

- One small web app and one task. The structural listing is short on MockMed;
  on a dense page the selective arm's listing grows, and a page whose controls
  have no accessible names gives the agent less to work with.
- Both arms took the same path in every trial, so this compares observation
  cost, not task success. Both passed 10 of 10.
- The model asked for no screenshot in any selective run. On a canvas or a
  remote desktop there is no structure, and the selective arm becomes the
  every-turn arm.
