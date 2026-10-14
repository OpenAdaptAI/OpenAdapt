# OpenAdapt: General-Purpose Computer Use Framework with Automated Experimentation

**Status**: Proposal
**Authors**: Richard Abrich, Claude
**Date**: 2026-03-16
**Related**: `openadapt-ml/docs/design/lora_per_task.md`, `verl_agent_decision.md`, `demo_retrieval_design.md`

---

## 1. Reframing: OpenAdapt Is a Framework, Not a Method

OpenAdapt is often described in terms of its most novel capability — trajectory-conditioned disambiguation via demo retrieval. But the codebase is far more general than that. It is a **general-purpose computer use framework** that supports:

- **10 agent implementations** (API, computer_use, Qwen3-VL, PolicyAgent, retrieval-augmented, scripted, etc.)
- **Abstract adapter/agent interfaces** (`BenchmarkAgent`, `BenchmarkAdapter`) — any agent can run on any benchmark
- **Multiple training approaches** (SFT via TRL/Unsloth, GRPO standalone, GiGPO via verl-agent)
- **Modular evaluation** with done-gate verification, execution trace collection, wandb integration
- **RL environment** (Gymnasium-style `RLEnvironment` + VAGEN `WAADesktopEnv`)
- **Production execution layer** (`openadapt-agent` with safety gates and session management)

Demo-conditioning, LoRA-per-task, and GRPO are all **experiment tracks within this framework** — not the framework itself.

### 1.1 Why This Framing Matters

The literature shows no single approach dominates GUI agent performance:

| Approach | Best Result | Where |
|----------|-------------|-------|
| Pure prompting (Agent S3) | 72.6% | OSWorld |
| SFT + multi-turn RL (UI-TARS-2) | 47.5% | OSWorld (open-source) |
| SFT on demo traces (ShowUI-Aloha) | +26.6pp | Custom benchmark |
| Online RL from scratch (DigiRL) | +49.5pp over SFT | Android-in-the-Wild |
| Self-evolving curriculum RL (WebRL) | 42.4% (from 4.8%) | WebArena-Lite |
| Step-wise GRPO + curriculum (DART-GUI) | 42.13% | OSWorld (best open RL) |
| Hierarchical plan/execute RL (HiPER) | 97.4% ALFWorld | ALFWorld |

The winning recipe is unknown and likely task-dependent. This argues for **systematic experimentation across approaches**, not betting on one.

### 1.2 OpenAdapt's Competitive Advantage

OpenAdapt's moat is not any single approach — it's the **evaluation infrastructure and environment**:
- WAA (Windows Agent Arena) integration with automated setup/teardown
- Azure VM pool management with automated provisioning
- Lossless canonical data schema preserving raw benchmark data
- Execution trace collection feeding training pipelines
- Done-gate verification for reliable scoring

As Karpathy's verl-agent decision doc noted: "The environment is the moat, not the training math."

## 2. Experiment Tracks

### 2.1 Current Tracks

| Track | Status | Approach | Key Question |
|-------|--------|----------|--------------|
| **Demo-conditioning** | Active (DemoController, RetrievalAgent) | In-context demos for disambiguation | Does trajectory retrieval improve accuracy? |
| **LoRA-per-task** | Proposed (`lora_per_task.md`) | Task-specific weight specialization + routing | Can overfit LoRAs + routing beat general agents? |
| **GRPO** | Active (standalone + verl backend) | Online RL with group advantages | Can RL improve over SFT baseline? |
| **SFT baseline** | Active (TRL trainer) | Supervised fine-tuning on demos | What's the SFT ceiling? |
| **API agents** | Active (Claude, GPT) | Prompted commercial VLMs | What's the prompted ceiling? |

### 2.2 Tracks Suggested by Literature

