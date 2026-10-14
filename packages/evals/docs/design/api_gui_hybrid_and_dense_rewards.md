# API-GUI Hybrid Agent + Dense Partial Rewards

> Date: 2026-03-17
> Status: PROPOSED
> Goal: Get task completion from 0% to >20% to unblock GRPO training

---

## 1. The Problem

At 80.5% per-step VLM accuracy, task completion compounds to near-zero:

| Steps | Completion |
|-------|-----------|
| 5     | 33.9%     |
| 10    | 11.5%     |
| 15    | 3.9%      |
| 20    | 1.3%      |

GRPO needs reward variance. All-zero rewards = zero gradient. **Must solve this before RL training.**

## 2. Two Complementary Solutions

### Solution A: API-GUI Hybrid (reduce VLM-dependent steps)

Replace deterministic steps with programmatic API calls. Only use the VLM for steps requiring visual understanding.

**Impact** — reducing VLM steps from N to M:

| Original (N) | VLM steps (M) | Completion |
|--------------|---------------|-----------|
| 10           | 4             | 42.0%     |
| 10           | 3             | 52.2%     |
| 15           | 5             | 33.9%     |
| 15           | 3             | 52.2%     |

Even modest API coverage (60% of steps) yields >40% task completion.

### Solution B: Dense Partial Rewards (gradient signal from failed trajectories)

Instead of binary 0/1, reward proportional to steps completed:
- 7/10 steps correct → reward = 0.7
- 3/10 steps correct → reward = 0.3
- GRPO group now has variance: [0.0, 0.2, 0.4, 0.6, 1.0] → meaningful advantages

---

## 3. API-GUI Hybrid Architecture

### Prior Art

| Project | Approach | Results |
|---------|----------|---------|
| **ComputerRL** (Tsinghua/Zhipu) | LLM generates API wrappers, model learns to choose API vs GUI via RL | 48.9% OSWorld, +134% over GUI-only |
| **UFO2** (Microsoft) | Puppeteer executor prefers API, falls back to GUI | +6-8% on office tasks |
| **AXIS** (Microsoft, ACL 2025) | API-first, auto-translates UI actions to API calls | 65-70% faster, 97-98% accuracy |

### Our Approach: Pre-Classified Task Plans (Phase 1)

```
Task Instruction
       │
       ▼
[Task Decomposer] (LLM call, runs once before episode)
       │
       ▼
Plan: [step1: API, step2: API, step3: GUI, step4: GUI, step5: API]
       │
       ▼
[Executor Loop]
  ├── API steps: execute via /execute_windows, verify with screenshot
  ├── GUI steps: invoke VLM agent, take action
  └── Fallback: if API fails → try GUI; if GUI fails → retry
```

### API Registry for WAA Tasks

**Office (LibreOffice)**:
- UNO API via `soffice --accept="socket,..."` — set cell values, apply formatting, insert formulas
- ScriptForge: `doc.GetValue("A1")`, `doc.SetFormula("B1", "=SUM(A1:A10)")`

**System Settings**:
- PowerShell: `Set-ItemProperty`, `Get-NetAdapter`, `Start-Process`
- Registry: `winreg` module for direct key manipulation

**VS Code**:
- CLI: `code --install-extension`, `code --goto file:line`
- Settings JSON: direct file edit at `%APPDATA%\Code\User\settings.json`

**File/App Management**:
- PowerShell: `Copy-Item`, `Move-Item`, `New-Item`
- win32gui: `SetForegroundWindow`, `FindWindow`

### Decision Logic

**Phase 1 (rule-based)**:
```python
class TaskDecomposer:
    def decompose(self, instruction: str) -> list[Step]:
        # LLM call: "Break this task into steps.
        # For each step, decide: can this be done via API (PowerShell,
        # UNO, registry, CLI) or does it require visual interaction?"
        # Returns: [{type: "api", action: "Start-Process calc"},
        #           {type: "gui", action: "Click on cell A1"}]
```

**Phase 2 (learned)**: Extend VLM action space to include API calls. Train via RL (ComputerRL approach).

### Failure Handling (UFO2 pattern)

1. Try API action
2. Verify with screenshot (did expected state change occur?)
3. If verification fails → fall back to GUI
4. If GUI fails → retry with alternative strategy
5. If all fail → log failure, advance to next step

### RL Integration

API steps are **environment scaffolding**, not policy decisions:
- API steps execute deterministically (not optimized by RL)
- Only GUI steps contribute to the policy gradient
- This breaks the compounding error chain: 10 steps with 6 API + 4 GUI = 0.8^4 = 42% instead of 0.8^10 = 12%

---

## 4. Dense Partial Rewards

### Approaches from Literature

