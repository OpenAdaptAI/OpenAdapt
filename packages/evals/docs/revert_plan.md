# Revert Plan: Restoring Stable Eval Baseline

## Problem Statement

Core4 Trial 1 (runstamp `20260306_160915`) with `--controller --done-gate --max-steps 30` had **6/7 infrastructure failures**. Only 1 run (val_zs_0e763496) completed successfully.

Meanwhile, the Codex session's trials 2-5 (runstamp `20260306_124032`) ran on commit `9071fca` and completed **all 40 runs** without a single infrastructure failure.

Between `9071fca` and current HEAD (`174e9bf`), 10 PRs were merged. These PRs added complexity that broke infrastructure reliability.

## What Changed (9071fca → HEAD)

| PR | Title | Risk Assessment |
|----|-------|-----------------|
| #97 | Controller: prevent plan step drift, reduce VLM false negatives | **HIGH** — 2,531 lines, rewrites controller + adds tests. Adds complexity to agent loop. |
| #103 | Prefer multilevel demo files over plain .txt | **LOW** — 32 lines, file selection logic only |
| #106 | Clean-desktop parity mode and env metadata | **MEDIUM** — new setup scripts run on VM before each task |
| #107 | Gate app readiness and classify infra setup failures | **HIGH** — 486 lines, adds focus check + strict readiness that caused false infra failures |
| #108 | PostHog telemetry instrumentation | **LOW** — additive, no behavior change |
| #110 | Done-gate to prevent premature task completion | **MEDIUM** — calls `/evaluate` after agent says "done", depends on socat proxy stability |
| #111 | Remove stale health-gate args in core4_eval.py | **LOW** — script fix only |
| #112 | Search all LibreOffice profile dirs for recovery | **LOW** — makes cleanup more thorough |
| #113 | Default strict_setup_readiness to False | **LOW** — fixes #107's breakage |
| #114 | Win32 API foreground check | **LOW** — alternative detection, not default-active |

### Root Cause Analysis

The Codex trials completed reliably because they ran on a simpler codebase:
- **No focus check** (#107) → no false "infra failure" classification
- **No done-gate** (#110) → no dependency on fragile socat proxy for `/evaluate` at every "done" signal
- **No clean-desktop scripts** (#106) → no pre-task VM manipulation that can timeout/fail
- **No strict readiness gate** → agent just runs, setup errors are retried naturally

The main failure modes in our trial:
1. **Evaluate endpoint down** (socat proxy died) → 3 runs marked as infra failures
2. **Focus check failure** → 1 run got 0 steps
3. **API call failure** → 1 run scored 0 in 2 steps

## Important Context: Task Difficulty

**Even with perfect infra, only 1/4 tasks ever scores 1.0.** The Codex session proves this:

| Task | Description | Across 5 trials (ZS+DC) |
|------|-------------|-------------------------|
| 0e763496 (Writer font) | Change font to Arial | **10/10 = 1.0** |
| 04d9aeaf (Calc formulas) | Enter formulas in spreadsheet | **0/10 = 0.0** |
| 0bf05a7d (Calc zero-pad) | Format cells with leading zeros | **0/10 = 0.0** |
| 70745df8 (VS Code settings) | Modify JSON settings | **0/10 = 0.0** |

So **infra reliability is a prerequisite but not sufficient** — we also need to improve agent capability on the harder tasks. But first we need reliable infra.

Also notable: **DC never outperforms ZS** in any trial. DC scores the same or worse. The demo-conditioning pipeline itself may need rethinking (this is a separate problem).

## Options

### Option A: Full Revert to 9071fca baseline

**Revert all 10 PRs**, going back to the version that ran reliably.

| Pro | Con |
|-----|-----|
| Guaranteed stable — 40/40 runs completed | Loses all improvements (LO recovery fix, done-gate, controller fixes, telemetry) |
| Simple to execute | Need to re-implement anything we want to keep |
| Clear before/after comparison | May confuse git history |

**How**: Create a branch, `git revert` all 10 merge commits in reverse order, or `git checkout 9071fca -- openadapt_evals/ scripts/ tests/` to restore the old code wholesale.

### Option B: Selective Revert (remove high-risk PRs only)

Revert only the PRs that caused infra failures: **#107** (focus check), **#106** (clean-desktop scripts). Keep the rest.

| Pro | Con |
|-----|-----|
| Keeps useful improvements (#112 LO recovery, #110 done-gate, #108 telemetry) | Done-gate still depends on fragile socat proxy |
| Smaller diff | May not fully restore reliability |
| Preserves controller fixes (#97) | Still more complex than baseline |

**Specific reverts:**
- Revert #107 entirely (focus check + strict readiness)
- Revert #113, #114 (fixes for #107 — no longer needed if #107 is reverted)
- Revert #106 (clean-desktop scripts)
- Keep #110 done-gate but make it **opt-in** (not breaking if evaluate endpoint is down)

### Option C: Feature-Flag Everything, Default Off

Don't revert — instead, make ALL new features opt-in with defaults matching the `9071fca` behavior.

| Pro | Con |
|-----|-----|
| No code lost | Complexity remains in codebase |
| Can enable features one-at-a-time for A/B testing | More config flags = more ways to misconfigure |
| No git history confusion | Doesn't actually simplify the code |

**Implementation**: Add `enable_focus_check=False`, `enable_clean_desktop=False`, `enable_done_gate=False` to config. Default everything off. Re-enable one at a time and test.

### Option D: Hybrid — Revert to Baseline, Cherry-Pick Safe PRs

Revert to `9071fca` baseline, then cherry-pick only the **LOW-risk** PRs that don't affect the eval loop:

1. Keep: #103 (multilevel demo preference), #108 (telemetry), #111 (script fix), #112 (LO recovery)
2. Drop: #97 (controller rewrite), #106 (clean-desktop), #107 (focus check), #110 (done-gate), #113/#114 (focus check fixes)

| Pro | Con |
|-----|-----|
| Stable baseline + useful fixes | Loses done-gate and controller improvements |
| Clear which features are "safe" | Cherry-pick conflicts possible |
| Easy to add features back one-at-a-time | |

## Recommendation

**Option B (Selective Revert)** is the best balance:

1. The focus check (#107, #113, #114) is the clearest cause of false infra failures — revert it.
2. The clean-desktop scripts (#106) add pre-task VM manipulation that can timeout — revert it.
3. The done-gate (#110) is valuable but must gracefully handle evaluate endpoint being down — fix it to be non-fatal.
4. Keep everything else (#97 controller fixes, #103, #108, #111, #112).

Then re-run the exact same Core4 trial to confirm we match the Codex session's reliability (40/40 completions).

### Execution Plan

1. Create branch `fix/restore-stable-baseline`
2. `git revert` the merge commits for #107, #106 (and #113, #114 which are fixes for #107)
3. Make done-gate (#110) non-fatal: if `/evaluate` is unreachable, log warning and accept "done" instead of marking infra failure
4. Run Core4 Trial 1 again (same 4 tasks, ZS only, max-steps 15) to validate
5. Compare results with Codex trial2 results as baseline
6. If stable, PR and merge. Then re-add features one-at-a-time with proper testing.

### Success Criteria

- All 4 ZS runs complete (0 infrastructure failures)
- 0e763496 scores 1.0 (validates scoring works)
- Other 3 tasks may score 0 but must complete without infra errors
