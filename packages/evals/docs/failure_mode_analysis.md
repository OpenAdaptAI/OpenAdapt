# Strategic Analysis: Systematic Failure Mode Recording and Training

**Date**: 2026-03-03
**Context**: DC eval on task `04d9aeaf` (LibreOffice Calc, 21 steps). ZS scored 0/1 (stuck after 1 step), DC scored 0/1 (completed 1/3 columns before step budget). DemoController + VLM verification architecture is operational but 0% success on harder tasks.

---

## 1. Failure Mode Taxonomy

Observed failures fall into four distinct categories. The boundaries matter because they determine whether the fix is deterministic (engineering) or statistical (training).

### Category A: Environment/Infrastructure Failures

These are bugs in the execution substrate. The agent's intent is correct, but the infrastructure prevents execution.

| ID | Failure Mode | Example | Fix Type |
|----|-------------|---------|----------|
| A1 | PyAutoGUI fail-safe trigger | Drag to (0,0) moves mouse to corner, all subsequent actions fail | Deterministic (coordinate clamping, done) |
| A2 | Multi-line type command | `pyautogui.write()` with `\n` causes "unterminated string literal" | Deterministic (newline splitting, done) |
| A3 | Document Recovery dialog | Previous QEMU crash leaves modal dialog blocking all interaction | Deterministic (pre-task dialog dismissal) |
| A4 | WAA server timeout/disconnect | Flask server drops connection mid-action | Deterministic (retry with backoff) |
| A5 | Task setup state mismatch | After `/setup`, target app is not visible or focused | Deterministic (post-setup app focus script) |

**Current status**: A1, A2 fixed in PR #83. A3-A5 are known but unfixed. These failures are fully eliminable with engineering effort -- no training needed.

### Category B: Agent Planning/Reasoning Failures

The agent misunderstands what to do, given the current screen state and the task/demo context. This is the model's "thinking" going wrong.

| ID | Failure Mode | Example | Fix Type |
|----|-------------|---------|----------|
| B1 | Premature task completion | Agent declares "done" after completing 1 of 3 columns | Training or prompt engineering |
| B2 | Step budget exhaustion via over-planning | Controller's retry+replan cycle consumes steps without progress | Hybrid (tune controller params + train for first-attempt accuracy) |
| B3 | Context leakage | Agent types years instead of formulas because previous step's context bleeds in | Training (attention pattern issue) |
| B4 | Demo format rigidity | DC agent abandons task when UI state doesn't match demo's observations | Prompt engineering (multi-level format, done conceptually) |
| B5 | Wrong strategy selection | Agent attempts batch multi-line entry instead of cell-by-cell | Training or few-shot examples |

**Key insight**: The DemoController already handles B1 (overrides premature "done" when steps remain, line 320-333 of `demo_controller.py`). But the underlying model tendency persists, and when the controller overrides it, the agent's subsequent actions may lack coherent intent.

### Category C: Agent Grounding/Perception Failures

The agent correctly understands what to do but misidentifies where to do it on screen. This is the VLM's spatial reasoning failing.

| ID | Failure Mode | Example | Fix Type |
|----|-------------|---------|----------|
| C1 | Wrong click target | Clicks sheet tab bar instead of cell B2 | Training (grounding accuracy) |
| C2 | Coordinate imprecision | Clicks 15px below target, hitting wrong row | Training or action validation layer |
| C3 | Drag target confusion | Drag-fill handle identified incorrectly, drags wrong cells | Training + deterministic fallback (mouseDown/moveTo/mouseUp) |
| C4 | UI element misidentification | Confuses toolbar icons, clicks wrong menu item | Training (VLM visual grounding) |

**These are the highest-value training targets.** Grounding accuracy directly determines whether correct reasoning translates to correct execution. Unlike planning failures (which can be mitigated by prompt engineering or controller logic), grounding failures require the model to learn better spatial correspondence between visual input and coordinate output.

