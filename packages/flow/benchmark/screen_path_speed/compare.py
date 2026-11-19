"""Interleaved before/after timing of the OpenEMR referral on the screen path.

Each arm is an engine checkout (``--arm NAME=PATH[:ENV=VALUE,...]``). Blocks
alternate the arm order; every block is one ``run.py`` process per arm that
resets the fixture, seeds the existing chart, and runs ``--per-block``
referrals from the same offset, so arms see the same referrals on the same
fixture and machine. Rows go to one JSONL file.

    python -m benchmark.screen_path_speed.compare --bundle B --out O \\
        --arm before=/path/a --arm after=/path/b \\
        --arm after-quiet50=/path/b:OPENADAPT_FLOW_PAGE_QUIET_MS=50 --blocks 4
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_arm(spec: str) -> tuple[str, Path, dict[str, str]]:
    name, _, rest = spec.partition("=")
    path, _, env_spec = rest.partition(":")
    env = dict(kv.split("=", 1) for kv in env_spec.split(",") if kv)
    return name, Path(path), env


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--arm", action="append", required=True)
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--per-block", type=int, default=4)
    a = ap.parse_args()
    arms = [parse_arm(s) for s in a.arm]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "rows.jsonl"
    for b in range(a.blocks):
        shift = b % len(arms)
        order = arms[shift:] + arms[:shift]
        for name, root, extra in order:
            env = dict(os.environ)
            env.update(extra)
            env["PYTHONPATH"] = str(root / "packages" / "flow")
            cmd = [
                sys.executable,
                "-m",
                "benchmark.screen_path_speed.run",
                "--bundle",
                a.bundle,
                "--out",
                str(out / "items"),
                "--results",
                str(results),
                "--arm",
                name,
                "--block",
                str(b + 1),
                "--per-block",
                str(a.per_block),
                "--start",
                str(b * a.per_block),
            ]
            print(f"block {b + 1} arm {name}", flush=True)
            # Run from the arm's own checkout: ``python -m`` puts the working
            # directory ahead of PYTHONPATH, so running from another checkout
            # would import that checkout's engine.
            subprocess.run(
                cmd, env=env, cwd=str(root / "packages" / "flow"), check=False
            )


if __name__ == "__main__":
    main()
