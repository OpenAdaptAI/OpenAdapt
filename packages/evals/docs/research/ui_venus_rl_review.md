# UI-Venus RL Training Code Review

> Date: 2026-03-18
> Source: Comprehensive review of UI-Venus repo, AReaL framework, AEnvironment

---

## Key Finding: UI-Venus RL training code is NOT open-source

The UI-Venus repo (github.com/inclusionAI/UI-Venus) contains **only inference, evaluation, and deployment code**. None of the four training stages (Mid-Training, Offline-RL, Online-RL, Model Merge) have public code.

---

## What IS in the repo

| Directory | Contents | Useful to us? |
|-----------|----------|---------------|
| `models/grounding/` | ScreenSpot-Pro eval scripts | Reference only |
| `models/navigation/` | vLLM-based inference agent | **Yes** — prompt format, output parsing |
| `Venus_framework/` | Android ADB deployment | **Pattern only** — ADB, not Windows |
| `scripts/` | Shell wrappers for eval | Reference only |
| `vis_androidworld/` | Trace visualization | No |

## The likely training framework: AReaL

[AReaL](https://github.com/inclusionAI/AReaL) (same org, Ant Group/inclusionAI) IS open-source and supports:
- GRPO natively (via PPOTrainer with normalization config)
- VLM training (VisionRLVRWorkflow)
- Multi-turn agent workflows (AgentWorkflow)
- Async rollout collection with stale weight tolerance

**Integration path**: Write an AReaL `AgentWorkflow` subclass wrapping WAADesktopEnv. This is a third option alongside TRL rollout_func and OpenEnv.

## UI-Venus Training Stages (from paper, no public code)

### Offline-RL: Per-step rewards
- Format reward: XML template compliance
- Action type reward: binary correct/incorrect
- Content reward: token-level F1
- Coordinate reward: hierarchical tolerance (exact > within-element > within-region)

### Online-RL: Trajectory-level rewards
```
R(tau) = 1_success * R_comp * eta^((T-T_min)/T_min) + Sum(R_p(a_t))
```
- R_comp: binary task completion
- Length decay: fewer steps = higher reward
- Invalid action penalty: per-step deduction
- Adaptive KL constraint
- Annealed entropy regularization

### Environment: Proprietary DaaS
- "Thousands of concurrent devices"
- Group Control Gateway with hash routing
- Not reusable — it's enterprise infrastructure

## Reusability Assessment

| Component | Reusable? | Notes |
|-----------|-----------|-------|
| RL training code | **No** | Not public |
| Inference/eval code | **Partially** | Prompt format, output parsing useful |
| AReaL framework | **Yes** | Alternative to TRL for GRPO training |
| DaaS infrastructure | **No** | Proprietary |
| Reward functions | **Concept only** | Paper describes them, no code |
| Venus_framework (Android) | **Pattern only** | ADB-based, not applicable to Windows |

## What we should take from this

1. **Their reward design is sophisticated** — format + action type + content + coordinate rewards for Offline-RL. Our milestone-based rewards are simpler but provide similar gradient signal.

2. **AReaL is a viable alternative to TRL** for the training backend. Supports async rollouts, GRPO, VLM training. Worth evaluating if TRL's rollout_func proves too limited.

3. **Their Online-RL uses trajectory-level advantages** (not step-level). Our dense milestone rewards would actually be better for training signal — step-level credit assignment via milestones gives finer-grained gradients.

4. **The UI-Venus model itself is the product**, not the training code. We should use UI-Venus as a pre-trained grounder and fine-tune it with our own GRPO pipeline (TRL or AReaL) + our own rewards (milestones + format compliance).
