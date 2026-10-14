# Agent Improvement: Options and Tradeoffs

## Current State

- Qwen3-VL-2B fine-tuned on 3 demos: **0% on WAA** (degenerate click loop, overfit)
- Qwen3-VL-2B zero-shot: **0% on WAA** (conceptually correct actions, wrong coordinates)
- Claude ApiAgent: **0% on WAA** (hard tasks, coordinate accuracy issues)
- WAA Navi baseline (GPT-4V + SoM): **19.5%** on WAA
- Human: **74.5%** on WAA

**Root cause**: Every agent we've tried outputs raw pixel coordinates. Coordinate
prediction from screenshots is the hardest part of GUI automation. We're solving the
hardest subproblem first and failing.

**What's changed since v1 of this doc (Feb 25)**:
- Element-based action space shipped for all agents (PRs #45-47, v0.7.2-v0.9.0)
- `click_element(id)` / `type_element(id, text)` with `target_node_id` on BenchmarkAction
- XML a11y tree parsing in live adapter — element rects extractable from WAA
- Mock adapter scores 1.0 on element-based actions end-to-end
- **Not yet tested on live WAA** — need to validate both SoM and a11y quality

---

## The Core Thesis

**PC Agent-E achieved a 141% improvement from just 312 demo trajectories** — going from
~15% to 36% on WAA-V2. This is the single strongest validation of the OpenAdapt bet:
demos dramatically improve GUI agents, and you don't need millions of them.

Similarly, LearnAct showed a single demonstration can take success rate from 19.3% to
51.7%, and ShowUI-Aloha reached 60.1% with demo-conditioned planning.

The question isn't whether demos help — they clearly do. The question is how to
*generate enough quality demos autonomously* to feed the flywheel.

---

## Element Grounding Strategy

The core strategy is **element grounding** — the model selects from detected interactive
elements instead of predicting raw pixel coordinates. This is what separates Navi (19.5%)
from our 0% agents: Navi uses Set-of-Mark (SoM) to turn coordinate prediction into
element selection. Whether to use SoM, UIA, or both is an empirical question that Phase 0
answers.

### Element sources

| Source | How | Pros | Cons |
|--------|-----|------|------|
| **SoM (visual detection)** | Detection model (OmniParser/GroundingDINO) finds elements in screenshot, overlays numbered marks | Universal — works on any screen, no platform dependency | Needs detection model, extra latency, mark quality varies |
| **A11y tree (programmatic)** | OS accessibility API (UIA on Windows) provides element hierarchy with rects | Free, exact rects, rich metadata (type, state, enabled) | Platform-specific, incomplete for custom UIs/games |
| **Hybrid** | SoM as primary source; a11y for verification/enrichment | Best coverage + richest signal | Most complex |

**Which source to use is an empirical question — Phase 0 answers it.**
We don't yet know SoM detection quality on real WAA Windows screenshots (OmniParser's
99.3% was on synthetic benchmarks; real-world accuracy is likely lower). We also don't
know UIA coverage for WAA's specific apps. The decision rule:

- **UIA-first** if UIA coverage >= 75% of required targets AND median candidate
  ambiguity <= 3 per step. UIA is free, exact, deterministic — no detection model needed.
- **SoM-first** if UIA coverage < 75% or custom UIs dominate. SoM is universal but
  adds latency and detection noise.
- **Hybrid** if UIA covers most targets but has gaps. Use UIA rects where available,
  SoM only for uncovered regions.

**A11y as verification/enrichment** (regardless of primary source):
- Confirms an element is actually interactive (Invokable/Focusable UIA patterns)
- Provides control type (Button vs static text that looks like a button)
- Reports enabled/disabled state
- Disambiguates when multiple candidates look identical

### Candidate set builder

Even with good element detection, models fail by picking the wrong element among many
plausible ones. A deterministic candidate set builder before the model chooses:
1. **Detect broadly**: SoM detection or UIA enumeration produces all elements
2. **Filter** by control types relevant to the action (Button/Edit/ListItem/MenuItem)
3. **Filter** by enabled/focusable (via a11y when available)
4. **Rank** by text match (exact > contains > fuzzy) to instruction keywords
5. **Rank** by spatial priors (dialogs: primary button bottom-right)
6. **Present top-K** candidates to the model (K = 10 initially, tune later)
7. *(Optional)* Re-render screenshot with marks only on the K candidates to reduce clutter

