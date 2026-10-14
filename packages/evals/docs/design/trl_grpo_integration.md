# TRL GRPOTrainer + WAADesktopEnv Integration Design

> Date: 2026-03-17
> Status: PROPOSED
> Scope: Replace standalone GRPO trainer with TRL GRPOTrainer + custom rollout_func

---

## 1. Motivation

Our standalone GRPO trainer (`openadapt-ml/training/grpo/trainer.py`, ~567 lines) reimplements gradient computation, rollout collection, advantage estimation, and checkpointing. TRL's GRPOTrainer provides all of this with battle-tested infrastructure, plus:

- VLM support (Qwen2.5-VL confirmed working)
- vLLM integration for fast generation
- LoRA/QLoRA out of the box
- W&B/TensorBoard logging
- Gradient accumulation, checkpointing
- Multiple loss types (grpo, dapo, dr_grpo, etc.)

The `rollout_func` API (experimental) allows custom environment interaction during training. Combined with Unsloth for VRAM efficiency, this gives us a production-grade training stack with ~100-200 lines of integration code.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────┐
│  GPU Server (g5.xlarge / A10G 24GB)                 │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │  Unsloth FastVisionModel                     │   │
│  │  (Qwen2.5-VL-7B, 4bit, LoRA)                │   │
│  │  ~6-8GB VRAM with Unsloth optimizations      │   │
│  └──────────┬───────────────────────────────────┘   │
│             │                                       │
│  ┌──────────▼───────────────────────────────────┐   │
│  │  TRL GRPOTrainer                             │   │
│  │  rollout_func = waa_rollout_func             │   │
│  │  GRPOConfig(num_generations=8, ...)          │   │
│  └──────────┬───────────────────────────────────┘   │
│             │                                       │
│  ┌──────────▼───────────────────────────────────┐   │
│  │  waa_rollout_func()                          │   │
│  │  • Takes prompts from trainer                │   │
│  │  • For each prompt:                          │   │
│  │    1. env.reset() → screenshot               │   │
│  │    2. Loop: screenshot → model → action      │   │
│  │       → env.step(action) → new screenshot    │   │
│  │    3. env.evaluate() → binary reward         │   │
│  │  • Returns {prompt_ids, completion_ids,      │   │
│  │    logprobs, env_reward}                     │   │
│  └──────────┬───────────────────────────────────┘   │
│             │ HTTP                                   │
└─────────────┼───────────────────────────────────────┘
              │
    ┌─────────▼──────────────┐
    │  WAA VM (waa-pool-00)  │
    │  Flask API:            │
    │  /screenshot           │
    │  /execute_windows      │
    │  /evaluate             │
    │  /probe                │
    └────────────────────────┘
```

---

## 3. Key API: TRL rollout_func

```python
# Signature
RolloutFunc = Callable[[list[str], GRPOTrainer], dict[str, Any]]

def waa_rollout_func(
    prompts: list[str],
    trainer: GRPOTrainer,
) -> dict[str, list]:
    """
    Custom rollout function for WAA desktop environment.

    Args:
        prompts: Task instructions (one per rollout group).
        trainer: Active GRPOTrainer instance.

    Returns:
        dict with keys:
            prompt_ids: list[list[int]] — tokenized prompts
            completion_ids: list[list[int]] — tokenized action sequences
            logprobs: list[list[float]] — per-token log probabilities
            env_reward: list[float] — binary task success (0.0 or 1.0)
                (forwarded to reward functions as kwargs["env_reward"])
    """
```

### What the rollout_func must do

1. **Receive prompts** — task instructions like "Change font to Arial in Writer"
2. **Run episodes** — for each prompt, run `num_generations` rollouts:
   a. `env.reset(task_id)` → initial screenshot
   b. Format screenshot + instruction as VLM input
   c. Generate action tokens with the model (using trainer's model)
   d. Parse action from tokens (e.g., `{"type": "click", "x": 0.5, "y": 0.3}`)
   e. `env.step(action)` → new screenshot, reward, done
   f. Repeat until done or max_steps
3. **Compute rewards** — `env.evaluate()` returns binary task success
4. **Return token-level data** — prompt_ids, completion_ids, logprobs for GRPO loss

### Key challenge: VLM multimodal inputs

TRL's `generate_rollout_completions` helper currently handles text-only generation. For VLM with screenshot images, our rollout_func must handle image tokenization manually:

```python
# Inside waa_rollout_func:
processor = trainer.processing_class  # Qwen2.5-VL processor
model = trainer.model

# Format multimodal input
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": [
        {"type": "image", "image": screenshot_pil},
        {"type": "text", "text": task_instruction},
    ]},
]
inputs = processor.apply_chat_template(messages, return_tensors="pt")
# inputs contains: input_ids, pixel_values, image_grid_thw, attention_mask

# Generate with model
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=256, return_dict_in_generate=True,
                              output_scores=True)

# Extract completion_ids and compute logprobs from scores
```

This is more involved than text-only rollout but follows standard HuggingFace VLM patterns.

---

## 4. OpenEnv Compatibility (Future-Proofing)

OpenEnv is Meta's standard for RL environments. Making WAADesktopEnv OpenEnv-compatible means it can plug into any framework that adopts this standard.

### OpenEnv Environment ABC

```python
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, Observation, State

