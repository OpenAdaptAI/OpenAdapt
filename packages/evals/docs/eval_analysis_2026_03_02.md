# Eval Analysis: Demo-Conditioned vs Zero-Shot (2026-03-02)

## Task

**ID**: `04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS` (LibreOffice Calc)
**Instruction**: "In a new sheet with 4 headers 'Year', 'CA changes', 'FA changes', and 'OA changes', calculate the annual changes for the Current Assets, Fixed Assets, and Other Assets columns. Set the results as percentage type."
**Complexity**: 21 steps in human recording; requires sheet creation, header entry, formula computation, drag-fill, percentage formatting.

## Bugs Fixed This Session

| Bug | Root Cause | Fix | Status |
|-----|-----------|-----|--------|
| Multi-line type → "unterminated string literal" | Hand-rolled `_escape_for_pyautogui()` missed `\n` | Replaced with `repr()` — Python's own escaping mechanism. Eliminated entire class of string-embedding bugs. | **Verified working** (0 errors in both runs) |
| Drag coordinates zeroed to (0,0) | `startCoordinate`/`endCoordinate` (camelCase) vs Claude API's `start_coordinate`/`coordinate` (snake_case) | Fixed field names in `_map_action()` | **Verified** (correct coords in trace) |
| Demo not persisted across steps | Demo only injected at step 1 | Re-inject demo text in every `tool_result` message | **Re-applied** |
| (0,0) coordinates trigger fail-safe | No validation at coordinate boundary | `_clamp_coord()` moves (0,0) → (eps, eps) | **Added** |
| Fail-safe not detected on HTTP 500 | Only checked 200 response bodies | Check ALL response bodies for fail-safe strings | **Verified** (0 fail-safe crashes) |

### Meta-fix: `repr()` replaces manual escaping

The multi-line type bug was a symptom of a deeper architectural problem: **generating Python source code via string concatenation to send data across a boundary**. This is the same class of vulnerability as SQL injection.

```python
# Before (fragile — misses \n, \0, unicode, etc.):
text.replace("\\", "\\\\").replace("'", "\\'").replace("\t", "\\t")

# After (provably correct — Python's own escaping):
repr(text)
```

`repr()` handles ALL characters: newlines, tabs, quotes, backslashes, unicode, null bytes. The manual `_escape_for_pyautogui` function was deleted entirely.

## Results

### Run Configuration

- **Agent**: ClaudeComputerUseAgent (claude-sonnet-4-6, computer_use beta)
- **Max steps**: 30
- **Demo file**: `demo_prompts_vlm/04d9aeaf-...txt` (8,697 bytes, 21 steps, VLM-enriched)
- **Demo format**: Step N → {Observation, Intent, Action, Result}
- **WAA server**: Azure VM (waa-pool-00), SSH tunnel localhost:5001

### Scores

| Metric | ZS (no demo) | DC-rigid (old format) | DC-multilevel (new format) |
|--------|-------------|----------------------|---------------------------|
| **Score** | 0.0 | 0.0 | 0.0 |
| **Steps used** | 30/30 | 16/30 (quit early) | 11/30 (quit early) |
| **Time** | 20 min | 8 min | 13 min |
| **Headers entered** | No | No | Yes (all 4 in 1 step) |
| **Years entered** | No | No | Yes (2015-2019 in 1 step) |
| **Formulas entered** | 10 (cols C + D) | 0 | 1 (col B) |
| **Drag-fill used** | No | No | Yes (B2:B5) |
| **Multi-line type errors** | 0 | 0 | 0 |
| **Fail-safe crashes** | 0 | 0 | 0 |
| **Behavior** | Productive but unfocused | Confused, opened file dialog, quit | Followed plan: headers → years → formula → drag-fill, quit too early |

### ZS Trace (30 steps)

```
 0-2:  Navigate spreadsheet (clicks)
 3:    Click sheet tab area
 4-6:  Attempt to add new sheet (triple click on tab)
 7-8:  Dialog interaction (double-clicks)
 9-11: Navigate/dismiss dialog (clicks + wait actions)
12-13: Escape + Enter (dismiss dialog)
14:    Wait (5 internal retries) then click
15-16: Navigate to sheet tabs
17:    Click cell for formulas
18:    TYPE 5 formulas for col C (with \n between each) ← MULTI-LINE SUCCESS
19:    Click next column
20:    TYPE 5 formulas for col D (with \n between each) ← MULTI-LINE SUCCESS
21-25: Navigate/select cells (formatting attempts)
26:    Click Name Box
27:    TYPE "B2:D6\n" (cell range selection)
28:    Click toolbar (formatting?)
29:    Ctrl+S (save)
```