This turns an open-ended generation problem into a bounded selection problem. Build this
as part of Phase 0 instrumentation — it's small engineering with high leverage.

### Action space

| Action Space | Example | When to use |
|-------------|---------|-------------|
| **Element selection** | `click(mark_7)` or `click_element("submitBtn")` | Primary — whenever candidates are available |
| **Coordinate fallback** | `click(589, 965)` | Fallback — when no candidate matches intent |

**Current implementation**: Hybrid element/coordinate in both Qwen3VLAgent and ApiAgent,
with `target_node_id` on BenchmarkAction. Adapter resolves to pixel coordinates at
execution time. Works with both SoM mark numbers and a11y IDs.

**For training data generation**: Element-based labels are strictly better. The teacher
(Claude) is excellent at UI semantic reasoning ("click the Submit button") but poor at
coordinate prediction. Matching teacher capabilities to label format is critical — this
is why Option K works where Option B fails.

### Target architecture (not blocking, but directional)

Separate intent from resolution:
```
Model outputs:  {action: "click", query: "Submit", constraints: {role: "Button", window: "active_dialog"}}
Executor resolves:  query → candidate set → top match → pixel coordinates → click
```
This pushes disambiguation into code (testable, deterministic) rather than the model.
Not required for v1, but the right long-term design.

---

## Options

### A. Continue Current Approach: Fine-Tune on More Coordinate-Based Demos

**What**: Record more demos via VNC, annotate, train Qwen3-VL-2B on 50+ demos.

**Pros**:
- Pipeline already built end-to-end
- No architecture changes needed
- Directly validates demo-conditioned thesis

**Cons**:
- 3 demos → overfitting; more demos help but coordinate prediction is fundamentally
  hard for 2B models (SeeClick needed 270K examples, OS-Atlas needed 13M)
- Human demo recording is the bottleneck (manual VNC work)
- Doesn't address the core problem: pixel coordinate accuracy
- Research shows 2B models need dedicated grounding pre-training (Stage 1 SFT) before
  they can reliably predict coordinates

**Estimated effort**: 2-3 days (recording) + 1 day (training)
**Expected outcome**: Marginal improvement, likely still 0% on hard WAA tasks
**Ralph-loopable**: No (requires human demo recording)

---

### B. DAgger: Claude Code Labels Eval Screenshots as Training Data

**What**: Run eval, Claude Code looks at each screenshot, determines correct action,
writes as training data. Train on Modal. Repeat.

**Pros**:
- Eliminates human demo recording bottleneck
- Fully automatable in a ralph loop
- Each iteration generates new data from new screen states

**Cons**:
- **Wasteful**: Using $15/M-token model to label data for $0.10/M-token model
- **Circular**: Claude also can't reliably determine pixel coordinates (it scores 0% too)
- **Noisy labels**: If teacher can't solve the task, training data quality is poor
- Doesn't compound — linear cost per label
- Doesn't address the fundamental coordinate prediction problem

**Estimated effort**: 2-3 days to build pipeline
**Expected outcome**: Marginal — training on noisy labels from a failing teacher
**Ralph-loopable**: Yes, but the loop converges to garbage

---

### C. Element Grounding (SoM, A11y, or Hybrid)

**What**: Agent selects from detected elements instead of predicting pixel coordinates.
Element source is determined empirically in Phase 0 (UIA, SoM, or hybrid).
This is what Navi uses to get 19.5% on WAA (SoM + GPT-4V).

**Pros**:
- **Sidesteps coordinate prediction entirely** — the hardest part
- BenchmarkAction already has `target_node_id` field
- Works with any base model without fine-tuning (in-context learning)
- Navi proves element selection works on WAA specifically (19.5%)
- WAA paper showed SoM gives up to 57% improvement

