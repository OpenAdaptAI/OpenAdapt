# Training Targets in the Planner-Grounder Architecture

> Date: 2026-03-18
> Based on: Literature review of 40+ papers, SE-GUI, CODA, GUI-G1, ComputerRL, AgentQ, UI-Venus

---

## 1. The Planner-Grounder Decomposition

The planner-grounder architecture (SeeAct 2024, UFO2 2025, Agent S2 2025) separates GUI automation into two capabilities:

- **Planner**: Sees screenshot + UI annotations, outputs a high-level instruction ("Click the Settings icon in the top-right corner"). Handles step-by-step reasoning, error recovery, backtracking, dynamic environment adaptation.
- **Grounder**: Sees screenshot + planner's instruction, outputs precise coordinates. Specialized for visual element localization.

Each component has different training characteristics, failure modes, and improvement strategies. This document analyzes what can be trained, what should be trained, and in what order.

---

## 2. Four Distinct Training Targets

### Target 1: Grounder Format Alignment

**The problem**: SOTA grounding models (UI-Venus 1.5, GUI-Actor, OS-Atlas) produce accurate coordinates but in inconsistent output formats — fractional [0,1], canvas [0,1000], raw pixels, model-specific function call syntax (`computer.mouse.single_click()`), or mixed formats within the same session. Production deployments require format normalization code (typically 100-300 lines) to handle these inconsistencies.

**The fix**: Align the grounder's output format to the execution layer's expected input. Three approaches:

| Approach | Effort | Result |
|----------|--------|--------|
| **Constrained decoding** (vLLM structured output, `outlines` grammar) | Zero training, inference config only | 100% format compliance, no accuracy impact |
| **SFT on format-corrected examples** | 500-1K examples, hours of training | Model learns to output clean format natively |
| **GRPO with format compliance reward** | Reward = 1.0 if valid JSON with consistent coords | Most robust, but overkill for pure format issues |

**Priority**: Highest immediate value. Eliminates engineering workarounds entirely. Constrained decoding should be tried first (zero cost).

**Literature**: GUI-Libra (Feb 2026) addresses the related problem of "partial verifiability" in grounder output — multiple valid click locations exist but only one is in the training data. Their Action-Aware SFT with KL-regularized GRPO stabilizes output format while preserving grounding accuracy.

### Target 2: Grounder Accuracy (RL)

**The problem**: While SOTA grounders achieve 90%+ on benchmarks (UI-Venus: 94.1% ScreenSpot-V2), they may underperform on domain-specific UIs not well represented in training data (enterprise applications, specialized workflows).

**The fix**: GRPO with dense grounding reward — "did the click land on the target element?"

**Literature**:
- **SE-GUI** (NeurIPS 2025): GRPO on 7B grounder, just 3K samples → 47.3% ScreenSpot-Pro, beating UI-TARS-72B. Dense policy gradient provides continuous feedback.
- **GUI-G1** (NeurIPS 2025): R1-Zero GRPO training, 17K samples → 90.3% ScreenSpot. Critical finding: chain-of-thought reasoning HURTS grounding performance. "Fast Thinking Template" (direct answer) works better.
- **GUI-Cursor** (Sep 2025): Reframes grounding as interactive cursor-driven search with multi-step RL → 93.9% ScreenSpot-v2 SOTA. Renders cursor on screenshot for explicit visual feedback.
- **GUI-Libra** (Feb 2026): Action-Aware SFT followed by KL-regularized GRPO. Addresses partial verifiability. 4B model outperforms proprietary models.

**Key insight from GUI-G1**: For grounding, thinking hurts. The grounder should be trained for fast, direct coordinate prediction — not reasoning. This is the opposite of what planners need.

**Priority**: Medium. Only valuable if the existing grounder (e.g., UI-Venus) underperforms on the target domain. If UI-Venus already clicks the right elements, grounding RL adds marginal value. Test first before investing.

**Reward design**:
```
reward = 1.0 if click_inside_target_element else 0.0
# Or continuous: reward = 1.0 - (distance_to_target / max_distance)
```

### Target 3: Planner Distillation (SFT)

**The problem**: The planner role is currently filled by API models (GPT-5.4, Claude) which have high cost (~$2.50/1M tokens), high latency (~25s/step), and send screenshots to external APIs (privacy concern for enterprises with sensitive data).