**Observations**: The ZS agent independently figured out the formula pattern `=(Sheet1.Cn-Sheet1.Cn-1)/Sheet1.Cn-1`, entered ALL formulas for TWO columns in just 2 steps (thanks to multi-line type fix), then attempted formatting. It used all 30 steps productively but didn't complete all 3 formula columns or percentage formatting.

### DC Trace (16 steps → quit)

```
 0-2:  Navigate spreadsheet (same clicks as ZS)
 3:    Double-click (different target than ZS — demo influence?)
 4-5:  Click dialog elements
 6-8:  Navigate/dismiss
 9-10: Escape + Enter
11-13: Click toolbar area (Open file?)
14:    TYPE "SmallBalanceSheet.xlsx"
15:    Enter
16:    DONE (no_tool_use — agent declared task complete)
```

**Observations**: The DC-rigid agent never created headers, never typed formulas, never reached the actual task. It appeared to open a "Save As" or "Open" dialog, type the source file name, and declare itself done. The demo's specific UI state descriptions conflicted with what the agent actually saw, causing confusion.

### DC-Multilevel Trace (11 steps → quit)

```
 0-1:  Click sheet tab area (navigating to add new sheet)
 2:    TYPE "Year\tCA changes\tFA changes\tOA changes\n" ← ALL 4 HEADERS IN 1 STEP
 3:    Click cell A2
 4:    TYPE "2015\n2016\n2017\n2018\n2019\n" ← ALL 5 YEARS IN 1 STEP
 5:    Click cell B2
 6:    TYPE "=(Sheet1.B3-Sheet1.B2)/Sheet1.B2" ← CORRECT FORMULA
 7:    Enter (confirm formula)
 8:    Click B2 (select for drag-fill)
 9:    DRAG B2 → B5 (fill handle) ← CORRECT DRAG-FILL
10:    Click B2 (verify?)
11:    Screenshot → DONE (declared task complete too early)
```

**Observations**: The DC-multilevel agent followed the PLAN structure precisely: headers → years → formula → drag-fill. It entered all 4 headers and all 5 years efficiently (1 step each), typed the correct formula, and attempted drag-fill. However, it quit after completing only column B (CA changes), leaving columns C and D undone. The early quit suggests the agent didn't check its progress against the full PLAN before declaring done.

**Key improvement over DC-rigid**: The multi-level format eliminated the observation mismatch problem entirely. The agent never got confused by UI state descriptions because there were none — it followed the goal-oriented PLAN + Think/Action/Expect trajectory instead.

**Remaining issue**: Premature task completion. The agent needs stronger "check ALL plan steps before declaring done" framing, or the runner should detect incomplete plans and prompt continuation.

## Analysis: Why the Rigid Demo Hurt

### The demo format problem

Our demo uses a rigid step-by-step format:
```
Step 1:
  Observation: The spreadsheet is open to "Sheet1," which contains financial data...
  Intent: To create a new sheet for calculating and displaying annual changes...
  Action: Right-click on the "Sheet1" tab at the bottom and select "Insert Sheet"...
  Result: A new, blank sheet named "Sheet2" is added to the workbook...
```

When the actual UI doesn't match the described observation (e.g., a dialog appeared, or the tab area looks different from what was described), the agent faces a **reconciliation conflict**: should it follow the demo's specific actions, or respond to what it actually sees?

In our case, the agent chose a third option: it abandoned the task structure entirely and performed an unrelated action (opening a file), then declared done.

### Literature context

This matches findings from multiple papers:

1. **LMAct** (ICML 2025, Google DeepMind): Found that demonstrations can *actively hurt* performance. On several tasks, performance *decreased* with >2 demos. "Frontier LMs struggle to leverage large demonstration datasets for interactive decision-making."

2. **DigiRL** (NeurIPS 2024): "Training with static demonstrations falls short for controlling real GUIs due to their failure to deal with real-world stochasticity and non-stationarity not captured in static observational data." SFT on demos: 17.7%. RL: 67.2%.

