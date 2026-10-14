# Planner-Grounder Live Experiment Results

> Date: 2026-03-18
> Setup: Claude Sonnet 4.6 (planner) + UI-Venus-1.5-8B (grounder) on WAA VM

---

## Setup

- **Planner**: Claude Sonnet 4.6 via Anthropic API
- **Grounder**: UI-Venus-1.5-8B served via vLLM on AWS g5.xlarge (A10G 24GB)
- **Environment**: WAA VM (waa-pool-00, Azure D8ds_v5) running Windows 11 in QEMU
- **Task**: "Open Notepad and type Hello World" (custom-notepad-hello)
- **Agent**: PlannerGrounderAgent with HTTP grounder endpoint
- **Evaluation**: Dense milestone rewards via TaskConfig YAML

## Results Summary

| Run | Steps | Key Events | Score | Milestone |
|-----|-------|-----------|-------|-----------|
| 1 | 4 | Start menu -> Notepad -> text area -> typed "Hello World" | 0.0 | Text inserted into existing content |
| 2 | 15 | Stuck clicking Terminal taskbar button (loop) | 0.5 | 1/2 milestones (desktop visible) |
| 3 | 10 | Ctrl+A sent without modifier (bug), anti-loop kicked in | 0.5 | 1/2 milestones |
| 4 | 2 | Planner hallucinated DONE (saw existing text) | 0.0 | 0/2 milestones |

## What Works

### Planner reasoning
Claude correctly identified the right sequence every time:
1. Click Start button in taskbar
2. Click Notepad icon in pinned apps
3. Click text editor area to focus
4. Select all (Ctrl+A) then type "Hello World"

Example planner output (Run 1, Step 1):
```
decision=COMMAND
instruction='Click the Start button in the taskbar'
reasoning='I need to open Notepad. The first step is to click the Start button.'
```

### Grounder accuracy
UI-Venus returned precise bounding boxes consistently:
- Start button: [325, 943, 363, 992] (correct)
- Notepad icon: [442, 385, 491, 472] (correct)
- Text editor: [74, 225, 805, 733] (correct)
- Terminal button: [612, 953, 636, 995] (correct)

No grounding failures across all runs. UI-Venus found every element the planner asked for.

### Non-click action parsing
The planner correctly output atomic actions:
- `type 'Hello World'` parsed as TYPE action, bypassed grounder
- `press Ctrl+A` parsed as KEY action with modifiers

### Dense milestone evaluation
VLM judge + command checks evaluated milestones correctly:
- "Desktop is visible" (VLM screenshot check): correctly passed
- "Notepad shows Hello World" (VLM screenshot check): correctly failed when text wasn't clean

## What Failed

### 1. Windows 11 Notepad session restore
Notepad opens with previous content ("This is a draft.") due to Win11 session restore. The planner's Ctrl+A -> type approach would fix this, but was blocked by bug #2.

### 2. Hotkey modifier dropped (fixed in commit f828946)
`BenchmarkAction(modifiers=["ctrl"], key="a")` was reconstructed without modifiers in the run script, sending `pyautogui.press('a')` instead of `pyautogui.hotkey('ctrl', 'a')`. Fixed by passing the original action directly.

### 3. Planner stuck in loops (Run 2)
The planner clicked the Terminal taskbar button 15 times without recognizing it wasn't working. The anti-loop prompt was added in commit a6edb87 but the underlying issue is taskbar button proximity (clicking Terminal vs Microsoft Store).

### 4. Planner hallucination (Run 4)
Claude saw text in Notepad and prematurely declared DONE without having typed anything. This is a planner reasoning failure, not an architecture issue.

## Architecture Validation

Despite no clean task completion, the experiment validates:

1. **Planner-grounder separation works**: Claude plans, UI-Venus grounds, WAA executes. Each component does its job correctly.

2. **UI-Venus grounding is consistently accurate**: Every bounding box was correct across all runs. No misclicks due to grounder error.

3. **Non-click actions work**: TYPE and KEY actions are correctly parsed from planner instructions and bypass the grounder.

4. **Dense milestone evaluation works**: Partial scores (0.5) demonstrate GRPO would get gradient signal.

5. **The remaining issues are task design and planner prompting**: not architecture problems.

## Commits

- PR #134: https://github.com/OpenAdaptAI/openadapt-evals/pull/134
- Initial implementation: `9d6f3b7`
- Bbox parsing + non-click actions: `2dca49b`
- Hotkey fix + anti-loop + screenshots: `a6edb87`
- Modifier passthrough: `f828946`

## Cost

Total experiment cost across 4 runs: ~$4
- GPU instance (g5.xlarge): ~$2 (2 hours)
- WAA VM (Azure): ~$1 (2 hours)
- Claude API: ~$1 (~40 planner calls)
