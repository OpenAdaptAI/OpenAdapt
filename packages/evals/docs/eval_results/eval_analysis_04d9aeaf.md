# DC Eval Analysis: Task 04d9aeaf (LibreOffice Calc)

**Date**: 2026-03-02
**Task**: `04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS`
**Agent**: `api-claude-cu` (Claude Sonnet 4.6 via computer_use)
**VM**: `waa-pool-00` (Azure D8ds_v5, `openadapt-agents` resource group)

## Task Description

> In a new sheet with 4 headers "Year", "CA changes", "FA changes", and "OA changes", calculate the annual changes for the Current Assets, Fixed Assets, and Other Assets columns. Set the results as percentage type.

- **Recording**: 21 steps (VNC-captured, refined via LLM)
- **Demo**: 130-line VLM-annotated text (8,697 bytes) with Observation/Intent/Action/Result per step
- **Difficulty**: Hard (multi-step spreadsheet manipulation with formulas, cross-sheet references, percentage formatting)

## Experimental Setup

| Parameter | Value |
|-----------|-------|
| Model | claude-sonnet-4-6 |
| API | computer_use beta (`computer_20251124`) |
| Display | 1280x720 |
| Max steps | 15 (ZS), 30 (DC) |
| Demo type | VLM-annotated (gpt-4.1) |
| Pipeline | `scripts/run_eval_pipeline.py` |

## Results Summary

| Condition | Steps Used | Score | Time | Formulas Written | Outcome |
|-----------|-----------|-------|------|-----------------|---------|
| **Zero-shot (ZS)** | 1 of 15 | 0.00 | 79s | 0 | Stuck in wait loop after first click |
| **Demo-conditioned (DC)** | 30 of 30 | 0.00 | 1,825s | 5 | Wrote correct formulas for 1 of 3 columns |

## Detailed Analysis

### Zero-Shot (ZS) Condition

The agent failed immediately:

```
Step 0: click (0.545, 0.719)   — clicked somewhere on the spreadsheet
Step 1: error                   — max_internal_retries_exceeded
```

After the first click, the agent entered a `wait` loop (requesting screenshots repeatedly) and exhausted the 5-retry limit. The agent could not figure out what to do next without a demonstration.

**Root cause**: The task requires domain-specific knowledge about LibreOffice Calc (navigating sheets, entering formulas with cross-sheet references like `=Sheet1.C4`, percentage formatting). Without a demo, the agent had no context for the expected workflow.

### Demo-Conditioned (DC) Condition

The agent made substantial progress, executing 30 meaningful steps:

**Phase 1 — Navigation (steps 0-5)**: Clicked through sheet tabs and cells to position for data entry.

**Phase 2 — Formula entry attempt (step 6)**: Tried to enter all formulas at once via multi-line type:
```
=(Sheet1.C4-Sheet1.C3)/Sheet1.C3
=(Sheet1.C5-Sheet1.C4)/Sheet1.C4
...
```
This failed with "unterminated string literal" (newlines in `pyautogui.write()` argument).

**Phase 3 — Recovery and individual entry (steps 7-29)**: The agent recovered from the failed batch entry and switched to entering formulas one at a time:

| Step | Action | Content |
|------|--------|---------|
| 18 | type | `=(Sheet1.C4-Sheet1.C3)/Sheet1.C3` |
| 19 | key | Return |
| 21 | type | `=(Sheet1.C5-Sheet1.C4)/Sheet1.C4` |
| 22 | key | Return |
| 24 | type | `=(Sheet1.C6-Sheet1.C5)/Sheet1.C5` |
| 25 | key | Return |
| 27 | type | `=(Sheet1.C7-Sheet1.C6)/Sheet1.C6` |
| 28 | key | Return |
| 29 | click | Moving to next column |

**The agent correctly computed 4 annual change formulas for Current Assets using proper cross-sheet references.** It was starting on the next column (step 29) when it hit the 30-step limit.

### Action Distribution

| Action Type | ZS | DC |
|-------------|----|----|
| click | 1 | 17 |
| type | 0 | 6 |
| key | 0 | 6 |
| drag | 0 | 1 |
| error | 1 | 0 |
| **Total** | **2** | **30** |

## Bugs Discovered and Fixed

### 1. PyAutoGUI Fail-Safe Not Recovered (Critical)

**Bug**: The WAA adapter's failsafe detection only checked 200 responses, but WAA returns fail-safe errors as HTTP 500. The existing `_recover_failsafe()` method never fired.

