# Decision: verl-agent/VAGEN for VLM RL Training

**Date**: 2026-03-02
**Status**: Adopted (spike complete, PR #84)
**Stakeholders**: OpenAdapt engineering, RL training partners

## Summary

After a comprehensive review of available RL training frameworks for multi-turn
VLM (Vision-Language Model) desktop automation, we chose **verl-agent/VAGEN**
as our training backend. This document records the reasoning, alternatives
considered, and the key architectural insight that drove the decision.

---

## The Key Insight

**verl-agent enables per-step verification within multi-step rollouts — a
requirement for complex computer-usage tasks that no other framework handles
well.**

Desktop automation tasks are inherently long-horizon: a 15-step episode might
involve navigating menus, typing text, clicking buttons, and verifying results.
Standard GRPO gives you a single reward at the end of the episode (did the task
succeed?), but tells you nothing about which individual steps helped or hurt.

verl-agent's GiGPO (Group-in-Group Policy Optimization) solves this with
**two-level advantage computation**:

1. **Episode-level** (standard GRPO): Did rollout A succeed while rollout B
   failed? Give higher advantage to A's actions.
2. **Step-level** (GiGPO innovation): Across all rollouts, find steps where
   the agent was in the **same state** (same screenshot). Compare the actions
   taken from that state — which ones led to better outcomes? Assign
   per-step advantages accordingly.

This is uniquely valuable for desktop automation because:

- **Episodes are long** (15+ steps), so episode-level signal is diluted
- **Only the final WAA evaluator** tells you if the task succeeded (binary reward)
- **The same intermediate state** (e.g., "File menu is open") appears across
  rollouts — GiGPO exploits this to figure out which click was correct
- **No critic model needed** — GiGPO is critic-free, computing advantages purely
  from group comparisons, keeping GPU memory manageable for large VLMs

Without per-step credit assignment, GRPO on a 15-step episode is like giving a
student a single grade on a 15-question exam without marking which answers were
wrong. GiGPO marks each answer.

---

## The Strategic Framing

> "The environment is the moat, not the training math."

This principle, articulated during our architecture review, drove the decision:

1. **Our core value is the WAA RL environment** — `RLEnvironment` in
   openadapt-evals provides Gym-like reset/step/observe/evaluate for desktop
   automation. Nobody else has this as a turnkey package.

2. **Training math is commodity** — GRPO loss is 15 lines of PyTorch. Anyone
   can write it. The value is in having a standard interface to plug into.

3. **Build on what others have built** — verl-agent has multi-turn VLM support,
   GiGPO, distributed training (FSDP, Ray), vLLM/sglang acceleration. Why
   reimplement any of this?

4. **The training example should be a recipe, not a library** — Users
   `pip install openadapt-evals`, write a 50-line adapter, and train with
   verl-agent. They don't need to install openadapt-ml for GRPO.

> "What's the right way to implement this so that more people will adopt it?
> Is less code better? Should we re-use standard libs and just focus on our
> core value, which is the WAA automation?" — project lead

The answer: yes. Our adapter is ~250 lines of glue. Everything else is
verl-agent's problem.

---

## Comprehensive Framework Review

We evaluated 6 approaches before selecting verl-agent/VAGEN:

### A. TRL GRPOTrainer (HuggingFace)

**Status**: Does NOT support multi-turn VLM GRPO (as of March 2026).

- **Single-turn VLM**: Works. `pixel_values` are buffered in `_buffered_inputs`
  and passed to the training forward pass. Tested with Qwen2.5-VL.
- **Multi-turn VLM**: Broken. Chat templating is applied before the rollout
  logic, flattening structured multimodal data (text + images) into plain text.
  The `rollout_func` receives flattened text, losing image information.
- **Open issues**:
  - [#5120](https://github.com/huggingface/trl/issues/5120): "Preserve
    structured multimodal messages through rollout and generation pipeline"
    (opened Feb 18, 2026, OPEN)
  - [#5119](https://github.com/huggingface/trl/issues/5119): "Decouple
    inference backend from rollout & agent logic" (OPEN)
  - [#4543](https://github.com/huggingface/trl/issues/4543): Multi-step
    training forces one shared prompt across all generations, but multi-step
    trajectories have different prefixes at each turn
- **NEW (2026)**: TRL added OpenEnv integration with `rollout_func` for
  multi-turn environment training (Gym-style). Works for text models. VLM
  support blocked by #5120 (chat template flattens multimodal data before
  rollout).
- **Verdict**: Not viable for our use case until #5120 is resolved. Monitoring.

### B. Standalone Loss Math (our initial approach)

**What we built**: 15-line `policy_gradient_loss` function + custom training
loop (~546 lines in `openadapt_ml/training/grpo/trainer.py`).

- **Pros**: Works today, simple, no external dependencies beyond HF/PEFT
- **Cons**:
  - Only episode-level rewards (no per-step credit assignment)
  - Reimplements model loading, LoRA setup, optimizer, checkpointing
  - No distributed training support
  - No vLLM/sglang acceleration
  - 546 lines of code we own and must maintain
- **Verdict**: Was the right call when TRL couldn't do multi-turn VLM. Now
  superseded by verl-agent integration.

### C. verl-agent (selected)

**Repository**: [langfengQ/verl-agent](https://github.com/langfengQ/verl-agent)

- **Multi-turn VLM GRPO**: Yes, first-class support
- **GiGPO**: Step-level credit assignment via two-level grouping
- **Qwen2.5-VL tested**: Yes (Sokoban VLM example with `run_sokoban_qwen3vl.sh`)
- **Architecture**: Step-wise interaction paradigm (no full history concatenation),
  customizable memory module per step
- **Algorithms**: GiGPO, GRPO, PPO, DAPO, GSPO, RLOO, REINFORCE++
- **Infrastructure**: Ray-based parallel environments, FSDP training, vLLM/sglang
- **Requirements**: 2+ GPUs minimum, Ray, vLLM
- **Verdict**: Best fit. Purpose-built for multi-turn VLM agent training.

### D. VAGEN / VAGEN-Lite

**Repository**: [RAGEN-AI/VAGEN](https://github.com/RAGEN-AI/VAGEN)

- Built on verl's `agent_loop` abstraction (same ecosystem as verl-agent)
- **Bi-Level GAE** for turn-aware credit assignment
- 3B model achieved 0.82 across 5 agent benchmarks (outperforming GPT-5 at 0.75)
- VAGEN-Lite (Feb 2026): lightweight reimplementation for easier customization
- **Environment protocol**: `GymImageEnv` — async `reset(seed)`, `step(action_str)`,
  `close()`, `system_prompt()`. This is the interface we implemented.
- **Verdict**: Excellent. We implemented its `GymImageEnv` protocol. Compatible
  with both VAGEN and verl-agent.

### E. OpenRLHF

**Repository**: [OpenRLHF/OpenRLHF](https://github.com/OpenRLHF/OpenRLHF)

- Supports multimodal models via [OpenRLHF-M](https://github.com/OpenRLHF/OpenRLHF-M)
  fork (LMM-R1 lineage), tested with Qwen2.5-VL and InternVL
- Implements GRPO, PPO, REINFORCE++ with Ray-based distributed training
- **Multi-turn agent support**: Added in 2025 — `AgentTrainer` with `env_rollout`
  function for Gym-style interaction. Text-based multi-turn works; multi-turn
  VLM with per-step images less documented but architecturally feasible
- **No per-step credit assignment**: Episode-level rewards only (same limitation
  as our standalone trainer)
- **Verdict**: Viable alternative for multi-turn VLM with strong distributed
  training. Lacks GiGPO-style step-level credit assignment, which is the key
  differentiator for long-horizon desktop tasks.

### F. Unsloth

**Repository**: [unslothai/unsloth](https://github.com/unslothai/unsloth)

- 1.5-2x speed, 90% less VRAM for Qwen3-VL/Gemma 3
- **Single-turn VLM GRPO**: Works. `UnslothGRPOTrainer` wraps TRL's GRPOTrainer
  with kernel optimizations. Tested with Qwen2.5-VL, Gemma 3, Llama 3.2-Vision.
- **Multi-turn text**: Supported via ART (Agent Reinforcement Training, OpenPipe
  collaboration). Text-only multi-turn environments work with `rollout_func`.
- **Multi-turn VLM**: NOT supported. `rollout_func` is silently ignored by
  `UnslothGRPOTrainer` ([#3573](https://github.com/unslothai/unsloth/issues/3573)),
  preventing custom environment interaction. Multi-GPU VLM training also broken
  ([#3571](https://github.com/unslothai/unsloth/issues/3571)).
- **Verdict**: Not applicable for our use case. Multi-turn VLM RL is blocked by
  the `rollout_func` issue. If resolved, Unsloth's VRAM savings could make it
  attractive for single-GPU experimentation, but it still lacks per-step credit
  assignment (GiGPO) and distributed training.

### Comparison Matrix

| Feature                     | TRL    | Standalone | verl-agent | VAGEN  | OpenRLHF | Unsloth  |
|-----------------------------|--------|------------|------------|--------|----------|----------|
| Single-turn VLM GRPO        | Yes    | Yes        | Yes        | Yes    | Yes      | Yes      |
| Multi-turn VLM GRPO         | **No** | Yes*       | **Yes**    | **Yes**| Partial† | **No**‡  |
| Per-step credit assignment   | No     | No         | **GiGPO**  | **GAE**| No       | No       |
| Distributed training         | Yes    | No         | Yes        | Yes    | Yes      | No§      |
| vLLM/sglang acceleration    | Yes    | No         | Yes        | Yes    | Yes      | No       |
| Qwen2.5-VL tested           | Yes    | Yes        | Yes        | Yes    | Yes      | Yes      |
| Lines of code we maintain   | ~200   | ~546       | **~250**   | ~250   | ~200     | ~200     |
| Ease of adoption             | High   | N/A        | Medium     | Medium | Medium   | High     |

*Standalone multi-turn VLM works but only has episode-level rewards.
†OpenRLHF has AgentTrainer for multi-turn text; VLM multi-turn less documented.
‡Unsloth `rollout_func` silently ignored (#3573), blocking multi-turn VLM.
§Unsloth multi-GPU VLM broken (#3571).

---

## Architecture

```
verl-agent / VAGEN                  openadapt-evals
┌─────────────────────┐             ┌──────────────────────┐
│ GRPOTrainer / GiGPO │             │ WAADesktopEnv        │
│  ↓                  │ GymImageEnv │  ↓                   │
│ AgentLoop           │ ──protocol──│ RLEnvironment        │
│  ↓                  │             │  ↓                   │
│ rollout_worker      │             │ WAALiveAdapter       │
│  (vLLM/sglang)      │             │  ↓                   │
│                     │             │ WAA Flask Server      │
│ They handle:        │             │ We handle:           │
│ - VLM forward pass  │             │ - Desktop automation │
│ - Log-prob storage  │             │ - Task setup/eval    │
│ - GiGPO advantages  │             │ - Action translation │
│ - FSDP training     │             │ - Screenshot capture │
│ - Checkpointing     │             │ - Stuck detection    │
└─────────────────────┘             └──────────────────────┘
```

Our adapter (`WAADesktopEnv`, ~250 lines) translates between:
- **openadapt-evals**: `BenchmarkObservation` (PNG bytes + a11y tree)
- **VAGEN**: `{"obs_str": "...", "multi_modal_input": {"<image>": [PIL.Image]}}`

The `RLEnvironment` in openadapt-evals is the **stable interface**. If
verl-agent is superseded (e.g., TRL fixes #5120), we swap the training backend
by writing a new 250-line adapter. The environment code doesn't change.

---

## What We Get for Free

By delegating to verl-agent, we avoid building and maintaining:

| Capability                        | Lines saved | Complexity saved       |
|-----------------------------------|-------------|------------------------|
| Multi-turn VLM rollout collection | ~200        | Image tensor management|
| GiGPO step-level advantages      | ~300        | State grouping logic   |
| Distributed training (FSDP)      | ~500        | Multi-GPU coordination |
| vLLM/sglang inference            | ~400        | Inference server mgmt  |
| Reference model management       | ~100        | Weight synchronization |
| Advanced logging (WandB, TB)     | ~100        | Metric tracking        |
| **Total**                         | **~1600**   |                        |

---

## Integration: VAGEN Environment Registry

Our `WAADesktopEnv` implements VAGEN's `GymImageEnv` protocol (async
`reset`/`step`/`close`/`system_prompt`), which is the **native environment
interface** for VAGEN. No additional adapter is needed.

**Note**: Earlier analysis referenced an `EnvironmentManagerBase` ABC and a
`make_envs()` dispatch function. These do not exist in the current VAGEN
codebase. The actual architecture uses:

- `GymBaseEnv` → `GymImageEnv` — the environment ABC (which we implement)
- `vagen/envs/registry.py` — YAML-based env registry for dispatch
- `GymAgentLoop` — training-time rollout orchestrator that instantiates envs

Integration steps (automated by `scripts/train_verl_e2e.py`):

1. **Register in VAGEN's env registry** — add `WAADesktop:
   openadapt_evals.adapters.verl_env.WAADesktopEnv` to
   `vagen/configs/env_registry.yaml`. This is the only configuration needed.
2. **Prepare parquet data** — VAGEN's `AgenticDataset` requires train/val
   parquet files even for env-based training
3. **Configure training** — provide env spec (server URL, task ID, max turns)
   via the VAGEN training YAML (see `configs/train_waa_vagen.yaml`)

The `GymImageEnv` protocol is our **portable interface**. If we switch to a
different framework, only the ~250-line `WAADesktopEnv` adapter changes. The
environment, evaluation, and infrastructure code remain untouched.

### VAGEN Remote Env Pattern (Optional)

For production deployments where the WAA VM and GPU VM have poor connectivity,
VAGEN provides a remote env service pattern:

- **Server** (WAA VM): `BaseGymHandler` + `build_gym_service()` → FastAPI
- **Client** (GPU VM): `GymImageEnvClient` (registered as `RemoteEnv`)

This adds HTTP session management, multipart encoding (JSON + images), and
automatic retry/failover. Currently unnecessary since `WAADesktopEnv` already
handles remote connectivity via the WAA Flask API, but documented for future
scaling to multi-VM env pools.

---

## Migration Path

1. **Current state**: Standalone trainer in openadapt-ml (PR #34, merged).
   Works, well-tested (56 unit tests + 5 E2E tests). Episode-level rewards only.

2. **Spike complete**: `WAADesktopEnv` adapter in openadapt-evals (PR #84, merged).
   40 tests passing. Implements GymImageEnv protocol.

3. **Next**: Test end-to-end with verl-agent on a GPU machine. If successful,
   the standalone trainer becomes a reference implementation / fallback, and
   verl-agent becomes the recommended training path. **Note**: Both backends
   coexist — see the [Dual Backend Strategy](#dual-backend-strategy) section
   for the comparison plan and dependency approach.

4. **Future**: If TRL resolves #5120 (multi-turn VLM support), evaluate whether
   to switch. TRL has broader adoption; switching would reduce the dependency
   footprint. But only if TRL also adds per-step credit assignment comparable
   to GiGPO.

---

## Dual Backend Strategy

Rather than deprecating the standalone trainer immediately, we maintain both
backends for comparison:

### Backend 1: Standalone (openadapt-ml)

- **Code**: `openadapt_ml/training/grpo/trainer.py` (~546 lines)
- **When to use**: Quick experiments, single-GPU, no Ray/vLLM dependency
- **Limitations**: Episode-level rewards only, no GiGPO, no distributed training
- **Config**: `GRPOConfig(backend="standalone", ...)`

### Backend 2: verl-agent (openadapt-evals)

- **Code**: `openadapt_evals/adapters/verl_env.py` (~250 lines adapter)
- **When to use**: Production training, multi-GPU, GiGPO per-step credit
- **Advantages**: Distributed training, vLLM/sglang, step-level advantages
- **Config**: `configs/train_waa_vagen.yaml`

### Dependency Strategy

The `GymImageEnv` and `GymBaseEnv` abstract base classes (~150 lines) are
**vendored** into `openadapt_evals/adapters/_vendored/` to avoid requiring the
full VAGEN installation. The vendored classes are pure interfaces with only a
`Pillow` dependency. Import priority:

1. `from vagen.envs.gym_image_env import GymImageEnv` (if VAGEN installed)
2. `from openadapt_evals.adapters._vendored.gym_image_env import GymImageEnv` (fallback)

The full VAGEN/verl-agent stack (Ray, vLLM, etc.) is only needed when actually
running distributed training, not for defining or testing environments.

### Comparison Plan

To validate the verl-agent integration provides real value over standalone:

1. Train on the same WAA task with both backends
2. Compare: final reward, training wall time, GPU memory usage
3. Specifically measure whether GiGPO's per-step credit improves sample
   efficiency on long-horizon tasks (15+ steps)
4. Document results in a comparison report

---

## Addendum: VAGEN vs verl-agent Clarification (2026-03-16)

**Status**: Corrects earlier conflation of VAGEN and verl-agent as a single project.

### Finding: VAGEN and verl-agent Are Separate Projects

Earlier sections of this document (and our training scripts) treated "verl-agent/VAGEN"
as a single system. Research in March 2026 revealed they are **separate projects**
with different interfaces, capabilities, and trajectories:

| Aspect | VAGEN (mll-lab-nu/VAGEN) | verl-agent (langfengQ/verl-agent) |
|--------|--------------------------|-----------------------------------|
| **Focus** | Environment framework | Training framework |
| **Key abstraction** | `GymImageEnv` protocol | `env_base.py` (own env interface) |
| **Algorithms** | GRPO, PPO only (on main/Lite) | GiGPO, GRPO, PPO, RLOO, DAPO, GSPO, REINFORCE++ |
| **Credit assignment** | Bi-Level GAE (legacy branch only) | GiGPO (active, on main) |
| **Entry point** | `vagen.main_ppo` | Own entry point + Hydra config system |
| **Current state** | Migrated to "VAGEN-Lite" (Feb 2026) | Actively maintained with GiGPO |

**VAGEN-Lite** (the current main branch) dropped Bi-Level GAE and only supports
standard GRPO/PPO. The original features survive only on a legacy branch. This means
VAGEN-Lite provides no advantage over our standalone trainer for per-step credit
assignment.

**verl-agent** is the project that actually implements GiGPO. It uses its **own**
`env_base.py` environment interface, NOT `GymImageEnv`. Our `train_verl_e2e.py`
script targets `vagen.main_ppo`, which is the wrong entry point for GiGPO training.

### Impact on Our Integration

1. **`train_verl_e2e.py` targets the wrong entry point** — It calls `vagen.main_ppo`
   but GiGPO lives in verl-agent, which has a different entry point and Hydra config
   system. This script will need retargeting.

2. **`configs/train_waa_vagen.yaml` needs updating** — The YAML is structured for
   VAGEN's config system, not verl-agent's.

3. **VAGEN-Lite is not useful for us** — It only provides vanilla GRPO/PPO, which
   our standalone trainer (`openadapt_ml/training/grpo/trainer.py`) already does.
   There is no advantage to adding the VAGEN dependency for equivalent functionality.

4. **GymImageEnv vendoring remains correct** — Our vendored `GymImageEnv` in
   `openadapt_evals/adapters/_vendored/` is a clean, stable environment interface
   regardless of which training framework consumes it. It stays.

### Corrected Path Forward

- **Phase 1 (now)**: Use the standalone GRPO trainer in openadapt-ml for initial
  validation. It works today, is well-tested (56 unit + 5 E2E tests), and has no
  external framework dependencies.

- **Phase 2 (if GiGPO per-step credit is needed)**: Integrate with **verl-agent
  directly** (not VAGEN-Lite). This requires writing a thin adapter from
  `WAADesktopEnv` to verl-agent's `env_base.py` interface. The `compute_anchor_state()`
  function (screenshot hashing for state grouping) is directly applicable to GiGPO's
  state-matching requirement.

- **Keep vendored GymImageEnv** as our stable environment interface. It decouples
  environment definition from training framework choice.

### What Remains Valid From the Original Decision

The core reasoning in this document still holds:

- **GiGPO is still the right algorithm** for per-step credit assignment in 15+ step
  desktop automation tasks. The algorithm itself is sound; only the project that
  implements it was misidentified.

- **WAADesktopEnv adapter is solid and reusable** — ~250 lines of well-tested glue
  code that translates between our `RLEnvironment` and any Gym-like training interface.

- **`compute_anchor_state()` is directly applicable** to GiGPO's state grouping
  mechanism. Screenshots at identical UI states hash to the same anchor, enabling
  cross-rollout step-level advantage computation.

- **TRL still cannot do multi-turn VLM RL** — Issues
  [#5119](https://github.com/huggingface/trl/issues/5119) and
  [#5120](https://github.com/huggingface/trl/issues/5120) remain OPEN as of
  March 2026.

- **"The environment is the moat, not the training math"** — This principle is
  reinforced by this correction. Training frameworks come and go (VAGEN already
  pivoted to Lite); our WAA RL environment is the durable asset.

### Updated Architecture Diagram

```
Phase 1 (Current)                    Phase 2 (If GiGPO needed)
┌─────────────────────┐              ┌─────────────────────┐
│ Standalone GRPO     │              │ verl-agent           │
│ (openadapt-ml)      │              │  GiGPO trainer       │
│  ↓                  │              │  ↓                   │
│ trainer.py          │              │ env_base.py adapter  │
│  ↓                  │              │  (NEW, ~100 lines)   │
│ WAADesktopEnv       │              │  ↓                   │
│  (GymImageEnv)      │              │ WAADesktopEnv        │
│  ↓                  │              │  (GymImageEnv)       │
│ RLEnvironment       │              │  ↓                   │
│  ↓                  │              │ RLEnvironment        │
│ WAA Flask Server    │              │  ↓                   │
└─────────────────────┘              │ WAA Flask Server     │
                                     └─────────────────────┘
```

---

## References

- [VAGEN](https://github.com/RAGEN-AI/VAGEN) — Multi-turn VLM agent training (GiGPO)
- [verl-agent](https://github.com/langfengQ/verl-agent) — GiGPO paper's original codebase
- [verl](https://github.com/verl-project/verl) — Volcano Engine RL for LLMs
- [GiGPO paper](https://arxiv.org/html/2505.10978) — Group-in-Group Policy Optimization
- [VAGEN paper](https://arxiv.org/abs/2510.16907) — World Model Reasoning for VLM Agents
- [TRL #5120](https://github.com/huggingface/trl/issues/5120) — Multimodal rollout pipeline
- [TRL #5119](https://github.com/huggingface/trl/issues/5119) — Backend/rollout decoupling
- [TRL GRPOTrainer docs](https://huggingface.co/docs/trl/main/grpo_trainer)
- [TRL PR #3072](https://github.com/huggingface/trl/pull/3072) — Original VLM support
