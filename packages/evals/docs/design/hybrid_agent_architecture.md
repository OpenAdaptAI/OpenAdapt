# Hybrid Agent Architecture: Claude Computer Use + Qwen3-VL Fine-Tuning

**Date**: 2026-02-23
**Status**: Proposed
**Branch**: `fix/pool-automation`

---

## 1. Context & Problem

Our WAA evaluations score 0.00 across all 6 conditions (3 tasks x ZS/DC) with GPT-5.1. Root cause analysis identified five compounding issues:

1. **Wrong model**: GPT-5.1 is a general VLM, not trained for coordinate prediction
2. **Wrong grounding**: We predict raw pixel coords; WAA Navi baseline uses SoM + element IDs
3. **A11y tree bug**: Returns WAA server console logs instead of UI elements
4. **No coordinate normalization**: Absolute pixels (4.3% accuracy vs 33.7% normalized)
5. **No screenshot annotation**: No SoM overlays or element markers

We need: (A) a quick win to validate infrastructure with non-zero scores, and (B) a fine-tunable open-source backbone for our demo-conditioning thesis.

---

## 2. Strategy: Option C (Hybrid)

### Track 1 — Claude `computer_use` (immediate baseline)

Add a new `ClaudeComputerUseAgent` that uses Anthropic's native `computer_use` tool. This gives us a model that is actually trained for coordinate prediction, with structured action output (no regex parsing). Validates that our benchmark infrastructure works end-to-end.

### Track 2 — Qwen3-VL fine-tuning pipeline (strategic)

Integrate Qwen3-VL-8B as a new agent backend with normalized coordinates and a proper action space. Build the SFT pipeline so we can fine-tune on our annotated demos and measure the ZS→DC delta that validates our core thesis.

---

## 3. Qwen3-VL vs Qwen2.5-VL Decision

**Recommendation: Qwen3-VL-8B-Instruct**

### Key architectural upgrades in Qwen3-VL

| Feature | Qwen2.5-VL | Qwen3-VL |
|---------|-----------|----------|
| Vision encoder | Custom ViT (14x14 patches) | **SigLIP-2** (16x16 patches) |
| Feature fusion | Final ViT layer only | **DeepStack**: multi-level ViT features fused into first 3 LLM layers |
| Position encoding | M-RoPE + T-RoPE | **Interleaved-MRoPE** (full-frequency t/w/h) |
| LLM backbone | Qwen2.5 (dense only) | Qwen3 (dense + MoE variants) |
| Context window | Standard | **256K native** (expandable to 1M) |
| Thinking mode | None | **Dual-mode**: Instruct + Thinking variants |
| Sizes | 3B, 7B, 72B | **2B, 4B, 8B, 30B-A3B (MoE), 32B, 235B-A22B (MoE)** |

### Why DeepStack matters for GUI grounding

DeepStack fuses features from multiple ViT depths into the LLM via 2-layer MLPs. Early ViT layers capture low-level visual details (button edges, text rendering), while deep layers capture semantics. This is exactly what GUI grounding needs — both pixel-level precision and semantic understanding of UI elements.

### Benchmark comparison (8B class)

| Benchmark | Qwen2.5-VL-7B | Qwen3-VL-8B |
|-----------|---------------|-------------|
| MMMU | 58.6 | ~70 (+12) |
| MathVista | 68.2 | ~77 (+9) |
| ScreenSpot | ~87% (72B) | ~94% (reported) |
| ScreenSpot-Pro | N/A (7B) | 60.5 (32B Instruct) |

GUI-Owl (fine-tuned on Qwen3-VL-8B) achieves **52.9% on OSWorld-Verified** — demonstrating the backbone's fine-tuning potential.

### Coordinate format

Both use normalized **[0, 1000]** coordinates:
- Points: `[x, y]` where each value is 0-1000
- Bounding boxes: `[x_min, y_min, x_max, y_max]` in 0-1000 range
- Convert to pixels: `pixel = coord / 1000 * dimension`

### Fine-tuning ecosystem

| Framework | Qwen3-VL Support | Notes |
|-----------|-----------------|-------|
| **ms-swift** | Full | Official recommendation. Auto-converts coords to 0-1000. SFT + GRPO |
| **Unsloth** | Full | 1.7x faster, 60% less VRAM. LoRA/QLoRA |
| **2U1/Qwen-VL-Series-Finetune** | Full | Includes MoE support. Use DeepSpeed ZeRO-2 |
| **LLaMA-Factory** | Partial | PR #9196. Template issues remain (#9458) |