### Category D: Verifier Failures

The VLM verifier (plan_verify.py) incorrectly assesses step or goal completion.

| ID | Failure Mode | Example | Fix Type |
|----|-------------|---------|----------|
| D1 | False negative on step verification | Verifier rejects correct action ("LibreOffice not Excel") | Prompt engineering (outcome-focused prompts, partially done) |
| D2 | False positive on step verification | Verifier accepts incorrect action, allowing controller to advance prematurely | Prompt engineering or verifier fine-tuning |
| D3 | Application confusion | Verifier judges based on wrong application semantics | Prompt engineering (application-agnostic outcome verification) |
| D4 | Partial verification inconsistency | "partially_verified" treated as success sometimes causes premature advancement | Controller logic (tuning `effectively_verified` threshold) |

**Current mitigation**: The `plan_verify.py` prompts have already been significantly refined with outcome-focused rules (lines 99-139), decision guides, and explicit instructions to ignore incidental details. Further prompt work has diminishing returns -- the remaining verifier failures likely require either switching to a more capable verification model or fine-tuning the verifier.

---

## 2. Recording/Capture System Design

### What Already Exists

The codebase has substantial data collection infrastructure that is already operational:

- **`ExecutionTraceCollector`** (`data_collection.py`): Records step-level traces with screenshots, actions, reasoning, agent logs, and task-level execution logs. Creates structured directories with `execution.json` per task.
- **`TraceExporter`** (`trace_export.py`): Converts benchmark traces to `openadapt-ml` Episode format with normalized coordinates, screenshots, and JSONL training samples.
- **`TaskLogHandler`**: Captures all Python logging during task execution with relative timestamps.
- **Agent logs**: `ApiAgent._last_step_logs` records LLM response text, parse strategy, timing, and token usage per step.

### What's Missing for Failure Analysis

The existing infrastructure records *what happened* but not *why it failed* or *what should have happened instead*. A failure-aware recording system needs three additional dimensions:

**1. Per-step failure classification** -- Extend `ExecutionStep` in `data_collection.py` to include:

```python
@dataclass
class ExecutionStep:
    # ... existing fields ...
    failure_type: str | None = None        # A1-A5, B1-B5, C1-C4, D1-D4
    failure_severity: str | None = None    # "blocking", "recoverable", "cosmetic"
    expected_outcome: str | None = None    # What should have happened
    actual_outcome: str | None = None      # What actually happened
    recovery_action: str | None = None     # What the correct recovery would be
```

**2. Verification result recording** -- The DemoController already produces `VerificationResult` objects at each step (line 373-406 of `demo_controller.py`), but these are only logged, not persisted in the execution trace. Routing verification results into `ExecutionTraceCollector.record_step()` would capture the verifier's reasoning, confidence scores, and raw VLM responses alongside the agent's actions.

**3. Paired failure/recovery trajectories** -- When the controller retries or replans, the failed attempt and its recovery are currently treated as independent steps. Grouping them into explicit (failure, recovery) pairs would create natural training data for DPO or contrastive learning.

### Proposed Recording Format

```json
{
  "step_idx": 7,
  "screenshot_path": "screenshots/step_007.png",
  "action": {"type": "click", "x": 245, "y": 89, ...},
  "reasoning": "Click on cell B2 to start entering formulas",
  "verification": {
    "status": "not_verified",
    "confidence": 0.85,
    "explanation": "Clicked on sheet tab bar, not cell B2"
  },
  "failure": {
    "type": "C1",
    "category": "grounding",
    "severity": "recoverable",
    "expected_outcome": "Cell B2 is selected and ready for input",
    "actual_outcome": "Sheet tab changed, cell selection lost",
    "correct_action": {"type": "click", "x": 245, "y": 312, ...},
    "correct_action_source": "human_annotation"
  }
}
```

