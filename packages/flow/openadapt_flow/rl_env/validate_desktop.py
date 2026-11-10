"""Validate a Windows desktop environment on a real guest.

    OPENADAPT_ENV_REFERENCE=1 python -m openadapt_flow.rl_env.validate_desktop \\
        environments/openenv/win-patient-chart --out results.json

Runs every task in-process against the configured guest with model-free
agents, the same contract as :mod:`openadapt_flow.rl_env.validate`:

* ``scripted``: the task's reference solution; every task should earn 1.
* ``screen_trusting``: the same actions, always claiming success, as an
  agent that believes the "saved" banner; it should earn 1 exactly on the
  feasible tasks. Its actions are the reference's and the record's verdict
  does not depend on the claim, so its row is derived from the reference
  episode (``derived_from``) rather than run again.
* ``planted_fault``: each feasible task's reference solution again, with a
  write fault planted at reset, claiming success; the record must refute
  every one (reward 0). This is the check that each task can fail.
* ``random``: seeded random clicks and keys and a random claim (the floor).

Two more checks run beside them:

* state leaks: after a writing episode, the next reset must restore a
  database whose digest equals the pristine seed's, show an empty note field,
  and run one app process;
* reset time per episode (stop app, restore database, start app, ready).

Needs ``OPENADAPT_ENV_REFERENCE=1`` (the reference solutions name elements by
automation id, which the pixels-only surface otherwise refuses).
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.rl_env.desktop import (
    REFERENCE_ENV,
    DesktopAction,
    OpenAdaptDesktopEnvironment,
    expected_digest,
)
from openadapt_flow.rl_env.tasks import Task

AGENTS = ("scripted", "screen_trusting", "planted_fault", "random")
PLANTED = ("drop", "wrong_row", "duplicate", "truncate", "stale")
FLOOR_MARGIN = 0.1


def _random_actions(rng: random.Random, width: int, height: int, steps: int = 10):
    for _ in range(steps):
        kind = rng.choice(["click", "click", "click", "type", "press"])
        if kind == "click":
            yield {
                "kind": "click",
                "x": rng.randrange(width),
                "y": rng.randrange(height),
            }
        elif kind == "type":
            yield {"kind": "type", "text": "".join(rng.choices("abcdefgh 0123", k=8))}
        else:
            yield {"kind": "press", "text": rng.choice(["enter", "tab", "escape"])}
    yield {"kind": "done", "claim": rng.choice(["success", "failure"])}


def run_episode(
    env: OpenAdaptDesktopEnvironment,
    task: Task,
    agent: str,
    seed: int = 0,
    *,
    fault: Optional[str] = None,
) -> dict[str, Any]:
    started = time.monotonic()
    obs = env.reset(
        task_id=task.id, episode_id=f"{agent}-{task.id}-{seed}", fault=fault
    )
    reset_s = obs.metadata.get("reset_s")
    if agent == "random":
        actions = list(
            _random_actions(random.Random(seed), obs.width or 1280, obs.height or 800)
        )
    else:
        actions = list(task.solution)
        if agent in ("screen_trusting", "planted_fault"):
            actions[-1] = {"kind": "done", "claim": "success"}
    steps = errors = 0
    for raw in actions:
        obs = env.step(DesktopAction(**raw))
        steps += 1
        errors += bool(obs.last_action_error)
        if obs.done:
            break
    judgement = obs.metadata.get("judgement", {})
    feasible = task.feasible and fault in (None, "ok")
    return {
        "task_id": task.id,
        "agent": agent,
        "seed": seed,
        "fault": fault or task.fault,
        "feasible": feasible,
        "steps": steps,
        "action_errors": errors,
        "reward": obs.reward,
        "verdict": obs.verdict or judgement.get("verdict"),
        "claim": judgement.get("claim"),
        "silent_wrong_effect": judgement.get("silent_wrong_effect"),
        "judge_error": judgement.get("error"),
        "reset_s": reset_s,
        "seconds": round(time.monotonic() - started, 2),
    }


def derive_trusting(ref: dict[str, Any]) -> dict[str, Any]:
    """The screen-trusting row from the reference episode of the same task."""
    from openadapt_flow.rl_env.reward import episode_reward

    verdict = ref["verdict"]
    reward = episode_reward(verdict, "success", ref["feasible"]) if verdict else 0.0
    return {
        **ref,
        "agent": "screen_trusting",
        "claim": "success",
        "reward": reward,
        "silent_wrong_effect": verdict == "REFUTED",
        "derived_from": "scripted",
        "reset_s": None,
    }


def leak_check(
    env: OpenAdaptDesktopEnvironment, writer: Task, reader: Task
) -> dict[str, Any]:
    """Write in one episode (duplicated), then check the next starts clean."""
    run_episode(env, writer, "scripted", fault="duplicate")
    after_write = env._guest.digest(env.spec.guest_app)  # noqa: SLF001 - the check's point
    env.reset(task_id=reader.id, episode_id=f"leak-{reader.id}")
    app = env.spec.guest_app
    variant = str(reader.setup.get("variant", "none"))
    restored = env._guest.digest(app)  # noqa: SLF001
    pristine = expected_digest(app, variant)
    baseline_rows = len(env._guest.dump(app)["records"])  # noqa: SLF001
    pristine_rows = None
    note_text = None
    try:
        from openadapt_flow.ir import StructuralLocator

        read = env._uia.read_structural(StructuralLocator(automation_id="noteBox"))  # noqa: SLF001
        note_text = (read or {}).get("text")
    except Exception:  # noqa: BLE001
        pass
    import os
    import subprocess
    import sys
    import tempfile

    from openadapt_flow.rl_env.desktop import PACK_DB

    with tempfile.TemporaryDirectory() as tmp:
        environ = {**os.environ, "OA_PACK_DIR": tmp}
        subprocess.run(
            [sys.executable, str(PACK_DB), "seed", app, "--variant", variant],
            env=environ,
            check=True,
            capture_output=True,
        )
        dumped = subprocess.run(
            [sys.executable, str(PACK_DB), "dump", app],
            env=environ,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        pristine_rows = len(json.loads(dumped)["records"])
    processes = env._guest.app_processes()  # noqa: SLF001
    passed = (
        after_write != pristine
        and restored == pristine
        and baseline_rows == pristine_rows
        and not (note_text or "").strip()
        and processes == 1
    )
    return {
        "writer": writer.id,
        "reader": reader.id,
        "digest_after_write_differs": after_write != pristine,
        "restored_digest_is_pristine": restored == pristine,
        "records_after_reset": baseline_rows,
        "records_in_pristine_seed": pristine_rows,
        "note_field_after_reset": note_text,
        "app_processes": processes,
        "passed": passed,
    }


def _mean(rows: list[dict[str, Any]]) -> float:
    return round(sum(r["reward"] or 0.0 for r in rows) / len(rows), 4) if rows else 0.0


def summarize(
    tasks: list[Task], rows: list[dict[str, Any]], leaks: list[dict]
) -> dict[str, Any]:
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
            "action_errors": sum(r["action_errors"] for r in mine),
        }
    feasible = sum(t.feasible for t in tasks)
    resets = [r["reset_s"] for r in rows if r["reset_s"] is not None]
    checks: dict[str, bool] = {
        "no_judge_errors": all(a["judge_errors"] == 0 for a in by_agent.values()),
    }
    if "scripted" in by_agent:
        checks["every_task_passes_with_its_solution"] = (
            by_agent["scripted"]["mean_reward"] == 1.0
        )
    if "screen_trusting" in by_agent:
        checks["screen_trusting_paid_only_on_feasible"] = by_agent["screen_trusting"][
            "mean_reward"
        ] == round(feasible / len(tasks), 4)
    if "planted_fault" in by_agent:
        planted = [r for r in rows if r["agent"] == "planted_fault"]
        checks["every_feasible_task_fails_with_a_planted_fault"] = all(
            r["verdict"] == "REFUTED" and r["reward"] == 0.0 for r in planted
        )
    if "random" in by_agent:
        checks["random_floor_margin"] = (
            round(1.0 - by_agent["random"]["mean_reward"], 4) >= FLOOR_MARGIN
        )
    if leaks:
        checks["no_state_leaks"] = all(leak["passed"] for leak in leaks)
    return {
        "tasks": len(tasks),
        "feasible_tasks": feasible,
        "agents": by_agent,
        "reset_s": {
            "episodes": len(resets),
            "median": round(statistics.median(resets), 2) if resets else None,
            "p95": round(sorted(resets)[int(0.95 * (len(resets) - 1))], 2)
            if resets
            else None,
            "max": max(resets) if resets else None,
        },
        "leak_checks": leaks,
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate(
    env_dir: Path,
    *,
    agents: tuple[str, ...] = AGENTS,
    only: Optional[list[str]] = None,
    random_seeds: int = 1,
    leak_checks: int = 2,
    out: Optional[Path] = None,
) -> dict[str, Any]:
    import os

    if os.environ.get(REFERENCE_ENV) != "1":
        raise SystemExit(f"set {REFERENCE_ENV}=1 for a reference run")
    env = OpenAdaptDesktopEnvironment(env_dir)
    tasks = [t for t in env.env.tasks if not only or t.id in only]
    rows: list[dict[str, Any]] = []
    leaks: list[dict] = []
    started = time.time()

    def save() -> None:
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps({"partial": True, "episodes": rows}, indent=1) + "\n"
            )

    try:
        for i, task in enumerate(tasks):
            for agent in agents:
                if agent == "planted_fault":
                    if not task.feasible:
                        continue
                    rows.append(
                        run_episode(env, task, agent, fault=PLANTED[i % len(PLANTED)])
                    )
                elif agent == "screen_trusting" and "scripted" in agents:
                    # Same actions as the reference; the record's verdict does
                    # not depend on the claim, so only the claim changes.
                    ref = next(
                        r
                        for r in rows
                        if r["task_id"] == task.id and r["agent"] == "scripted"
                    )
                    rows.append(derive_trusting(ref))
                elif agent == "random":
                    for seed in range(random_seeds):
                        rows.append(run_episode(env, task, agent, seed))
                else:
                    rows.append(run_episode(env, task, agent))
            print(
                f"  {task.id}: "
                + " ".join(
                    f"{r['agent']}={r['reward']}({r['verdict']},{r['reset_s']}s)"
                    for r in rows
                    if r["task_id"] == task.id
                ),
                flush=True,
            )
            save()
        feasible = [t for t in tasks if t.feasible]
        for k in range(min(leak_checks, max(len(feasible) - 1, 0))):
            leaks.append(leak_check(env, feasible[k], feasible[k + 1]))
    finally:
        env.close()
    summary = summarize(tasks, rows, leaks)
    summary["env"] = env.env.manifest.name
    summary["surface"] = env.surface
    summary["run"] = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "wall_s": round(time.time() - started, 1),
        "server_platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "model_calls": 0,
        # "pack" keeps whatever else the desktop runs between episodes (a
        # shared desktop); "account" stops every process of the account.
        "reset_scope": getattr(env._guest, "reset_scope", None),  # noqa: SLF001
    }
    result = {"summary": summary, "episodes": rows}
    if out is not None:
        out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("--agents", default=",".join(AGENTS))
    parser.add_argument("--only", action="append")
    parser.add_argument("--random-seeds", type=int, default=1)
    parser.add_argument("--leak-checks", type=int, default=2)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = validate(
        args.env_dir,
        agents=tuple(a for a in args.agents.split(",") if a),
        only=args.only,
        random_seeds=args.random_seeds,
        leak_checks=args.leak_checks,
        out=args.out,
    )
    print(
        json.dumps(
            {k: v for k, v in result["summary"].items() if k != "leak_checks"}, indent=2
        )
    )
    return 0 if result["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