**The fix**: Distill the API planner's behavior into an open-source model via SFT on successful trajectories. The dual-model setup generates training data naturally — every successful trajectory is a (screenshot, task_state) → (high-level instruction) training pair.

**Approach**:
1. Run the dual-model (API planner + grounder) on a set of tasks
2. Collect successful trajectories: sequence of (screenshot + state → planner instruction) pairs
3. SFT an open model (EvoCUA-32B, Qwen3-VL-32B, or even 8B) on these pairs
4. The distilled planner should reproduce the API model's step-by-step reasoning

**Literature**:
- **CODA** (2025): "Cerebrum" (planner) + "Cerebellum" (executor). Trains only the planner with GRPO while executor is frozen. "The decoupled approach is substantially more data efficient."
- **EvoCUA** (Meituan, Jan 2026): Self-evolving curriculum learning. 32B model achieves 56.7% OSWorld — beating 72B models. Demonstrates that training methodology matters more than model size.
- **Fara-7B** (Microsoft): 7B model trained with synthetic data + iterative refinement achieves performance "comparable to 70B+ models" on web navigation.

**Priority**: High long-term value. Enables self-hosted, privacy-safe, low-latency operation. But requires a working dual-model baseline generating successful trajectories first.

**Data collection strategy**:
- Successful dual-model runs provide (screenshot, task) → (instruction) pairs naturally
- Each completed task generates 5-20 training examples (one per step)
- 100 successful task completions → 500-2000 SFT examples
- This is enough for meaningful planner SFT (Fara used synthetic data at similar scale)

### Target 4: Planner RL (GRPO)

**The problem**: SFT-distilled planners replicate the API model's behavior but don't improve beyond it. RL can discover novel planning strategies — better error recovery, more efficient step sequences, adaptation to new UI layouts.

**The fix**: GRPO on the planner with task completion reward (milestone-based for density).

**Literature**:
- **CODA** (2025): GRPO on planner only, executor frozen. Application-specific expert planners then aggregated via SFT.
- **AgentQ** (2024): MCTS + DPO on planner with fixed navigator. 18.6% → 81.7% success rate on booking tasks.
- **ComputerRL** (2025): End-to-end RL on unified model. Requires "Entropulse" (alternating RL/SFT phases) to prevent entropy collapse during extended training.
- **HCAPO** (Mar 2026): Hindsight credit assignment for planner RL. Uses the LLM itself as post-hoc critic. +13.8pp over vanilla GRPO on ALFWorld.