class WAAOpenEnv(Environment[WAAAction, WAAObservation, WAAState]):
    SUPPORTS_CONCURRENT_SESSIONS = False

    def reset(self, seed=None, episode_id=None, **kwargs) -> WAAObservation:
        # Delegates to WAADesktopEnv.reset()
        ...

    def step(self, action: WAAAction, timeout_s=None, **kwargs) -> WAAObservation:
        # Delegates to WAADesktopEnv.step()
        ...

    @property
    def state(self) -> WAAState:
        # Returns current step count, episode_id, etc.
        ...
```

### OpenEnv Transport

OpenEnv uses **WebSocket** (not HTTP request/response):
- Client connects to `ws://<host>:<port>/ws`
- Messages: `WSResetMessage`, `WSStepMessage`, `WSStateMessage`, `WSCloseMessage`
- Responses: `WSObservationResponse`, `WSStateResponse`, `WSErrorResponse`

### Priority

OpenEnv compatibility is **P2** — not needed for the 2-week sprint. The rollout_func approach works without OpenEnv. But the abstraction should be designed so that adding OpenEnv is a thin wrapper later.

---

## 5. Unsloth Integration

Unsloth wraps TRL's GRPOTrainer with memory optimizations. Key benefits:

| Metric | TRL + FA2 | Unsloth | Savings |
|--------|-----------|---------|---------|
| VRAM (8B, 20K ctx) | 510.8GB | 54.3GB | ~90% |
| GRPO loss computation | 78.3GB | 9.8GB | ~87% |
| Training speed | 1x | 1.5-2x | 50-100% |

### Setup

```python
from unsloth import FastVisionModel

model, tokenizer = FastVisionModel.from_pretrained(
    model_name="Qwen/Qwen2.5-VL-7B-Instruct",
    max_seq_length=4096,
    load_in_4bit=True,           # QLoRA
    fast_inference=True,          # vLLM backend
    gpu_memory_utilization=0.6,
    float8_kv_cache=True,         # 2x KV cache reduction
)

model = FastVisionModel.get_peft_model(
    model,
    r=16,
    lora_alpha=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                     "gate_proj", "up_proj", "down_proj"],
)
```

### Compatibility with rollout_func

Unsloth wraps TRL's GRPOTrainer, so `rollout_func` should work since it's a GRPOTrainer constructor parameter. Unsloth doesn't modify this API. However, this is untested — we should validate early.

### Fallback

If Unsloth + rollout_func has issues, fall back to standard TRL:
```python
from transformers import Qwen2_5_VLForConditionalGeneration
model = Qwen2_5_VLForConditionalGeneration.from_pretrained(...)
```

This uses more VRAM but is fully supported.

---

## 6. Implementation Plan

### Phase 1: rollout_func MVP (~100 lines, 2-3 days)

**File**: `openadapt_evals/training/trl_rollout.py`

```python
def waa_rollout_func(prompts, trainer):
    """TRL GRPOTrainer rollout_func for WAA desktop environment."""
    # 1. Create/reuse WAALiveAdapter + RLEnvironment
    # 2. For each prompt × num_generations:
    #    a. env.reset(task_id)
    #    b. Multi-step episode: screenshot → generate → parse → step
    #    c. Collect action token sequences + logprobs
    #    d. env.evaluate() → reward
    # 3. Return {prompt_ids, completion_ids, logprobs, env_reward}
```

**Dependencies**: TRL >= 0.17, openadapt-evals (WAADesktopEnv), Unsloth (optional)

**Test**: Run against WAA VM with Qwen2.5-VL-3B (smaller, faster validation), single GRPO step.

### Phase 2: Training script (~50 lines, 1 day)

**File**: `openadapt_evals/training/train_trl_grpo.py`

```python
from trl import GRPOConfig, GRPOTrainer
from unsloth import FastVisionModel  # optional
from openadapt_evals.training.trl_rollout import waa_rollout_func

model, tokenizer = FastVisionModel.from_pretrained(...)
model = FastVisionModel.get_peft_model(model, ...)

config = GRPOConfig(
    output_dir="./grpo_output",
    num_generations=8,
    max_completion_length=256,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=4,
    num_train_epochs=1,
    loss_type="grpo",
    logging_steps=1,
    save_steps=50,
    use_vllm=True,
    vllm_mode="colocate",
    vllm_gpu_memory_utilization=0.3,
)

trainer = GRPOTrainer(
    model=model,
    processing_class=tokenizer,
    args=config,
    train_dataset=task_dataset,  # list of task instructions
    rollout_func=waa_rollout_func,
)

trainer.train()
```

### Phase 3: OpenEnv wrapper (~80 lines, 1 day, P2)

**File**: `openadapt_evals/training/openenv_server.py`

Wrap WAADesktopEnv as an OpenEnv-compatible WebSocket server. This makes the environment pluggable into any OpenEnv-compatible trainer.

