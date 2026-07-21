# Benchmark: compiled replay vs. computer-use agent — OpenEMR (real app)

Date: 2026-07-08. Same head-to-head as the [MockMed benchmark](../BENCHMARK.md),
run against a real third-party application: the official OpenEMR public
demo (`https://demo.openemr.io/openemr/index.php`, fake patients only, instance resets daily).
One task, two ways to automate it, one success check.

**Task** (18 compiled steps): log in as the demo
admin, search the demo patient "Phil", open the chart of "Belford, Phil",
scroll the dense Medical Record Dashboard to the Messages card, open
Patient Messages, add a note (a distinct parameterized value per run in
BOTH arms), save.

![latency and cost](latency_cost.png)

| | compiled replay | computer-use agent |
|---|---|---|
| runs | 20 | 10 |
| success rate | 100% (20/20) | 100% (10/10) |
| latency p50 | 39.2 s | 70.4 s |
| latency p95 | 41.0 s | 82.6 s |
| model cost / run | $0 | $0.5522 |
| total model cost | $0 | $5.52 |
| tokens (uncached in / out, total) | 0 / 0 | 496 / 27,272 |
| cache tokens (write/read, total) | 0 / 0 | 1,317,803 / 563,928 |

Failed runs, reported honestly:

Compiled arm:

- none

Compiled runs that self-flagged, also reported honestly (success is judged by the arm-independent OCR check both arms share, not the replayer's self-report):

- compiled run 20: postconditions flagged expected-screen drift at step_017 and the replayer aborted; the arm-independent OCR check verified the note saved (matched 100%)

On a shared instance every run — ours and other visitors' — grows the message list, so a postcondition can drift after the action lands. The self-flag is the point: the replayer detects the drift and halts instead of improvising.

Agent arm:

- none

## Methodology

The [MockMed benchmark](../BENCHMARK.md) remains the CI-reproducible
methodology anchor — same orchestrator, same agent harness, same style of
OCR success check, on an app anyone can rerun deterministically. This is
the real-world result on a live third-party instance, with the caveats
below.

- **Record + compile once.** The workflow is recorded fresh against the
  live demo via `scripts/openemr_demo.py` and compiled into a
  vision-anchored bundle. Recording and compiling are a one-time cost and
  are not included in per-run latency.
- **Fresh browser per run, shared server state.** Each run of either arm
  gets a fresh chromium browser (no session state). Unlike MockMed, the
  server side is a single shared public instance that every run (and every
  other internet visitor) mutates.
- **Same interface.** Both arms drive the same `PlaywrightBackend`,
  vision-only: PNG screenshots in; pixel-coordinate clicks, typed text,
  key presses, and wheel scrolls out. Neither arm uses DOM selectors at
  run time.
- **Agent arm.** Model `claude-sonnet-5` with the
  `computer_20251124` computer-use tool (beta header
  `computer-use-2025-11-24`), a 40-action
  budget (18 steps plus headroom for dense,
  slow screens), and history bounded to the last 3 screenshots. The task
  prompt states user intent — credentials as a user would state them, the
  target patient, the exact note text — not steps or coordinates. Every
  executed action returns a settled screenshot.
- **Same success criterion, implemented once.** After each run, the final
  screenshot is checked by `verify_note_saved` (OCR): a contiguous run of
  at least 16 characters of the run's note must appear in the frame's
  OCR text (whitespace-squashed; retried at 2x resolution when the raw
  frame does not pass, because rapidocr drops dense table lines at
  1280x800). Neither arm's self-reported success is used.
- **Distinct, mutually dissimilar note per run in BOTH arms** (no two
  notes share a 16-character squashed substring — unit-tested), so
  success proves parameter substitution against live state and one run's
  note cannot satisfy another run's check.
- **Pacing.** Runs are spaced ~30s apart as
  public-demo courtesy; the pacing gap is excluded from latency.
- **Latency** is wall-clock around the replay / agent loop only.
- **Cost** is computed from API `usage` token counts at list pricing
  ($3.00 /
  $15.00 per MTok input/output
  for claude-sonnet-5, plus prompt-cache writes at 1.25x and cache
  reads at 0.1x the input rate). An introductory $2/$10 rate applies
  through 2026-08-31, so billed cost today is about a third lower than
  reported. Compiled replay makes zero model calls.
- **Identity-protection coverage: not captured in this results.json.** The armed-coverage metric was added to the generator on 2026-07-10; future runs report how many click steps carry the pre-click identity check and list the unarmed steps (which proceed with NO identity verification — see docs/LIMITS.md).
- **Prompt caching.** The agent loop places `cache_control` breakpoints on
  the tool definition and the newest user message each turn, so each API
  call reuses the cached conversation prefix; screenshot truncation
  partially invalidates the prefix each turn, so the realized hit rate is
  below 100% by design. Cache token counts are reported per run.
- **Hard cost guardrails.** Every agent run is capped at
  $1.50 (list price; the loop stops with `stopped="cost_cap"`
  and the run is recorded as-is), and the whole agent arm is capped at
  $8.00 — if the next run could exceed the ceiling, the arm
  stops and the truncation is disclosed above. Both checks run after each
  API call rather than before it, so each bound allows an overshoot of at
  most one API call's marginal cost. Runs that crash mid-way keep their
  partial spend on their recorded rows, counted against the ceiling. A
  preflight API call runs before any spend; two consecutive auth/billing
  failures abort the arm.

## Caveats — read before quoting these numbers

- **The demo instance is shared and mutable.** Anyone on the internet can
  (and does) modify it, and it resets daily. Every successful run also
  appends a message that grows the dashboard for subsequent runs. Failure
  modes here can be demo-instance weather, not tooling; N is small by
  design (public-demo courtesy).
- **Not CI-reproducible.** The numbers depend on the live instance's state
  and load on the day of the run. The MockMed benchmark is the
  reproducible anchor; treat this as a field result.
- **The agent arm has a small N** (10) because agent runs cost real
  money, real minutes, and real load on a shared public service. Its
  success rate carries wide error bars.
- **Network variance affects both arms** (live remote server), unlike the
  local MockMed target.
- **Model version pinned.** Results describe `claude-sonnet-5` with
  the `computer_20251124` tool on 2026-07-08; newer models will
  differ.
- **The compiled arm needs a demonstration first.** The one-time
  record + compile step (about a minute of human demonstration) is the
  price of the fast replays; the agent needs only the prompt.
- **OCR verification on dense EMR text under-counts.** rapidocr sometimes
  drops the exact table line containing the note (a known limitation
  documented in
  [docs/showcase-openemr/FINDINGS.md](../../docs/showcase-openemr/FINDINGS.md)),
  so a "failed" verification can be a measurement miss with the note
  plainly visible in the final screenshot. The check errs conservative
  and is identical for both arms. Every run's final screenshot is saved
  to `benchmark/openemr/finals/` (local only, not committed) so failed
  verdicts can be audited against what was actually on screen.
- Single machine (macOS-15.7.3-arm64-arm-64bit).

## Reproduce

```
.venv/bin/python scripts/openemr_demo.py benchmark
```

Records a fresh demonstration against the public demo, compiles it, then
runs both arms. Requires network access to the demo and
`ANTHROPIC_API_KEY` (or `~/.anthropic/api_key`). The agent arm costs real
money (about $5.52 at list price for 10 runs
when this was generated) and takes about an hour with pacing. Fake demo
patients only — never point this at a real OpenEMR install.
