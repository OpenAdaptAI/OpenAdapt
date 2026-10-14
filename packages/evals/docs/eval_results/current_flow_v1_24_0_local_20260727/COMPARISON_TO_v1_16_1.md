# Flow 1.24.0 versus the published 1.16.1 evidence

The comparison in `docs/eval_results/current_flow_v1_16_1_local_20260718` was
still the published result eight minor releases later. This directory re-runs it
against the current published wheel with the **byte-identical runner** (SHA-256
`ac58c0b9a02cfc991144a1f5c7a2814c6b6b748bcba27b87f8e1027ee575970f`), the same
task script, the same arm-independent oracle, the same three trials per cell,
and no retries. Nothing about the methodology was changed to suit the numbers.

**What is not held constant: the target application.** MockMed ships *inside*
the Flow wheel, so repinning the wheel repins the engine **and** the application
under test. `openadapt_flow/mockmed/static/{app.js,styles.css}` differ between
`v1.16.1` and `v1.24.0`; the load-bearing change is [`c416b7d`][c416b7d], which
added a patient banner to the top of the New Encounter form. This is a
two-variable comparison, and for the `clean` over-halt below the fixture is the
variable that moved — see "Attribution" in that section. Every number here is
reported exactly as measured.

[c416b7d]: https://github.com/OpenAdaptAI/openadapt-flow/commit/c416b7d404ab6480351ec1d1809bcc26bdee1b4a

## Exact binding

| | 1.16.1 (published 2026-07-18) | 1.24.0 (this run, 2026-07-27) |
|---|---|---|
| Flow version | `1.16.1` | `1.24.0` |
| Bundled MockMed fixture | pre-`c416b7d` | post-`c416b7d` (**differs**) |
| Flow commit | `113ce992b491576d77236f495b983165ce7a63bd` | `4ca7566b73154769398d3135507060fa020aad0a` |
| Wheel SHA-256 | `c7073283475e7ae722db2478b499d962364851104e09edb71f254ba21c1310cd` | `170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef` |
| Evals commit | `7629ba5a2447c919e499e88e9b4eedbc80c8f3ab` | `1fec68532e70…` |
| Runner SHA-256 | `ac58c0b9…970f` | `ac58c0b9…970f` (identical) |
| Platform | `macOS-15.7.3-arm64-arm-64bit` | `macOS-15.7.3-arm64-arm-64bit` |
| Python / Playwright | 3.12.7 / 1.61.0 | 3.12.7 / 1.61.0 |

## Compiled replay, side by side

Task success is the independent screenshot/OCR oracle; over-halt means the arm
reported halt/incomplete while that oracle confirmed the intended effect.

| Condition | 1.16.1 success | 1.24.0 success | 1.16.1 over-halt | 1.24.0 over-halt | 1.16.1 steady median | 1.24.0 steady median |
|---|---:|---:|---:|---:|---:|---:|
| `clean` | 3/3 | 3/3 | 0 | **2** | 7.189s | **12.263s** |
| `theme` | 3/3 | 3/3 | **3** | **0** | 12.661s | 6.822s |
| `rename` | 3/3 | 3/3 | 0 | 0 | 7.114s | 6.500s |

Silent incorrect success, wrong-action writes, model calls, and model cost are
**0** in every cell of both runs. The steelmanned DOM controls are unchanged:
3/3 on `clean` and `theme` at ~0.2s steady, and 0/3 on `rename`, failing loudly
at the first renamed locator before any mutation.

## The over-halt moved into the no-drift baseline

The `region_stable` postcondition failure on `step_010` (the `Save Encounter`
click) that fired 3/3 under `theme` drift on 1.16.1 no longer fires under
`theme` — Flow's fix landed. It now fires **2/3 under `clean`**, where there is
no drift at all:

```
Postconditions failed for step 'step_010' (click 'Save Encounter'):
expected screen state not reached (semantic drift) — failed: region_stable
region=(0, 228, 696, 223) — run aborted
```

In every one of those trials the independent oracle confirmed the encounter was
saved correctly. A false halt under cosmetic drift is defensible; a false halt
on an undrifted application is not, whatever its cause. It is not a flake — an
independent replication (`replication/`) reproduced the same 2/3 pattern with
the same failing region.

### Attribution: the fixture moved, not engine reliability

This delta is **not** an engine regression. `c416b7d` added a patient banner to
the top of the New Encounter form, which made the band the 1.16.1 bundle had
mined for `step_010` identical before and after the Save click. The compiler's
largest-changed-region search therefore moved *down*, onto the saved-encounter
row — which renders the run's `note` parameter. The compiled bundle thus froze
run-specific data as if it were a stable property of the application, and each
trial's verdict turned on the glyphs of that trial's unique note. That is why
the count is 2/3 rather than 3/3: the surviving trial is the one whose note
happened to hash close enough to the demonstrated one, not a trial in which the
engine behaved differently.

The underlying engine defect this exposed is real but different from the one
originally claimed here: a parameter's demonstrated value could become a pixel
invariant, because nothing screened a downstream CLICK whose changed region
renders that parameter (`lint_param_leakage` covered text postconditions and
parameterized TYPE steps only). The fix tracked in
[`openadapt-flow#285`][flow-285] screens the candidate region against the
demonstrated parameter values and reports any drop in `param_hygiene.json`.
Recompiling with a Flow release carrying that fix should not produce this
postcondition at all; the numbers above remain the measurement of the wheels as
published.

[flow-285]: https://github.com/OpenAdaptAI/openadapt-flow/pull/285

The counted trials are retained in `clean_postcondition_over_halt.json`.
`theme_postcondition_over_halt.json` is retained with zero observations as the
evidence that the 1.16.1 theme regression is fixed.

Consequence beyond timing: the aborted run stops **before** independent effect
verification, so the run reports `HALTED_BEFORE_EFFECT` for a save that
demonstrably landed. `transaction_probe/` measures that directly — it is the
same failure the `ok` / `effect_verified` cell records.

## Improvement: identity coverage

The compiled bundle now arms **5 of 8** identity-applicable clicks, against
**1 of 8** on 1.16.1. The three that remain unarmed are refused for stated
reasons (row text too generic, or the only readable text is the target's own
mutable label) rather than silently skipped. Task completion is still not a
universal wrong-target-immunity claim for unarmed steps.

## What is not claimed

- No zero-shot / computer-use-agent comparison was run, on either release. No
  model was called and no VM was started.
- Synthetic MockMed, one workflow, one macOS host, headless Chromium.
- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer application is
  represented.
