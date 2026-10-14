# Standalone GRPO Trainer vs TRL-backed GRPO Trainer

Comprehensive feature comparison and deprecation readiness assessment.

**Date**: 2026-03-29
**Status**: TRL path is the recommended path forward; standalone is self-deprecating but NOT yet safe to remove.

## Feature-by-Feature Comparison

| # | Feature | Standalone | TRL | Parity? |
|---|---------|-----------|-----|---------|
| 1 | Vision-safe loss (`vision_loss_mode`) | 3 modes: exclude/include/checkpoint. Solves Qwen3 vision merge crash. | Not implemented. TRL handles loss internally. | **NO** |
| 2 | Constrained decoding (Outlines) | `_get_outlines_generator()` with cache sentinel. Regex: `Thought: ...\nAction: CLICK\|TYPE\|WAIT\|DONE`. | Ported: `_build_outlines_generator()` in `trl_rollout.py`. Same API. | **YES** |
| 3 | Task rotation | Round-robin: `step % len(task_ids)`. | Dataset-driven: TRL iterates HF `Dataset`. | **YES** (different mechanism, same effect) |
| 4 | Pre-rollout health check | `probe()` before each group. Skips if server unresponsive. | **Missing.** Dead server causes exceptions in rollout. | **NO** |
| 5 | Corrupt screenshot retry | Double-layer: WAADirect 3-attempt retry + PIL error catch. | **Missing.** Crash or zero-reward on corrupt screenshot. | **NO** |
| 6 | Group-relative advantages | Custom `compute_group_advantages()` in `reward.py`. | Delegated to TRL (native GRPO implementation). | **YES** |
| 7 | Loss diagnostics | `loss=%+.2e \|loss\|=%.2e grad_norm=%.4f adv=%s`. Returns `loss_abs` + advantage list. | TRL logs via W&B/TensorBoard. `TelemetryCallback` extracts `loss` and `reward`, but no `loss_abs` or `grad_norm`. | **PARTIAL** |
| 8 | Callback hooks | All 4: `on_model_loaded`, `on_before_collect`, `on_rollout_complete`, `on_step_complete`. | `on_model_loaded`: YES. `on_step_complete`: PARTIAL (via `HookBridge`). `on_before_collect`, `on_rollout_complete`: **stored but never fired**. | **PARTIAL** (2/4 work) |
| 9 | Model loading | HF + PEFT + BitsAndBytes. No working Unsloth path. | HF + PEFT + BitsAndBytes + **Unsloth** (working `FastVisionModel` path). | **YES** (TRL superset) |
| 10 | WAA client | `WAADirect`: full HTTP client with health check, retry, stuck detection. | `WAALiveAdapter` + `RLEnvironment`. Different layer, equivalent functionality. | **YES** (different approach) |
| 11 | Training loop | Manual: `zero_grad` → per-rollout loss → `backward` → `clip_grad_norm` → `step`. | Delegated to TRL (scheduling, distributed, gradient accumulation). | **YES** (TRL more sophisticated) |
| 12 | Truncation warning | Explicit check: logs warning when `gen_len >= max_new_tokens - 1`. | **Missing.** | **NO** |
| 13 | Stuck detection | `is_stuck()`: MD5 hash of recent screenshots. | **Missing.** Relies on `max_steps` only. | **NO** |
| 14 | Telemetry | Manual calls to `track_training_*()`. | `TelemetryCallback` (equivalent). | **YES** |
| 15 | Weave tracing | Config field exists but unused. | Actually initializes Weave. | **YES** (TRL actually uses it) |
| 16 | Multiple loss types | GRPO/REINFORCE only. | `grpo`, `dapo`, `dr_grpo`, `rloo`, etc. | **YES** (TRL superset) |
| 17 | Distributed training | Single-GPU only. | Multi-GPU via Accelerate, DeepSpeed, FSDP. | **YES** (TRL superset) |
| 18 | vLLM inference | HF `model.generate()` only. | TRL supports `use_vllm`. | **YES** (TRL superset) |
| 19 | LR scheduling | Flat AdamW, no scheduler. | Warmup, cosine decay, etc. | **YES** (TRL superset) |