### Known issue

DeepStack features were zeroed out in vLLM < 0.11.0 (grounding accuracy dropped to <2%). **Requires vLLM >= 0.11.0** or updated SGLang.

### License

Apache 2.0 for all Qwen3-VL sizes, including 235B-A22B.

### Hardware

| Config | VRAM | Time |
|--------|------|------|
| Qwen3-VL-8B + LoRA (Unsloth) | 24 GB (1x RTX 4090) | 4-12 hours |
| Qwen3-VL-8B + QLoRA (Unsloth) | 16-20 GB | 6-16 hours |
| Qwen3-VL-8B full FT | 40+ GB (multi-GPU) | Longer |

---

## 4. Track 1: Claude Computer Use Agent

### 4.1 Architecture

```
ClaudeComputerUseAgent (new)
  │
  ├── Uses: client.beta.messages.create()
  ├── Beta: "computer-use-2025-11-24"
  ├── Tool: computer_20251124
  ├── Actions: structured tool_use blocks (no regex parsing)
  ├── Screenshots: provided as tool_result (base64 PNG)
  └── Multi-step: conversation maintained across steps
```

### 4.2 How Claude computer_use differs from current ApiAgent

| Aspect | Current ApiAgent | ClaudeComputerUseAgent |
|--------|-----------------|----------------------|
| API | `client.messages.create()` | `client.beta.messages.create()` |
| Action output | Free text `computer.click(x, y)` parsed via regex | Structured `tool_use` blocks with typed fields |
| Screenshot input | Image content block in user message | `tool_result` with base64 image |
| Conversation | Single-turn per step (system + user) | Multi-turn (full conversation history) |
| Coordinate source | Model predicts pixel coords (unreliable) | Model trained specifically for coordinate prediction |
| Action parsing | 5 regex strategies in `_parse_api_response()` | Direct `block.input["action"]` field access |

### 4.3 Supported actions

```
screenshot, left_click, right_click, middle_click,
double_click, triple_click, type, key, scroll,
left_click_drag, mouse_move, left_mouse_down,
left_mouse_up, hold_key, wait, zoom (Opus 4.5+)
```

### 4.4 Agent loop

```python
# Simplified flow
messages = [{"role": "user", "content": task_instruction}]

for step in range(max_steps):
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        tools=[{"type": "computer_20251124", "name": "computer",
                "display_width_px": 1024, "display_height_px": 768}],
        messages=messages,
        betas=["computer-use-2025-11-24"],
    )
    messages.append({"role": "assistant", "content": response.content})

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            action = block.input  # {"action": "left_click", "coordinate": [500, 300]}
            execute_action(action)
            screenshot = capture_screenshot()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": [{"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(screenshot).decode()
                }}],
            })

    if not tool_results:
        break  # Claude signaled completion
    messages.append({"role": "user", "content": tool_results})
```

### 4.5 Model selection

| Model | Tool Version | Cost (in/out per MTok) | Notes |
|-------|-------------|----------------------|-------|
| Claude Sonnet 4.6 | `computer_20251124` | $3 / $15 | Best value. Same computer_use as Opus |
| Claude Opus 4.6 | `computer_20251124` | $5 / $25 | Highest capability |
| Claude Haiku 4.5 | `computer_20250124` | $1 / $5 | Budget option |

**Recommendation**: Start with Sonnet 4.6. Estimated ~$0.15-$0.50 per 15-step episode.

### 4.6 Demo conditioning with computer_use

To test our core thesis (ZS vs DC), inject the demo into the initial user message:

```python
# Zero-shot
messages = [{"role": "user", "content": task_instruction}]

# Demo-conditioned
demo_text = format_demo_for_prompt(annotated_demo)
messages = [{"role": "user", "content": f"""Here is a demonstration of a similar completed task:

{demo_text}

Now complete this task: {task_instruction}"""}]
```

The demo persists in conversation history across all steps (solving the "100% first-action / 0% episode" problem).

### 4.7 Screenshot resolution

Claude auto-downscales images exceeding 1568px on the longest edge or ~1.15 megapixels. When this happens, returned coordinates are in the downscaled space and must be upscaled.

For WAA's 1280x720 screenshots: no downscaling needed. Declare `display_width_px=1280, display_height_px=720`.