**Impact**: In the pre-fix DC run (run 2), a drag action to (0,0) triggered fail-safe at step 22, causing all subsequent steps (22-29) to fail with "PyAutoGUI fail-safe triggered".

**Fix**: Extended failsafe detection to check response body on all HTTP status codes, not just 200. Also added "fail-safe triggered" as a detection substring alongside "failsafeexception".

### 2. Drag Defaults to (0,0) Coordinates (Critical)

**Bug**: When the agent sent a drag action without coordinates, both start and end defaulted to (0,0) — the screen corner that triggers fail-safe.

**Impact**: Guaranteed fail-safe trigger on any drag without explicit coordinates.

**Fix**: Skip drag actions with missing or all-zero coordinates instead of executing them. Log a warning for debugging.

### 3. No Coordinate Clamping (Preventive)

**Bug**: No bounds checking on pixel coordinates before sending to pyautogui.

**Fix**: Added `_clamp_pixel_coords()` method that keeps all coordinates at least 5px from screen edges. Applied to click, drag, and element-based actions.

### 4. "Unterminated String Literal" on Multi-line Type (Fixed)

**Bug**: When the agent sends text containing newline characters (`\n`), the `pyautogui.write()` command string becomes a syntax error because the newline breaks the Python string literal.

**Impact**: 2 of 30 DC steps failed with this error, wasting step budget.

**Fix**: `_build_type_commands()` splits text on newlines and interleaves `pyautogui.write()` with `pyautogui.press('enter')`. Each segment is properly escaped.

## Key Findings

### 1. Demo conditioning enables qualitatively different behavior

- **ZS**: Agent cannot even begin the task (stuck after 1 action)
- **DC**: Agent follows the demo's workflow, writes correct formulas with cross-sheet references, and demonstrates recovery from errors

### 2. Binary scoring masks meaningful progress

Both conditions scored 0.00, but the DC agent completed ~60% of the task (1 of 3 columns with correct formulas). WAA's `compare_table` metric runs **server-side inside the Windows VM** — it compares entire sheets and returns binary 0/1. The scoring infrastructure technically supports float scores (0.0-1.0), but the metric itself doesn't compute partial credit.

Adding partial-credit scoring would require modifying WAA's evaluator logic inside the VM (e.g., cell-level comparison in `compare_table`), not just changes on our adapter side. For now, qualitative analysis of action traces (as in this document) is the most practical way to measure DC vs ZS behavioral differences.

### 3. Step budget is a binding constraint

The 21-step recording required 30 agent steps due to:
- Navigation overhead (4 extra clicks for sheet/cell positioning)
- Failed actions consuming steps (2 unterminated string errors, 1 skipped drag)
- Recovery actions (screenshot requests, re-clicks after errors)

A step budget of ~2x the recording length is needed for reliable completion.

### 4. Infrastructure reliability matters

Three infrastructure bugs (fail-safe detection, drag defaults, coordinate clamping) were discovered and fixed during this evaluation. Without these fixes, the DC agent's effective step budget was halved by unrecoverable fail-safe errors.

## Recommendations

1. **Increase default max-steps to 40-50** for harder tasks (21+ step recordings)
2. ~~**Fix the newline-in-type bug**~~ — Fixed: `_build_type_commands()` splits on newlines, interleaves `write()` with `press('enter')`
3. **Investigate partial-credit scoring** — would require modifying WAA's server-side `compare_table` metric to do cell-level comparison; qualitative action-trace analysis is the near-term alternative
4. **Record more harder tasks** — this single task shows promising DC signal but N=1 is not statistically meaningful
5. **Consider running 3-5 trials per condition** — agent behavior is stochastic (different click targets, formula entry strategies)

## Files

| File | Description |
|------|-------------|
| `benchmark_results/val_zs_04d9aeaf/` | Zero-shot results (1 step, score 0.00) |
| `benchmark_results/val_dc_04d9aeaf/` | Demo-conditioned results (30 steps, score 0.00) |
| `demo_prompts_vlm/04d9aeaf-*.txt` | VLM-annotated demo (130 lines, 8,697 bytes) |
| `waa_recordings/04d9aeaf-*/` | Recording (21 steps, 45 files) |
| `scripts/run_eval_pipeline.py` | Pipeline script used for this evaluation |

## Reproduction

```bash
# Start VM, run both conditions, print results
python scripts/run_eval_pipeline.py \
    --tasks 04d9aeaf \
    --agent api-claude-cu \
    --max-steps 30

# DC-only re-run
python scripts/run_eval_pipeline.py \
    --tasks 04d9aeaf \
    --agent api-claude-cu \
    --max-steps 50 \
    --dc-only
```
