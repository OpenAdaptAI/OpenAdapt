# Benchmark: compiled replay vs. computer-use agent

Date: 2026-07-08. One task, two ways to automate it, one success check.

**Task** (MockMed, the bundled demo clinic app; fake data only): sign in as
`nurse.demo`, open the first referral task, create a New Encounter of type
Triage, enter a note, save.

![latency and cost](latency_cost.png)

| | compiled replay | computer-use agent |
|---|---|---|
| runs | 100 | 20 |
| success rate | 100% (100/100) | 100% (20/20) |
| latency p50 | 4.9 s | 37.5 s |
| latency p95 | 5.1 s | 43.4 s |
| model cost / run | $0 | $0.2716 |
| total model cost | $0 | $5.43 |
| tokens (uncached in / out, total) | 0 / 0 | 1,684,942 / 25,085 |

## Drift (`?drift=theme`, one run per arm)

MockMed re-rendered with a dark palette, which invalidates every recorded
template crop:

- compiled (healing on): succeeded in 9.7s, 8 heals
- agent (as-is): succeeded in 87.4s, 23 actions, $0.6319 — close to the
  25-action budget. In an earlier smoke run under the same drift the agent
  exhausted its budget and failed, so treat the drift rows as single
  observations either way (see caveats).

## Methodology

- **Record + compile once.** The demo is recorded through the Playwright
  demo driver and compiled into a vision-anchored bundle
  (`openadapt-flow demo-record` + `compile`). Recording and compiling are a
  one-time cost and are not included in per-run latency.
- **Identical environments.** Each run of either arm gets a fresh chromium
  browser + page against the same locally served MockMed app (app state
  lives entirely in the page, so a fresh page is a fresh instance).
- **Same interface.** Both arms drive the same `PlaywrightBackend`,
  vision-only: PNG screenshots in, pixel-coordinate clicks / typed text /
  key presses out. Neither arm uses DOM selectors at run time.
- **Agent arm.** Model `claude-sonnet-5` with the
  `computer_20251124` computer-use tool (beta header
  `computer-use-2025-11-24`), a 25-action budget, and history bounded to
  the last 3 screenshots. The task prompt states user intent (the numbered
  task above), not steps or coordinates. Every executed action returns a
  settled screenshot, using the same settle logic the replayer uses.
- **Same success criterion.** After each run, a screenshot of the final
  state is checked by OCR (`openadapt_flow.vision.find_text`): the
  `Encounter saved — <note>` banner AND the `Triage — <note>` encounter row
  must both be visible. Neither arm's self-reported success is used.
- **Latency** is wall-clock around the replay / agent loop only (browser
  and server startup excluded for both arms).
- **Cost** is computed from API `usage` token counts at list pricing
  ($3.00 /
  $15.00 per MTok input/output
  for claude-sonnet-5). An introductory $2/$10 rate applies through
  2026-08-31, so billed cost today is about a third lower than reported.
  Compiled replay makes zero model calls.
- **Identity-protection coverage: not captured in this results.json.** The armed-coverage metric was added to the generator on 2026-07-10; future runs report how many click steps carry the pre-click identity check and list the unarmed steps (which proceed with NO identity verification — see docs/LIMITS.md).

## Caveats — read before quoting these numbers

- **MockMed is a simple app.** Five screens, no scrolling, no popups, high
  contrast, big labels. It is close to a best case for both arms; harder
  apps would slow and likely degrade both, plausibly at different rates.
- **The agent arm has a smaller N** (20 vs 100) because agent
  runs cost real money and minutes. Its success rate carries wider error
  bars.
- **Model version pinned.** Results describe `claude-sonnet-5` with the
  `computer_20251124` tool on 2026-07-08; newer models will differ.
- **The compiled arm needs a demonstration first.** The one-time
  record + compile step (about a minute of human demonstration) is the
  price of the fast replays; the agent needs only the prompt.
- **Drift is n=1 per arm** — an existence result, not a rate.
- **Latency includes deliberate settle waits** (screenshot stability
  polling) in both arms; a tuned production loop could shave both.
- Single machine (macOS-15.7.3-arm64-arm-64bit), local server, no network
  variance in the compiled arm; agent latency includes real API round
  trips.

## Reproduce

```
openadapt-flow benchmark --n-compiled 100 --n-agent 20 --out benchmark/
```

Requires `ANTHROPIC_API_KEY` (or `~/.anthropic/api_key`). The agent arm
costs real money (about $5.43 at list price for
20 runs when this was generated).