### 4.8 Implementation plan

**New file**: `openadapt_evals/agents/claude_computer_use_agent.py`

```
ClaudeComputerUseAgent(BenchmarkAgent):
    __init__(api_key, model, demo, display_width, display_height)
    act(observation, task, history) -> BenchmarkAction
    reset()

    # Internal
    _build_initial_messages(task, demo)
    _call_api(messages) -> response
    _process_tool_use(block) -> BenchmarkAction
    _build_tool_result(screenshot_bytes, tool_use_id) -> dict
```

Maps Claude's structured actions to `BenchmarkAction`:

| Claude action | BenchmarkAction |
|---------------|----------------|
| `left_click` + `coordinate` | `BenchmarkAction(type="click", x=norm_x, y=norm_y)` |
| `type` + `text` | `BenchmarkAction(type="type", text=...)` |
| `key` + `text` | `BenchmarkAction(type="key", key=..., modifiers=[...])` |
| `scroll` + `scroll_direction` | `BenchmarkAction(type="scroll", scroll_direction=...)` |
| `left_click_drag` | `BenchmarkAction(type="drag", x=..., y=..., end_x=..., end_y=...)` |
| `double_click` | `BenchmarkAction(type="click", x=..., y=..., raw_action={"double": True})` |
| No tool_use blocks | `BenchmarkAction(type="done")` |

**CLI registration**: Add `api-claude-cu` agent type in `benchmarks/cli.py`.

---

## 5. Track 2: Qwen3-VL Agent + Fine-Tuning Pipeline

### 5.1 Architecture

```
Qwen3VLAgent (new)
  │
  ├── Inference: vLLM >= 0.11.0 or transformers (local)
  ├── Coordinates: normalized [0, 1000]
  ├── Action format: structured text (click(x=500, y=300))
  ├── Thinking mode: optional <think>...</think> blocks
  └── Fine-tuning: ms-swift or Unsloth (LoRA on 24GB GPU)
```

### 5.2 Action space

Following the established GUI agent conventions (UI-TARS, Smol2Operator):

```python
# Desktop actions — coordinates in [0, 1000] range
click(x=500, y=300)
double_click(x=500, y=300)
right_click(x=500, y=300)
type(text="hello world")
press(keys=["ctrl", "c"])
scroll(direction="up", amount=3)
drag(from_coord=[200, 300], to_coord=[800, 500])
# Meta
wait()
finished()
```

### 5.3 Coordinate normalization

```python
def pixel_to_norm(px_x, px_y, width, height):
    """Convert pixel coordinates to [0, 1000] normalized."""
    return int(px_x / width * 1000), int(px_y / height * 1000)

def norm_to_pixel(norm_x, norm_y, width, height):
    """Convert [0, 1000] normalized to pixel coordinates."""
    return norm_x / 1000 * width, norm_y / 1000 * height
```

### 5.4 Training data format

Convert our annotated demos to SFT format:

```json
{
  "image": "step_001_screenshot.png",
  "conversations": [
    {
      "role": "user",
      "content": "<image>\nInstruction: Open Notepad, create draft.txt, type text, save.\n\nPrevious actions:\nclick(x=450, y=320)"
    },
    {
      "role": "assistant",
      "content": "<think>\nThe Save As dialog is open. I need to type the filename in the File name field.\n</think>\ntype(text=\"draft.txt\")"
    }
  ]
}
```

### 5.5 Demo-conditioned inference

At inference time, retrieve the most similar demo trajectory and inject into context:

```
System: You are a GUI agent. Complete desktop tasks by outputting actions.

Here is a demonstration of a similar completed task:
Step 0: [observation] The desktop shows PowerShell. [action] click(x=450, y=950) to open search
Step 1: [observation] Search results show Notepad. [action] click(x=300, y=200) to open Notepad
...

Now complete this task:
Instruction: {task_instruction}
Current screenshot: <image>
Previous actions: {action_history}
```

This validates the LearnAct finding (+198% improvement with 1 demo).

### 5.6 Annotated demo conversion

Our existing demos at `openadapt-ml/.../annotated_demos/*.json` use this format:

```json
{
  "steps": [
    {
      "step_index": 0,
      "observation": "The BEFORE image shows...",
      "intent": "The user is attempting to...",
      "action": "The user clicked on...",
      "action_raw": "CLICK(0.294, 0.532)",
      "result_observation": "The AFTER image shows..."
    }
  ]
}
```

