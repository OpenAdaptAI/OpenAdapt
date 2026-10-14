# ROI & Cost Analysis: Planner-Grounder Desktop Automation Stack

> Date: 2026-03-18

---

## 1. Per-Episode Cost Breakdown

### Current: API Planner + Hosted Grounder

| Component | Cost per step | Steps/episode | Per episode |
|-----------|--------------|---------------|-------------|
| Claude Sonnet planner | $0.01-0.03 | 10 | $0.10-0.30 |
| UI-Venus grounder (A10G) | $0.0003 | 10 | $0.003 |
| WAA VM (Azure D8ds_v5) | $0.006 | 10 | $0.06 |
| **Total per episode** | | | **$0.16-0.36** |

### After Planner Distillation: Fully Self-Hosted

| Component | Cost per step | Steps/episode | Per episode |
|-----------|--------------|---------------|-------------|
| Distilled planner (A10G) | $0.0003 | 10 | $0.003 |
| UI-Venus grounder (same GPU) | $0.0003 | 10 | $0.003 |
| WAA VM | $0.006 | 10 | $0.06 |
| **Total per episode** | | | **$0.07** |

**Savings: 55-80% per episode.**

---

## 2. GRPO Training Cost

### With API Planner (current)

| Parameter | Value |
|-----------|-------|
| Rollouts per task | 8 |
| Tasks per training run | 100 |
| Steps per rollout | 10 |
| Total planner API calls | 8,000 |
| Cost per call | $0.03 |
| **API cost per training run** | **$240** |
| GPU cost (A10G, ~8 hrs) | $8 |
| WAA VM cost (~8 hrs) | $3 |
| **Total per training run** | **$251** |

### With Planner Cache (50% hit rate)

| | |
|---|---|
| API calls saved | 4,000 |
| **API cost** | **$120** |
| **Total per training run** | **$131** |

### With Distilled Planner (fully self-hosted)

| | |
|---|---|
| API calls | 0 |
| GPU cost (planner + grounder, ~8 hrs) | $8 |
| WAA VM cost (~8 hrs) | $3 |
| **Total per training run** | **$11** |

---

## 3. Planner Distillation ROI

### One-Time Investment

| Item | Cost |
|------|------|
| Trajectory collection (100 episodes × $0.30) | $30 |
| SFT training (A10G, 4 hours) | $4 |
| Validation (20 episodes on GPU) | $1 |
| **Total** | **$35** |

Or **$5** if piggybacked on GRPO training (trajectories collected for free).

### Break-Even

| Scenario | Investment | Savings/episode | Break-even |
|----------|-----------|----------------|------------|
| Dedicated collection | $35 | $0.23/episode | 152 episodes |
| Piggybacked on GRPO | $5 | $0.23/episode | 22 episodes |
| GRPO training runs | $35 | $229/run | 0.15 runs |

**One GRPO training run pays for distillation 6x over.**

---

## 4. Infrastructure Cost Comparison

### AWS Instance Options (with nested virt, PR #124)

| Instance | Cost/hr | VRAM | Use |
|----------|---------|------|-----|
| m8i.2xlarge | $0.46 | — | WAA VM (CPU, nested virt) |
| g5.xlarge | $1.01 | 24GB A10G | Grounder + distilled planner |
| g5.2xlarge | $1.21 | 24GB A10G | Grounder + larger planner |

### Dual-Model on Single A10G (24GB)

| Model | VRAM (4-bit) | Inference speed |
|-------|-------------|-----------------|
| Distilled planner (8B) | ~5GB | 1-2s/step |
| UI-Venus grounder (8B) | ~5GB | 1-2s/step |
| **Both on 1 GPU** | **~10GB** | **3-4s/step total** |

Both models fit on a single A10G with 14GB headroom. No need for separate GPU instances.

### vs API Latency

| Setup | Latency per step |
|-------|-----------------|
| Claude API planner | ~5-25s (network + inference) |
| Self-hosted 8B planner (vLLM) | ~1-2s |
| **Speedup** | **5-12x faster** |

---

## 5. Long-Term Cost at Scale

### 10 GRPO training runs

| Scenario | Total cost |
|----------|-----------|
| API planner, no cache | $2,510 |
| API planner + cache | $1,310 |
| Distilled planner (after $35 investment) | $145 |
| **Savings with distillation** | **$2,365 (94%)** |

### Production deployment (1000 episodes/month)

| Scenario | Monthly cost |
|----------|-------------|
| API planner | $300/month |
| Distilled planner | $70/month |
| **Savings** | **$230/month (77%)** |

---

## 6. Dense Rewards ROI

### Problem: Binary Rewards at 0% Task Completion

Without dense rewards, GRPO gets zero gradient signal. Training is impossible.

### Solution: Milestone-Based Dense Rewards

| Metric | Binary (old) | Dense milestones (new) |
|--------|-------------|----------------------|
| Reward variance in GRPO group | 0 (all zeros) | >0 (0.0, 0.33, 0.67, 1.0) |
| Gradient signal | Zero | Non-zero |
| Training possible | No | Yes |
| Implementation cost | — | Already shipped (PR #125) |

**ROI: infinite.** Without dense rewards, the entire RL investment produces nothing.

---

## 7. Format Alignment ROI

### Problem: 35% of Actions Fail Due to Output Format

Grounder model outputs coordinates in mixed formats (fractional, canvas, pixel). Engineers write 300 lines of sanitization code per model integration.

### Solution: Format-Aware SFT or Constrained Decoding

| Approach | Effort | Result |
|----------|--------|--------|
| Constrained decoding (vLLM) | 10 lines config | 100% format compliance, zero training |
| Format SFT (500 examples) | Few hours | Model outputs clean format natively |
| Custom sanitization code | 300 lines per model | Brittle, model-specific |

**ROI: eliminates 300 lines of engineering per model integration.**

---

## 8. Summary: Where to Invest

| Investment | Cost | Annual savings | ROI |
|-----------|------|---------------|-----|
| Dense rewards (milestones) | Already shipped | Enables GRPO (was impossible) | ∞ |
| Planner cache | ~2 hours dev | ~$1,200/year (10 training runs) | 600x |
| Planner distillation | $35 one-time | ~$2,365/year (10 training runs) | 67x |
| Format SFT | $5 one-time | Engineering time | High |
| Self-hosted dual-model | $10/day GPU | Eliminates API dependency | Strategic |
