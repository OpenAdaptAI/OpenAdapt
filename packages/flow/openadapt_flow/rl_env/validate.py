"""Run an environment end to end over the OpenEnv protocol.

    python -m openadapt_flow.rl_env.validate path/to/env --out results.json
    python -m openadapt_flow.rl_env.validate --base-url http://127.0.0.1:8000 ...

Starts the environment's server (unless ``--base-url`` names a running one),
connects with OpenEnv's own ``GenericEnvClient`` over WebSocket, and runs
every task with four model-free agents:

* ``scripted``: the task's reference solution. Every task should reach 1.
* ``screen_trusting``: the same actions, but always claims success, as an
  agent that believes the "saved" banner does. It should earn 1 exactly on
  the feasible tasks; on every other task the record refutes the claim.
* ``lazy_failure``: reports failure at once without acting. It must earn 0,
  or not trying would pay on every infeasible task.
* ``random``: seeded random actions and a random claim, for the floor.

The summary checks OpenEnv's own reward contract: the scripted mean reaches
the maximum and the random mean sits at least ``floor_margin`` below it.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from openadapt_flow.rl_env import lifecycle
from openadapt_flow.rl_env.agents import RandomAgent, scripted
from openadapt_flow.rl_env.tasks import EnvDirectory, Task, load_environment

FLOOR_MARGIN = 0.1
AGENTS = ("scripted", "screen_trusting", "lazy_failure", "random")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(env_dir: Path, port: int) -> subprocess.Popen[bytes]:
    """Start a server in its own process group; stop it with ``lifecycle.stop``.

    Its log goes to a temporary file, not a pipe nobody reads, so a long run
    cannot fill the pipe and block the server.
    """
    log = tempfile.TemporaryFile()
    proc = lifecycle.spawn(
        [
            sys.executable,
            "-m",
            "openadapt_flow.rl_env.server",
            str(env_dir),
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited with {proc.returncode}")
            try:
                if (
                    httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code
                    == 200
                ):
                    log.close()  # the server holds its own copy of the file
                    return proc
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("server did not become healthy in 60 s")
    except RuntimeError as exc:
        lifecycle.stop(proc)
        log.seek(0)
        tail = log.read()[-4000:].decode(errors="replace")
        log.close()
        raise RuntimeError(f"{exc}\n{tail}") from None
    except BaseException:
        lifecycle.stop(proc)
        log.close()
        raise


def run_episode(client: Any, task: Task, agent: str, seed: int) -> dict[str, Any]:
    started = time.monotonic()
    result = client.reset(task_id=task.id, episode_id=f"{agent}-{task.id}-{seed}")
    obs = result.observation
    steps = 0
    errors = 0
    if agent == "lazy_failure":
        result = client.step({"kind": "done", "claim": "failure"})
        obs = result.observation
        steps = 1
    elif agent == "random":
        policy = RandomAgent(seed)
        while not result.done:
            result = client.step(policy.act(obs, steps))
            obs = result.observation
            steps += 1
            errors += bool(obs.get("last_action_error"))
    else:
        for action in scripted(task):
            if agent == "screen_trusting" and action["kind"] == "done":
                action = {"kind": "done", "claim": "success"}
            result = client.step(action)
            obs = result.observation
            steps += 1
            errors += bool(obs.get("last_action_error"))
            if result.done:
                break
    meta = result.metadata or obs.get("metadata") or {}
    judgement = meta.get("judgement", {})
    return {
        "task_id": task.id,
        "fault": task.fault,
        "feasible": task.feasible,
        "agent": agent,
        "seed": seed,
        "steps": steps,
        "action_errors": errors,
        "reward": result.reward,
        "done": result.done,
        "verdict": obs.get("verdict") or judgement.get("verdict"),
        "claim": judgement.get("claim"),
        "silent_wrong_effect": judgement.get("silent_wrong_effect"),
        "judge_error": judgement.get("error"),
        "seconds": round(time.monotonic() - started, 2),
    }


def _mean(rows: list[dict[str, Any]]) -> float:
    return round(sum(r["reward"] or 0.0 for r in rows) / len(rows), 4) if rows else 0.0


def summarize(env: EnvDirectory, rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_agent: dict[str, Any] = {}
    for agent in AGENTS:
        mine = [r for r in rows if r["agent"] == agent]
        if not mine:
            continue
        by_agent[agent] = {
            "episodes": len(mine),
            "mean_reward": _mean(mine),
            "verdicts": {
                v: sum(r["verdict"] == v for r in mine)
                for v in ("CONFIRMED", "REFUTED", "INDETERMINATE")
            },
            "silent_wrong_effects": sum(bool(r["silent_wrong_effect"]) for r in mine),
            "judge_errors": sum(bool(r["judge_error"]) for r in mine),
        }
    feasible = sum(t.feasible for t in env.tasks)
    expected_trusting = round(feasible / len(env.tasks), 4) if env.tasks else 0.0
    checks = {"no_judge_errors": all(a["judge_errors"] == 0 for a in by_agent.values())}
    if "scripted" in by_agent:
        checks["scripted_reaches_max"] = by_agent["scripted"]["mean_reward"] == 1.0
    if "random" in by_agent:
        floor = by_agent["random"]["mean_reward"]
        checks["random_floor_margin"] = round(1.0 - floor, 4) >= FLOOR_MARGIN
    if "lazy_failure" in by_agent:
        checks["lazy_failure_earns_nothing"] = (
            by_agent["lazy_failure"]["mean_reward"] == 0.0
        )
    if "screen_trusting" in by_agent:
        checks["screen_trusting_paid_only_on_feasible"] = (
            by_agent["screen_trusting"]["mean_reward"] == expected_trusting
        )
    return {
        "env": env.manifest.name,
        "licence": env.manifest.licence.model_dump(),
        "tasks": len(env.tasks),
        "feasible_tasks": feasible,
        "agents": by_agent,
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate(
    env_dir: Path,
    *,
    base_url: Optional[str] = None,
    random_seeds: int = 2,
    only: Optional[list[str]] = None,
    agents: tuple[str, ...] = AGENTS,
    split: Optional[str] = None,
) -> dict[str, Any]:
    from openenv import GenericEnvClient

    env = load_environment(env_dir)
    tasks = [
        t
        for t in env.tasks
        if (not only or t.id in only) and (split is None or t.split == split)
    ]
    proc = None
    if base_url is None:
        port = _free_port()
        proc = start_server(env.directory, port)
        base_url = f"http://127.0.0.1:{port}"
    rows: list[dict[str, Any]] = []
    started = time.time()
    try:
        with GenericEnvClient(
            base_url=base_url, message_timeout_s=180
        ).sync() as client:
            for task in tasks:
                for agent in agents:
                    seeds = range(random_seeds) if agent == "random" else (0,)
                    for seed in seeds:
                        rows.append(run_episode(client, task, agent, seed))
                print(
                    f"  {task.id}: "
                    + " ".join(
                        f"{r['agent']}={r['reward']}"
                        for r in rows
                        if r["task_id"] == task.id
                    ),
                    flush=True,
                )
    finally:
        if proc is not None:
            lifecycle.stop(proc)
    summary = summarize(env.model_copy(update={"tasks": tasks}), rows)
    summary["pack_sha256"] = env.pack_sha256
    summary["run"] = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "wall_s": round(time.time() - started, 1),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "transport": "openenv GenericEnvClient over WebSocket",
        "base_url_kind": "started locally" if proc is not None else "given",
        "model_calls": 0,
    }
    return {"summary": summary, "episodes": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--random-seeds", type=int, default=2)
    parser.add_argument("--only", action="append")
    parser.add_argument("--agents", default=",".join(AGENTS))
    parser.add_argument("--split", help="only this split's tasks")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = validate(
        args.env_dir,
        base_url=args.base_url,
        random_seeds=args.random_seeds,
        only=args.only,
        agents=tuple(a for a in args.agents.split(",") if a),
        split=args.split,
    )
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0 if result["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
