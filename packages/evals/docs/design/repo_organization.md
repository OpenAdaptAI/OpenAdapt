# Repository Organization: Current State vs Target

> Date: 2026-03-18
> Status: Document for future reorganization (not blocking current work)

---

## Current: Everything in openadapt-evals

All new code from this sprint landed in openadapt-evals because it depends on
RLEnvironment, WAALiveAdapter, and PlannerGrounderAgent. This is pragmatic for
shipping but creates a "training/" directory inside an evaluation repo.

## Where things are vs where they belong

| Component | Current location | Should be in | Move when | Why |
|-----------|-----------------|-------------|-----------|-----|
| `training/trl_rollout.py` | evals | **openadapt-ml** | After customer sprint | Training code, not evaluation |
| `training/trajectory_logger.py` | evals | **openadapt-ml** | After customer sprint | Training data collection |
| `training/planner_cache.py` | evals | evals (ok) | — | Agent optimization, not training |
| `scripts/train_trl_grpo.py` | evals | **openadapt-ml** | After customer sprint | Training script |
| `adapters/local/adapter.py` | evals | **openadapt-desktop** | When desktop repo has code | Local replay is the product |
| `adapters/scrub_middleware.py` | evals | **openadapt-desktop** | When desktop repo has code | Governance is the product |
| `agents/planner_grounder_agent.py` | evals | evals (ok) | — | Agent orchestration |
| `task_config.py` | evals | evals (ok) | — | Task definition for eval |
| `vlm_evaluator.py` | evals | evals (ok) | — | Evaluation utility |
| `openenv/` | evals | evals (ok) | — | Environment bridge |
| `correction_store.py` | evals | evals (ok) | — | Correction flywheel |
| `correction_capture.py` | evals | evals (ok) | — | Correction flywheel |
| `correction_parser.py` | evals | evals (ok) | — | Correction flywheel |

## Dependency direction

Current (wrong):
```
openadapt-evals contains training/ which imports from evals adapters
```

Target:
```
openadapt-ml imports from openadapt-evals (adapters, environments)
openadapt-desktop imports from openadapt-evals (adapters) and openadapt-privacy (scrubbing)
openadapt-evals stays focused on evaluation, agents, environments
```

## When to reorganize

Not during the customer sprint (ends ~Mar 28). After that:

1. Move `training/` to openadapt-ml, add openadapt-evals as dependency
2. Move LocalAdapter + ScrubMiddleware to openadapt-desktop when that repo gets implementation
3. Consider whether PlannerGrounderAgent should move if we adopt CODA or Agent Lightning

## What stays in openadapt-evals permanently

- Agents (ApiAgent, HttpAgent, PlannerGrounderAgent, etc.)
- Adapters (WAALiveAdapter, WAAMockAdapter)
- RLEnvironment + dense rewards
- OpenEnv bridge
- TaskConfig + VLM evaluator
- Correction flywheel
- VM CLI + pool management
- Benchmark runners
