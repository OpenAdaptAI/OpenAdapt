# Planner Distillation: Eliminating API Costs

> Date: 2026-03-18
> Goal: Replace Claude/GPT planner ($0.30-0.50/episode) with self-hosted model ($0/episode)

---

## Summary

Collect successful planner trajectories → SFT an open 7-8B model → self-hosted planner at $0/episode. Break-even in 10-117 episodes. Total cost: $5-35.

## Approach

### Data Collection (Free — piggybacked on GRPO training)
Every PlannerGrounderAgent episode already calls the API planner. Log the inputs (screenshot, a11y tree, task, history) and outputs (decision, instruction, reasoning). Filter to successful episodes. **Zero additional cost.**

### SFT Training
- **Base model**: Qwen2.5-VL-7B-Instruct (best tooling) or EvoCUA-8B (best planning)
- **LoRA** r=16-32, vision layers frozen
- **250-2000 examples** is sufficient (TeichAI: 250 samples, $52 API cost)
- **Training time**: 2-4 hours on A10G (~$4)

### Planner Cache (complements distillation)
- Hash (screenshot pHash + task + action_history) → cache planner response
- 40-60% cache hit rate during GRPO (8 rollouts from same initial state)
- Halves API costs during data collection

## Cost Analysis

| Path | API Cost | Compute | Total | Break-even |
|------|----------|---------|-------|------------|
| Dedicated collection (100 episodes) | $30 | $4 | $34 | 70 episodes |
| Piggybacked on GRPO training | $0 | $4 | $4 | 10 episodes |
| With caching (50% hit rate) | $15 | $4 | $19 | 40 episodes |

## Implementation: ~320 lines across 4 files

1. `trajectory_logger.py` (~80 lines) — log planner inputs/outputs to JSONL
2. `planner_sft_dataset.py` (~60 lines) — convert JSONL to TRL SFT format
3. `scripts/train_planner_sft.py` (~100 lines) — SFT training wrapper
4. `planner_cache.py` (~50 lines) — perceptual hash-based planner response cache
5. PlannerGrounderAgent update (~30 lines) — `planner_provider="http"` for local model
