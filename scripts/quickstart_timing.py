"""Time the quickstart against the tutorial it replaced, interleaved.

The quickstart used to run the engine's tutorial with ``--break-it``: one
checked run and one run with the save set to fail. It now runs the program
five times and then the failing save. This script runs the two commands in
turn, ``--rounds`` times each, headless and not on a terminal, and writes
each wall time with the machine's load average, so a busy machine slows both
sides alike.

    python scripts/quickstart_timing.py --rounds 3 --out timing.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COMMANDS = {
    "before": ["flow", "tutorial", "--break-it", "--out"],
    "after": ["quickstart", "--headless", "--out"],
}


def _launcher() -> str:
    found = Path(sys.executable).parent / "openadapt"
    return str(found) if found.exists() else (shutil.which("openadapt") or "openadapt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    launcher = _launcher()
    rows = []
    with tempfile.TemporaryDirectory() as work:
        env = {
            **os.environ,
            "OPENADAPT_SEAL_HOME": str(Path(work) / "seal"),
            "BROWSER": "true",
        }
        for round_index in range(args.rounds):
            for name, argv in COMMANDS.items():
                out = Path(work) / f"{name}-{round_index}"
                started = time.monotonic()
                result = subprocess.run(
                    [launcher, *argv, str(out)],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                seconds = time.monotonic() - started
                rows.append(
                    {
                        "command": name,
                        "round": round_index,
                        "exit_code": result.returncode,
                        "wall_s": round(seconds, 2),
                        "load_1m": round(os.getloadavg()[0], 1),
                        "lines": len(result.stdout.splitlines()),
                    }
                )
                print(json.dumps(rows[-1]), flush=True)
    summary = {
        name: sorted(row["wall_s"] for row in rows if row["command"] == name)
        for name in COMMANDS
    }
    document = {
        "machine": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "commands": {name: " ".join(argv) for name, argv in COMMANDS.items()},
        "runs": rows,
        "median_wall_s": {
            name: values[len(values) // 2] for name, values in summary.items()
        },
    }
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document["median_wall_s"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