3. **ShowUI-Aloha** (Jan 2026): Demonstrated that a single demo CAN improve performance dramatically (+26.6pp) — but using a {Observation, **Think**, Action, Expectation} format that includes reasoning, and crucially, with a PlannerMemory module that adapts the plan when the environment diverges.

4. **Plan-and-Act** (ICML 2025): Dynamic replanning alone added 10.31pp. Without it, static plans degrade. Full pipeline improved from 9.85% (direct prediction) to 57.58% — a 6x improvement.

5. **Instruction Agent** (Sep 2025, Microsoft): Achieved 60% success on tasks where ALL other agents scored 0% — using a single expert trajectory with step-by-step natural language instructions PLUS a backtracker module for error recovery.

## Implications and Options

### The design space

The literature reveals a clear spectrum from rigid to flexible demo conditioning:

```
RIGID ←────────────────────────────────────────────────→ FLEXIBLE

Raw action   Step-by-step   Semantic steps   Abstract plan   Goal only
replay       with states    with intent      with subgoals
             (OUR CURRENT)  (ShowUI-Aloha)   (Plan-and-Act)
```

Our current format sits near the rigid end. The evidence strongly suggests moving rightward.

### Option A: Abstract the demo format (semantic steps with intent)

Transform demos from specific state descriptions to goal-oriented step summaries:

```
# Current (too rigid — describes specific UI states):
Step 11:
  Observation: The new sheet contains headers... with all cells below empty.
  Intent: To calculate the annual percentage change...
  Action: Click cell B2 and type "=(Sheet1.B3-Sheet1.B2)/Sheet1.B2".
  Result: Cell B2 now contains a formula...

# Proposed (more abstract — describes what to do, not what you see):
Step 4: Enter the annual change formula for Current Assets
  Goal: Populate the CA changes column with formulas that compute
        (current_year - prev_year) / prev_year for each year pair.
  Approach: In each row of column B, enter a formula referencing the
           corresponding rows in Sheet1's column B.
  Example: =(Sheet1.B3-Sheet1.B2)/Sheet1.B2
```

**Tradeoffs**:
- (+) Robust to UI state mismatch — doesn't assume specific screen appearance
- (+) Preserves intent and approach, which is what disambiguates
- (-) Loses grounding — agent must figure out WHERE to click
- (-) Harder to auto-generate from recordings

### Option B: Plan-then-act (hierarchical, inspired by Plan-and-Act)

Extract a high-level plan from the demo, let the agent execute it:

```
PLAN (derived from demonstration):
1. Create a new sheet in the workbook
2. Set up headers: Year, CA changes, FA changes, OA changes
3. Enter years 2016-2019 in column A
4. For each asset column (CA=B, FA=C, OA=D):
   a. Enter formula =(Sheet1.Xn-Sheet1.Xn-1)/Sheet1.Xn-1 for each year
   b. Fill down for all years
5. Select the data range and format as percentage

Execute each step using your best judgment about the current screen state.
If a step doesn't apply to what you see, skip it and move to the next.
```

**Tradeoffs**:
- (+) Maximum flexibility — agent adapts to any UI state
- (+) Natural mismatch recovery (skip/replan)
- (+) Captures the "what" without prescribing the "how"
- (-) Loses the fine-grained disambiguation that is OpenAdapt's core thesis
- (-) Similar to what any planning agent could derive zero-shot

### Option C: Adaptive conditioning with mismatch detection

Keep step-by-step demos but add explicit mismatch handling:

```
DEMONSTRATION (adapt as needed — your screen may look different):

Step 1: Create a new sheet
  If you see a sheet tab bar → right-click and insert new sheet
  If you see a dialog → dismiss it first, then insert sheet
  If a new sheet already exists → use it

Step 2: Enter headers in row 1
  Type "Year" in A1, then Tab, type "CA changes", Tab, "FA changes", Tab, "OA changes"
  If headers already exist → verify them and move on
```

**Tradeoffs**:
- (+) Preserves fine-grained demo detail (the disambiguation signal)
- (+) Handles the mismatch problem explicitly
- (-) Verbose — context window cost is high
- (-) Hard to auto-generate the "If..." branches

### Option D: Multi-level conditioning (most aligned with literature)

Combine a high-level plan WITH a reference trajectory, inspired by ShowUI-Aloha + Instruction Agent:

```
GOAL: Calculate annual asset changes in a new spreadsheet sheet.

PLAN:
1. Create new sheet → 2. Headers → 3. Years → 4. Formulas → 5. Format as %

REFERENCE TRAJECTORY (for disambiguation — adapt actions to your actual screen):
Step 1: [Think] I need to create a new sheet. I'll right-click the sheet tab.
        [Action] Right-click "Sheet1" tab → select "Insert Sheet"
        [Expect] New blank sheet appears
Step 2: [Think] Now I'll set up the four headers.
        [Action] Type "Year" → Tab → "CA changes" → Tab → "FA changes" → Tab → "OA changes"
        [Expect] Row 1 has all four headers
...

If your screen doesn't match what's expected, re-evaluate based on the PLAN and decide the best next action.
```

**Tradeoffs**:
- (+) Combines the benefits of planning AND trajectory disambiguation
- (+) [Think] field provides reasoning that helps the model understand WHY each action is taken
- (+) Explicit "re-evaluate" instruction for mismatch recovery
- (+) Aligns with ShowUI-Aloha format that showed +26.6pp improvement
- (-) Most complex format to generate
- (-) Longest context window usage

### Option E: RL fine-tuning (long-term, highest ceiling)

DigiRL showed 17.7% → 67.2% improvement by moving from SFT on demos to online RL. WebRL showed 4.8% → 42.4%. The trajectory data becomes training signal rather than inference-time context.

**Tradeoffs**:
- (+) Highest performance ceiling by far
- (+) No context window cost at inference time
- (+) Handles stochasticity naturally
- (-) Requires fine-tuning infrastructure (already have via Modal)
- (-) Task-specific training needed
- (-) This is OpenAdapt-ML's domain, not just eval infrastructure

## Recommendation

### Immediate (next eval): Option D (multi-level conditioning)

The evidence from ShowUI-Aloha, Instruction Agent, and Plan-and-Act converges on this approach. Key changes:

1. **Add a PLAN section** above the step-by-step trajectory — gives the agent a fallback when specific steps don't match
2. **Add [Think] fields** to each step — captures reasoning that helps the model adapt
3. **Add [Expect] fields** — lets the agent detect when reality diverges from the demo
4. **Add explicit "adapt if needed" framing** — grants permission to deviate from the demo

This can be implemented as a transformation of our existing VLM-enriched demos (add plan extraction + think field generation via a single LLM call).

### Medium-term: Option A + retrieval

Abstract the demo format to goal-oriented steps. Build a retrieval system that finds the most relevant demo for the current task. This is the LearnAct approach.

### Long-term: Option E (RL)

Use trajectory data for training, not just inference-time conditioning. This has the highest performance ceiling but requires infrastructure investment.

## Key Insight for OpenAdapt

OpenAdapt's core thesis is trajectory-conditioned disambiguation — using demonstrations to help agents understand WHAT to do in ambiguous situations. The evidence says this thesis is correct (ShowUI-Aloha: +26.6pp, Instruction Agent: 0% → 60%), BUT:

1. **The demo must be abstracted**, not a literal replay
2. **The agent needs permission and ability to deviate** when reality doesn't match
3. **Reasoning (Think/Intent) is the disambiguation signal**, not specific observations
4. **A high-level plan provides fallback** when step-level details don't apply

The DC agent didn't fail because demo-conditioning is wrong. It failed because our demo format is too rigid and doesn't handle observation mismatch. This is a solvable formatting problem, not a fundamental limitation.

## Sources

- LMAct (ICML 2025): arxiv.org/abs/2412.01441
- ShowUI-Aloha (Jan 2026): arxiv.org/abs/2601.07181
- Instruction Agent (Sep 2025): arxiv.org/abs/2509.07098
- Plan-and-Act (ICML 2025): arxiv.org/abs/2503.09572
- DigiRL (NeurIPS 2024): arxiv.org/abs/2406.11896
- WebRL (ICLR 2025): arxiv.org/abs/2411.02337
- LearnAct (Apr 2025): arxiv.org/abs/2504.13805
- RAG-GUI (EMNLP 2025): arxiv.org/abs/2509.24183
- AdaptAgent (NeurIPS 2024 WS): arxiv.org/abs/2411.13451
- RT-Trajectory (ICLR 2024): arxiv.org/abs/2311.01977
- AgentTrek (ICLR 2025): arxiv.org/abs/2412.09605
- BacktrackAgent (EMNLP 2025): arxiv.org/abs/2505.20660
