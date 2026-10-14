# AReaL Evaluation: Recommended Training Backend

> Date: 2026-03-18
> Verdict: Adopt AReaL, phase out TRL rollout_func

---

## Summary

AReaL (inclusionAI/Ant Group) is the same org that builds UI-Venus. v1.0.2,
4.8K stars, actively maintained. Provides everything TRL does plus async
rollouts, agent workflows, and first-class VLM support.

## Why adopt

1. **AgentWorkflow** maps directly to WAADesktopEnv: multi-turn loop,
   external environment, return reward. Tau2 example proves this works.
2. **VLM training is first-class**: Qwen2.5-VL, Qwen3-VL via FSDP.
3. **Async rollouts**: Desktop environments take 30-120s per rollout.
   Synchronous training wastes GPU time. AReaL overlaps rollouts with
   training (2.77x speedup).
4. **Same org as UI-Venus**: Maximum compatibility for training grounder models.
5. **Stable API**: v1.0.2 vs TRL's experimental rollout_func.
6. **Dense rewards work**: Per-completion rewards via dict return.

## Integration path (~100 lines)

Write a WAADesktopWorkflow:
```python
class WAADesktopWorkflow:
    async def run(self, data, **extra_kwargs):
        client = AsyncOpenAI(base_url=extra_kwargs["base_url"])
        env = WAADesktopEnv(task_id=data["task_id"])
        obs = env.reset()
        for step in range(max_steps):
            response = await client.chat.completions.create(...)
            action = parse_action(response)
            obs, reward, done, info = env.step(action)
            if done: break
        return reward
```

AReaL handles token tracking, logprobs, gradient computation transparently.

## vs TRL

| | TRL | AReaL |
|---|---|---|
| rollout_func stability | Experimental | Stable (v1.0) |
| VLM support | Manual | First-class |
| Async rollouts | No | Yes (2.77x speedup) |
| Agent workflows | None | Built-in (proxy mode) |
| Algorithms | GRPO only | GRPO, PPO, DAPO, 9+ more |
| Single GPU | Yes | Yes (d1p1t1 mode) |

## Adoption plan

Phase 1: Write WAADesktopWorkflow, test on single A10G with Qwen2.5-VL-3B
Phase 2: Validate dense milestone rewards via per-completion dict
Phase 3: Scale to 4x A10G (g5.12xlarge)
Phase 4: Remove TRL dependency