**Cons**:
- SoM: requires detection model, adds latency, quality unknown on real WAA screenshots
  (OmniParser's 99.3% was on synthetic benchmarks — real-world likely lower)
- A11y: platform-specific, incomplete for custom UIs, unknown coverage on WAA apps
- Both: need candidate set builder to manage ambiguity (many elements look similar)

**Detection backend options**:
- **OmniParser** (Microsoft): Wrapped in `openadapt-grounding` repo (PyPI published,
  `OmniParserClient`). FastAPI server on GPU (~$1/hr). Could co-deploy on WAA Azure VM.
  Skip temporal smoothing, ElementLocator, VLM providers — just use the HTTP client.
- **GroundingDINO**: Open-vocabulary object detection, text-promptable. More flexible.
- **UIA only**: Free, exact, deterministic. No detection model needed. But coverage
  unknown for WAA's apps — Phase 0 answers this.

**Estimated effort**: 1-2 days (deploy detection model + integrate, or test UIA-only)
**Expected outcome**: Non-zero scores likely — Navi proves the approach works on WAA
**Ralph-loopable**: Yes — iterate on detection config, candidate filtering, prompt format

---

### E. Two-Stage SFT: Grounding Pre-Training + Agentic Fine-Tuning

**What**: The standard approach that works at 2B-3B scale. First train on millions of
(screenshot, element description) → coordinate pairs. Then fine-tune on task trajectories.

**Evidence**:
- Qwen-GUI-3B: 86.4% on ScreenSpot-v2 with only 24K examples (single RTX 4090)
- Smol2Operator: Functional GUI agent from 2.2B base with two-phase SFT
- Jedi dataset: Took OSWorld from 23% → 51% with grounding data alone

**Pros**:
- **Industry-standard approach** — proven at exactly our model scale
- Massive open-source grounding datasets available (OS-Atlas 13M, Jedi 4M)
- Addresses the root cause (coordinate accuracy)
- Compatible with demo-conditioned inference afterward
- Could use Modal for training

**Cons**:
- Requires significant training compute (Stage 1 is large-scale)
- Need to curate/adapt grounding datasets for Windows desktop
- Two separate training phases to manage
- More complex pipeline than current approach
- Stage 1 may take hours on A10G (millions of examples)

**Estimated effort**: 3-5 days (dataset prep + Stage 1 training + Stage 2 fine-tuning)
**Expected outcome**: Dramatically better grounding accuracy; competitive with SOTA at 2B
**Ralph-loopable**: Partially — hyperparameter tuning and dataset selection iterations

---

### F. RL (GRPO) After SFT

**What**: After SFT, apply Group Relative Policy Optimization using eval scores as reward.
Generate N candidate actions per step, score them, train on the best ones.

**Evidence**:
- SE-GUI: 47.3% on ScreenSpot-Pro with only 3K samples + GRPO, beating UI-TARS-72B
- GTA1: 45.2% on OSWorld with GRPO + test-time scaling
- CRAFT-GUI: Curriculum GRPO outperforms prior SOTA by 5-10%

**Pros**:
- **Extremely sample-efficient** (3K samples can beat 72B models)
- Uses eval scores directly as reward signal — naturally fits our pipeline
- Pushes beyond SFT ceiling
- Self-improving: generates its own training signal from environment interaction

**Cons**:
- Requires SFT base first (Option E as prerequisite)
- RL training is finicky — reward shaping, hyperparameters
- Need infrastructure for generating multiple candidates per step
- More complex to implement than SFT
- Needs environment interaction during training (VM must be running)

**Estimated effort**: 5-7 days (after SFT base)
**Expected outcome**: Could match or exceed SOTA at 2B if SFT base is solid
**Ralph-loopable**: Yes — RL hyperparameter tuning + reward shaping iterations

---

### G. Use Existing Open-Source GUI Agents Directly

**What**: Instead of training from scratch, use pre-trained GUI agents as the base:
- **UI-TARS-1.5-7B** (ByteDance): Open-source, 40% on OSWorld
- **Smol2Operator-2.2B** (HuggingFace): Open-source, already fine-tuned for GUI
- **Qwen-GUI-3B**: 86.4% ScreenSpot-v2, open-source

**Pros**:
- **Immediately more capable** than our from-scratch fine-tune
- Already trained on grounding (Stage 1 done for us)
- Smol2Operator is 2.2B — same scale as our current model
- Can add demo-conditioning on top (the OpenAdapt differentiator)
- Minimal training needed — just adapt to WAA action format

**Cons**:
- Need to adapt action space to our BenchmarkAction format
- May need format conversion for demo-conditioning
- Less control over training process
- 7B models need more GPU memory (A10G can handle it though)
- Licensing varies (check each model)

**Estimated effort**: 1-2 days (adapt agent wrapper + action format conversion)
**Expected outcome**: Non-zero WAA scores likely (these models can ground reliably)
**Ralph-loopable**: Yes — iterate on prompt engineering, demo format, action parsing

---

### H. Hybrid: Strong Planner + Small Grounder

**What**: Use Claude/GPT for task decomposition and planning ("open Notepad, type text,
save file"), use a small grounding model to execute each sub-step ("click the element
that says 'Search'").

**Evidence**:
- GTA1 (Salesforce): 45.2% on OSWorld with this exact approach
- Separates reasoning (hard, needs large model) from grounding (trainable, small model)

**Pros**:
- Plays to each model's strengths
- Claude is excellent at planning; small models can learn grounding
- Demo-conditioning applies naturally to the planning layer
- Grounding model is cheap to train and run
- Can swap planner (Claude → GPT → local) independently of grounder

**Cons**:
- Two-model inference increases latency and complexity
- API costs for planner (Claude/GPT per step)
- Need to define sub-step interface between planner and grounder
- More moving parts to debug

**Estimated effort**: 3-5 days
**Expected outcome**: Strong if grounding model is reliable (Option E helps)
**Ralph-loopable**: Yes — iterate on planning prompts, sub-step decomposition, grounder

---

### I. Self-Play from Successes (Curriculum Learning)

**What**: Start with the easiest possible tasks. When the agent succeeds, capture the
trajectory as training data. Train on successes. Attempt harder tasks. Repeat.

**Pros**:
- **Self-sustaining data generation** — successes compound
- No human labeling needed once bootstrapped
- Natural curriculum: easy → hard
- Training data is guaranteed correct (it produced success)
- Aligns training distribution with deployment distribution

**Cons**:
- **Chicken-and-egg**: need first success to bootstrap
- If agent can't succeed at anything, loop never starts
- Successful trajectories may not generalize (narrow demonstrations)
- Needs diverse task pool to avoid overfitting to easy tasks
- Selection bias: only trains on what it can already do

**Estimated effort**: 2-3 days (pipeline), ongoing (curriculum design)
**Expected outcome**: Compounding improvement IF bootstrapped; zero if not
**Ralph-loopable**: Yes — this IS a ralph loop by nature

---

### J. Claude Code as Agent Architect (Prompt + Code Iteration Loop)

**What**: Claude Code iterates on the agent's architecture, prompts, and code using eval
scores as the objective function. Not generating training data — modifying the system.

**Interventions Claude Code can make**:
- Modify system prompt (add UI layout hints, chain-of-thought, few-shot examples)
- Add accessibility tree to prompt
- Add SoM preprocessing
- Modify action space (element IDs, text grounding, hybrid)
- Add error recovery (detect stuck loops, try alternatives)
- Change which tasks to attempt (curriculum)
- Modify coordinate handling (normalization, scaling)

**Pros**:
- **Highest leverage per iteration** — each change is architectural, not a single label
- Fast feedback loop (change code → eval → analyze screenshots → iterate)
- Claude Code can reason about WHY failures happen (multimodal analysis)
- No training needed for prompt/architecture changes
- Naturally discovers what Option C-H would prescribe

**Cons**:
- Relies on Claude Code's judgment about what to change
- May get stuck in local optima
- Architecture changes are harder to evaluate than data improvements
- Risk of over-engineering or adding complexity that doesn't help

**Estimated effort**: Ongoing (ralph loop)
**Expected outcome**: Discovers the right architecture through iteration
**Ralph-loopable**: Yes — this is the ideal ralph loop task

---

### K. Element-Based DAgger: Claude Labels Elements, Not Coordinates

**What**: Combine Option B (DAgger) with Option C (element grounding). Claude sees a
SoM-marked screenshot (+ optional a11y metadata), picks the correct *element*, and that
becomes training data for the small model. The teacher is no longer being asked a question
it can't answer.

**Why this fixes Option B's fatal flaw**: Option B failed because Claude can't predict
pixel coordinates any better than the student. But Claude is excellent at UI semantic
reasoning — "click mark 7 (the Submit button)" — which is exactly what element selection
requires. Teacher and student operate in the same action space. The distribution shift
that killed coordinate DAgger disappears.

**The loop**:
```
1. Run eval: Claude + SoM-marked screenshots on WAA task
2. For each step where Claude chose correctly (task advanced):
   - Record (screenshot, marks, instruction) → element_action as training example
3. Filter: only keep examples that pass the verification gate
4. Train small model on accumulated element-based examples
5. Eval small model → new score
6. Repeat
```

**Bootstrap with micro-tasks**: Full task completion is hard. But individual *verified
transitions* are easy — "click File menu," "type in search box," "press Enter." Include
trivially-solvable micro-tasks to seed hundreds of verified steps fast. Treat "task
success" as optional; you mainly need verified local transitions to train the student's
grounding/execution policy.

**Verification gate** (critical): Claude will sometimes pick the wrong element for the
right reasons — ambiguous UI, multiple similar marks, mislabeling. Training on
confident-but-wrong labels is worse than obviously noisy ones.

Gate hierarchy (strongest to weakest):
1. **Environment signal** (best): WAA step reward / task-advanced flag if available
2. **A11y delta**: a11y tree changed meaningfully — new window, focus moved to expected
   control, text value in target field changed, new control appeared
3. **State predicates** (per task type): file exists, window title changed, specific text
   present — defer unless a11y delta proves too noisy
4. **Screenshot diff** (weakest fallback): only use if nothing else available

Also log **negative examples** (teacher action that *didn't* advance) for future use:
contrastive learning, rejection sampling in GRPO, or filtering heuristic training.

**Pros**:
- **Fully ralph-loopable** — entire pipeline is autonomous
- **Teacher is competent** — Claude's UI reasoning is strong when freed from coordinates
- **Distribution-aligned** — student trains on the same action space it uses at inference
- **Self-improving** — more eval runs → more training data → better student → harder tasks
- **Builds on shipped infrastructure** — a11y tree grounding already in all agents
- **Cheap** — Claude API cost per label is low (one API call per step)

**Cons**:
- Depends on element detection quality (SoM or UIA — Phase 0 validates this)
- Verification gate adds complexity and may discard many examples — track pass rate
  (if only 5% of steps pass, economics change: need thousands of eval runs)
- Claude's element selections may cluster on easy/obvious elements (selection bias)
- Still needs enough verified transitions to generate meaningful training data
- Risk of training on superficially correct but strategically wrong actions
  (clicked the right element but wrong plan)

**No-op blacklist** (critical for verification gate):
- Reject transitions where only focus/hover/caret changed with no structural delta
- Reject repeated action on same target with no downstream change
- Log rejected examples as potential negatives for contrastive learning

**Estimated effort**: 2-3 days (pipeline), builds on existing eval infrastructure
**Expected outcome**: Compounding data flywheel if element detection is decent.
If detection is garbage, reveals this early and we pivot.
**Ralph-loopable**: Yes — this is designed as a ralph loop from the ground up

---

## Comparison Matrix

| Option | Expected Impact | Time to First Result | Cost | Ralph-Loopable | Addresses Root Cause |
|--------|----------------|---------------------|------|----------------|---------------------|
| A. More demos | Low | Days | Low | No | No (still coordinates) |
| B. DAgger (coords) | Low | Days | High | Yes | No (noisy teacher) |
| C. Element grounding | Medium-High | 1-2 days | Low | Yes | Yes (sidesteps coords) |
| E. Two-stage SFT | High | 3-5 days | Medium | Partial | Yes (grounding training) |
| F. RL (GRPO) | Very High | 5-7 days | Medium | Yes | Yes (self-improvement) |
| G. Use existing agents | Medium-High | 1-2 days | Low | Yes | Yes (pre-trained grounding) |
| H. Hybrid planner | High | 3-5 days | Medium | Yes | Yes (separation of concerns) |
| I. Self-play | High (if bootstrapped) | 2-3 days | Low | Yes | Partially |
| J. Code iteration | Variable | Hours | Low | Yes | Discovers the answer |
| **K. Element DAgger** | **High** | **2-3 days** | **Low-Med** | **Yes** | **Yes (competent teacher + right action space)** |

---

## Recommended Strategy (v3)

### Phase 0 — Validate element grounding on live WAA [NEXT]

**Goal**: Measure **Recall@K** — can the candidate set builder surface the correct target
element? This single metric dominates everything downstream. If the right element isn't
in the top-K candidates, no amount of prompting or training fixes it.

**Headline metric**: Recall@10 for candidate builder (UIA-only, SoM-only, hybrid).

**Target set definition**: "Required targets" must be operationally defined or the
coverage numbers are hand-wavy. Source the target set from:
- WAA task metadata (if it exposes target elements per step) — check first
- Else: human-label a 50-step mini-eval across 5 WAA tasks (cheap, do once)
- Else: use UIA oracle (closest matching UIA node) as proxy

**Instrumentation** (measure per-step on the target set):
- Recall@K: is the correct element in top-K candidates? (K=5, 10)
- Ambiguity: average # of candidates per step (if median > 5, need tighter filtering)
- Rect fidelity: rects non-empty, on-screen, non-jittering
- Actionability: % of candidates actually interactive (via a11y when available)
- Detection backend comparison: UIA vs SoM vs hybrid on same screenshots

Also build the candidate set builder (filter → rank → top-K) as part of this phase.
It's small engineering with high leverage and directly improves both Claude's element
selection (for K) and any existing agent's performance (for G).

**Go/no-go**: Recall@10 >= 0.8 on any backend → proceed with that backend.
Recall@10 < 0.6 on all backends → investigate per-app breakdown, consider coordinate
approaches, or re-evaluate the element grounding strategy entirely.

**Time bound**: 1 day. This is a gate, not a phase. Don't build detection model
deployment infra until you've checked UIA coverage (free, instant).

**Status of shipped infrastructure**: Element-based action space (PRs #45-47) works with
both SoM marks and a11y IDs via `target_node_id`. Adapter resolves to pixel coords at
execution time.

---

### Phase 0.5 — Wrap an existing agent (Option G) [PARALLEL]

**In parallel with Phase 0**, wrap Smol2Operator-2.2B or UI-TARS-1.5-7B in a
BenchmarkAgent. These already have visual grounding Stage 1 baked in. If one scores > 0%,
it provides:
- Immediate movement (baseline to improve upon)
- Successful transitions for self-play data (Phase 2)
- Diverse screen states for K-style training data

**Format integration** (define upfront or you'll burn a day on glue):
- Input to agent: raw screenshot (these models do their own visual grounding)
- Output from agent: model-native format (e.g., Smol2Operator outputs `click(x, y)`,
  UI-TARS outputs `click <point>`)
- Mapping to BenchmarkAction: parse coordinates, normalize to viewport, set
  `type="click"`, `x=norm_x`, `y=norm_y`. No `target_node_id` needed — these models
  predict coordinates directly (that's their grounding strength).

Only attempt this if the wrapper plugs into BenchmarkAgent in < 2 hours. If format
conversion is a slog, deprioritize until the candidate builder from Phase 0 is solid.

**Go/no-go**: Any agent > 0% within 2 days → use its outputs as seed data.

---

### Phase 1 — Element DAgger flywheel (Option K) [PRIMARY PATH]

Once Phase 0 confirms element grounding is viable (Recall@10 >= 0.8):

1. Run Claude + element candidates on WAA tasks (easiest first + micro-tasks)
2. Record every step that passes the verification gate
3. Accumulate element-based training data
4. If Phase 0.5 agent is running: capture its successful transitions too
5. Train Qwen3-VL-2B on accumulated element-based examples (Modal)
6. Eval small model → new score
7. Repeat

**Micro-task source**: Use the first 3-5 steps of the easiest WAA tasks as micro-tasks —
"open Notepad," "click File menu," "type in text area," "click Save," "confirm dialog."
These are trivially UIA-visible and solvable, generating hundreds of verified
transitions fast. Full task completion is not required for grounding training — verified
local transitions are the real training signal.

**Go/no-go** (gate on leading indicators, not raw success rate — success rate is noisy
and depends on task difficulty mix):
- Recall@K >= 0.8 on micro-task suite → candidate builder is working
- Student matches teacher on verified transitions >= 0.7 → training is transferring
- Loop rate < 20% → agent isn't stuck
- Verification gate pass rate > 15% → enough data to sustain the flywheel
  (if < 5%, need thousands of eval runs — economics don't work)

**Time bound**: 4 days max before hard re-evaluation. If leading indicators are bad
after 2 days → switch base model (Smol2Operator) or fall back to Option E.

---

### Phase 2 — Self-play + demo library (Option I)

Once Phase 1 produces a model with >= 10% success rate:

1. Run model on all WAA tasks
2. Capture successful trajectories as demo library
3. Demo-condition the model on related tasks
4. Train on the expanded success set
5. Repeat

This is where the OpenAdapt thesis gets validated: *do demos improve the agent's
success rate on new tasks?* PC Agent-E says yes (141% improvement from 312 demos).

**Go/no-go**: Demo-conditioned agent outperforms zero-shot by > 20% on held-out tasks →
proceed. No improvement → demos aren't helping at this quality level, focus on Phase 3.

**Time bound**: 1 week. If the flywheel isn't spinning by then, pivot to Phase 3 directly.

---

### Phase 3 — Push to SOTA (Option E + F)

Two-stage SFT (grounding pre-training + agentic fine-tuning) followed by GRPO on the
demo library from Phase 2. This is the heavy investment that produces a small, fast,
cheap model competitive with 72B models.

**Prerequisites**: Demo library of >= 100 successful trajectories from Phase 2, or
fall back to open-source grounding datasets (OS-Atlas, Jedi).

---

### Fallback paths

```
Phase 0 fails (SoM coverage < 60%)
  → Check per-app breakdown: if 80% of tasks use 3 well-covered apps, filter to those
  → Try GroundingDINO instead of OmniParser (different detection strengths)
  → Hybrid: UIA-first rects + SoM only for gaps
  → Pure a11y for apps with good UIA + coordinate fallback for the rest

Phase 0.5 stalls (no existing agent > 0%)
  → Check action format adaptation — may need action space conversion, not model issue
  → Try a different base model
  → Fall back to Claude + SoM as the sole data generator for Phase 1

Phase 1 stalls (< 5% after 4 days)
  → Switch base model (Smol2Operator-2.2B, UI-TARS-1.5-7B)
  → Option H (hybrid): Claude plans, small model grounds
  → Option E (two-stage SFT): Skip DAgger flywheel, train grounding directly on
    open-source data (OS-Atlas, Jedi)

Phase 2 stalls (demos don't help)
  → Examine demo quality — are the trajectories too narrow/repetitive?
  → Try retrieval-augmented demos instead of training-time injection
  → Skip to Phase 3 with open-source grounding data
```

---

## Metrics to Track Every Run

Without these, you're chasing vibes.

**Step-level**:
- Invalid action rate (unparseable model output)
- Stuck-loop rate (3+ identical actions)
- Recovery success rate (after loop detection, did alternative action help?)

**Grounding** (the most important category):
- **Recall@K**: is the correct target in top-K candidates? (K=5, 10) — headline metric
- Top-1 pick accuracy given candidates (did model choose correctly?)

**Element quality**:
- SoM coverage: % of targets detected by visual model
- A11y coverage: % of targets with a11y nodes
- Ambiguity: median # of candidates per intent
- Rect stability: jitter across consecutive frames

**Training data (K loop)**:
- Steps attempted → steps passed verification gate → unique examples added per iteration
- Gate pass rate (if < 5%, economics don't work — need too many eval runs)
- Teacher-student match rate on verified transitions

**Efficiency**:
- Prompt size (tokens) — monitor for bloat as candidates grow
- Candidate count per step
- Action parse failure rate

---

## Key Research References

### Demo-conditioned results (validates OpenAdapt thesis)

| Paper/System | Scale | Result | Key Insight |
|-------------|-------|--------|-------------|
| **PC Agent-E** | — | **36% WAA-V2** | **141% improvement from just 312 demo trajectories** |
| **LearnAct** | Gemini-1.5-Pro | **51.7%** (from 19.3%) | **Single demonstration → 168% improvement** |
| **ShowUI-Aloha** | — | **60.1%** | Demo-conditioned planning |

These three results are the strongest evidence for the OpenAdapt bet. You don't need
millions of demos. Hundreds — or even one — can dramatically change outcomes.

### Grounding and training approaches

| Paper/System | Scale | Result | Key Technique |
|-------------|-------|--------|---------------|
| SE-GUI | 7B | 47.3% ScreenSpot-Pro | GRPO with 3K samples |
| Qwen-GUI-3B | 3B | 86.4% ScreenSpot-v2 | Two-stage SFT, 24K examples |
| Smol2Operator | 2.2B | Functional GUI agent | Two-phase SFT, open-source |
| UI-TARS-1.5 | 7B | 40% OSWorld | SFT + RL + DPO |
| GTA1 | 7B grounder | 45.2% OSWorld | Separate planner + RL grounder |
| Navi (WAA baseline) | GPT-4V | 19.5% WAA | SoM + CoT |
| Jedi dataset | — | 23%→51% OSWorld | Grounding data alone |