**Prerequisites**:
- Non-zero task completion rate (otherwise GRPO gradient is zero)
- A frozen, reliable executor (so planner changes don't shift execution quality)
- Dense milestone rewards (binary task completion is too sparse)
- All of these are satisfied AFTER Targets 1-3 are addressed

**Priority**: Lowest immediate priority but highest long-term potential. The correction flywheel (human corrections improving the demo library) may be a more practical planner improvement mechanism for enterprise deployments.

---

## 3. Recommended Staging

```
Stage 0: Dual-model baseline
  Planner: API model (GPT-5.4 / Claude)
  Grounder: UI-Venus-1.5-8B
  Result: Working task completion on real tasks

Stage 1: Grounder format alignment [HOURS]
  Fix: Constrained decoding or SFT on format-corrected examples
  Result: Clean, consistent output format. No sanitization code.

Stage 2: Planner distillation via SFT [DAYS]
  Fix: SFT open model on successful dual-model trajectories
  Result: Self-hosted planner, no API dependency, 5-10x faster

Stage 3: Grounder domain specialization via GRPO [DAYS-WEEKS]
  Fix: GRPO with dense grounding reward on domain-specific UIs
  Result: Grounder specialized for target application UIs
  Only if: UI-Venus underperforms on the target domain

Stage 4: Planner RL via GRPO [WEEKS]
  Fix: GRPO with milestone rewards, HCAPO credit assignment
  Result: Planner discovers novel strategies, better error recovery
  Only if: SFT-distilled planner is insufficient
  Alternative: Correction flywheel (human corrections, no RL)
```

---

## 4. Reward Design Per Target

| Target | Reward Signal | Density | Example |
|--------|--------------|---------|---------|
| Grounder format | Is output valid JSON with coords in [0,1]? | Dense (every step) | `1.0 if json.loads(output) and 0<=x<=1 else 0.0` |
| Grounder accuracy | Did click hit target element? | Dense (every step) | `1.0 if click_in_bbox(x,y,target) else 0.0` |
| Planner step quality | Did the instruction lead to progress? | Medium (per milestone) | `milestones_passed / total_milestones` |
| Planner task completion | Did the full task succeed? | Sparse (end of episode) | `1.0 if task_complete else 0.0` |

---

## 5. Compute Requirements

| Target | Model Size | Samples | Hardware | Time |
|--------|-----------|---------|----------|------|
| Format SFT | 8B | 500-1K | 1x A10G | Hours |
| Format constrained decoding | Any | 0 | Inference only | Minutes (config) |
| Grounder GRPO | 7-8B | 3K-17K | 1x A10G | Days |
| Planner SFT | 8-32B | 500-2K | 1-2x A10G | Hours-Days |
| Planner GRPO | 8-32B | 10K+ | 2-4x A10G | Weeks |

---

## 6. Open-Source Model Options

### For the Grounder
| Model | Size | ScreenSpot-V2 | Notes |
|-------|------|---------------|-------|
| UI-Venus-1.5-8B | 8B | 94.1% | SOTA. Ant Group (inclusionAI). Qwen2.5-VL base. |
| GUI-Actor-7B | 7B | 44.6% SP-Pro | Microsoft. Beats UI-TARS-72B on ScreenSpot-Pro. |
| ShowUI-2B | 2B | 75.1% zero-shot | Smallest viable grounder. |

### For the Planner (replacing API models)
| Model | Size | OSWorld | VRAM (4-bit) | Notes |
|-------|------|---------|-------------|-------|
| EvoCUA-32B | 32B | 56.7% | ~18GB | Meituan. #1 open-source. |
| EvoCUA-8B | 8B | 46.1% | ~5GB | Beats 72B models via curriculum learning. |
| OpenCUA-72B | 72B | 45.0% | ~40GB | Reflective CoT planning. |
| Qwen3-VL-32B | 32B | 41.0% | ~18GB | General purpose VLM. |

### Fully Self-Hosted Dual-Model Stack
| Config | Planner | Grounder | Total VRAM | Gap vs GPT-5.4 |
|--------|---------|----------|-----------|----------------|
| Best | EvoCUA-32B | UI-Venus-1.5-8B | 23GB (1x A10G) | ~18pp |
| Budget | EvoCUA-8B | UI-Venus-1.5-2B | 7GB | ~29pp |

---

## 7. How This Maps to OpenAdapt

| Training Target | OpenAdapt Component | Status |
|----------------|--------------------|---------|
| Grounder format SFT | TRL rollout_func + format reward | Built (PR #127) |
| Grounder GRPO | RLEnvironment + dense rewards + TRL | Built (PRs #125, #127, #129) |
| Planner SFT data collection | PlannerGrounderAgent trajectories | PlannerGrounderAgent not yet built |
| Planner GRPO | TRL rollout_func + milestone rewards | Built (PR #127), needs executor-only mode |
| Correction flywheel (planner alt) | correction_store + DemoController | Built (PR #125, core modules on main) |

**The missing piece**: `PlannerGrounderAgent` (~200 lines) that cleanly separates the two roles and enables trajectory collection for planner SFT.

---

## References

- SeeAct (ICML 2024): Established action generation vs action grounding terminology
- CODA (2025): Decoupled planner GRPO with frozen executor
- SE-GUI (NeurIPS 2025): GRPO grounder, 3K samples, 7B beats 72B
- GUI-G1 (NeurIPS 2025): CoT hurts grounding, direct answer works better
- GUI-Cursor (Sep 2025): Cursor-driven grounding RL, 93.9% ScreenSpot-v2
- GUI-Libra (Feb 2026): Action-Aware SFT + KL-regularized GRPO
- ComputerRL (Aug 2025): End-to-end RL, Entropulse for stability
- AgentQ (Aug 2024): MCTS + DPO on planner, 18.6% → 81.7%
- HCAPO (Mar 2026): Hindsight credit assignment, +13.8pp over GRPO
- UI-Venus 1.5 (Feb 2026): Progressive RL training, SOTA grounding
- EvoCUA (Jan 2026): Self-evolving curriculum, 56.7% OSWorld
- Fara-7B (Microsoft): 7B matches 70B+ via synthetic data