Conversion needed:
- `action_raw` coords are already normalized [0, 1] — multiply by 1000 for Qwen3-VL format
- Pair with screenshot PNGs from the captured recordings
- Add `<think>` blocks from `intent` + `observation` fields

### 5.7 Fine-tuning pipeline (ms-swift)

```bash
# Phase 1: Grounding SFT (public datasets + our demos)
swift sft \
  --model Qwen/Qwen3-VL-8B-Instruct \
  --dataset /path/to/gui_grounding_data.json \
  --train_type lora \
  --lora_rank 64 \
  --torch_dtype bfloat16 \
  --num_train_epochs 3

# Phase 2: Agentic reasoning SFT (our demos with <think> blocks)
swift sft \
  --model /path/to/phase1_checkpoint \
  --dataset /path/to/agentic_data.json \
  --train_type lora \
  --num_train_epochs 2

# Phase 3 (future): GRPO RL
swift rlhf \
  --rlhf_type grpo \
  --model /path/to/phase2_checkpoint \
  --reward_type rule_based
```

### 5.8 Implementation plan

**New file**: `openadapt_evals/agents/qwen3vl_agent.py`

```
Qwen3VLAgent(BenchmarkAgent):
    __init__(model_path, demo, use_thinking, device)
    act(observation, task, history) -> BenchmarkAction
    reset()

    # Internal
    _build_prompt(observation, task, history, demo)
    _run_inference(prompt, image) -> str
    _parse_action(response) -> BenchmarkAction
    _normalize_coords(action, viewport) -> BenchmarkAction
```

**New file**: `openadapt_ml/training/convert_demos.py`

```
convert_annotated_demo_to_sft(demo_json_path, screenshot_dir) -> list[dict]
    # Reads annotated demo JSON
    # Pairs with screenshot PNGs
    # Outputs ms-swift compatible training samples
```

**CLI registration**: Add `qwen3vl` agent type in `benchmarks/cli.py`.

---

## 6. Shared Infrastructure Changes

### 6.1 Fix a11y tree bug

The "UI Elements" field in `api_agent.py` currently receives WAA server console logs (Flask output, pyautogui command history) instead of actual UI elements. This needs fixing in `adapters/waa/live.py` — the `/accessibility` endpoint response parsing needs to extract the actual UI tree from the XML response.

### 6.2 CLI agent registry

Update `benchmarks/cli.py` agent creation to support new agent types:

```python
AGENT_REGISTRY = {
    "api-openai": lambda args: ApiAgent(provider="openai", ...),
    "api-claude": lambda args: ApiAgent(provider="anthropic", ...),
    "api-claude-cu": lambda args: ClaudeComputerUseAgent(...),  # NEW
    "qwen3vl": lambda args: Qwen3VLAgent(...),                  # NEW
    "noop": lambda args: NoopAgent(),
    "retrieval-claude": lambda args: RetrievalAugmentedAgent(...),
}
```

### 6.3 eval-suite integration

The existing `eval-suite` command already supports `--agent` and `--demo-dir`. New agents plug in directly:

```bash
# Track 1: Claude computer_use baseline
openadapt-evals eval-suite \
  --tasks 37e10fc4-...,0c9dda13-...,366de66e-... \
  --agent api-claude-cu \
  --demo-dir /path/to/annotated_demos \
  --no-pool-create --server http://localhost:5001

# Track 2: Qwen3-VL (after fine-tuning)
openadapt-evals eval-suite \
  --tasks 37e10fc4-...,0c9dda13-...,366de66e-... \
  --agent qwen3vl \
  --demo-dir /path/to/annotated_demos \
  --model-path /path/to/finetuned_checkpoint
```

---

## 7. Implementation Order

### Phase 1: Claude Computer Use (1-2 days)

1. **Create `claude_computer_use_agent.py`** (~200 lines)
   - Implement `ClaudeComputerUseAgent(BenchmarkAgent)`
   - Handle `computer_use` tool_use / tool_result loop
   - Map Claude actions → BenchmarkAction
   - Support demo injection in initial message
2. **Register in CLI** (~10 lines in `cli.py`)
   - Add `api-claude-cu` agent type
3. **Test locally with mock adapter** then on Azure VM
4. **Run eval-suite**: ZS + DC on 3 tasks with Sonnet 4.6

**Expected outcome**: Non-zero scores validating infrastructure works.