| Track | Approach | Literature Support | Effort |
|-------|----------|-------------------|--------|
| **Step-wise GRPO** | Per-step credit assignment (GiGPO, DART-GUI style) | DART-GUI +14.6pp OSWorld, GiGPO +12% ALFWorld | Medium — extend existing GRPO |
| **Hierarchical RL** | Separate planner + executor (HiPER) | HiPER 97.4% ALFWorld, +8.3% WebShop | High — new training architecture |
| **Self-evolving curriculum** | Auto-generate tasks from failures (WebRL) | WebRL: 4.8% → 42.4% on WebArena-Lite | Medium — extend eval runner |
| **Environment synthesis** | Auto-create training environments (GUI-Genesis) | GUI-Genesis: 10x latency reduction, +14.5% | High — new capability |
| **PPO with value model** | PPO instead of GRPO (UI-TARS-2 finding) | UI-TARS-2: PPO more stable than GRPO | Medium — add PPO trainer |
| **LoRA composition** | Compose skill-LoRAs for novel tasks (LoRAHub) | LoRAHub matches ICL with zero-shot throughput | Low — extend LoRA-per-task |
| **Data flywheel** | Continuous data generation + retraining (UI-TARS-2) | UI-TARS-2: automatic weakness targeting | Medium — orchestration layer |
| **UI-Venus base model** | Start from GUI-pretrained VLM instead of generic Qwen | UI-Venus-1.5-8B: AndroidWorld 77.6%, ScreenSpot-V2 96.2% | Low — config change |
| **ARPO (experience replay)** | GRPO augmented with replay buffer | ARPO: 29.9% OSWorld, SOTA RL-trained | Medium — extend GRPO |

### 2.3 UI-Venus as a Base Model Experiment

