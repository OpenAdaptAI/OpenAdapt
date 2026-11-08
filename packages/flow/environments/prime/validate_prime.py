"""Run one environment through the Prime Intellect ``verifiers`` path, model-free.

    python environments/prime/validate_prime.py environments/openenv/mockmed --out prime-mockmed.json

Starts the OpenEnv server and the scripted-model stub, then runs verifiers'
``eval`` twice with the ``openadapt-web-envs`` taskset: once with the
reference claims and once claiming success every time (trusting the screen).
Traces stay local: the run goes through ``openadapt_web_envs.run_eval``,
which never uploads without ``--allow-upload``. The summary is each task's
``openenv_reward``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
sys.path.insert(0, str(FLOW))

from openadapt_flow.rl_env.tasks import load_environment  # noqa: E402
from openadapt_flow.rl_env.validate import _free_port, start_server  # noqa: E402


def _eval(base_url: str, model_url: str, out: Path, n: int) -> dict[str, float]:
    # Through the wrapper, which adds --no-push and the local runtime.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_web_envs.run_eval",
            "--env.taskset.base-url",
            base_url,
            "--model",
            "scripted",
            "--client.base-url",
            model_url,
            "--client.api-key-var",
            "OPENADAPT_STUB_KEY",
            "--env.player.max-turns",
            "25",
            "-n",
            str(n),
            "-c",
            "4",
            "-o",
            str(out),
        ],
        check=True,
        env={**os.environ, "OPENADAPT_STUB_KEY": "stub"},
        stdout=subprocess.DEVNULL,
    )
    rewards: dict[str, float] = {}
    for traces in out.rglob("traces.jsonl"):
        for line in traces.read_text().splitlines():
            row = json.loads(line)
            name = row["task"]["data"]["name"]
            trace = row["traces"][0] if row.get("traces") else {}
            score = trace.get("rewards", {}).get("openenv_reward", {}).get("score")
            rewards[name] = score if row.get("ok") else None
    return rewards


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    env = load_environment(args.env_dir)
    port, stub_port = _free_port(), _free_port()
    server = start_server(env.directory, port)
    procs = [server]
    summary: dict[str, Any] = {"env": env.manifest.name, "tasks": len(env.tasks)}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for label, claim in (("scripted", None), ("screen_trusting", "success")):
                stub = subprocess.Popen(
                    [
                        sys.executable,
                        str(HERE / "scripted_model.py"),
                        str(env.directory),
                        "--port",
                        str(stub_port),
                    ]
                    + (["--claim", claim] if claim else []),
                    stdout=subprocess.DEVNULL,
                )
                procs.append(stub)
                time.sleep(2)
                started = time.monotonic()
                rewards = _eval(
                    f"http://127.0.0.1:{port}",
                    f"http://127.0.0.1:{stub_port}/v1",
                    Path(tmp) / label,
                    len(env.tasks),
                )
                stub.terminate()
                stub.wait()
                done = [r for r in rewards.values() if r is not None]
                summary[label] = {
                    "episodes": len(rewards),
                    "errors": len(rewards) - len(done),
                    "mean_reward": round(sum(done) / len(done), 4) if done else None,
                    "wall_s": round(time.monotonic() - started, 1),
                    "rewards": dict(sorted(rewards.items())),
                }
    finally:
        for proc in procs:
            proc.terminate()
    feasible = sum(t.feasible for t in env.tasks)
    summary["checks"] = {
        "scripted_reaches_max": summary["scripted"]["mean_reward"] == 1.0,
        "screen_trusting_paid_only_on_feasible": summary["screen_trusting"][
            "mean_reward"
        ]
        == round(feasible / len(env.tasks), 4),
        "no_errors": summary["scripted"]["errors"]
        == summary["screen_trusting"]["errors"]
        == 0,
    }
    summary["passed"] = all(summary["checks"].values())
    summary["transport"] = (
        "verifiers v1 eval -> openadapt-web-envs taskset -> OpenEnv WebSocket"
    )
    summary["model_calls"] = 0
    args.out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "scripted"}, indent=2)[
            :2000
        ]
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