| Method | How it works | Overhead |
|--------|-------------|---------|
| **Milestone-based** (ADMIRE) | Define intermediate states, reward fraction reached | Low — need milestone definitions |
| **Step verification** (ours) | VLM verifies each step, reward = steps_verified / total | Medium — VLM call per step |
| **Progress reward model** (ProgRM) | Trained model predicts [0,1] progress score | High — need training data |
| **WAA evaluator extension** | Many eval scripts check multiple conditions; return fraction met | Low — extend existing code |

### Recommended: Milestone + WAA Evaluator Extension

**Phase 1**: Extend WAA `/evaluate` to return partial scores. Many evaluation scripts already check multiple conditions (file exists AND content correct AND formatting correct). Return fraction of conditions satisfied.

**Phase 2**: Add per-step milestone detection. After each step, run lightweight checks:
- "Is the target app open?" → milestone 1
- "Is the correct file loaded?" → milestone 2
- "Does cell A1 contain the expected value?" → milestone 3

```python
# In rollout_func / reward computation:
reward = milestones_reached / total_milestones  # e.g., 3/5 = 0.6
# GRPO group: [0.0, 0.2, 0.4, 0.6, 0.8] → meaningful advantages
```

### Integration with TRL GRPOTrainer

Extra keys in rollout_func return dict are forwarded to reward functions:
```python
def waa_rollout_func(prompts, trainer):
    # ... run episodes ...
    return {
        "prompt_ids": ...,
        "completion_ids": ...,
        "logprobs": ...,
        "env_reward": [0.6, 0.2, 0.8, ...],  # dense partial rewards
    }
```

---

## 5. Implementation Plan

### Phase 1: Dense Partial Rewards (1 week, highest priority)

**Why first**: Unblocks GRPO even without API-GUI hybrid. Even if task completion stays at 0%, partial rewards give gradient signal.

| Task | Est. | Deliverable |
|------|------|-------------|
| Extend WAA evaluator for partial scores | 2 days | `/evaluate` returns `{success: bool, partial_score: float, conditions: [...]}` |
| Step verification reward (reuse DemoController verify) | 1 day | reward = steps_verified / total_steps |
| Integration with TRL rollout_func | 1 day | env_reward in rollout_func return dict |
| Tests | 1 day | Mock tests + validation against live VM |

### Phase 2: API-GUI Hybrid (2 weeks)

| Task | Est. | Deliverable |
|------|------|-------------|
| APIActionRegistry (PowerShell, UNO, win32) | 3 days | Registry with 10-15 common actions |
| TaskDecomposer (LLM-based step classification) | 2 days | Classifies steps as API vs GUI |
| Executor with API/GUI routing + fallback | 3 days | Hybrid execution loop |
| Integration with RLEnvironment | 1 day | `step()` accepts API or GUI actions |
| Tests + validation on Core4 tasks | 2 days | End-to-end on real tasks |

### Phase 3: Learned Action Selection (4+ weeks, future)

Extend VLM action space to include API calls. Train via GRPO so the model learns when to use API vs GUI. This is the ComputerRL approach.

---

## 6. Where This Code Lives

| Component | Repo | Rationale |
|-----------|------|-----------|
| APIActionRegistry | openadapt-evals | Part of the environment/adapter layer |
| TaskDecomposer | openadapt-evals | Pre-execution planning |
| Dense reward computation | openadapt-evals | Extension of RLEnvironment |
| TRL rollout_func | openadapt-evals | Training integration |
| GRPO training script | openadapt-ml | Training loop |

---

## 7. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|-----------|
| API actions break task state | HIGH | Verify with screenshot after each API step |
| UNO/COM unreliable on QEMU VM | MEDIUM | Test early; fallback to GUI |
| Dense rewards cause reward hacking | MEDIUM | Normalize rewards within GRPO groups |
| Task decomposer misclassifies steps | LOW | Conservative: default to GUI if unsure |
| Milestone definitions don't generalize | LOW | Start with Core4 tasks, expand |

---

## 8. References

- ComputerRL: 48.9% OSWorld, API-GUI hybrid, [arxiv 2508.14040](https://arxiv.org/abs/2508.14040)
- UFO2: Windows AgentOS, [arxiv 2504.14603](https://arxiv.org/abs/2504.14603)
- AXIS: API-first agents, [arxiv 2409.17140](https://arxiv.org/abs/2409.17140)
- ADMIRE: Adaptive milestone rewards, [arxiv 2602.11524](https://arxiv.org/abs/2602.11524)
- ProgRM: Progress reward model, [arxiv 2505.18121](https://arxiv.org/abs/2505.18121)
- GUI-Genesis: Verifiable rewards, [arxiv 2602.14093](https://arxiv.org/abs/2602.14093)
- AgentQ: MCTS + DPO, [arxiv 2408.07199](https://arxiv.org/abs/2408.07199)