The `correct_action` and `correct_action_source` fields are the key addition. They can be populated three ways: (1) human annotation during review, (2) post-hoc inference from the agent's eventual successful recovery, (3) VLM-based analysis of the screenshot to determine the correct action.

### Storage Recommendation

JSON files within the existing `benchmark_results/` directory structure are sufficient for the current scale (dozens of eval runs, not thousands). The `ExecutionTraceCollector` already handles directory creation, screenshot management, and summary generation. Adding failure metadata requires only extending the existing `record_step()` interface, not building new infrastructure.

If scale increases to thousands of failure examples, migration to SQLite or Parquet is straightforward: the JSON structure maps directly to a flat table with one row per step.

---

## 3. Viability of Training on Failures

### Can failure data become useful training signal?

Yes, but the approach matters enormously. Three viable training strategies, in order of expected impact:

**Strategy 1: SFT on corrected trajectories (highest near-term ROI)**

Take the agent's failed trajectories, have a human or stronger model annotate the correct actions, and fine-tune on the corrected versions. This is essentially the "Instruction Agent" approach (0% to 60% on hard tasks) but applied to failure recovery.

- **Data requirement**: 50-100 corrected trajectories (each 15-30 steps)
- **Expected improvement**: 10-30pp success rate increase (extrapolating from Instruction Agent's results on similar task complexity)
- **Cost**: ~$500 in human annotation time + ~$100 in GPU compute for LoRA fine-tuning
- **Risk**: Low. SFT on correct trajectories is well-understood and does not require reward model design.

**Strategy 2: DPO on (bad action, good action) pairs (medium-term)**

For each failure step, create a preference pair: (screenshot, wrong_action, correct_action). DPO training teaches the model to prefer correct grounding without needing to define a reward function.

- **Data requirement**: 200-500 preference pairs (one per failure step, not per trajectory)
- **Expected improvement**: 5-15pp grounding accuracy improvement (primarily Category C failures)
- **Cost**: ~$300 annotation + ~$200 GPU compute
- **Risk**: Medium. DPO quality is sensitive to the quality of the preference pairs. Ambiguous cases (where both actions are plausible) can hurt training stability.

**Strategy 3: Online RL with WAA environment (highest ceiling, longest timeline)**

This is the DigiRL/WebRL approach. The verl-agent integration (PR #84) provides the infrastructure. GiGPO gives per-step credit assignment, which directly addresses the sparse reward problem (binary task success at the end of 15+ steps).

- **Data requirement**: ~1000 rollouts (each 15-30 steps) for meaningful RL signal, generating ~15,000-30,000 step-level training samples
- **Expected improvement**: 30-50pp (DigiRL achieved 17.7% to 67.2%, but their environment was simpler)
- **Cost**: ~$5,000-10,000 in GPU compute (H100 hours) + ~$5,000-15,000 in WAA VM time for rollouts
- **Risk**: High. RL training is notoriously unstable, reward hacking is possible (agent learns to trigger false-positive verifier responses), and the WAA environment adds latency that makes rollout collection slow.

### What DigiRL teaches us about training on failures

DigiRL's key insight was that SFT on offline demos plateaus at 17.7%, but online RL -- where the agent learns from its own failures in a live environment -- reaches 67.2%. The critical factor was not the training algorithm but the online data generation: the agent fails, observes the consequences of its failure, and adjusts.

This directly supports building a failure recording pipeline. But it also suggests that **static failure datasets have limited value compared to live environment interaction**. The strongest approach is: fix deterministic failures (Category A) first, then run the RL pipeline (Strategy 3) which implicitly learns from Category B and C failures. Collecting and annotating failures manually (Strategy 1) is a bridge to buy signal before the RL infrastructure is ready.

---

## 4. Prioritized Action Plan

### Tier 1: Deterministic Fixes (1-2 weeks, eliminates Category A)

These have guaranteed impact, zero risk, and require no training data.

1. **Pre-task dialog dismissal** (A3): After `/setup`, send a keystroke sequence (Escape, Alt+F4 for dialogs, then re-focus target app). The DemoController already calls `adapter.reset(task)` at line 243 -- add a post-reset cleanup step.

2. **Post-setup app focus** (A5): After task setup, use `/execute` to run `python -c "import pyautogui; pyautogui.hotkey('alt', 'tab')"` to ensure the target application is in the foreground. Alternatively, parse the task instruction to identify the target app and use `wmctrl` or equivalent to focus it.

3. **Retry with backoff for WAA timeouts** (A4): The `WAALiveAdapter.step()` method should retry HTTP failures 2-3 times with exponential backoff before raising.

**Expected impact**: Eliminates ~20% of eval failures that are pure infrastructure issues masquerading as agent failures.

### Tier 2: Controller Parameter Tuning (1 week, reduces Category B/D waste)

4. **Reduce retry/replan overhead**: Currently `max_retries=2` and `max_replans=2`, meaning a single failed step can consume up to 6 agent actions (2 retries x 1 action + 2 replans x 1 action + replan VLM calls). On a 30-step budget, this is 20% of the budget for a single step failure. Reduce to `max_retries=1, max_replans=1` and increase step budget to 40-50.

5. **Implement action validation layer**: Before sending an action to the WAA server, validate that click coordinates are within the active window region (not on taskbar, not on title bar for non-intentional clicks). This is a ~50-line coordinate sanity checker that catches Category C2 errors deterministically.

6. **Verification model upgrade**: Switch from `gpt-4.1-mini` to `gpt-4.1` for verification. The cost per verification call increases from ~$0.001 to ~$0.01, but on a 30-step task that is $0.30 vs $0.03 -- negligible relative to the $3+ agent API cost. The accuracy improvement on nuanced judgments (D1, D3) is likely significant.

**Expected impact**: 10-15% reduction in wasted steps, translating to ~10pp more tasks reaching completion within step budget.

### Tier 3: SFT on Corrected Trajectories (2-4 weeks, addresses Category B/C)

7. **Collect 50-100 failure trajectories** across 10-15 different tasks. Run each task 3-5 times to get diverse failure modes.

8. **Annotate corrections**: For each failure step, identify the correct action. This can be partially automated: if the agent eventually succeeds at a step after retrying, the successful action is the correction for the failed attempt.

9. **Fine-tune a LoRA** on corrected trajectories using the existing `trace_export.py` pipeline to generate JSONL training data.

**Expected impact**: 10-30pp success rate improvement on tasks similar to the training distribution.

### Tier 4: Online RL (1-3 months, addresses all categories statistically)

10. **Use the verl-agent/VAGEN integration** (PR #84) with GiGPO for per-step credit assignment.

11. **Start with the 3 easy tasks** (notepad, settings, archive) where we already have 100% ZS success to validate the RL pipeline end-to-end.

12. **Scale to harder tasks** once the pipeline is validated, running 100+ rollouts per task.

**Expected impact**: 30-50pp improvement, but contingent on RL training stability and sufficient compute.

---

## 5. Data Requirements and Cost Estimates

### Data Requirements by Strategy

| Strategy | Trajectories | Steps per Trajectory | Total Training Samples | Annotation Effort |
|----------|-------------|---------------------|----------------------|-------------------|
| SFT on corrected trajectories | 50-100 | 15-30 | 750-3,000 | 1-2 hours per trajectory |
| DPO preference pairs | 100-200 trajectories to extract pairs from | 15-30 | 200-500 pairs | 30 min per trajectory |
| Online RL (GiGPO) | 1,000+ rollouts | 15-30 | 15,000-30,000 | None (automated) |

### Cost Breakdown

**Per eval run (current):**
- Agent API calls: ~$3 (30 steps x ~$0.10/step for Claude Sonnet 4.6 with screenshots)
- Verifier API calls: ~$0.30 (30 verifications x ~$0.01/call for GPT-4.1-mini)
- VM time: ~$5 (30 min at ~$10/hr for D8ds_v5)
- **Total per run: ~$8**

**For SFT data collection (50-100 runs):**
- Eval runs: 50-100 x $8 = $400-800
- Human annotation: 50-100 x $20/trajectory (at $10/hr, ~2 hr each) = $1,000-2,000
- LoRA training: ~$100-200 (4 hours on 1x H100)
- **Total: $1,500-3,000**

**For online RL (1,000+ rollouts):**
- Eval runs: 1,000 x $8 = $8,000
- Training compute: $5,000-10,000 (100+ hours on 2-4x H100)
- **Total: $13,000-18,000**

### When Training Becomes More Cost-Effective Than Prompt Engineering

Prompt engineering improvements (multi-level format, outcome-focused verification, controller tuning) are essentially free in compute cost -- they require only engineering time. At current team velocity, each prompt engineering iteration takes ~1 day of effort and yields ~5pp improvement.

Training becomes cost-effective when:
1. Prompt engineering improvements plateau (expected after 3-5 more iterations, ~2 weeks)
2. Remaining failures are primarily Category C (grounding), which prompt engineering cannot address
3. The marginal cost of one more prompt iteration ($0 compute + 1 day engineering) exceeds the amortized cost of training ($3,000 for SFT / expected 20pp improvement = $150 per percentage point)

Based on current trajectory, **prompt engineering should be exhausted first** (Tiers 1-2 above), and **SFT on corrected trajectories should begin in parallel** as soon as 20+ failure trajectories are collected during prompt engineering iterations. Online RL is a 1-3 month horizon investment that requires the eval infrastructure to be stable (no Category A failures) and the step budget to be generous enough for meaningful exploration.

---

## 6. Literature Context: How Failure Modes Map to Published Approaches

| Our Failure Category | Relevant Paper | Their Approach | Applicability |
|---------------------|---------------|----------------|---------------|
| B1 (Premature completion) | Instruction Agent | Backtracker module that detects incomplete state | High -- directly addresses our problem |
| B4 (Demo rigidity) | ShowUI-Aloha | Think/Action/Expect format with adaptation permission | High -- already planned (Option D) |
| B2 (Step budget waste) | Plan-and-Act | Dynamic replanning with budget awareness | Medium -- our DemoController already replans, but without budget awareness |
| C1-C4 (Grounding) | DigiRL, WebRL | Online RL with environment interaction | High -- grounding errors are the primary training target |
| D1-D4 (Verifier) | BacktrackAgent | Learned backtracking policy instead of fixed verification rules | Medium -- replaces our VLM verifier with a trained module |
| A1-A5 (Infrastructure) | None | No paper addresses this; it's engineering work | N/A |

The gap in the literature is notable: no paper addresses Category A failures because their environments (Android, web browsers) are more deterministic than our WAA/QEMU/pyautogui stack. Our infrastructure failure rate is likely higher than what published systems face, which means our Tier 1 deterministic fixes will have disproportionately large impact relative to what published improvement numbers suggest.

---

## Summary

The most impactful near-term investments, ranked:

1. **Fix remaining infrastructure bugs** (Tier 1): guaranteed 20% failure elimination, 1-2 weeks
2. **Tune controller parameters and add action validation** (Tier 2): expected 10-15% waste reduction, 1 week
3. **Collect failure trajectories and SFT** (Tier 3): expected 10-30pp improvement, 2-4 weeks, $1,500-3,000
4. **Online RL via verl-agent** (Tier 4): expected 30-50pp improvement, 1-3 months, $13,000-18,000

The recording infrastructure (`ExecutionTraceCollector`, `TraceExporter`) already exists and needs only minor extensions to support failure classification. The critical missing piece is not tooling but annotated data: the system can record failures today, but converting them to training signal requires either human annotation (Tier 3) or online RL with automated reward (Tier 4).
