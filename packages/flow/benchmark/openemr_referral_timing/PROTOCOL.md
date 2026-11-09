# OpenEMR referral timing benchmark: protocol (written 2026-10-14, before any timed run)

## Question
How long does it take, end to end, to register a referred patient in the local OpenEMR test copy and have the result verified, for (a) OpenAdapt replay of a compiled bundle and (b) a Claude Sonnet computer-use agent; and is the result correct?

## Fixed conditions
- Machine: MacBook-Pro-5.local (12 cores), shared with other sessions. Load is sampled every 2 s during each run and recorded; each run waits for the prior study's load gate (1-min load < 8 for 4 consecutive 5 s samples, give up after 20 min and start anyway, flagged).
- Fixture: the `realrun` OpenEMR podman fixture (state under site-v2/realrun/openadapt-flow/benchmark/openemr_local/state). Same reset as the prior study: `OpenEMRFixture.reset()` (drop + restore verified baseline, restart app) then seed the existing made-up chart Rosa Delgado 1958-03-27 through the REST API.
- Patients: the 8 referrals in site-v2/visuals/referral/harness/referrals.py: 7 real (normal) referrals r1, r2, r3, r4, r5, r7, r8, plus the mismatch r6 (Rosa Delgado, different DOB) as the duplicate-halt check.
- Browser: headless Chromium, 1280x800, DPR 1, fresh browser context per run (no cookies carried over), loopback CDP port for the screen recorder.
- Footage: CDP screencast JPEG q95 for every run (same recorder as the prior study), encoded to MP4 after each block (outside timing); frames then deleted to save disk.

## Unit of work: a block
A block = one fixture reset + Rosa seed, then the 8 referrals run back to back with one arm and one start mode, no reset between them (same start state as the prior study's batches). Each run creates at most one chart for a different person, so runs in a block do not interfere, except that the patient list grows by one per run. Referral order is rotated by one position per block (block k starts at referral k mod 8), so each referral appears in each position about equally. Arms and modes are interleaved in time (round-robin over arm x mode) so slow drift in machine load or API latency hits all arms.

## Arms
1. `replay-main`: OpenAdapt engine at pinned main, bundle compiled 2026-10-10 (site-v2/visuals/referral/bundle), 0 model calls.
2. `replay-dom`: same engine, bundle re-recorded/recompiled with DOM structured identity (scratch/replay-speed/dom-identity/out/bundle), 0 model calls.
3. `agent-sonnet`: claude-sonnet-5-5 via the prior study's harness code (site-v2/visuals/agent/harness/cua.py: same toolset, prompt, instruction, effort `medium`, caps, settle-before-screenshot). Spend ledger is separate (scratch/benchmark/spend.json), total cap $15, stop starting new runs at $14; per-run cap $0.75 (prior runs cost $0.07-0.12).

Engine commits: start pin = origin/main 0c642bc0 (#503 merged). At the end, the replay arms are re-run on the newest merged main (if it differs) as a separate, labelled dataset; pinned commit SHA is recorded per run.

## Start modes
- `cold`: a new Python process per run. Clock starts in the parent immediately before spawning; includes interpreter start, imports, OCR model load (lazy, on first use), Chromium launch, sign-in, opening the form, the task, read-back.
- `warm`: one long-lived Python process per block; modules imported, Chromium launched and the OCR engine (or the API client) initialised once before the block, untimed (recorded as `warm_setup_s`). Clock starts at the call for that run; includes new browser context, sign-in, opening the form, the task, read-back.
Fixture reset/seed is a test precondition, never inside the clock (recorded separately).

## Wall-clock definition
`wall_s` = clock start (above) -> independent read-back finished and verdict computed. Measured with time.time() in one process (cold: child writes absolute marks, parent spawn time is the start). Phases (absolute marks, so they sum to wall):
- `proc_start` (cold only): spawn -> main() entered
- `imports` (cold only): engine/harness imports
- `browser_launch`: Playwright start + Chromium launch (cold) or new context/page (warm)
- `signin_form`: OpenEMR sign-in + open blank New Patient form + stable pre-trial count snapshot (the setup the prior study kept "off camera"; here it is counted)
- `recorder_start`: screen recorder attach (measurement apparatus; reported, and `wall_ex_recorder_s` = wall - recorder_start is also reported)
- `task`: replay `Replayer.run` (engine) or the agent loop (first model call -> final report)
- `readback`: 1.0 s oracle settle (same as prior) + SQL/REST read-back + table-count deltas + verdict
Teardown (recorder stop, browser close) is after the clock and recorded.
Agent task is further split into model-wait and browser-action seconds (from cua.py); replay task into per-step elapsed (engine report).

## Correctness (per run)
- Normal referral: correct iff exactly 1 new chart and the read-back of that chart matches 12/12 referral fields.
- r6 mismatch: correct iff 0 new charts (the run halted or cancelled). Engine outcome / agent stop reason recorded.
Read-back is by the test setup (direct SQL on the fixture DB), never by the arm under test.
A run also records: engine outcome or agent stop reason, actions (replay: steps executed; agent: input actions), screens sent to a model (replay 0), model calls, tokens, list-price cost, local screenshots (replay).
Validity guard: the OpenEMR container start time is checked before and after each run; a change (someone else reset the shared fixture) marks the run `invalid` and it is excluded and reported.

## N
Target: 10 blocks per arm per mode = 10 runs per patient per arm per mode (plus 10 r6 checks). If time on the shared machine does not permit 10 cold blocks for the replay arms, or the agent spend cap binds, the achieved N is reported. Agent cold vs warm differ only in process start (the model dominates), so the agent arm runs 10 warm blocks and as many cold blocks as budget allows (target 3).

## Statistics
Per arm x mode, over correct normal runs (7 patients pooled and per patient): median, p10/p90 (linear interpolation), min/max, mean, and a 95% CI for the median (bootstrap, 10,000 resamples, percentile) and for the mean (t). Arm differences: median of differences is not paired by design, so the difference of medians with a bootstrap CI. Raw per-run CSV with every phase. Failed/incorrect runs are listed, not dropped silently.

## Not in scope / caveats fixed in advance
- No website edits. Healthcare prospect not named anywhere.
- Agent timing depends on Anthropic API latency at run time; it is reported with the time of day.
- The agent is given the same "do not create a duplicate" instruction as the prior study.

## Amendment (2026-10-14 23:45 ET, after block 1; before any analysis of differences)
Other sessions kept the 12-core machine's 1-min load at ~9-10 (test suites, the other executor's probes), so the load gate at 8 stalled runs 5-8 min each; the full plan would have taken ~17 h. From block 2 on: gate threshold 12 (= core count; load is still recorded per run and reported), and replay cold blocks capped at 5 (N=5 per patient per replay arm cold). Warm arms keep 10 blocks; agent cold keeps 3.
