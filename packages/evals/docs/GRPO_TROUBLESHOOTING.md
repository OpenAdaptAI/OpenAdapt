# GRPO Training Troubleshooting Guide

Quick reference for the training run starting 2026-03-22.

---

## 1. Pre-Flight Checklist

Run these before starting real training:

```bash
# 1a. Validate pipeline (no VM, no GPU needed)
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks \
    --mock \
    --output ./grpo_output_mock

# Expected output:
#   "Mock pipeline validation PASSED"
#   Rewards list with variance > 0

# 1b. Check WAA server is reachable
curl -s http://localhost:5001/health | python -m json.tool

# 1c. Verify SSH tunnel is alive
ssh -O check azureuser@<VM_IP>

# 1d. Check GPU memory
nvidia-smi

# 1e. Verify packages
python -c "import trl; print(trl.__version__)"   # need >= 0.17
python -c "import peft; print(peft.__version__)"
python -c "import transformers; print(transformers.__version__)"
python -c "from openadapt_evals.training.trl_rollout import make_waa_rollout_func; print('OK')"
```

---

## 2. Common Errors and Fixes

### 2.1 `ModuleNotFoundError: No module named 'trl'`

```bash
pip install trl>=0.17
# Or with uv:
uv pip install trl>=0.17
```

Also need: `pip install peft transformers accelerate bitsandbytes datasets`

### 2.2 Model loading failures

**Qwen2.5-VL models** (including 3B, 7B): Use `AutoModelForVision2Seq` (this is what our scripts use).

```python
from transformers import AutoModelForVision2Seq, AutoProcessor
model = AutoModelForVision2Seq.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct", ...)
```

If you see `ValueError: Unrecognized model` or similar, update transformers:
```bash
pip install transformers>=4.45.0
```

For Qwen3-VL-8B specifically: same `AutoModelForVision2Seq` class works. For Qwen3.5-9B (non-VL), it uses native vision and may need a different loader -- stick with Qwen2.5-VL or Qwen3-VL for now.

### 2.3 CUDA Out of Memory (OOM)

**VRAM requirements (approximate, with 4-bit LoRA r=16):**

| Model | Base VRAM | + LoRA | + Rollout | Total |
|-------|-----------|--------|-----------|-------|
| Qwen2.5-VL-3B | ~4GB | ~5GB | ~7GB | Works on 8GB+ |
| Qwen2.5-VL-7B | ~8GB | ~10GB | ~14GB | Works on 16GB+ |
| Qwen3-VL-8B | ~9GB | ~11GB | ~18GB | A10G 24GB OK |
| Qwen3.5-9B | ~10GB | ~14GB | ~22GB | A10G tight, A100 preferred |

**Fixes for OOM:**

```bash
# Use Unsloth (saves ~40% VRAM)
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks \
    --server-url http://localhost:5001 \
    --model Qwen/Qwen2.5-VL-7B-Instruct \
    --use-unsloth \
    --output ./grpo_output

# Reduce group size (fewer concurrent rollouts in memory)
--num-generations 2   # default is 4

# Reduce completion length
--max-completion-length 128   # default is 256

# Reduce batch size
--batch-size 1 --gradient-accumulation 8
```

If using the standalone trainer (`openadapt-ml/training/grpo/trainer.py`), it already does per-step backward to avoid OOM on long trajectories.

### 2.4 `ConnectionRefusedError` / WAA server unreachable

The WAA server must be running during training because rollouts execute live actions.

```bash
# Check tunnel
ss -tlnp | grep 5001
# Or on macOS:
lsof -i :5001

# Re-establish SSH tunnel
ssh -L 5001:localhost:5000 -L 8006:localhost:8006 -N azureuser@<VM_IP> &

# Verify WAA Flask is up inside VM
ssh azureuser@<VM_IP> "docker exec winarena curl -s http://172.30.0.2:5000/health"
```