### Phase 4: HCAPO integration (~80 lines, P2)

Add hindsight credit assignment as a custom reward computation inside the rollout_func. Per-step advantages computed from the policy's own hindsight probabilities.

---

## 7. Data Flow

```
TRL GRPOTrainer
    │
    ├── Samples batch of prompts (task instructions)
    │
    ├── Calls waa_rollout_func(prompts, trainer)
    │       │
    │       ├── For each prompt, runs N=8 rollouts:
    │       │       │
    │       │       ├── env.reset(task_id) → screenshot (PIL Image)
    │       │       │
    │       │       ├── Loop (max_steps=15):
    │       │       │   ├── processor(screenshot + instruction) → input_ids, pixel_values
    │       │       │   ├── model.generate() → action_tokens + logprobs
    │       │       │   ├── parse_action(action_tokens) → {"type": "click", "x": 0.5, "y": 0.3}
    │       │       │   ├── env.step(action) → new_screenshot, reward, done
    │       │       │   └── if done: break
    │       │       │
    │       │       └── env.evaluate() → binary reward (0.0 or 1.0)
    │       │
    │       └── Returns {prompt_ids, completion_ids, logprobs, env_reward}
    │
    ├── Computes GRPO advantages from env_reward (group-relative)
    ├── Computes policy gradient loss with clipping
    ├── Backward pass + optimizer step
    └── Checkpoint + logging
```

---

## 8. Configuration

### Task Dataset

```python
# Simple: list of task instructions with task_ids
task_dataset = [
    {"prompt": "Open Task Manager and end the Chrome process",
     "task_id": "taskmanager-end-chrome-WOS"},
    {"prompt": "Change the font to Arial 14pt in LibreOffice Writer",
     "task_id": "0e763496-b6bb-4508-a427-fad0b6c3e195-WOS"},
]
```

### Environment Config

```python
WAA_CONFIG = {
    "server_url": "http://localhost:5001",  # SSH tunnel to VM
    "max_steps": 15,
    "action_delay": 0.5,
    "screen_size": (1280, 720),  # Current VM resolution, fine for training
}
```

### Model Config

```python
MODEL_CONFIG = {
    "model_name": "Qwen/Qwen2.5-VL-7B-Instruct",  # or Qwen3-VL, or smaller
    "load_in_4bit": True,
    "max_seq_length": 4096,
    "lora_r": 16,
    "lora_alpha": 16,
}
```

---

## 9. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|-----------|------------|
| rollout_func doesn't support VLM image inputs | HIGH | MEDIUM | Manual image tokenization in rollout_func (standard HF pattern). Fall back to custom training loop if needed. |
| Unsloth + rollout_func incompatible | MEDIUM | LOW | Fall back to standard TRL (more VRAM, still works). Test early. |
| rollout_func API changes (marked "experimental") | MEDIUM | MEDIUM | Pin TRL version. Keep standalone trainer as fallback. |
| GRPO produces zero gradient (all rewards 0 or 1) | HIGH | HIGH | Start on Task Manager (40.9% SFT success rate). Need reward variance. |
| VM latency makes training impractical | MEDIUM | LOW | Phase 2 showed ~6s per step. 15 steps × 8 rollouts = ~12 min per GRPO step. Acceptable for validation. |
| Model outputs invalid actions | HIGH | MEDIUM | CoT warm-up SFT required before GRPO (base model must produce valid JSON actions first). |

---

## 10. What We're NOT Building

| Temptation | Why not |
|-----------|---------|
| Custom GRPO loss computation | TRL handles this with battle-tested code |
| vLLM integration | TRL + Unsloth handle this |
| Checkpoint management | TRL handles this |
| Multi-GPU distribution | TRL/Unsloth handle this (vllm_mode="server") |
| Full OpenEnv server (P1) | rollout_func is sufficient for the 2-week sprint |
| Dense rewards / HCAPO (P1) | Binary rewards first, optimize credit later |
| Parallel VM rollouts (P1) | Single VM first, pool later |

---

## 11. Success Criteria

### Week 1 (Mar 17-22)
- [ ] rollout_func runs one GRPO step against live WAA VM
- [ ] Loss value is non-NaN and decreasing
- [ ] Checkpoint saved and loadable

### Week 2 (Mar 23-30)
- [ ] 100+ GRPO steps completed
- [ ] At least one task shows reward improvement over baseline
- [ ] Training script documented and reproducible
- [ ] Enterprise customer can run training with their own model/tasks

---

## 12. References

- [TRL GRPOTrainer docs](https://huggingface.co/docs/trl/main/en/grpo_trainer)
- [TRL OpenEnv integration](https://huggingface.co/docs/trl/main/en/openenv)
- [OpenEnv GitHub](https://github.com/meta-pytorch/OpenEnv)
- [Unsloth VLM docs](https://docs.unsloth.ai)
- [GRPO training research report](../grpo_training_research_2026_03_17.md)
- [OSS RL tooling research](memory: research_oss_rl_tooling_2026_03_17.md)
- [Enterprise customer GRPO spec](../private/grpo_env_spec_external_agent.md)
