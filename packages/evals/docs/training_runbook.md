# Training Runbook: First GRPO/GiGPO Training Loop on WAA

## Overview

Step-by-step playbook for running verl-agent/VAGEN RL training on a GPU VM
connected to a WAA Windows VM. Validated on AWS g5.xlarge (A10G 24GB) with
Azure WAA VM (waa-pool-00).

**Stack**: PyTorch 2.8.0, vLLM 0.11.0, Ray 2.54.0, VAGEN, Qwen2.5-VL-3B-Instruct

## Pre-Flight Checklist

### Azure WAA VM (waa-pool-00)

```
[ ] VM running:
    az vm show -n waa-pool-00 -g openadapt-agents --query powerState -o tsv

[ ] IP confirmed:
    az vm show -n waa-pool-00 -g openadapt-agents -d --query publicIps -o tsv

[ ] Docker container running:
    ssh azureuser@<WAA_IP> "docker ps --format '{{.Names}} {{.Status}}'"

[ ] Port 5000 (Flask API):
    curl -s http://<WAA_IP>:5000/probe | head -5

[ ] Port 5051 (socat bridge for evaluate_server):
    curl -s http://<WAA_IP>:5051/probe | head -5
    If fails, re-establish bridge:
      CONTAINER_PID=$(ssh azureuser@<WAA_IP> "docker inspect --format '{{.State.Pid}}' <container>")
      ssh azureuser@<WAA_IP> "rm -f /tmp/waa-bridge.sock"
      ssh azureuser@<WAA_IP> "nsenter -t $CONTAINER_PID -n socat UNIX-LISTEN:/tmp/waa-bridge.sock,fork TCP:localhost:5050 &"
      ssh azureuser@<WAA_IP> "socat TCP-LISTEN:5051,fork,reuseaddr UNIX-CONNECT:/tmp/waa-bridge.sock &"

[ ] Task setup works:
    curl -s -X POST http://<WAA_IP>:5051/setup \
      -H "Content-Type: application/json" \
      -d '{"task_id":"<TASK_UUID>"}'
```

### AWS GPU VM

```
[ ] nvidia-smi works and shows expected GPU(s)
[ ] conda activate verl-agent works
[ ] python -c "import vagen; print(vagen.__file__)" succeeds
[ ] python -c "from openadapt_evals.adapters.verl_env import WAADesktopEnv" succeeds
[ ] WAADesktop registered in ~/verl-agent/vagen/configs/env_registry.yaml
[ ] WAA VM reachable: curl -s http://<WAA_IP>:5000/probe
[ ] wandb configured: wandb login --verify
[ ] Disk space: df -h / (need 50GB+ free)
```

### Connectivity Smoke Test

```bash
# From GPU VM
conda run -n verl-agent python3 -c "
import asyncio
from openadapt_evals.adapters.verl_env import WAADesktopEnv
env = WAADesktopEnv({
    'server_url': 'http://<WAA_IP>:5000',
    'evaluate_url': 'http://<WAA_IP>:5051',
    'task_id': '<TASK_UUID>',
    'max_steps': 3,
    'evaluate_at_done': True,
    'action_type': 'fractional',
})
obs, info = asyncio.run(env.reset(seed=42))
print('Reset OK, obs keys:', obs.keys())
obs, reward, done, info = asyncio.run(env.step('CLICK(x=0.5, y=0.5)'))
print(f'Step OK, reward={reward}, done={done}')
asyncio.run(env.close())
print('Smoke test passed!')
"
```

## Instance Selection

| Instance | GPUs | VRAM | $/hr (OD) | $/hr (Spot) | Use Case |
|----------|------|------|-----------|-------------|----------|
| g5.xlarge | 1x A10G | 24GB | $1.006 | $0.43 | Smoke test, single-GPU dev |
| g5.2xlarge | 1x A10G | 24GB | $1.21 | ~$0.52 | Single-GPU with more RAM |
| g5.12xlarge | 4x A10G | 96GB | $5.67 | $2.90 | Multi-GPU training (recommended) |
| g6.12xlarge | 4x L4 | 96GB | $4.60 | $2.26 | Budget multi-GPU alternative |

## Key Architecture Constraints

1. **n_envs must be 1** — only one WAA VM, multiple envs would clobber state
2. **Use `rollout.n` for GRPO group size** — generates N responses sequentially, not parallel envs
3. **Entry point is `vagen.main_ppo`**, not `verl.trainer.main_ppo` — VAGEN extends verl with multi-turn agent support
4. **Hydra config system** — use `--config-path` and `--config-name=vagen_multiturn`
5. **Use `rollout.name=vllm`** — already validated; VAGEN examples use sglang but vLLM works

## Launch Commands

### Step 1: Create training data YAML on GPU VM

