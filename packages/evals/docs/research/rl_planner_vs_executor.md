# RL Target: Planner vs Executor in Planner-Grounder Architecture

> Date: 2026-03-18
> Source: Literature review of 20+ papers on GUI agent RL training

---

## Recommendation

**Start with RL on the executor (grounder). Stage in planner RL later, or use the correction flywheel instead.**

---

## Why Executor RL First

| Dimension | Executor RL | Planner RL |
|-----------|------------|------------|
| Reward density | Dense (every click) | Sparse (task end) |
| Cold start risk | None (always has signal) | Fatal at 0% completion |
| Compute | Low (3K-17K samples, 1 GPU) | High (thousands of envs) |
| Action space | Bounded (pixel coords) | Unbounded (natural language) |
| Literature results | SE-GUI: 47.3% ScreenSpot-Pro (7B beats 72B) | Needs milestone rewards or MCTS |
| Demonstrated accuracy | GUI-Cursor: 93.9% ScreenSpot-v2 | ComputerRL: 48.9% OSWorld (unified) |

### Key papers for executor RL
- **SE-GUI** (NeurIPS 2025): GRPO on 7B grounder, 3K samples, dense rewards → 47.3% ScreenSpot-Pro
- **GUI-G1** (NeurIPS 2025): R1-Zero GRPO for grounding, 17K samples → 90.3% ScreenSpot. Finding: CoT HURTS grounding.
- **GUI-Cursor** (Sep 2025): Multi-step cursor-driven RL → 93.9% ScreenSpot-v2
- **GUI-Libra** (Feb 2026): Action-aware SFT + KL-regularized GRPO → outperforms proprietary models at 4B

### Key papers for planner RL
- **CODA** (2025): Trains ONLY the planner with GRPO, executor frozen. "Decoupled approach is substantially more data efficient."
- **ComputerRL** (2025): End-to-end RL on unified model, needs Entropulse to prevent entropy collapse
- **AgentQ** (2024): MCTS + DPO on planner, 18.6% → 81.7% with fixed executor
- **HCAPO** (Mar 2026): Hindsight credit assignment, +13.8pp over GRPO on ALFWorld

---

## Recommended Staging

### Stage 0: Baseline (current)
DemoController (rule-based planner) + API model or VLM executor

### Stage 1: SFT executor on successful demonstrations
Fine-tune 7B VLM on (screenshot + instruction → coordinates). Establishes baseline.

### Stage 2: GRPO executor with dense hit/miss reward
- Reward: did the click land on the target element? (dense, every step)
- Use DemoController-generated instructions as planner input
- 3K-17K samples, single GPU, weeks not months
- **This is the highest-ROI stage**

### Stage 3a: Planner RL with milestone rewards (optional)
- Only after Stage 2 gives reliable executor (non-zero task completion)
- Use HCAPO/GiGPO for credit assignment
- ProgRM/ADMIRE-style milestone rewards

### Stage 3b: Correction flywheel (alternative to planner RL)
- DemoController + human corrections = planner improvement WITHOUT RL
- Each correction adds to demo library
- May be more practical than planner RL for enterprise deployments

---

## How This Maps to OpenAdapt

- **DemoController** = rule-based planner (frozen, improved via corrections)
- **TRL rollout_func** = executor training loop
- **Dense milestone rewards** = already built (TaskConfig + evaluate_dense)
- **Correction flywheel** = already built (correction_store + correction_capture)
- **GRPO on executor**: modify rollout_func so planner is fixed, only executor generates actions + receives gradients

The OpenAdapt stack is already aligned with the literature's recommended approach. The missing piece is the `PlannerGrounderAgent` that separates the two roles cleanly.
