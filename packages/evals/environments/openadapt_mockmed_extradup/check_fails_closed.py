"""Run vf-eval against the scripted policy and check the reward fails closed.

Starts the scripted policy on a free port, runs ``vf-eval`` once per case,
reads each run's ``metadata.json``, and exits non-zero unless the gold
write averaged 1.0 and every hacking case averaged 0.0. This is the check
the ``prime-env`` CI job runs; it needs ``verifiers`` and this environment
installed in the current interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import openadapt_mockmed_extradup as env_module
import scripted_policy

EXPECTED = {case: (1.0 if case == "control" else 0.0) for case in env_module.ALL_CASES}


def run_case(case: str, base_url: str, output_dir: Path, num_examples: int) -> float:
    vf_eval = shutil.which("vf-eval") or str(Path(sys.executable).with_name("vf-eval"))
    command = [
        vf_eval,
        env_module.ENV_ID,
        "-m",
        f"scripted/{case}",
        "-b",
        base_url,
        "-k",
        "SCRIPTED_POLICY_KEY",
        "-n",
        str(num_examples),
        "-r",
        "1",
        "-a",
        json.dumps({"envs": ["mockmed", "openemr"], "num_tasks": num_examples}),
        "--save-results",
        "-o",
        str(output_dir),
        "--disable-tui",
    ]
    env = {**os.environ, "SCRIPTED_POLICY_KEY": "scripted"}
    subprocess.run(command, check=True, env=env, cwd=output_dir)
    runs = sorted(
        output_dir.glob(f"evals/{env_module.ENV_ID}--scripted--{case}/*/metadata.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not runs:
        raise RuntimeError(f"vf-eval wrote no metadata.json for {case}")
    return float(json.loads(runs[-1].read_text(encoding="utf-8"))["avg_reward"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("prime-eval"))
    parser.add_argument("--num-examples", type=int, default=2)
    parser.add_argument("--cases", nargs="*", default=list(env_module.ALL_CASES))
    args = parser.parse_args(argv)
    # vf-eval runs with the output directory as its cwd and resolves -o
    # against that, so a relative path would nest itself.
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    server = scripted_policy.serve("127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    failures: list[str] = []
    try:
        for case in args.cases:
            average = run_case(case, base_url, args.output_dir, args.num_examples)
            expected = EXPECTED[case]
            mark = "ok" if average == expected else "MISS"
            print(f"{mark}\t{case}\tavg_reward={average}\texpected={expected}", flush=True)
            if average != expected:
                failures.append(case)
    finally:
        server.shutdown()
    if failures:
        print(f"reward did not fail closed on: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"fails closed: {len(args.cases)} cases, {args.num_examples} examples each")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
