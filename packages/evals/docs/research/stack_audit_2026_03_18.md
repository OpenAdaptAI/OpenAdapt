# Stack Audit: What to Keep, Replace, or Delete

> Date: 2026-03-18
> Verdict: 70% of our custom RL code is commodity. Keep the 30% that's unique.

---

## Summary

Of ~2,900 lines of custom RL/training code built this session:
- **~1,000 lines (34%) are genuinely unique** — WAA environment bridge, dense milestones, TaskConfig, VLM evaluator
- **~1,100 lines (38%) should be replaced** by existing OSS frameworks
- **~800 lines (28%) are fine but could be simplified**

## What to KEEP (unique, nobody else has this)

| Component | Lines | Why it's unique |
|-----------|-------|----------------|
| Dense milestone rewards (`evaluate_dense`) | 58 | No OSS desktop agent RL has partial-credit rewards |
| TaskConfig YAML (bidirectional WAA translation) | ~400 | Only project extending WAA tasks programmatically |
| VLM evaluator (screenshot judge) | 90 | Plain-English evaluation criteria |
| OpenEnv environment wrapper | 243 | Bridge between WAA and the RL ecosystem |
| WAALiveAdapter + RLEnvironment core | existing | Months of operational knowledge |

## What to REPLACE

| Component | Lines | Replace with | Why |
|-----------|-------|-------------|-----|
| TRL rollout_func | 310 | TRL native OpenEnv integration | rollout_func is experimental, may be removed |
| Training script | ~380 | OpenPipe ART or Agent Lightning | Commodity code, better maintained upstream |
| PlannerGrounderAgent | 610 | CODA (ICLR 2026) or GTA1 | More robust, published, open-source |
| Action parser | 40 | Absorbed by framework | Every agent framework has this |

## What to SIMPLIFY

| Component | Lines | Action |
|-----------|-------|--------|
| OpenEnv server.py | 90 | Delete — users run uvicorn directly |
| OpenEnv models.py | 94 | Re-export OpenEnv types, don't inline |
| RLEnvironment | 598 | Strip to reset/step/evaluate/evaluate_dense (~200 lines) |

## Key Competitors We Should Evaluate

- **CODA** (ICLR 2026): Exact same planner-grounder split, open-source training code, trains only planner
- **Agent Lightning** (Microsoft): Zero-code-change RL training, 12.8K stars, verl-backed
- **OpenPipe ART**: Native OpenEnv integration, handles training server + LoRA management
- **GTA1** (Salesforce, ICLR 2026): Planner + RL-trained grounder, 45.2% OSWorld

## The Minimum Viable Stack (~930 lines)

1. RLEnvironment (stripped) — reset/step/evaluate/evaluate_dense
2. WAAOpenEnvEnvironment — OpenEnv bridge
3. TaskConfig — YAML tasks with WAA translation
4. VLM evaluator — screenshot-based evaluation
5. Everything else comes from upstream frameworks

## Risk: TRL rollout_func

Our rollout_func builds on TRL's **experimental** API — "may change or be removed at any time without prior notice." The OpenEnv bridge is the insurance policy.
