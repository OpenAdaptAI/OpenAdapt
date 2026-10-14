# Exact-current local Flow performance

This is a deterministic-runtime overhead and bounded robustness comparison. It is **not** a zero-shot computer-use comparison.

## Source and environment

- Flow commit: `4ca7566b73154769398d3135507060fa020aad0a` (version `1.24.0`; tracked-clean source)
- Release tag: `v1.24.0`
- Wheel SHA-256: `170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef`
- Evals base commit: `1fec68532e70dd05d6ad8668870c376c7fd8d996`
- Runner SHA-256: `ac58c0b9a02cfc991144a1f5c7a2814c6b6b748bcba27b87f8e1027ee575970f`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.7`
- Playwright: `1.61.0`
- Chromium: `Playwright-managed headless Chromium`
- Network/provider use: loopback bundled MockMed only; no cloud VM, hosted runner, or model API

## Counted result

One synthetic MockMed workflow: sign in, open the intended referral, create a Triage encounter, enter a trial-unique note, and save. Each arm used a fresh browser. The arm-independent screenshot/OCR oracle required the exact saved note, Triage row, and intended patient, and separately flagged wrong-target writes.

| Condition | Arm | Runs | Task success | Silent incorrect | Wrong action | Over-halt | Halt/error | Steady median | Steady p95 | End-to-end median | End-to-end p95 | Model calls | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `clean` | compiled replay | 3 | 3/3 | 0 | 0 | 2 | 0 | 11.882s | 11.940s | 12.477s | 12.547s | 0 | $0.00 |
| `clean` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.201s | 0.201s | 0.871s | 0.872s | 0 | $0.00 |
| `clean` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.202s | 0.203s | 0.859s | 0.997s | 0 | $0.00 |
| `theme` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 7.241s | 7.425s | 8.322s | 8.336s | 0 | $0.00 |
| `theme` | DOM positional | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.200s | 0.203s | 0.928s | 0.990s | 0 | $0.00 |
| `theme` | DOM name-scoped | 3 | 3/3 | 0 | 0 | 0 | 0 | 0.201s | 0.215s | 0.967s | 1.490s | 0 | $0.00 |
| `rename` | compiled replay | 3 | 3/3 | 0 | 0 | 0 | 0 | 7.322s | 7.717s | 7.938s | 8.602s | 0 | $0.00 |
| `rename` | DOM positional | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.062s | 30.072s | 30.673s | 31.080s | 0 | $0.00 |
| `rename` | DOM name-scoped | 3 | 0/3 | 0 | 0 | 0 | 3 | 30.061s | 30.095s | 30.721s | 30.770s | 0 | $0.00 |

Steady time wraps only the replay/script action loop. End-to-end time additionally includes fresh browser launch, the independent final screenshot/OCR oracle, and browser teardown. Local server startup and one-time record/compile are excluded from both and reported separately below. p95 is nearest-rank; with n=3 it is the slowest counted trial.

Outcome definitions: **silent incorrect success** means the arm reported completion but the independent oracle did not confirm the intended effect; **wrong action** means the oracle observed a write to the wrong patient or encounter type; **over-halt** means the arm reported halt/incomplete while the independent oracle confirmed the intended effect; **halt/error** means the arm stopped and the intended effect was absent. The same definitions apply to every arm. The selector controls therefore can count as over-halts when alternate final-state evidence contradicts them; their rename failures did not, because the oracle confirmed no write.

## One-time setup

- Bundled app server startup: 0.002s (n=1 diagnostic)
- Setup trial 1: record 2.790s; compile 7.044s; bundle `d7ca5ed85a305c40822177845ccd5ea3ccdeac1affbc36d6a7fd536c55509886`
- Setup trial 2: record 2.410s; compile 6.529s; bundle `6f47eea069a773ae4cdf8a946e603a4620a2011a3f87c87e4b9b5ccd1c970756`
- Setup trial 3: record 2.383s; compile 7.344s; bundle `0b790dcc67b69efd236cea439f38c36fe765d5e90a43fe54e52a018e6e27096a`

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