```bash
cat > ~/verl-agent/train_waa.yaml << 'EOF'
envs:
  - name: WAADesktop
    n_envs: 1
    data_source: waa
    seed: [1, 100, 1]
    max_turns: 15
    response_length_per_turn: 512
    config:
      server_url: "http://<WAA_IP>:5000"
      evaluate_url: "http://<WAA_IP>:5051"
      task_id: "<TASK_UUID>"
      max_steps: 15
      evaluate_at_done: true
      action_type: fractional
EOF
cp ~/verl-agent/train_waa.yaml ~/verl-agent/val_waa.yaml
```

### Step 2: Launch GRPO training

```bash
cd ~/verl-agent && \
PYTHONUNBUFFERED=1 conda run -n verl-agent python3 -m vagen.main_ppo \
    --config-path=$(pwd)/vagen/configs \
    --config-name=vagen_multiturn \
    data.train_files=$(pwd)/train_waa.yaml \
    data.val_files=$(pwd)/val_waa.yaml \
    data.train_batch_size=1 \
    data.max_prompt_length=2048 \
    data.max_response_length=512 \
    data.return_raw_chat=True \
    data.return_multi_modal_inputs=True \
    algorithm.adv_estimator=grpo \
    algorithm.kl_ctrl.kl_coef=0.0 \
    actor_rollout_ref.model.path=Qwen/Qwen2.5-VL-3B-Instruct \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=1 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.fsdp_config.param_offload=True \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \
    actor_rollout_ref.rollout.name=vllm \
    actor_rollout_ref.rollout.mode=async \
    actor_rollout_ref.rollout.n=4 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \
    actor_rollout_ref.rollout.enforce_eager=True \
    actor_rollout_ref.rollout.enable_chunked_prefill=True \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.agent.agent_loop_config_path=$(pwd)/vagen/configs/agent.yaml \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.total_training_steps=10 \
    trainer.test_freq=5 \
    trainer.save_freq=10 \
    trainer.val_before_train=True \
    trainer.logger=[console,wandb] \
    trainer.project_name=openadapt-waa-rl \
    trainer.experiment_name=grpo_waa_smoke \
    2>&1 | tee ~/grpo_waa_training.log
```

## Monitoring

### WandB Metrics

| Metric | Healthy Sign |
|--------|-------------|
| `train/reward_mean` | Increasing from ~0.0 |
| `train/reward_std` | > 0 (needed for GRPO signal) |
| `train/reward_max` | Hits 1.0 = first success |
| `train/entropy` | Decreasing (more decisive) |
| `rollout/episode_length` | Varying (not all hitting max) |

### GPU Health

```bash
watch -n 5 nvidia-smi     # Memory <20GB/GPU with offloading
tail -f ~/grpo_waa_training.log
```

## Iteration Plan

| Run | Steps | Instance | Cost | Goal |
|-----|-------|----------|------|------|
| 0 (Smoke) | 2-3 | g5.xlarge | ~$5 | Pipeline runs without crashes |
| 1 (Signal) | 10 | g5.12xlarge | ~$50 | Rewards computed, wandb logs |
| 2 (Training) | 50 | g5.12xlarge | ~$250 | Look for reward_mean trend |
| 3 (Extended) | 100+ | g5.12xlarge | ~$500 | Only if Run 2 shows signal |

## Common Failure Modes

| Issue | Symptom | Fix |
|-------|---------|-----|
| OOM | `CUDA out of memory` | Reduce `gpu_memory_utilization` to 0.4, reduce `rollout.n` to 2 |
| WAA unresponsive | Timeout/ConnectionError | Check Docker, re-establish socat bridge. NEVER `az vm restart` |
| PyAutoGUI fail-safe | `FailSafeException` | `curl -X POST .../execute -d '{"command":"python -c \"import pyautogui; pyautogui.FAILSAFE=False; pyautogui.moveTo(500,400)\""}'` |
| WAADesktop not found | `KeyError: 'WAADesktop'` | Re-register in env_registry.yaml, verify import path |
| All rewards 0.0 | No learning signal | Check evaluate endpoint, task may be too hard for 3B |
| Ray issues | Dead workers | `ray stop --force && ray start --head --num-gpus=4` |

## Success Criteria

For first run (pipeline validation):
- Pipeline runs without crashes
- Rollouts complete (episodes reach DONE or max_steps)
- `reward_std > 0` (variance in outcomes)
- Actions are parseable (`is_action_valid` mostly True)

For 04d9aeaf task (LibreOffice Calc, extremely hard):
- **Minimum**: At least 1 episode scores 1.0 in 100 steps
- **Good**: reward_mean > 0.1 after 100 steps
- **Note**: Even Claude Sonnet scored 0/1 on this task. Consider easier tasks (notepad, settings) for faster iteration.
