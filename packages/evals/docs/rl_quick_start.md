# RL Environment Quick Start

## What it provides

`RLEnvironment` wraps a `BenchmarkAdapter` to expose a Gymnasium-like
interface for reinforcement learning without pulling in `gymnasium` as a
dependency.  The core loop is:

```
obs = env.reset()
while not done:
    action = policy(obs)
    step = env.step(action)        # -> RolloutStep(obs, action, reward, done, info)
    obs = step.observation
score = env.evaluate()             # -> float in [0, 1]
```

For convenience, `collect_rollout(agent_fn)` runs the full loop, calls the
evaluator at the end, and returns a list of `RolloutStep` objects with the
terminal reward already assigned.

---

## Prerequisites

1. A WAA server running on a cloud VM (see the main README for VM setup).
2. An SSH tunnel forwarding the WAA port to your local machine.
3. Python 3.10+ with `openadapt-evals` installed (`uv sync`).

---

## SSH tunnel setup

The WAA Flask server listens on port 5000 inside the VM.  Forward it to
your local machine:

```bash
ssh -L 5001:localhost:5000 azureuser@<vm-ip>
```

For persistent tunnels add `-f -N` and keep-alive options:

```bash
ssh -f -N \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -L 5001:localhost:5000 \
    azureuser@<vm-ip>
```

---

## Verify connection

```bash
openadapt-evals probe --server http://localhost:5001
```

A successful probe prints the server status and confirms screenshots are
being served.

---

## Python example

```python
from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
from openadapt_evals.adapters.rl_env import RLEnvironment, ResetConfig

# 1. Connect
adapter = WAALiveAdapter(WAALiveConfig(server_url="http://localhost:5001"))
env = RLEnvironment(adapter=adapter, default_task_id="<WAA-UUID>")

# 2. Reset
obs = env.reset()
print(f"Screen: {env.screen_size}")        # (1920, 1200)
print(f"Screenshot: {len(obs.screenshot)} bytes")

# 3. Observe (no side-effects)
obs = env.observe()

# 4. Act with pixel coordinates
step = env.pixel_action(x=960, y=600)      # absolute pixels
step = env.pixel_action(x_frac=0.5, y_frac=0.5)  # normalised [0,1]

# 5. Evaluate
score = env.evaluate()
print(f"Score: {score:.2f}")
```

---

## Normalised vs absolute coordinates

`pixel_action` accepts two styles of coordinates:

| Style | Parameters | Example |
|-------|-----------|---------|
| Absolute pixels | `x=885, y=600` | Exact screen position |
| Normalised fractions | `x_frac=0.461, y_frac=0.5` | Fraction of screen width/height |

When fractions are provided they are converted to pixels using
`env.screen_size`.  On a 1920x1200 display, `x_frac=0.5` becomes
`x=960`.

Normalised coordinates are useful when the same policy must generalise
across different screen resolutions.

---

## Rollout collection

For RL training you typically need full episode trajectories:

```python
from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation

def my_agent(obs: BenchmarkObservation) -> BenchmarkAction:
    # Your VLM model produces an action from the screenshot
    ...

env.reset()
rollout = env.collect_rollout(agent_fn=my_agent, max_steps=15, stuck_window=3)

for step in rollout:
    print(step.action.type, step.reward, step.done)
```

`collect_rollout` handles:

- Calling `agent_fn(obs)` at each step.
- Terminating when the agent returns a `"done"` action **or** after
  `max_steps`.
- **Stuck detection**: if the last `stuck_window` screenshots are
  byte-for-byte identical the episode is terminated early (the agent is
  not making progress).
- Running `evaluate()` at the end and assigning the score as the terminal
  reward on the last `RolloutStep`.

---

## Reset tiers

| Tier | Method | Latency | Use case |
|------|--------|---------|----------|
| Task setup | `reset(ResetConfig(task_setup_only=True))` | ~5 s | Between rollouts on the same VM |
| QEMU reboot | `reset(ResetConfig(qemu_reboot=True))` | ~90 s | When the guest OS is in a bad state |
| Snapshot restore | *(future)* | ~15 s | Fast deterministic resets |

The default is `task_setup_only=True`, which closes open windows and
re-runs the task's setup commands.  This is fast enough for most training
loops.

---

## GRPO integration overview

Group Relative Policy Optimisation (GRPO) assigns rewards relative to a
group of rollouts for the same prompt.  With the RL environment:

1. **Collect a group** of N rollouts for the same task.
2. Each rollout gets a **binary reward** (0 or 1) from the WAA evaluator.
3. Compute **group-relative advantages**: `A_i = r_i - mean(r)`.
4. Update the policy using the advantages and the action log-probabilities
   captured during rollout.

```
for task_id in task_ids:
    env.reset(ResetConfig(task_id=task_id))
    group = [env.collect_rollout(policy_fn) for _ in range(N)]
    rewards = [rollout[-1].reward for rollout in group]
    advantages = [r - mean(rewards) for r in rewards]
    update_policy(group, advantages)
```

The `scripts/run_grpo_rollout.py` script provides a runnable example of
the collection phase with a random agent.

---

## Cost estimates

Running WAA requires a VM with nested virtualisation support:

| Resource | Spec | Approximate cost |
|----------|------|-----------------|
| Azure VM (general purpose, 8 vCPU, 32 GB) | D8ds_v5 | ~$0.38/hr |
| AWS VM (general purpose, 8 vCPU, 32 GB) | m8i.2xlarge | ~$0.46/hr |
| AWS VM (compute-optimized, 8 vCPU, 16 GB) | c8i.2xlarge | ~$0.41/hr |
| AWS VM (legacy bare metal, 96 vCPU) | m5.metal | ~$4.61/hr |

Intel Xeon 6 families (C8i, M8i, R8i) support nested virtualisation on
standard (non-metal) AWS instances since late 2025, reducing AWS costs
by ~10x compared to legacy metal instances.

A single rollout (15 steps) typically completes in 1--3 minutes depending
on action delay and evaluator latency.  At the lower rate that is roughly
$0.006--$0.02 per rollout.

---

## API reference

| Method / Property | Signature | Returns |
|-------------------|-----------|---------|
| `RLEnvironment(adapter, default_task_id)` | constructor | `RLEnvironment` |
| `reset(config=None)` | `ResetConfig \| None -> BenchmarkObservation` | Initial observation |
| `step(action)` | `BenchmarkAction -> RolloutStep` | Post-action step |
| `pixel_action(x, y, action_type, text, key, x_frac, y_frac)` | `... -> RolloutStep` | Convenience step from pixel coords |
| `observe()` | `-> BenchmarkObservation` | Current observation (no side-effects) |
| `evaluate()` | `-> float` | Score in [0.0, 1.0] |
| `collect_rollout(agent_fn, max_steps, stuck_window)` | `Callable -> list[RolloutStep]` | Full episode trajectory |
| `screen_size` | property | `tuple[int, int]` -- (width, height) |

### Data classes

| Class | Key fields |
|-------|-----------|
| `ResetConfig` | `task_id`, `task_setup_only`, `qemu_reboot` |
| `RolloutStep` | `observation`, `action`, `reward`, `done`, `info` |
