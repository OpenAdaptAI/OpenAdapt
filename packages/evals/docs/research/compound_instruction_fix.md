# Compound Instruction Problem: Analysis and Fix

> Date: 2026-03-19
> Status: Implementing (structured planner output)

## Problem

The planner outputs compound instructions like "type X then press Enter" but
only the first action executes. The parser catches "type" but misses "then
press Enter".

## How Other Systems Handle This

| System | Approach |
|--------|----------|
| SeeAct (ICML 2024) | Strict one-action-per-step, structured triplet output |
| Agent S2 | Function-calling format constrains to single action |
| CODA (ICLR 2026) | Planner outputs intent/reasoning, executor outputs single pyautogui call |
| UFO2 (Microsoft) | Embraces multi-action with runtime UIA validation between each |
| UI-TARS | `\n` suffix in type action means "press Enter after" |
| CoAct-1 | Code as actions (Python/Bash instead of individual clicks) |

## Root Cause

Free-form `instruction` text allows LLMs to naturally produce compound
descriptions. Every successful system uses structured output formats that
can only express one action.

## Fix: Three Layers

### Layer 1: Structured planner JSON (primary)
Change output from `{"instruction": "..."}` to
`{"action_type": "click", "action_value": "", "target_description": "..."}`.
Constrains to one action type per response.

### Layer 2: Action queue with `\n` encoding (UI-TARS pattern)
`action_value: "chrome://settings\n"` means type URL then press Enter.
The `\n` suffix queues an Enter keypress after the type action.

### Layer 3: Continuation extraction (safety net)
Regex detects "then/and then/followed by" patterns and queues the second
action. Only fires if structured parsing doesn't catch it.

## What NOT to do

- Don't rely on prompt wording alone (already tried, doesn't work)
- Don't build NLP decomposer (overkill, fragile)
- Don't auto-submit URLs (non-generalizable heuristic)
- Don't execute queued actions without state validation