[UI-Venus](https://github.com/inclusionAI/UI-Venus) (Ant Group, Feb 2026) is the current SOTA open-source GUI agent. Its 4-stage training pipeline (mid-training on 10B GUI tokens → offline GRPO → online GRPO → TIES-Merge) produces models that already understand GUI grounding and navigation.

**Key insight:** UI-Venus model weights are Apache 2.0 on HuggingFace, but **training code is NOT open-sourced**. We can use the models as a pretrained starting point — getting Stages 1-3 for free — and fine-tune on our tasks.

| Model | Size | Fits on A10G? | Key Benchmark |
|-------|------|---------------|---------------|
| `inclusionAI/UI-Venus-1.5-2B` | 2B | Yes (easily) | Lightweight agent |
| `inclusionAI/UI-Venus-1.5-8B` | 8B | Yes (LoRA) | Best size/quality for fine-tuning |
| `inclusionAI/UI-Venus-1.5-30B-A3B` | 30B MoE (3B active) | Borderline | SOTA but large |

**The experiment:** Does starting from UI-Venus instead of vanilla Qwen3-VL improve Core4 scores? This is a one-line config change:

```yaml
# Experiment A: Qwen3-VL base
base_model: Qwen/Qwen3-VL-8B-Instruct

# Experiment B: UI-Venus base
base_model: inclusionAI/UI-Venus-1.5-8B
```

Then run LoRA-per-task SFT and/or GRPO from each base and compare.

### 2.4 The Experiment Matrix

Each track can be evaluated on the same benchmarks using the same infrastructure:

```
                    ┌─────────────────────────────────────┐
                    │        OpenAdapt Eval Harness       │
                    │  (WAA + RLEnvironment + done-gate)  │
                    └──────────────┬──────────────────────┘
                                   │
         ┌─────────────┬───────────┼───────────┬──────────────┐
         │             │           │           │              │
    Demo-Cond    LoRA/task      GRPO      SFT+RL       API Agents
    (retrieval)  (routing)   (standalone)  (pipeline)   (Claude/GPT)
         │             │           │           │              │
         └─────────────┴───────────┴───────────┴──────────────┘
                                   │
                    ┌──────────────┴──────────────────────┐
                    │      Same metrics, same tasks       │
                    │  score, steps, latency, cost, ...   │
                    └─────────────────────────────────────┘
```

## 3. Automated Experimentation Architecture

### 3.1 The Autoresearch Pattern

Karpathy's autoresearch ([github.com/karpathy/autoresearch](https://github.com/karpathy/autoresearch), 38K stars, March 2026) proved that `hypothesis → modify → evaluate → keep/discard` loops, when automated, outperform manual research. In one session: 125 experiments in 10.5 hours, val_bpb 0.9979 → 0.9697 (24 kept improvements) — finding a bug (QK-Norm missing scalar multiplier) that Karpathy himself missed. Over 700 experiments in 2 days, 11% faster Time-to-GPT-2.

**The shocking insight: there is no orchestration code.** The entire "system" is:
- `program.md` — a prompt document fed to Claude Code (the agent runtime)
- `train.py` — one mutable file the agent edits
- `prepare.py` — one immutable file with the evaluation function
- Claude Code running in the repo directory with instruction "NEVER STOP"

No scheduler, no experiment tracker, no Python framework. The agent uses its built-in ability to edit files and run bash commands. Git is the experiment tracker. `results.tsv` (untracked) is the private lab notebook.

**The exact experiment loop (from `program.md`):**

1. Agent reads git log + results.tsv to see what's been tried
2. Agent edits `train.py` (architecture, optimizer, hyperparams — anything)
3. `git add train.py && git commit -m "short description"`
4. `uv run train.py > run.log 2>&1` (output redirected to avoid context pollution)
5. `grep "^val_bpb:" run.log` (extract the one metric that matters)
6. If val_bpb improved → **keep** (commit stays, branch advances)
7. If worse or equal → **discard** (`git reset --hard HEAD~1`)
8. If crash → `tail -n 50 run.log`, attempt fix or log as crash, discard
9. Append row to `results.tsv`: `commit<TAB>val_bpb<TAB>memory_gb<TAB>status<TAB>description`
10. GOTO 1. **NEVER STOP.**

**Core design principles:**
- **One mutable file** — agent's full context fits in one read, diffs are small, failures trivially revertible
- **Fixed-time evaluation** — every experiment takes exactly 5 minutes, making any two directly comparable
- **Git as a ratchet** — branch only advances on improvements; `git reset --hard HEAD~1` on everything else
- **Output redirect** — `> run.log 2>&1` prevents training output from flooding the agent's context window
- **results.tsv as private memory** — records ALL attempts (including failures) but is NOT committed; the git history only shows successes
- **The LLM agent can make arbitrary code changes**, not just hyperparameter sweeps — this is what differentiates it from Optuna/Ray Tune/W&B Sweeps

### 3.2 Mapping Autoresearch to OpenAdapt

| Autoresearch Component | OpenAdapt Equivalent |
|----------------------|---------------------|
| `train.py` (mutable) | Agent prompt, controller config, training hyperparams, action parsing |
| `prepare.py` (immutable) | WAA eval harness, benchmark tasks, scoring functions |
| `program.md` (strategy) | Experiment strategy document (what to explore, constraints) |
| `val_bpb` (metric) | Mean task score across benchmark suite |
| 5-minute budget | Max-steps budget per eval run |
| Git branch per session | Git branch per experiment track |
| `results.tsv` | Eval results directory + wandb |

### 3.3 The Oracle Problem: Why GUI Agent Eval Is Fundamentally Harder

The autoresearch pattern works because `val_bpb` is continuous, low-variance, and cheap to compute. **GUI agent evaluation has none of these properties.** This is the single biggest difference and the thing most likely to make the search loop fail.

| Aspect | Autoresearch (LLM training) | OpenAdapt (GUI agent eval) |
|--------|---------------------------|---------------------------|
| Experiment time | 5 min (fixed, predictable) | 30+ min (variable, per-task) |
| Throughput | ~12/hr, ~100 overnight | ~2/hr, ~16 overnight |
| Metric | val_bpb (continuous, low-variance) | Task success rate (binary per task, high-variance) |
| Determinism | Near-deterministic (fixed seed) | Non-deterministic (UI state, timing, popups) |
| Failure modes | OOM, NaN, Python crashes | Infra failures, VM timeouts, app state, socat proxies |
| Mutable surface | Model architecture (Python code) | Agent prompts + config (natural language + YAML) |
| Oracle reliability | Very high (math is math) | Variable (evaluator can be wrong, infra can fail) |

**Core risk: building an elaborate hill-climber on a noisy oracle.** If the evaluator is weak or the environment is flaky, search will select garbage confidently — prompts that game the evaluator or coincidentally avoid infra failure modes, not prompts that improve agent behavior.

#### 3.3.1 Tiered Oracle Architecture

The solution is not one eval metric. It is a **tiered oracle** where cheap checks gate expensive ones:

| Tier | What | Cost | Purpose |
|------|------|------|---------|
| **0: Static constraints** | Prompt length, schema validity, forbidden patterns, no parsing regressions | Free | Prune garbage before running anything |
| **1: Offline proxies** | Action-parser accuracy on labeled traces, grounding accuracy on frozen screenshots, retrieval hit-rate | Seconds | Fast triage — imperfect but high-throughput |
| **2: Replay evals** | WAA `/evaluate` on fixed tasks, repeated runs, seeded VMs where possible | 30 min | Primary signal source for search loop |
| **3: Live evals** | Real workflows, changing UI states, production-like conditions | Hours | Promotion-grade evidence, not inner-loop |
| **4: Post-hoc adjudication** | VLM grading, rule-based checks, human review on sample | Variable | Ambiguous cases, evaluator calibration |

**For MVP, Tier 0 + Tier 2 is sufficient.** Add Tier 1 when you have labeled trace corpora. Add Tier 3 and 4 as the system proves itself.

#### 3.3.2 Multi-Objective Scoring (Not Single Scalar)

A single `if score > best: keep` is dangerous. The acceptance rule should be **lexicographic**:

1. **Reject** if safety/invariant constraints fail (Tier 0)
2. **Reject** if success rate drops beyond tolerance (e.g., >10% regression on any single task)
3. **Reject** if cost/latency exceeds budget (e.g., >2x token spend)
4. **Among survivors**, prefer higher success rate
5. **Tiebreak** on fewer retries, lower latency, lower cost
6. **Require minimum effect size** — improvement must exceed noise floor (e.g., 2/N more tasks succeed across repeated runs)

Without this, the search will game the oracle — finding prompts that technically "succeed" by lowering the bar rather than improving behavior.

#### 3.3.3 The First Thing to Optimize Is the Evaluator

**Before running autoresearch on agent prompts, run it on evaluator prompts.**

- Mutate: evaluator prompt, grading rubric, scoring thresholds
- Measure against: human-labeled adjudication set (manually score 50-100 task outcomes)
- Optimize for: evaluator agreement with human labels, calibration, robustness
- Detect: reward hacking, evaluator drift, false positives/negatives

This is counterintuitive — you want to improve the agent, not the evaluator — but a weak evaluator makes everything downstream unreliable. The evaluator is the foundation.

#### 3.3.4 Mitigations for Environment Variance

1. **Repeated runs**: Each experiment runs N times (N=3 minimum). Use median score, not single run.
2. **Canary baselines**: Periodically re-run a known-good config as a canary. If the canary score drops, the environment is flaky, not the candidate.
3. **Hold-out workflows**: Optimize on tasks A,B,C but validate on held-out task D. Catches evaluator gaming.
4. **Manual audit**: Sample 10% of "improvements" for human review. Catches false positives early.
5. **Confidence threshold**: Require improvement to exceed a minimum effect size (e.g., 2+ more task successes across repeated runs) to filter noise.

### 3.3.5 Mutation Surface Ordering: Signal-to-Blast-Radius

Not all "small" surfaces are equally useful. Order by **highest signal, lowest coupling**:

| Priority | Surface | Why First |
|----------|---------|-----------|
| 1 | **Evaluator prompt / grading rubric** | Improves the oracle itself; high leverage; easy attribution |
| 2 | **Action parsing prompt for one workflow family** | Localized behavior; direct offline metrics possible; easy A/B on labeled traces |
| 3 | **Recovery policy for one failure mode** | Narrow scope (e.g., stale element, modal interruption); high practical value |
| 4 | **Retrieval/ranking config for one subproblem** | Only if retrieval I/O is well-instrumented; otherwise coupling is too high |
| **Avoid early** | Global planner prompt, full retrieval policy, model choice, multi-file code mutations | Too coupled — you won't know what worked |

### 3.4 Two Levels of Automation

**Level 1: Hyperparameter/config search (autoresearch-style)**

The agent modifies configuration — prompts, thresholds, hyperparameters — and evaluates:

```
program.md: "Improve mean score on Core4 tasks by modifying the agent system prompt,
             demo retrieval threshold, done-gate max_overrides, and max_steps.
             Do not modify the evaluation harness or benchmark tasks.
             Each run takes ~30 min. Budget: 20 experiments."

Mutable surface:
  - Agent system prompt template
  - Demo retrieval similarity threshold
  - Done-gate parameters
  - Controller retry/replan logic
  - Max steps per task

Immutable surface:
  - WAA VM + evaluate endpoint
  - Benchmark task definitions
  - Scoring function
```

**Level 2: Architecture search (wright-style)**

The agent modifies code — new agent types, training approaches, action representations — tests, and PRs:

```
Wright task: "Implement step-wise GRPO credit assignment in
             openadapt-ml/training/grpo/trainer.py. Use the GiGPO approach:
             group rollouts by screenshot hash at each step, compute per-step
             advantages within groups. Run the GRPO test suite to validate.
             Then run a 3-task eval and compare against the baseline GRPO."

Wright loop:
  1. Clone openadapt-ml
  2. Claude implements GiGPO in trainer.py
  3. Run pytest → fix failures → iterate
  4. Run eval comparison script
  5. PR with results
```

### 3.5 Wright vs. Autoresearch: Complementary, Not Competing

| Dimension | Autoresearch | Wright |
|-----------|-------------|--------|
| **What it does** | Iterates on ONE file to optimize ONE metric | Implements code changes across a repo, tests, creates PRs |
| **Infrastructure** | Zero — it IS Claude Code + a prompt | Full stack (Supabase, Telegram, worker, queue) |
| **Human involvement** | Write `program.md`, go to sleep | Submit task via Telegram, review PR |
| **Scope of changes** | Single file, config-level (Level 1) | Any files, architecture-level (Level 2) |
| **Output** | Git branch with monotonic improvement | Pull request with test results |
| **Loop** | hypothesis → edit → eval → keep/discard (forever) | task → edit → test → fix → PR (once) |
| **Duration** | Runs for hours/days autonomously | One task, minutes to hours |
| **Analogy** | An automated researcher running 100 experiments overnight | An automated developer implementing a feature |

Wright handles **code changes** (implement new approaches, fix bugs, create PRs).
Autoresearch handles **experiment iteration** (try variations, keep what works, discard what doesn't).

**Together:**

```
┌─────────────────────────────────────────────────────────────┐
│                   EXPERIMENT ORCHESTRATOR                     │
│                                                               │
│  1. Wright implements a new approach (Level 2)               │
│     → PR with code changes + test suite passing              │
│                                                               │
│  2. Autoresearch loop optimizes that approach (Level 1)      │
│     → Iterates on config/prompts within the new approach     │
│     → Git branch with best config + results.tsv              │
│                                                               │
│  3. Compare across approaches                                │
│     → Leaderboard: approach × task × metric                  │
│     → Wright creates summary PR with comparison table        │
│                                                               │
│  4. Data flywheel                                            │
│     → Successful trajectories → training data → retrain      │
│     → Failed trajectories → curriculum for next RL round     │
└─────────────────────────────────────────────────────────────┘
```

### 3.6 Practical Implementation Options

**Option A: Autoresearch pattern for OpenAdapt (no fork needed)**

No code to fork — just create a `program.md` and point Claude Code at it:

```markdown
# program.md (OpenAdapt experiment loop)
You are optimizing a GUI agent's performance on desktop automation tasks.

## Files
- `agent_config.yaml` - MUTABLE. Agent system prompt, planning template, thresholds.
- `eval.sh` - IMMUTABLE. Runs eval suite, outputs `mean_score: X.XX`.

## Loop
1. Read results.tsv and git log to understand what's been tried
2. Edit agent_config.yaml with a hypothesis
3. git add agent_config.yaml && git commit -m "description"
4. bash eval.sh > run.log 2>&1
5. grep "mean_score:" run.log
6. If score improved: KEEP. If worse: git reset --hard HEAD~1
7. Log to results.tsv (commit, score, status, description)
8. NEVER STOP.
```

This is literally all you need. Then: `claude --dangerously-skip-permissions` in the repo directory.

Fork `karpathy/autoresearch`, replace:
- `train.py` → agent config + prompt template
- `prepare.py` → eval runner script (calls WAA)
- `program.md` → OpenAdapt experiment strategy
- `val_bpb` → mean task score
- 5-min budget → 30-min eval budget (or shorter per-task budget)

Pros: Proven pattern, minimal code, git-as-memory
Cons: Single-agent, synchronous, no multi-GPU

**Option B: Wright with ML experiment extensions**

Extend wright's `TestRunner` to support `ml-eval` type:
- Custom parser for eval output (scores, not pass/fail)
- Longer timeouts (30+ min per eval)
- GPU VM dispatch (SSH into Azure/AWS GPU instance)
- Result persistence beyond pass/fail (metrics, comparison tables)

Pros: Already handles code changes + PRs, Telegram notifications, budget control
Cons: Requires extensions, no git-as-memory pattern

**Option C: Hybrid — Wright for Level 2, autoresearch for Level 1**

Use wright for implementing new approaches (code changes → tests → PRs).
Use autoresearch-style loop for optimizing each approach (config iteration → eval → keep/discard).

Pros: Best of both, clean separation of concerns
Cons: Two systems to maintain

**Recommendation: Option C.** The two tools solve different problems and compose naturally.

## 4. Experiment Management Infrastructure

### 4.1 Experiment Registry

Extend the existing eval results structure:

```
experiments/
├── registry.jsonl                    # All experiment runs
├── tracks/
│   ├── demo_conditioning/
│   │   ├── config.yaml              # Track-level defaults
│   │   └── runs/
│   │       ├── dc_baseline_20260316/
│   │       └── dc_multilevel_20260316/
│   ├── lora_per_task/
│   │   ├── config.yaml
│   │   └── runs/
│   │       ├── lora_writer_font_20260316/
│   │       └── lora_calc_formulas_20260316/
│   ├── grpo/
│   │   ├── config.yaml
│   │   └── runs/
│   │       ├── grpo_standalone_20260316/
│   │       └── grpo_gigpo_20260316/
│   └── api_baseline/
│       ├── config.yaml
│       └── runs/
│           ├── claude_opus_20260316/
│           └── gpt5_20260316/
└── leaderboard.json                  # Auto-generated comparison
```

### 4.2 Unified Experiment Config

```yaml
# experiment.yaml
track: lora_per_task
name: writer_font_rank8_dropout01
description: "LoRA rank 8 with dropout 0.1 on Writer font task"

agent:
  type: PolicyAgent
  lora_path: loras/writer_font_r8_d01/
  base_model: Qwen/Qwen2.5-VL-7B-Instruct

eval:
  adapter: waa_live
  task_ids: ["0e763496"]
  max_steps: 30
  done_gate: true
  done_gate_max_overrides: 3
  runs: 3  # Statistical significance

training:  # Optional — for tracks that require training
  method: sft
  data: trajectories/writer_font/
  epochs: 3
  lora_rank: 8
  lora_dropout: 0.1

compare_against:
  - demo_conditioning/dc_baseline
  - api_baseline/claude_opus
```

### 4.3 Leaderboard Generation

Auto-generated from experiment results:

```
| Track              | Agent          | Core4 Mean | Writer Font | Calc Formula | Steps (avg) | Cost/run |
|--------------------|----------------|------------|-------------|--------------|-------------|----------|
| api_baseline       | Claude Opus    | 0.45       | 1.0         | 0.3          | 22          | $2.10    |
| demo_conditioning  | DC + ApiAgent  | 0.50       | 1.0         | 0.4          | 18          | $2.50    |
| lora_per_task      | PolicyAgent    | TBD        | TBD         | TBD          | TBD         | $0.02    |
| grpo               | PolicyAgent    | TBD        | TBD         | TBD          | TBD         | $0.02    |
| sft_baseline       | PolicyAgent    | TBD        | TBD         | TBD          | TBD         | $0.02    |
```

## 5. Concrete Actions (Revised Ordering)

The correct sequencing is: **evaluator quality → narrow agent surfaces → broader search → training loops**. This is the opposite of the intuitive ordering ("optimize the agent first") but avoids building on a weak oracle.

### Phase 0: Build the Oracle (This Week)

1. **Build human-labeled eval corpus** — Manually score 50-100 task outcomes from existing Core4 runs. Record: task, agent action trace, human judgment (success/partial/fail), failure mode classification. This is the ground truth the evaluator is measured against.

2. **Measure evaluator reliability** — Run the WAA `/evaluate` endpoint on the labeled corpus. Compute agreement rate, false positive rate, false negative rate. If evaluator agreement is <80%, the oracle is too weak for search.

3. **Run evaluator-as-mutable-surface autoresearch** — First autoresearch target:
   - Mutable: evaluator prompt / grading rubric
   - Metric: agreement with human-labeled corpus
   - Budget: 10-20 experiments (fast — evaluator runs are seconds, not minutes)
   - This improves the oracle itself before using it to judge agents.

### Phase 1: Narrow Agent Optimization (Next 2 Weeks)

4. **Create autoresearch setup for one narrow surface** — Pick the highest signal-to-blast-radius mutation:
   - `eval.sh` (immutable): calls WAA eval harness with Tier 0 constraints + Tier 2 replay
   - One mutable YAML file (e.g., action parsing prompt for one workflow family)
   - `program.md` with lexicographic acceptance rule (not single scalar)
   - Repeated runs (N=3) per experiment, canary baseline checks
   - Budget: 10 experiments overnight (~5 hours)

5. **Add experiment registry + lineage** — Every candidate captures: exact diff, base config version, model version, task set, per-task scores, cost/latency, evaluator version. ~200 lines.

6. **Reframe public-facing materials** — OpenAdapt is a "general-purpose computer use framework with automated experimentation." Update README, docs, grant applications.

### Phase 2: Broader Search (Next Month)

7. **Expand mutable surfaces** — Add second and third mutation targets (recovery policy, retrieval config). Each gets its own autoresearch loop with isolated mutable file.

8. **Extend wright for ML experiments** — Add `ml-eval` test runner type:
   - Parses eval scores (not just pass/fail)
   - Supports longer timeouts (30+ min)
   - Can dispatch to GPU VM via SSH
   - Reports metrics in PR body

9. **LoRA-per-task Phase 0** — Train single-task LoRA on Writer font, evaluate against DemoController and API baseline using the now-trustworthy evaluator.

10. **Leaderboard dashboard** — Auto-generated comparison across all tracks.

### Phase 3: Training Loops (Next Quarter)

11. **Implement step-wise GRPO** — Use wright to implement GiGPO credit assignment. Compare against baseline GRPO.

12. **UI-Venus base model experiment** — Swap Qwen3-VL for UI-Venus-1.5-8B as base, measure Core4 delta.

13. **Data flywheel** — Successful executions → training data. Retrain LoRAs and RL policies on expanding datasets.

14. **Self-evolving curriculum** — WebRL-style task generation from failures.

## 6. Reproducibility as First-Class Output

For GUI agent optimization, lineage is not "nice to have." It is required to trust results.

Every candidate must capture:

| Artifact | Why |
|----------|-----|
| Exact mutable artifact diff | What changed |
| Base config version (git hash) | What it changed from |
| Model version | Which weights were used |
| VM/container image version | Environment reproducibility |
| Task set + seeds (where available) | What was evaluated |
| Screenshots / traces / logs | Evidence for manual audit |
| Evaluator version + prompt | Which oracle judged it |
| Per-task score breakdown | Not just the aggregate |
| Cost / latency / token stats | Secondary objectives |
| Repeated run variance | Confidence in the result |

Without this, you will not know whether you improved the agent or just got lucky with environment variance. This is where Wright's operational machinery adds value — not as a code editor, but as a **provenance system**.

## 7. What the Real System Is

The synergy between autoresearch and wright is NOT:
- "Wright for research" or "autoresearch for agents"

It IS:
> **A governed search-and-promotion system for agent behavior, built on top of a tiered oracle whose first job is to become trustworthy.**

The components:
- **Wright layer**: orchestration, queueing, branching, execution, artifacts, approvals
- **Experiment adapter layer**: `runEvalSuite()` against WAA / recordings / labeled corpora (not just `runTests()`)
- **Search policy layer**: propose mutations to prompts, configs, training params — constrained to one file at a time
- **Selection layer**: lexicographic multi-objective scoring with confidence thresholds
- **Registry layer**: experiment lineage, metrics, diffs, prompts, models, datasets
- **Promotion layer**: auto-open PRs only for winning changes, with evidence attached

The weak version (commodity): AI writes code and opens PRs.
The strong version (defensible): a search system for improving real-world task-execution agents, with reproducible lineage and trustworthy evaluation.

## 8. How This Changes the LoRA-per-Task Doc

The LoRA-per-task design (`openadapt-ml/docs/design/lora_per_task.md`) remains valid as-is — it's a detailed design for one experiment track. Add a note at the top linking it to this umbrella document:

> **Context**: This document describes one experiment track within the broader OpenAdapt experimentation framework. See `openadapt-evals/docs/design/experiment_framework.md` for the umbrella architecture and how this track compares to others (demo-conditioning, GRPO, SFT, API baselines).

## 9. How This Changes the Grant Applications

The GPU grant applications in `openadapt-internal` should be updated to frame OpenAdapt as:

- **Not**: "We need GPUs to train a demo-conditioned agent"
- **Instead**: "We need GPUs to run automated experimentation across multiple training approaches (SFT, GRPO, LoRA specialization, curriculum RL) on a production-grade computer use evaluation framework — with the autoresearch pattern enabling 10-20x more experiments per GPU-day than manual research"

This is a much stronger pitch because:
1. It positions OpenAdapt as infrastructure, not a single bet
2. It justifies GPU spend with experiment throughput metrics
3. It aligns with the autoresearch hype wave (38K stars, Karpathy endorsement)
4. It shows a systematic approach rather than "we think X will work"

## 10. Key Literature References

### Automated Experimentation
- Autoresearch (Karpathy, Mar 2026): [github.com/karpathy/autoresearch](https://github.com/karpathy/autoresearch)
- AutoResearch-RL (Mar 2026): [arXiv 2603.07300](https://arxiv.org/abs/2603.07300)

### GRPO for GUI Agents
- DART-GUI (Sep 2025): [arXiv 2509.23866](https://arxiv.org/abs/2509.23866) — Step-wise GRPO, 42.13% OSWorld
- ARPO (May 2025): [arXiv 2505.16282](https://arxiv.org/abs/2505.16282) — GRPO + experience replay, 29.9% OSWorld
- GiGPO (NeurIPS 2025): [arXiv 2505.10978](https://arxiv.org/abs/2505.10978) — Anchor state grouping
- HiPER (Feb 2026): [arXiv 2602.16165](https://arxiv.org/abs/2602.16165) — Hierarchical plan/execute RL
- RC-GRPO (Feb 2026): [arXiv 2602.03025](https://arxiv.org/abs/2602.03025) — Reward-conditioned GRPO
- MobileRL (2025): [arXiv 2509.18119](https://arxiv.org/abs/2509.18119) — Difficulty-adaptive GRPO

### RL for GUI Agents
- UI-TARS-2 (Sep 2025): [arXiv 2509.02544](https://arxiv.org/abs/2509.02544) — PPO > GRPO for stability
- DigiRL (NeurIPS 2024): [arXiv 2406.11896](https://arxiv.org/abs/2406.11896) — +49.5pp RL over SFT
- WebRL (ICLR 2025): [arXiv 2411.02337](https://arxiv.org/abs/2411.02337) — Self-evolving curriculum
- VAGEN (2025): [arXiv 2510.16907](https://arxiv.org/abs/2510.16907) — Multi-turn RL for VLM agents
- CRAFT-GUI (Aug 2025): [arXiv 2508.11360](https://arxiv.org/abs/2508.11360) — Curriculum GRPO
- Agent-R (Jan 2025): [arXiv 2501.11425](https://arxiv.org/abs/2501.11425) — MCTS recovery trajectories

### Environment Synthesis
- GUI-Genesis (Feb 2026): [arXiv 2602.14093](https://arxiv.org/abs/2602.14093) — Auto-synthesize web environments
- InfiniteWeb (Jan 2026): [arXiv 2601.04126](https://arxiv.org/abs/2601.04126) — Scalable web environment synthesis

### GUI Agent Models
- UI-Venus 1.5 (Ant Group, Feb 2026): [arXiv 2602.09082](https://arxiv.org/abs/2602.09082) — SOTA open-source, 4-stage GRPO pipeline
- UI-Venus 1.0 (Aug 2025): [arXiv 2508.10833](https://arxiv.org/abs/2508.10833) — Reinforcement Fine-Tuning
- ShowUI-Aloha (2025): [arXiv 2601.07181](https://arxiv.org/abs/2601.07181) — +26.6pp from demo traces

### Frameworks
- OSWorld (NeurIPS 2024): [os-world.github.io](https://os-world.github.io/)
- WindowsAgentArena: [microsoft.github.io/WindowsAgentArena](https://microsoft.github.io/WindowsAgentArena/)
- OpenHands: [arXiv 2407.16741](https://arxiv.org/abs/2407.16741)
- Agent-R1: [arXiv 2511.14460](https://arxiv.org/abs/2511.14460)
- verl: [github.com/volcengine/verl](https://github.com/volcengine/verl)

### Distributed Autoresearch
- hyperspaceai/agi: [github.com/hyperspaceai/agi](https://github.com/hyperspaceai/agi) — 35 agents, 333 experiments overnight via P2P gossip
