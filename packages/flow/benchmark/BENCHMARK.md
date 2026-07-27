# Benchmark: compiled replay vs. computer-use agent

Date: 2026-07-08. Engine: a pre-`v0.2.0` source checkout declaring
openadapt-flow 0.1.0. One task, two ways to automate it, one success check.

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

**Measured on Flow 0.1.0, 2026-07-08.** The measurement used a pre-`v0.2.0`
development source checkout; its exact runtime HEAD was not retained. The rows
were first committed in `b2eec0be` after parent `45f5ba8a`; those two SHAs
describe artifact history, not the runtime used for the measurement. Not
re-measured on a later release.

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

## Workflow complexity across the benchmark suite (2026-07-26)

An honest assessment of what the suite exercised BEFORE the multi-system
benchmarks landed: mostly single-application, linear, worklist-driven form
fills of roughly 5-15 recorded steps, with the fault-model studies driving a
single consequential write. Real back-office work is longer, cross-system,
document- and email-driven, and exception-heavy. The `ap_invoice` and
`o2c_recon` benchmarks were added to close that gap; the table below states
the shape of each benchmark so the difference is not overstated either (the
new benchmarks actuate through the api tier and do not measure GUI
perception; see each README).

| benchmark | steps (executed actions) | apps | input/output modalities | branching | exception paths |
|---|---|---|---|---|---|
| MockMed encounter (this file) | 6 compiled steps | 1 | browser GUI | none | none |
| MockLoan disbursement | ~6 compiled steps | 1 | browser GUI | none | none |
| `openemr_local` registration | ~15-25 UI actions | 1 | browser GUI (real EMR) | none | duplicate-search confirm |
| `openimis_claims` claim entry | ~10-20 UI actions | 1 | browser GUI (real AGPL app, repo-only) | none | none |
| `frappe_lending` loan application | ~10 UI actions | 1 | browser GUI (real app) | none | none |
| `canvas_ladder` / `rdp_ladder` / `citrix_ica_hdx` | 1-3 probe actions | 1 | pixel-only surface | none | qualification refusals |
| `effect_e2e` / `silent_wrong_action` / `lending_fault_model` | 1 consequential write | 1 | REST + SQLite | none | 10-class fault taxonomy |
| `effectbench` task pack | 1-3 writes per task | 1 | app API/DB oracles | none | per-task faults |
| **`ap_invoice`** (new) | **32** | **2** (ERP + mail gateway) | email in (maildir), PDF document, REST API, UI gateway, email out | 2 branch points (match route; discount eligibility) | 4: missing PO, ambiguous duplicate, collateral adjacent-row overwrite, uncertain payment delivery (`RECONCILIATION_REQUIRED` + suppressed retry) |
| **`o2c_recon`** (new) | **26** | **2** (billing + ledger) | CSV worklist in, CSV results write-back (re-read), REST API, UI gateway, 2 SQLite systems of record | 3-way branch (match / adjust / missing) | 4: missing record (explicit halt terminal), ambiguous duplicate, stale snapshot (optimistic concurrency), phantom file write |

Both new benchmarks run every consequential write through the real
`Replayer`'s api actuation tier after the real Standard-profile run gate admits
the sealed bundle and binds a single-use authorization to its exact inputs.
Every write is verified through a separate persisted-state read (read-only SQL,
REST oracles, a maildir read, or a CSV re-read) and judged by a direct-file
adjudicator whose expectations come from immutable source fixtures rather than
the prepared worklist. This is not an independent service/failure domain. Zero
model calls; healthy-path
governed runs classify `VERIFIED` under the Section-3 transaction taxonomy.
Measured headline over both benchmarks (n=3 per cell; 60 base runs): governed
silent-incorrect-success 0/30 governed runs and healthy-path over-halts 0/6;
naive banner-oracle
silent-incorrect-success 6/30 (the collateral overwrite and the phantom
write-back classes). Deterministic coverage
matrix, not a sampled incidence rate. See `benchmark/ap_invoice/README.md`
and `benchmark/o2c_recon/README.md` for what each does and does not prove.
