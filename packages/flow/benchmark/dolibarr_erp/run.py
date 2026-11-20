"""Run policies through the Dolibarr OpenEnv environment and keep every episode.

    python -m benchmark.dolibarr_erp.run --policy scripted --out results/
    python -m benchmark.dolibarr_erp.run --policy noop-success --out results/
    python -m benchmark.dolibarr_erp.run --policy claude --model claude-sonnet-5-5 --k 3 --out results/

Policies:

``scripted``      each task's reference solution (checks that an honest, correct
                  run earns the reward: the grader's false-reject rate)
``noop-success``  do nothing, report success   (trivial floor)
``noop-failure``  do nothing, report failure   (trivial floor)
``claude``        a Claude computer-use agent on screenshots (agents.py)

Episodes go through ``OpenAdaptWebEnvironment``, the class the OpenEnv server
wraps, so the numbers describe the environment a buyer runs. Each episode's
row is written to ``<out>/<policy>/<task>/r<k>.json`` as soon as it ends; a
rerun skips rows that exist. Extra (held-out) tasks load from
``OPENADAPT_ENV_EXTRA_TASKS``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
ENV_DIR = HERE.parents[1] / "environments" / "openenv" / "dolibarr_erp"
LOAD_LIMIT = 35.0


def _wait_for_load() -> None:
    while os.getloadavg()[0] > LOAD_LIMIT:
        print(f"   waiting: host load {os.getloadavg()[0]:.0f} > {LOAD_LIMIT:.0f}", flush=True)
        time.sleep(30)


def scripted(env: Any, task: Any, obs: Any) -> tuple[Any, dict[str, Any]]:
    from openadapt_flow.rl_env.environment import WebAction

    errors = []
    for step in task.solution:
        obs = env.step(WebAction(**step))
        if obs.last_action_error:
            errors.append(f"{step}: {obs.last_action_error}")
        if obs.done:
            break
    return obs, {"errors": errors}


def noop(claim: str) -> Callable[..., Any]:
    def run(env: Any, task: Any, obs: Any) -> tuple[Any, dict[str, Any]]:
        from openadapt_flow.rl_env.environment import WebAction

        return env.step(WebAction(kind="done", claim=claim, message="")), {}

    return run


def episode(env: Any, task: Any, policy: Callable[..., Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    obs = env.reset(task_id=task.id)
    reset_s = time.monotonic() - t0
    t1 = time.monotonic()
    obs, extra = policy(env, task, obs)
    if not obs.done:
        from openadapt_flow.rl_env.environment import WebAction

        obs = env.step(WebAction(kind="done", claim=None))
    meta = obs.metadata or {}
    return {
        "task": task.id,
        "split": task.split,
        "tags": task.tags,
        "reward": obs.reward,
        "verdict": obs.verdict,
        "judgement": meta.get("judgement"),
        "steps": obs.step,
        "reset_s": round(reset_s, 2),
        "episode_s": round(time.monotonic() - t1, 1),
        **extra,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m benchmark.dolibarr_erp.run", allow_abbrev=False)
    ap.add_argument("--policy", required=True, choices=["scripted", "noop-success", "noop-failure", "claude"])
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--k", type=int, default=1, help="runs per task")
    ap.add_argument("--task", action="append", help="only these task ids")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-usd", type=float, default=0.0, help="claude: stop when the ledger passes this")
    a = ap.parse_args(argv)

    from openadapt_flow.rl_env.environment import OpenAdaptWebEnvironment

    env = OpenAdaptWebEnvironment(ENV_DIR, screenshots=a.policy == "claude")
    tasks = [t for t in env.env.tasks if not a.task or t.id in a.task]
    label = a.policy if a.policy != "claude" else f"{a.model}@{a.effort}"
    out = Path(a.out) / label
    if a.policy == "scripted":
        policy: Callable[..., Any] = scripted
    elif a.policy.startswith("noop-"):
        policy = noop(a.policy.split("-", 1)[1])
    else:
        from benchmark.dolibarr_erp import agents

        ledger = Path(a.out) / "spend.json"
        policy = agents.claude_policy(a.model, a.effort, ledger=ledger, max_usd=a.max_usd)
    try:
        for k in range(1, a.k + 1):
            for task in tasks:
                row_path = out / task.id / f"r{k}.json"
                if row_path.exists():
                    continue
                _wait_for_load()
                row_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    row = episode(env, task, policy)
                except Exception as exc:  # noqa: BLE001 - a harness error is its own outcome
                    if type(exc).__name__ == "SpendCapReached":
                        print(f"stopping: {exc}", flush=True)
                        return 2
                    row = {"task": task.id, "split": task.split, "harness_error": f"{type(exc).__name__}: {exc}",
                           "trace": traceback.format_exc()[-2000:]}
                row.update({"policy": label, "rep": k, "load": round(os.getloadavg()[0], 1)})
                row_path.write_text(json.dumps(row, indent=2, default=str) + "\n")
                j = row.get("judgement") or {}
                rec = j.get("record") or {}
                print(f"{label} r{k} {task.id}: reward={row.get('reward')} record_ok={rec.get('record_ok')} "
                      f"claim={j.get('claim')} problems={rec.get('problems') or row.get('harness_error') or row.get('errors')}",
                      flush=True)
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
