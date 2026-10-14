# Exact-current local Flow performance

This is a deterministic-runtime overhead and bounded robustness comparison. It is **not** a zero-shot computer-use comparison.

## Source and environment

- Flow commit: `d1b054c718d60465fb8a2e1853e6a84c7f26dec2` (version `1.32.0`; tracked-clean source)
- Release tag: `v1.32.0`
- Wheel SHA-256: `d58dcf8b7e54c8a45199db4390a588dc261bb754c5ab2af0fdb540e44cb6bb8a`
- Evals base commit: `b7d4fe842f5a9ae9e4188df6087911b24a1706f2`
- Runner SHA-256: `fe49e0d9fc9ebf6d66120fc8f16683390071b96af6d252b82afbb94bb7449119`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.7`
- Playwright: `1.62.0`
- Chromium: `Playwright-managed headless Chromium`
- Network/provider use: loopback bundled MockMed only; no cloud VM, hosted runner, or model API

## Counted result

One synthetic MockMed workflow: sign in, open the intended referral, create a Triage encounter, enter a trial-unique note, and save. Each arm used a fresh browser. The arm-independent screenshot/OCR oracle required the exact saved note, Triage row, and intended patient, and separately flagged wrong-target writes.

| Condition | Arm | Runs | Task success | Silent incorrect | Wrong action | Over-halt | Halt/error | Steady median | Steady p95 | End-to-end median | End-to-end p95 | Model calls | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `clean` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 6.778s | 6.782s | 7.387s | 7.491s | 0 | $0.00 |
| `clean` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.208s | 0.212s | 0.851s | 0.868s | 0 | $0.00 |
| `clean` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.211s | 0.211s | 0.893s | 0.895s | 0 | $0.00 |
| `theme` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 6.908s | 7.044s | 7.531s | 7.644s | 0 | $0.00 |
| `theme` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.213s | 0.219s | 0.866s | 0.942s | 0 | $0.00 |
| `theme` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.209s | 0.212s | 0.855s | 0.921s | 0 | $0.00 |
| `rename` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 6.731s | 6.807s | 7.333s | 7.406s | 0 | $0.00 |
| `rename` | DOM positional | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.072s | 30.074s | 30.713s | 30.740s | 0 | $0.00 |
| `rename` | DOM name-scoped | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.070s | 30.071s | 30.694s | 30.716s | 0 | $0.00 |

Steady time wraps only the replay/script action loop. End-to-end time additionally includes fresh browser launch, the independent final screenshot/OCR oracle, and browser teardown. Local server startup and one-time record/compile are excluded from both and reported separately below. p95 is nearest-rank; with n=3 it is the slowest counted trial.

Outcome definitions: **silent incorrect success** means the arm reported completion but the independent oracle did not confirm the intended effect; **wrong action** means the oracle observed a write to the wrong patient or encounter type; **over-halt** means the arm reported halt/incomplete while the independent oracle confirmed the intended effect; **halt/error** means the arm stopped and the intended effect was absent. The same definitions apply to every arm. The selector controls therefore can count as over-halts when alternate final-state evidence contradicts them; their rename failures did not, because the oracle confirmed no write.

## One-time setup

- Bundled app server startup: 0.003s (n=1 diagnostic)
- Setup trial 1: record 2.863s; compile 7.136s; bundle `1bb8a6610ef282fdfed87347bbc51848a45d1e5f270ef445fe193ec094611a56`
- Setup trial 2: record 2.497s; compile 6.668s; bundle `6c22b821093d20625bb704244103f2f1854899c115c14565fe9918f8d8e6333e`
- Setup trial 3: record 2.639s; compile 6.612s; bundle `b679dd26e59dbade2f624ec056e3ae3a3bff335a88e73a5079becfb5e433f52f`

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