**If the tunnel dies during training**: The rollout will fail for that episode, log the error, and return reward=0.0 (see `trl_rollout.py` line 310-315). Training continues, but you get wasted gradient steps. Fix the tunnel and it self-heals.

### 2.5 All rewards are identical (zero gradient signal)

GRPO computes group-relative advantages: `advantage[i] = (reward[i] - mean) / std`. If all rollouts in a group get the same reward, std=0 and the step is skipped (no gradient update).

**Symptoms:**
- Log shows `"skipped": True` for every training step
- Loss stays at 0.0
- `reward_mean` is always 0.0 or always 1.0

**Fixes:**
- **Mix easy + hard tasks**: Include tasks the model can partially solve (e.g., `notepad-hello`) alongside harder ones
- **Increase group size**: `--num-generations 8` gives more chances for variance
- **Lower temperature**: Try `temperature=0.7` (default in standalone trainer) for more exploration without random noise
- **Use milestone-based dense rewards**: Tasks with `milestones:` in YAML give partial credit (0.25, 0.5, etc.) instead of binary 0/1. The `notepad-hello.yaml` example has milestones.

### 2.6 `KeyError: 'pixel_values'` during TRL training

TRL's GRPOTrainer recomputes log-probs during the training forward pass but only stores token IDs from `rollout_func`, losing the image `pixel_values`. This is a known TRL limitation for VLM multi-turn trajectories.

**Workaround**: Use the standalone trainer in `openadapt-ml` which handles this:
```bash
python -c "
from openadapt_ml.training.grpo.config import GRPOConfig
from openadapt_ml.training.grpo.trainer import GRPOTrainer

config = GRPOConfig(
    model_name='Qwen/Qwen2.5-VL-7B-Instruct',
    server_url='http://localhost:5001',
    task_ids=['custom-notepad-hello'],
    num_rollouts_per_step=4,
    num_training_steps=100,
    output_dir='./grpo_checkpoints',
)
trainer = GRPOTrainer(config)
trainer.train()
"
```

The standalone trainer does its own forward pass with images at loss computation time (see `_compute_rollout_loss` in `trainer.py`).

### 2.7 `ImportError: openadapt-evals is required for rollout collection`

The standalone trainer in openadapt-ml needs openadapt-evals for `RLEnvironment`:

```bash
pip install openadapt-evals
# Or from source:
pip install -e /path/to/openadapt-evals
```

### 2.8 SSH tunnel drops during multi-hour training

Long training runs (hours) will hit SSH idle timeouts.

**Fix -- add to `~/.ssh/config`:**
```
Host waa-vm
    HostName <VM_IP>
    User azureuser
    ServerAliveInterval 60
    ServerAliveCountMax 10
    LocalForward 5001 localhost:5000
    LocalForward 8006 localhost:8006
```

**Or use autossh:**
```bash
autossh -M 0 -f -N \
    -o "ServerAliveInterval 60" \
    -o "ServerAliveCountMax 10" \
    -L 5001:localhost:5000 \
    -L 8006:localhost:8006 \
    azureuser@<VM_IP>
```

### 2.9 `torch.OutOfMemoryError` during rollout generation (not training)

Rollout generation (model.generate) can spike VRAM. Fixes:

```bash
# With Unsloth, set lower GPU memory utilization
# (see load_model_unsloth in train_trl_grpo.py -- defaults to 0.6)
--use-unsloth

# Reduce max_new_tokens in generate
# The standalone trainer uses max_new_tokens=100
# The TRL script uses max_completion_length=256
--max-completion-length 128
```

### 2.10 VLM outputs unparseable actions

The rollout function handles parse failures gracefully:
- `trl_rollout.py`: Falls back to `BenchmarkAction(type="done")` which ends the episode
- `trainer.py`: Parses DSL format `CLICK(x=0.5, y=0.3)`, falls back to `DONE()`

If you see many episodes ending immediately at step 0, the model may be generating garbage. This is expected early in training -- rewards should provide gradient signal to improve over time.

---

## 3. Two Training Paths

We have two GRPO implementations. Choose based on your situation:

### Path A: TRL Script (`scripts/train_trl_grpo.py`)
- Uses TRL's GRPOTrainer
- Supports `--use-unsloth` for VRAM efficiency
- Supports `--use-vllm` for faster generation
- Better for standard setups
- **Known limitation**: VLM pixel_values not passed through rollout_func (may cause issues during TRL's internal log-prob recomputation)

### Path B: Standalone Trainer (`openadapt-ml/training/grpo/trainer.py`)
- Custom GRPO math (REINFORCE with group-relative advantages)
- Per-step backward to avoid OOM
- Handles VLM images correctly during loss computation
- beta=0.0 (no KL penalty, no reference model needed)
- **Recommended for VLM desktop agent training**

```bash
# Path A
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks \
    --server-url http://localhost:5001 \
    --model Qwen/Qwen2.5-VL-7B-Instruct \
    --use-unsloth \
    --output ./grpo_output

# Path B
python -c "
from openadapt_ml.training.grpo.config import GRPOConfig
from openadapt_ml.training.grpo.trainer import GRPOTrainer

config = GRPOConfig(
    model_name='Qwen/Qwen2.5-VL-7B-Instruct',
    server_url='http://localhost:5001',
    task_ids=['custom-notepad-hello'],
    num_rollouts_per_step=4,
    num_training_steps=100,
    save_every_steps=25,
    output_dir='./grpo_checkpoints',
)
trainer = GRPOTrainer(config)
trainer.train()
"
```

---

## 4. Monitoring Training Progress

### 4.1 Log output

Both trainers log per-step metrics:
```
Step 5/100: reward=0.25 loss=0.0034 time=45.2s
```

Key signals:
- `reward_mean` should trend upward over steps
- `loss` should be non-zero (zero means skipped due to no reward variance)
- `step_time` shows how long each rollout+gradient step takes

### 4.2 Watch for stalls

```bash
# Tail the training log
tail -f grpo_output/training.log

# Check GPU utilization -- should be high during forward/backward, dip during rollouts
watch -n 2 nvidia-smi
```

### 4.3 Check reward distribution

If using the TRL script with `--mock`, it saves `mock_results.json` with reward stats. For live training, grep the log:

```bash
grep "reward=" training.log | awk -F'reward=' '{print $2}' | head -20
```

---

## 5. Checkpoints: Saving, Resuming, Evaluating

### 5.1 Where checkpoints are saved

- **TRL script**: `--output ./grpo_output` (saves at `--save-steps` intervals + final)
- **Standalone trainer**: `checkpoints/grpo/step_N/` directories (saves at `save_every_steps` intervals + final)

Each checkpoint is a LoRA adapter directory containing:
```
step_50/
  adapter_model.safetensors
  adapter_config.json
```

### 5.2 Resume training from checkpoint

**TRL script:**
```bash
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks \
    --server-url http://localhost:5001 \
    --model Qwen/Qwen2.5-VL-7B-Instruct \
    --lora-checkpoint ./grpo_output/checkpoint-50 \
    --output ./grpo_output_resumed
```

**Standalone trainer:**
```python
config = GRPOConfig(
    model_name='Qwen/Qwen2.5-VL-7B-Instruct',
    lora_checkpoint='./grpo_checkpoints/step_50',
    # ... other config
)
```

### 5.3 Evaluate a checkpoint

Run the checkpoint against WAA tasks using the standard eval pipeline:

```bash
# Single task evaluation
openadapt-evals run \
    --agent policy \
    --model-checkpoint ./grpo_checkpoints/step_100 \
    --task notepad_hello \
    --server http://localhost:5001

# Full evaluation
python scripts/run_full_eval.py \
    --server-url http://localhost:5001 \
    --grounder-endpoint http://localhost:8000/v1 \
    --save-screenshots
```

---

## 6. Comparing Before/After with TraceAnalyzer

After running evaluations on both the base model and a checkpoint:

```python
from openadapt_evals.analysis import TraceAnalyzer

# Load traces
baseline = TraceAnalyzer("benchmark_results/baseline_eval.jsonl")
trained = TraceAnalyzer("benchmark_results/grpo_step100_eval.jsonl")

# Summary stats
print(baseline.summary())
print(trained.summary())

# Diff: which tasks improved/regressed
diff = baseline.compare(trained)
print(f"Improved: {len(diff['improved'])}")
print(f"Regressed: {len(diff['regressed'])}")
print(f"Unchanged: {len(diff['unchanged'])}")

# Failure mode analysis
for fm in trained.failure_modes():
    print(f"  {fm['mode']}: {fm['count']} episodes")
```

**CLI version:**
```bash
# Summary
python -m openadapt_evals.analysis benchmark_results/grpo_step100_eval.jsonl

# Compare two runs
python -m openadapt_evals.analysis benchmark_results/baseline_eval.jsonl \
    --compare benchmark_results/grpo_step100_eval.jsonl \
    --report diff_report.html

# JSON output for scripting
python -m openadapt_evals.analysis benchmark_results/grpo_step100_eval.jsonl --json
```

---

## 7. Recommended Training Recipe

For the first run, start conservative:

```bash
# Step 1: Mock validation (30 seconds, no GPU/VM)
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks \
    --mock \
    --output ./grpo_mock_test

# Step 2: Single-task live validation (5-10 minutes)
# Use the simplest task with milestones for dense rewards
python -c "
from openadapt_ml.training.grpo.config import GRPOConfig
from openadapt_ml.training.grpo.trainer import GRPOTrainer

config = GRPOConfig(
    model_name='Qwen/Qwen2.5-VL-3B-Instruct',  # smallest model first
    server_url='http://localhost:5001',
    task_ids=['custom-notepad-hello'],
    num_rollouts_per_step=4,
    num_training_steps=5,       # just 5 steps to validate
    save_every_steps=5,
    max_steps_per_episode=10,
    output_dir='./grpo_smoke_test',
)
trainer = GRPOTrainer(config)
trainer.train()
"

# Step 3: Full training run
python -c "
from openadapt_ml.training.grpo.config import GRPOConfig
from openadapt_ml.training.grpo.trainer import GRPOTrainer

config = GRPOConfig(
    model_name='Qwen/Qwen2.5-VL-7B-Instruct',
    load_in_4bit=True,
    server_url='http://localhost:5001',
    task_ids=['custom-notepad-hello', 'custom-calc-formula', 'custom-create-desktop-folder'],
    num_rollouts_per_step=8,
    num_training_steps=200,
    save_every_steps=25,
    max_steps_per_episode=15,
    learning_rate=5e-6,
    output_dir='./grpo_full_run',
)
trainer = GRPOTrainer(config)
trainer.train()
"
```

---

## 8. Quick Diagnostic Flowchart

```
Training step hangs?
  -> Check SSH tunnel: lsof -i :5001
  -> Check WAA server: curl http://localhost:5001/health
  -> Check VM status: az vm show -n waa-pool-00 -g openadapt-agents --query powerState

Loss always 0.0?
  -> All rewards identical (no variance)
  -> Add easier tasks with milestones for partial credit
  -> Increase --num-generations to 8

OOM during generate?
  -> Use --use-unsloth
  -> Reduce --max-completion-length to 128
  -> Use smaller model (3B instead of 7B) for smoke test

OOM during backward?
  -> Standalone trainer already does per-step backward
  -> Reduce --batch-size to 1
  -> Increase --gradient-accumulation to compensate

Episodes end immediately (step 0)?
  -> Model generating unparseable output
  -> Check log for "No JSON found" or "Could not parse VLM output"
  -> Expected early in training, should improve with gradient updates
  -> If persistent, the prompt format may be mismatched -- verify SYSTEM_PROMPT

WAA /execute returns errors?
  -> Windows VM may need reboot (NEVER use az vm restart -- kills QEMU)
  -> Instead: ssh in, docker restart winarena, wait 5 min for Windows to boot
```
