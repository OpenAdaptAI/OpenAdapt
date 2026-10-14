#!/usr/bin/env python3
"""Example: collect rollouts from WAA for GRPO training.

Demonstrates the RL environment wrapper by connecting to a WAA server (or
using a mock adapter) and collecting one or more rollouts with a simple
random agent.  The output can be saved to JSON for downstream processing
by an RL training framework.

Usage:
    # Single rollout with mock adapter (no VM required):
    python scripts/run_grpo_rollout.py --mock --task-id mock_notepad_001

    # Single rollout against a live WAA server:
    python scripts/run_grpo_rollout.py --server http://localhost:5001 --task-id <WAA_UUID>

    # Multiple rollouts:
    python scripts/run_grpo_rollout.py --server http://localhost:5001 --task-id <WAA_UUID> -n 5

    # Save rollouts to file:
    python scripts/run_grpo_rollout.py --mock --task-id mock_notepad_001 --output rollouts.json
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import fire

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.adapters.rl_env import RLEnvironment, RolloutStep


def _make_adapter(
    server: str,
    mock: bool,
):
    """Instantiate the appropriate adapter."""
    if mock:
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        return WAAMockAdapter(num_tasks=20, domains=["notepad", "chrome", "settings"])

    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

    return WAALiveAdapter(WAALiveConfig(server_url=server))


def _random_agent(obs: BenchmarkObservation) -> BenchmarkAction:
    """Trivial agent that picks a random pixel coordinate and clicks.

    This exists purely for demonstration -- replace it with a VLM-based
    policy for real training.
    """
    viewport = obs.viewport or (1920, 1200)
    x = random.randint(0, viewport[0] - 1)
    y = random.randint(0, viewport[1] - 1)
    return BenchmarkAction(type="click", x=float(x), y=float(y))


def _rollout_to_dict(rollout: list[RolloutStep]) -> list[dict[str, Any]]:
    """Serialise a rollout to a list of JSON-friendly dicts."""
    steps = []
    for s in rollout:
        steps.append(
            {
                "action_type": s.action.type,
                "action_x": s.action.x,
                "action_y": s.action.y,
                "action_text": s.action.text,
                "action_key": s.action.key,
                "reward": s.reward,
                "done": s.done,
                "screenshot_bytes": len(s.observation.screenshot)
                if s.observation.screenshot
                else 0,
            }
        )
    return steps


def main(
    server: str = "http://localhost:5001",
    task_id: str | None = None,
    n: int = 1,
    max_steps: int = 15,
    stuck_window: int = 3,
    output: str | None = None,
    mock: bool = False,
) -> None:
    """Collect GRPO-style rollouts from the WAA RL environment.

    Args:
        server: WAA server URL (ignored when --mock is set).
        task_id: WAA task ID.  Required for live mode.  For mock mode
            defaults to the first available mock task.
        n: Number of rollouts to collect.
        max_steps: Maximum steps per rollout.
        stuck_window: Consecutive identical screenshots before early stop.
        output: Path to save rollouts as JSON.  Prints to stdout if omitted.
        mock: Use WAAMockAdapter instead of a live server.
    """
    adapter = _make_adapter(server=server, mock=mock)

    if task_id is None:
        tasks = adapter.list_tasks()
        if not tasks:
            raise SystemExit("No tasks available. Provide --task-id explicitly.")
        task_id = tasks[0].task_id
        print(f"Auto-selected task: {task_id}")

    env = RLEnvironment(adapter=adapter, default_task_id=task_id)

    all_rollouts: list[dict[str, Any]] = []

    for i in range(n):
        print(f"\n--- Rollout {i + 1}/{n} ---")
        t0 = time.time()

        env.reset()
        rollout = env.collect_rollout(
            agent_fn=_random_agent,
            max_steps=max_steps,
            stuck_window=stuck_window,
        )

        elapsed = time.time() - t0
        reward = rollout[-1].reward if rollout else 0.0

        print(f"  Steps:  {len(rollout)}")
        print(f"  Reward: {reward:.3f}")
        print(f"  Time:   {elapsed:.1f}s")

        all_rollouts.append(
            {
                "rollout_index": i,
                "task_id": task_id,
                "num_steps": len(rollout),
                "reward": reward,
                "elapsed_seconds": round(elapsed, 2),
                "steps": _rollout_to_dict(rollout),
            }
        )

    # Summary
    rewards = [r["reward"] for r in all_rollouts]
    print(f"\n{'=' * 40}")
    print(f"Collected {n} rollout(s)")
    print(f"  Mean reward: {sum(rewards) / len(rewards):.3f}")
    print(f"  Min  reward: {min(rewards):.3f}")
    print(f"  Max  reward: {max(rewards):.3f}")
    print(f"  Mean steps:  {sum(r['num_steps'] for r in all_rollouts) / n:.1f}")

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(all_rollouts, f, indent=2)
        print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    fire.Fire(main)
