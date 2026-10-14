# Eval Suite V2 Results — Claude Computer Use (Sonnet 4.6)

**Date**: 2026-02-23
**Agent**: ClaudeComputerUseAgent (claude-sonnet-4-6, computer_use_20251124)
**Benchmark**: WAA (Windows Agent Arena) live, 3 tasks × 2 conditions
**VM**: Azure Standard_D8ds_v5, waa-pool-00

## Results

| Condition | Task | Score | Steps | Time (s) | Success |
|-----------|------|-------|-------|----------|---------|
| ZS Settings | Turn off notifications | 1.00 | 5 | 146 | Yes |
| DC Settings | Turn off notifications | 1.00 | 5 | 155 | Yes |
| ZS Archive | Create folder + move .docx | 1.00 | 15 | 457 | Yes |
| DC Archive | Create folder + move .docx | 1.00 | 15 | 477 | Yes |
| ZS Notepad | Create + save draft.txt | 1.00 | 14 | 397 | Yes |
| DC Notepad | Create + save draft.txt | 1.00 | 15 | 360 | Yes |

**Overall: 6/6 (100%) success rate**

## Comparison with V1 (Pre-fix)

V1 scored 0/6 (0%) due to a bug where Claude's `screenshot` and `wait` actions
were mapped to `BenchmarkAction(type="done")`, causing premature episode termination.

| Condition | V1 Score | V1 Steps | V2 Score | V2 Steps |
|-----------|----------|----------|----------|----------|
| ZS Settings | 0.00 | 1 | **1.00** | 5 |
| DC Settings | 0.00 | 3 | **1.00** | 5 |
| ZS Archive | 0.00 | 15 | **1.00** | 15 |
| DC Archive | 0.00 | 15 | **1.00** | 15 |
| ZS Notepad | 0.00 | 3 | **1.00** | 14 |
| DC Notepad | 0.00 | 3 | **1.00** | 15 |

## Key Fix

The `ClaudeComputerUseAgent.act()` method now loops internally when Claude
requests `screenshot` or `wait` actions (up to 5 retries), sending the current
screenshot back as a `tool_result` rather than returning `done` to the runner.
See commit `0b185eb`.

## Observations

1. **Demo conditioning shows no benefit on these tasks** — zero-shot already achieves
   100%. Need harder tasks where ZS fails to measure DC value.
2. **Settings task** is fast (5 steps) — simple toggle.
3. **Archive/Notepad tasks** use all 15 steps — more complex multi-step workflows.
4. Claude computer_use with Sonnet 4.6 is highly effective for Windows GUI automation.

## Next Steps

- Run against harder WAA tasks where zero-shot may fail
- Test with additional agents (GPT, Qwen3-VL) for comparison
- Increase max-steps to see if archive/notepad complete faster with more budget
