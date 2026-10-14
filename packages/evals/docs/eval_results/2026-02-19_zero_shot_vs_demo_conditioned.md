# Zero-Shot vs Demo-Conditioned Evaluation Results

**Date**: 2026-02-19
**Model**: GPT-5.1 (OpenAI)
**Benchmark**: WAA (Windows Agent Arena) Live
**VM**: Azure Standard_D8ds_v4, waa-pool-00
**Max Steps**: 20 per task
**Evaluate Server**: Docker-baked flask endpoint (port 5050)

---

## Interactive Viewer

**[Open Comparison Viewer](comparison.html)** - Side-by-side screenshot replay with playback controls, click markers, and action logs.

Individual run viewers:
- [Zero-Shot Settings](../../benchmark_results/waa-live_eval_20260219_194444/viewer.html)
- [Zero-Shot Archive](../../benchmark_results/waa-live_eval_20260219_202921/viewer.html)
- [Zero-Shot Notepad](../../benchmark_results/waa-live_eval_20260219_203754/viewer.html)
- [Demo-Cond Settings](../../benchmark_results/waa-live_eval_20260219_204442/viewer.html)
- [Demo-Cond Archive](../../benchmark_results/waa-live_eval_20260219_205121/viewer.html)
- [Demo-Cond Notepad](../../benchmark_results/waa-live_eval_20260219_205910/viewer.html)

---

## Summary

| Condition | Task | Score | Steps | Time | Action Types |
|-----------|------|-------|-------|------|-------------|
| Zero-shot | Settings | 0.00 | 20 | 470s | click:20 |
| Zero-shot | Archive | 0.00 | 20 | 513s | click:20 |
| Zero-shot | Notepad | 0.00 | 20 | 407s | click:17, type:3 |
| **Demo-cond** | **Settings** | **0.00** | **20** | **398s** | **click:16, type:4** |
| **Demo-cond** | **Archive** | **0.00** | **20** | **468s** | **click:18, key:1, type:1** |
| **Demo-cond** | **Notepad** | **0.00** | **17** | **399s** | **click:13, type:3, key:1, done:1** |

All 6 evaluations scored 0.00 (binary pass/fail). However, qualitative analysis reveals significant behavioral differences.

---

## Task Details

### 1. Settings (Turn Off Notifications)

**Task ID**: `37e10fc4-b4c5-4b02-a65c-bfae8bc51d3f-wos`
**Instruction**: *"I need to 'turn off' notifications for my system in the settings."*
**Evaluator**: Registry check (`HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications` → `ToastEnabled = 0`)

#### Zero-Shot Behavior
The agent clicked randomly around the taskbar and Start menu for 20 steps. It never opened Settings or navigated to Notifications. All 20 actions were clicks, many repeating the same coordinates.

```
Step  0: click(640, 686) — taskbar area
Step  1: click(690, 710) — loop substitution
Step  2: click(690, 710) — loop substitution
Step  3: click(640, 706) — taskbar area
...
Step 19: click(103, 214) — random Settings sidebar
```

#### Demo-Conditioned Behavior
The agent searched for "Settings" via the Start menu search bar (type actions), and repeatedly tried to open it. It got stuck in a loop: open Start → type "Settings" → click search result → Settings opens → gets dismissed → repeat.

```
Step  0: click(608, 684) — Start menu
Step  1: click(469, 692) — search bar
Step  2: type("Settings") — search for Settings
Step  3: click(430, 259) — click Settings result
Step  4: click(640, 692) — Start menu again
...
```