## What's in Standalone but NOT in TRL

These features must be ported before the standalone trainer can be removed:

| Priority | Feature | Risk if Missing | Effort |
|----------|---------|-----------------|--------|
| **P0** | Pre-rollout health check | Wastes GPU time on dead server; all rewards 0.0 | Low |
| **P0** | Corrupt screenshot retry | Crashes entire rollout; ~1-5% occurrence on Azure VMs | Low |
| **P1** | Stuck detection | Agent loops on identical screenshots; wastes max_steps | Low |
| **P1** | Truncation warning | Operators miss truncated output → mysterious low rewards | Low |
| **P1** | Prompt format mismatch | Standalone: `CLICK(x=..., y=...)` DSL. TRL: `{"type":"click"}` JSON. Training on one, evaluating on other degrades performance. | Medium |
| **P2** | `on_before_collect` / `on_rollout_complete` callbacks | Stored in HookBridge but never fired | Medium |
| **P2** | Rich loss diagnostics (`loss_abs`, `grad_norm`, per-rollout advantages) | Harder to debug RL training | Low |

## What's in TRL but NOT in Standalone

| Feature | Value |
|---------|-------|
| Unsloth integration (working) | 2x speedup, 90% VRAM reduction |
| Multiple loss types (dapo, dr_grpo, etc.) | Algorithm flexibility |
| Distributed training | Multi-GPU scaling |
| vLLM inference | Fast rollout collection |
| LR scheduling | Better convergence |
| Weave tracing (active) | LLM/agent observability |
| Dataset-native task management | HF ecosystem integration |

## Test Coverage Comparison

| Area | Standalone Tests | TRL Tests |
|------|-----------------|-----------|
| Constrained decoding | 11 tests (regex, Outlines API, cache, DFA) | 0 tests for `_build_outlines_generator()` |
| Vision loss | 10 tests (consistency, slicing, merge crash, integration) | N/A (TRL handles loss internally) |
| Action parsing | 6 tests (DSL format) | 9 tests (JSON format) |
| Config separation | N/A | 4 tests |
| Rollout function | N/A | 5 tests |
| Telemetry | N/A | 2 tests |
| Training step metrics | 3 tests (diagnostics, advantages, skipping) | 0 tests |
| **Total** | **22 tests** | **19 tests** |

### Tests Needed for TRL Deprecation Readiness

1. `test_trl_rollout_constrained_decoding` — Outlines generator builds and compiles
2. `test_trl_rollout_corrupt_screenshot` — graceful handling after fix
3. `test_trl_rollout_stuck_detection` — early break after fix
4. `test_trl_rollout_truncation_warning` — warning emitted after fix
5. `test_trl_health_check` — zeros returned when server down after fix
6. `test_trl_callback_bridge` — verify `on_before_collect` / `on_rollout_complete` fire
7. `test_trl_prompt_format_consistency` — DSL vs JSON alignment
8. `test_trl_unsloth_loading` — mock FastVisionModel construction
9. `test_trl_diagnostics_callback` — `loss_abs`, `grad_norm` at each step
10. `test_trl_lora_checkpoint_resume` — existing LoRA loads correctly

## Deprecation Path

### Phase 1: Close P0 gaps (unblock production use)
- Port health check, corrupt retry to `trl_rollout.py`
- Add stuck detection
- Unify or make prompt format configurable

### Phase 2: Achieve test parity
- Write 10 tests listed above
- Port truncation warning, diagnostic callback
- Fix callback bridge (fire `on_before_collect` / `on_rollout_complete`)

### Phase 3: Remove standalone trainer
- Delete `training/standalone/trainer.py`
- Keep shared utilities: `config.py`, `prompt.py`, `reward.py`, `waa_direct.py`, `model_loader.py`
- Update all documentation and examples