### Phase 2: Qwen3-VL Agent (2-3 days)

5. **Create `qwen3vl_agent.py`** (~250 lines)
   - Inference via vLLM or transformers
   - Normalized [0, 1000] coordinate format
   - Optional thinking mode
6. **Create `convert_demos.py`** in openadapt-ml (~100 lines)
   - Convert annotated demo JSON to ms-swift SFT format
7. **Register in CLI** (~10 lines in `cli.py`)
8. **Zero-shot eval**: Run Qwen3-VL-8B-Instruct base model on 3 tasks

### Phase 3: Fine-Tuning + DC Eval (3-5 days)

9. **Convert our 3 annotated demos** to SFT format
10. **Augment with public grounding data** (Smol2Operator stage-1)
11. **Fine-tune Qwen3-VL-8B** with LoRA on Lambda Labs / Azure GPU
12. **Run eval-suite**: ZS + DC comparison on fine-tuned model
13. **Measure ZS→DC delta** — validates core thesis

### Phase 4: RL + Scaling (future)

14. **GRPO RL** with click-in-bbox reward
15. **Scale to full WAA benchmark** (154 tasks)
16. **Online RL** in Azure VM

---

## 8. Cost Estimates

### Track 1: Claude Computer Use

| Item | Cost |
|------|------|
| Sonnet 4.6 per 15-step episode | ~$0.15-$0.50 |
| 6 conditions (3 tasks x ZS/DC) | ~$1-$3 |
| Azure VM (D8ds_v4, ~2 hours) | ~$0.76 |
| **Total for Track 1 eval** | **~$2-$4** |

### Track 2: Qwen3-VL Fine-Tuning

| Item | Cost |
|------|------|
| Lambda Labs 1x A10 (24GB) for LoRA SFT | ~$0.75/hr x 8hrs = $6 |
| Inference on Azure VM | ~$0.76/2hrs |
| **Total for Track 2 eval** | **~$7-$10** |

---

## 9. Success Criteria

1. **Track 1**: At least 1 of 3 tasks scores > 0.00 with Claude computer_use
2. **Track 1**: Measurable ZS vs DC difference in behavior (steps, action patterns)
3. **Track 2**: Qwen3-VL-8B base model produces valid actions (clicks land on UI elements)
4. **Track 2 + Phase 3**: Fine-tuned model outperforms base on our 3 tasks
5. **Core thesis**: DC outperforms ZS with statistical significance across conditions

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Anthropic API credits exhausted | High | Blocks Track 1 | Use Haiku 4.5 ($1/MTok) or request credits |
| WAA `/evaluate` endpoint not deployed | Known | All scores = 0.00 | Deploy `waa_server_patch.py` first |
| vLLM DeepStack bug with Qwen3-VL | Medium | Wrong coordinates | Pin vLLM >= 0.11.0 |
| 3 demos insufficient for meaningful SFT | Medium | No improvement | Augment with public grounding data |
| Qwen3-VL coordinate format mismatch | Low | Wrong clicks | Verify [0, 1000] format in test |

---

## 11. References

- [Anthropic Computer Use Docs](https://docs.anthropic.com/en/docs/agents-and-tools/computer-use)
- [anthropics/claude-quickstarts/computer-use-demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo)
- [Qwen3-VL Technical Report (arXiv:2511.21631)](https://arxiv.org/abs/2511.21631)
- [Qwen3-VL GitHub](https://github.com/QwenLM/Qwen3-VL)
- [ms-swift Qwen3-VL Best Practice](https://swift.readthedocs.io/en/latest/BestPractices/Qwen3-VL-Best-Practice.html)
- [Unsloth Qwen3-VL Guide](https://docs.unsloth.ai/models/qwen3-vl-how-to-run-and-fine-tune)
- [2U1/Qwen-VL-Series-Finetune](https://github.com/2U1/Qwen-VL-Series-Finetune)
- [Smol2Operator (HuggingFace)](https://huggingface.co/blog/smol2operator)
- [LearnAct: Few-Shot Demo Retrieval](https://arxiv.org/abs/2504.13805)
- [UI-TARS-1.5](https://github.com/bytedance/UI-TARS)
- [GTA1: GRPO for GUI Grounding](https://huggingface.co/blog/HelloKKMe/grounding-r1)
- [GUI-Owl (OSWorld 52.9%)](https://www.emergentmind.com/topics/gui-owl)