The demo guided the agent to search for Settings (matching the demo's approach), but it couldn't navigate within Settings to Notifications.

#### Demo Prompt (5 steps)
1. Click Start menu
2. Click Settings icon in pinned apps
3. Click System in sidebar
4. Click Notifications
5. Toggle notifications off (annotated as `TYPE("")` — problematic)

**Issue**: Demo Step 5 has `ACTION: TYPE("")` — an empty type action. The actual action needed is a click on the toggle switch. This annotation error may confuse the agent about how to perform the final step.

---

### 2. Archive (Create Folder & Move .docx Files)

**Task ID**: `0c9dda13-428c-492b-900b-f48562111f93-WOS`
**Instruction**: *"Create a new folder named 'Archive' in the Documents folder and move all .docx files into it."*
**Evaluator**: Python command checks if Archive folder exists and contains .docx files

#### Zero-Shot Behavior
The agent clicked around the taskbar and what appears to be a Start menu/File Explorer area, but never successfully opened File Explorer or navigated to Documents. All 20 actions were clicks, with many repeated coordinates indicating it was stuck.

```
Step  0: click(603, 683) — taskbar
Step  1: click(580, 540) — Start menu area
Step  2: click(607, 683) — taskbar
...
Step 19: click(412, 514) — Start menu area
```

#### Demo-Conditioned Behavior
The agent started with `type("e")` and `press("enter")` — matching the demo's approach of using keyboard shortcut to open File Explorer. It then clicked in the File Explorer area, but got stuck in a repetitive loop clicking between two coordinates.

```
Step  0: type("e") — keyboard shortcut for File Explorer
Step  1: key(enter) — confirm
Step  2: click(777, 458) — File Explorer area
Step  3: click(632, 483)
Step  4: click(777, 458) — stuck in loop
...
Step 19: click(640, 458)
```

The demo's first step (type "e" to open File Explorer) was correctly reproduced, but the agent couldn't navigate within File Explorer to create the folder and move files.

#### Demo Prompt (9 steps)
1. TYPE("e") to open File Explorer
2. Click Documents folder
3. Right-click to create new folder
4. TYPE("Archive")
5. Select meeting_notes file
6. Select proposal file
7. Select report file
8. Double-click Archive folder
9. Paste files (Ctrl+V)

---

### 3. Notepad (Create and Save File) ★ Best Result

**Task ID**: `366de66e-cbae-4d72-b042-26390db2b145-WOS`
**Instruction**: *"Please open Notepad, create a new file named 'draft.txt', type 'This is a draft.', and save it to the Documents folder."*
**Evaluator**: Check file exists at `C:\Users\Docker\Documents\draft.txt`, compare content to gold standard

#### Zero-Shot Behavior
The agent clicked around the Start menu, eventually typed "notepad" (step 6) to search, but never successfully opened the Notepad app. It repeated the same search→click→search pattern without progressing.

```
Step  0: click(640, 688) — taskbar
...
Step  6: type("Notepad") — searched for Notepad
Step  7: click(560, 246) — clicked something
Step  8: click(548, 86)  — clicked elsewhere
Step  9: click(560, 246)
...
Step 16: type("notepad") — searched again
Step 17: click(560, 246) — same pattern
```

Screenshots show: Start menu open with suggestions (step 6), "notepad" in recent searches (step 12), blank desktop (step 19). Never opened Notepad.

#### Demo-Conditioned Behavior ★
This was the most coherent execution across all 6 runs. The agent followed a near-perfect task sequence:

```
Step  0: click(512, 690)           — Click Start/taskbar
Step  1: type("notepad")           — Search for Notepad
Step  2: click(640, 246)           — Click Notepad in results
Step  3: click(552, 92)            — Click in Notepad menu area
Step  4: click(640, 246)           — Navigate in Notepad
Step  5: click(427, 92)            — Click menu item
Step  6: click(640, 690)           — Click in content area
Step  7: click(430, 690)           — Click in content area
Step  8: click(624, 332)           — Click in content area
Step  9: click(640, 690)           — More navigation
Step 10: click(263, 411)           — Click in text editing area
Step 11: type("This is a draft.")  — ★ Type correct content
Step 12: hotkey('ctrl', 's')       — ★ Ctrl+S to save
Step 13: click(184, 150)           — Navigate Save As dialog
Step 14: click(384, 620)           — Click in filename field area
Step 15: type("draft.txt")         — ★ Type correct filename
Step 16: click(640, 650)           — Click (near Save button area)
Step 17: DONE                      — Signal task completion
```

**Screenshot Evidence**:

| Step | Screenshot | Description |
|------|-----------|-------------|
| 12 | `step_012.png` | Notepad open with "This is a draft." typed correctly |
| 15 | `step_015.png` | Save As dialog, Documents folder, filename "This is a draft..txt" (auto-populated) |
| 17 | `step_017.png` | Save As dialog, filename corrected to "draft.txt", Documents folder selected |

**Why it failed**: The agent signaled DONE at step 17 **without clicking the Save button**. The Save As dialog was still open when the agent declared task completion. One additional click on the "Save" button would have saved the file and likely passed the evaluation.

#### Demo Prompt (6 steps)
1. TYPE("notepad") to search
2. TYPE("Thisisadraft.") — note: the annotation compressed the content
3. CLICK on save area
4. TYPE("draft.txt")
5. Confirm save (click Yes)
6. TYPE("") — return to PowerShell

---

## Analysis

### Action Diversity

| Metric | Zero-Shot | Demo-Conditioned |
|--------|-----------|-----------------|
| Unique action types | 2 (click, type) | 4 (click, type, key, done) |
| Type actions | 6 / 60 (10%) | 8 / 57 (14%) |
| Key/hotkey actions | 0 / 60 (0%) | 2 / 57 (3.5%) |
| Done signals | 0 / 60 (0%) | 1 / 57 (1.8%) |
| Mean steps | 20.0 | 19.0 |
| Mean time | 463s | 422s |

### Behavioral Patterns

**Zero-shot agents**:
- Default to repetitive clicking on taskbar/Start menu area
- Occasionally discover search (notepad task) but can't follow through
- Get trapped in action loops (same coordinates repeated)
- Never signal task completion (always hit max steps)

**Demo-conditioned agents**:
- Reproduce first steps of demo (search, keyboard shortcuts)
- Use diverse action types matching the demo
- Navigate further into the task but struggle with precise interactions
- One agent (notepad) correctly executed the full task sequence minus the final save click

### Environment Issues

1. **OneDrive popup**: "Turn On Windows Backup" notification appears in virtually every screenshot, partially obscuring the desktop and potentially intercepting clicks
2. **Action loop detection**: When the same click is repeated 3+ times, coordinates are offset by (50, 24) — this may redirect the agent to unintended UI elements
3. **Resolution**: VM configured for 1440x900, screenshots confirm this resolution
4. **Windows first-boot state**: Fresh Windows 11 with OOBE notifications and setup prompts that wouldn't appear on a pre-configured system

### Demo Annotation Quality Issues

1. **Settings Step 5**: `ACTION: TYPE("")` — empty type action instead of a click on the toggle. The evaluator expects a registry change that only a toggle click produces.
2. **Archive Steps 5, 7, 9**: `ACTION: TYPE("")` — empty type actions used for file selection and paste operations. These should be Ctrl+click, Ctrl+A, or Ctrl+V.
3. **Notepad Step 2**: `ACTION: TYPE("Thisisadraft.")` — content compressed without spaces. The correct content has spaces: "This is a draft."

These annotation issues suggest the VLM annotator (GPT-4o) sometimes outputs empty or incorrect actions, particularly for toggle/checkbox interactions and keyboard shortcuts.

---

## Recommendations

### Short-term (next eval run)
1. **Dismiss OneDrive popup** in Windows setup (add registry key or scheduled task to suppress)
2. **Fix demo annotations**: Replace `TYPE("")` with correct actions (CLICK for toggles, HOTKEY for Ctrl+V)
3. **Increase max steps to 30**: The notepad agent was close but may have succeeded with a few more steps

### Medium-term
1. **Pre-configure Windows**: Skip OOBE, disable OneDrive backup prompts, set up a "clean" baseline state
2. **Improve action loop detection**: Instead of arbitrary coordinate offset, try alternative actions (scroll, escape, right-click)
3. **Add partial credit scoring**: Binary 0/1 misses the qualitative difference between random clicking and near-success

### Long-term
1. **Train on successful traces**: Use trace_export.py to build training data from any successful runs
2. **Multi-attempt evaluation**: Run each task 3-5 times to account for stochasticity
3. **Grounded actions**: Connect to accessibility tree for more reliable element targeting

---

## Raw Data Locations

```
benchmark_results/
├── waa-live_eval_20260219_194444/   # Zero-shot settings
├── waa-live_eval_20260219_202921/   # Zero-shot archive
├── waa-live_eval_20260219_203754/   # Zero-shot notepad
├── waa-live_eval_20260219_204442/   # Demo-cond settings
├── waa-live_eval_20260219_205121/   # Demo-cond archive
├── waa-live_eval_20260219_205910/   # Demo-cond notepad
│   ├── metadata.json
│   ├── tasks/{task_id}/
│   │   ├── execution.json
│   │   ├── task.json
│   │   └── screenshots/step_*.png
│   └── viewer.html                  # Individual interactive viewer
│
docs/eval_results/
├── comparison.html                  # Side-by-side comparison viewer
└── 2026-02-19_zero_shot_vs_demo_conditioned.md  # This document

demo_prompts/
├── 37e10fc4-b4c5-4b02-a65c-bfae8bc51d3f-wos.txt  # Settings (5 steps)
├── 0c9dda13-428c-492b-900b-f48562111f93-WOS.txt   # Archive (9 steps)
└── 366de66e-cbae-4d72-b042-26390db2b145-WOS.txt   # Notepad (6 steps)
```