### What NOT to Port
- **Vision-safe loss**: TRL handles forward pass differently; the Qwen3 crash was caused by manual `torch.cat(prompt_ids, action_ids)` which TRL doesn't do. Test on real hardware before deciding.
- **Manual advantage computation**: TRL implements GRPO natively.
- **Manual optimizer loop**: TRL is strictly better.

## Action Format Decision: DSL vs JSON

The standalone trainer uses DSL (`CLICK(x=0.50, y=0.30)`), the TRL path uses JSON (`{"type":"click","x":0.5,"y":0.3}`). These must be unified.

### Finding: openadapt-types Already Solves This

`openadapt-types` (PyPI, v0.1.0) is the **canonical action schema** for the OpenAdapt ecosystem. It explicitly converges:
1. `openadapt_ml.schema.episode.Action` (24-type enum)
2. `openadapt_evals.adapters.base.BenchmarkAction` (string-typed dataclass)
3. `omnimcp.types.ActionDecision` (element_id-based)

Key types:
- `ActionType` enum: 21 action types (click, type, key, scroll, drag, wait, done, etc.)
- `ActionTarget`: 3 grounding strategies: `node_id` > `description` > `(x, y)` coordinates
- `Action`: Pydantic v2 model with `reasoning` field (chain-of-thought), `raw` field (lossless)
- `ActionResult`: Execution outcome with error taxonomy

Since `Action` is a Pydantic v2 model, `Action.model_json_schema()` works directly with Outlines JSON schema constrained decoding — no fragile regex needed.

### Options

| Option | Pros | Cons |
|--------|------|------|
| **A: Standardize JSON** | Universal parsing, Outlines JSON schema, eval agent alignment | Client trained on DSL, more tokens |
| **B: Standardize DSL** | Fewer tokens, battle-tested | Fragile regex parsing, non-standard, eval agents use JSON |
| **C: Support both** | No migration | Two parsers, format mismatch risk during training/eval |
| **D: Import openadapt-types, JSON canonical, accept DSL** | Single source of truth, Outlines JSON schema, backward-compatible parsing | Adds a dependency (lightweight: pydantic only) |

### Recommendation: Option D

Use `openadapt_types.Action` as the canonical format. Accept DSL input for backward compatibility.

**JSON output format** (what the model produces):
```json
{"type": "click", "target": {"x": 0.50, "y": 0.30, "is_normalized": true}, "reasoning": "I need to click the button"}
```

**Why:**
- **Already exists.** No new schema to design or maintain.
- **Outlines JSON schema.** `outlines.json(model, Action)` directly from Pydantic model — replaces fragile regex.
- **Eval agent alignment.** PlannerGrounder, DemoExecutor already output JSON.
- **Chain-of-thought.** `reasoning` field preserves thought for credit assignment in RL.
- **Backward-compatible.** Add a parser that accepts DSL → `Action` for existing checkpoints.
- **Lightweight.** Only dependency is pydantic (already in openadapt-evals).

### Known inconsistency in trl_rollout.py

The current `trl_rollout.py` has an **internal contradiction**: the system prompt asks for JSON format, but `ACTION_REGEX` constrains to DSL format. If constrained decoding is on, the model produces DSL but `parse_action_json()` expects JSON. This must be fixed.

### Migration path

1. Add `openadapt-types` to openadapt-evals core dependencies.
2. Fix `trl_rollout.py`: align system prompt + constrained decoding to `Action` JSON schema.
3. Add universal parser to `openadapt-types`: DSL string → `Action`, JSON string → `Action`.
4. Add `BenchmarkAction.to_action()` / `Action.to_benchmark_action()` converters.
5. Standalone trainer's `parse_vlm_output_to_action()` already accepts both DSL and JSON — no change needed.
6. New training configs default to JSON via `openadapt-types`.

## Bottom Line

The TRL path is architecturally superior and the right direction. But the standalone trainer has 6 battle-tested robustness features from real WAA training that the TRL path lacks. **Don't remove the standalone trainer until P0/P1 gaps are closed and test parity is achieved.** Estimated effort: 1-2 days.
