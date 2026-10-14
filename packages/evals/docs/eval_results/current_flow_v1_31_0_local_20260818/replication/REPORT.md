# Exact-current local Flow performance

This is a deterministic-runtime overhead and bounded robustness comparison. It is **not** a zero-shot computer-use comparison.

## Source and environment

- Flow commit: `2d225dea9a0ad29ca84ce1b037cc0ac671367e28` (version `1.31.0`; tracked-clean source)
- Release tag: `v1.31.0`
- Wheel SHA-256: `81133db1528ad1bb1f26e3fcb6aea61b0651db6d905cf2e4943e8383c1f3d29c`
- Evals base commit: `1082192e9c2ec299d31330608c1016a939d3b88d`
- Runner SHA-256: `fe49e0d9fc9ebf6d66120fc8f16683390071b96af6d252b82afbb94bb7449119`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.13`
- Playwright: `1.61.0`
- Chromium: `Playwright-managed headless Chromium`
- Network/provider use: loopback bundled MockMed only; no cloud VM, hosted runner, or model API

## Counted result

One synthetic MockMed workflow: sign in, open the intended referral, create a Triage encounter, enter a trial-unique note, and save. Each arm used a fresh browser. The arm-independent screenshot/OCR oracle required the exact saved note, Triage row, and intended patient, and separately flagged wrong-target writes.

| Condition | Arm | Runs | Task success | Silent incorrect | Wrong action | Over-halt | Halt/error | Steady median | Steady p95 | End-to-end median | End-to-end p95 | Model calls | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `clean` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 7.110s | 7.229s | 7.920s | 7.955s | 0 | $0.00 |
| `clean` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.217s | 0.224s | 0.948s | 1.052s | 0 | $0.00 |
| `clean` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.213s | 0.216s | 0.924s | 1.233s | 0 | $0.00 |
| `theme` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 8.062s | 8.962s | 8.740s | 9.774s | 0 | $0.00 |
| `theme` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.215s | 0.218s | 1.053s | 1.134s | 0 | $0.00 |
| `theme` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.214s | 0.218s | 1.130s | 1.442s | 0 | $0.00 |
| `rename` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 7.776s | 9.919s | 8.475s | 10.894s | 0 | $0.00 |
| `rename` | DOM positional | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.080s | 30.087s | 30.779s | 30.914s | 0 | $0.00 |
| `rename` | DOM name-scoped | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.092s | 30.108s | 30.876s | 30.999s | 0 | $0.00 |

Steady time wraps only the replay/script action loop. End-to-end time additionally includes fresh browser launch, the independent final screenshot/OCR oracle, and browser teardown. Local server startup and one-time record/compile are excluded from both and reported separately below. p95 is nearest-rank; with n=3 it is the slowest counted trial.

Outcome definitions: **silent incorrect success** means the arm reported completion but the independent oracle did not confirm the intended effect; **wrong action** means the oracle observed a write to the wrong patient or encounter type; **over-halt** means the arm reported halt/incomplete while the independent oracle confirmed the intended effect; **halt/error** means the arm stopped and the intended effect was absent. The same definitions apply to every arm. The selector controls therefore can count as over-halts when alternate final-state evidence contradicts them; their rename failures did not, because the oracle confirmed no write.

## One-time setup

- Bundled app server startup: 0.004s (n=1 diagnostic)
- Setup trial 1: record 2.987s; compile 21.354s; bundle `380cdecdf04dcd29f6d40dc93fa8788def26a61f58312259ce571a8bcc12c1d1`
- Setup trial 2: record 3.208s; compile 9.884s; bundle `62520e2519f3974c911f49627d8ca16280c9685d90721f25203618b917ab8a4d`
- Setup trial 3: record 2.787s; compile 9.862s; bundle `28ed00717caafe85e770e56909c41ecf2314ff61b9e601985a83a39d9e7f455a`

## What this establishes

- The independent final-state oracle confirmed the intended effect for every compiled clean, theme-drift, and label-drift trial. Compiled theme runs reported 0/3 over-halts; these are counted rather than relabelled as clean completions.
- The selector controls are steelmanned Playwright scripts. Their clean/theme speed is the correct reminder that API/structural actuation should remain the preferred tier where available.
- The `rename` surface changes `Open` to `View` and `Save Encounter` to `Submit Encounter`. Both selector controls failed loudly at the first renamed locator before mutation. These are unsupported-drift halts, not silent wrong actions.
- Label drift is an intentionally bounded robustness probe, not evidence for arbitrary drift or arbitrary applications.
- The compiled bundle had 5/8 identity-applicable clicks armed. Task completion is not a universal wrong-target-immunity claim for unarmed steps.
- The exact theme postcondition failures are retained in `theme_postcondition_over_halt.json` as a compact regression artifact.

## Exact-current Flow versus zero-shot: not run

No current Flow-versus-zero-shot result is claimed. The Azure WAA VM was not started and no model was called. The existing `scripts/eval_flow_on_waa.py` live replay path currently leaves the WAA evaluator unwired (so it cannot independently score success), while its hybrid live path explicitly returns before execution because the adapter is not wired. A valid future run therefore requires, in order:

1. Wire `WAALiveAdapter.evaluate` into the Flow replay path and wire the same model/adapter into the zero-shot arm.
2. Prepare one exact compiled bundle per retained task and bind Flow/evals/model/environment revisions in the run manifest.
3. Obtain explicit approval for Azure VM start and a hard model spend cap; then confirm VM snapshot/readiness without changing the task set.
4. Run at least three trials per task per condition for both arms, using WAA's evaluator as the oracle and recording correct, silent incorrect, over-halt, timeout/error, latency, tokens, and cost.
5. Deallocate the VM, verify no orphaned resources, and publish the immutable raw summaries plus a normalized report.

## Caveats

- Synthetic MockMed, one workflow, one macOS host, headless Chromium.
- This report runs the exact published wheel named above, extracted locally, and binds it to the release-tagged tracked-clean source.
- Browser startup is fresh per run but OS/browser caches are warm after the first launch; the Latin-rotated arm order reduces but does not eliminate host-order effects.
- Final screenshots were inspected and hashed during the run; only their SHA-256 hashes are retained in this committed report.
- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer application is represented.
