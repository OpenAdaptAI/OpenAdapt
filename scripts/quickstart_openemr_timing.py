"""Time ``openadapt quickstart --app openemr``, cold and warm.

Cold: the quickstart's OpenEMR fixture is removed first (its Compose project
with its volumes, and its state folder), so the run sets OpenEMR up again.
The pinned images stay: the script never deletes images, which other copies of
the fixture on the computer may use. Whether they were present is recorded.
Warm: the fixture from the previous run is still running and is reused.

Each run is headless and not on a terminal (plain output). The script records
the wall time, the exit code, the machine's load average before and after,
and the times the output itself reports (setup, Act 2, Act 3, total).

    python scripts/quickstart_openemr_timing.py --cold 1 --warm 3 --out timing.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUN_TIMEOUT_S = 1800


def _launcher() -> str:
    found = Path(sys.executable).parent / "openadapt"
    return str(found) if found.exists() else (shutil.which("openadapt") or "openadapt")


def _images_present() -> dict[str, bool]:
    lock = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "packages/flow/benchmark/openemr_local/environment.lock.json"
        ).read_text()
    )
    return {
        name: subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        ).returncode
        == 0
        for name, image in lock["services"].items()
    }


def _discard_fixture() -> None:
    """The driver's own discard: this project's containers, volumes and state."""

    flow = Path(__file__).resolve().parents[1] / "packages" / "flow"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from benchmark.openemr_local import quickstart as q; "
            "q.make_fixture().discard()",
        ],
        cwd=flow,
        env={**os.environ, "PYTHONPATH": str(flow)},
        check=True,
        timeout=300,
    )


def _reported(output: str) -> dict[str, float | str | None]:
    def number(pattern: str) -> float | None:
        match = re.search(pattern, output, re.MULTILINE)
        return float(match.group(1)) if match else None

    how = re.search(r"OpenEMR 8\.0\.0\.3 at \S+: (.+?) [\d.]+s$", output, re.MULTILINE)
    return {
        "setup_s": number(r"OpenEMR 8\.0\.0\.3 at \S+: .+? ([\d.]+)s$"),
        "setup": how.group(1) if how else None,
        "act2_s": number(r"saved and checked · .* · ([\d.]+)s$"),
        "act3_s": number(r"OpenAdapt: +■ .*  ([\d.]+)s$"),
        "total_reported_s": number(r"Cost .* · ([\d.]+)s in all$"),
        "checked": (
            re.search(r"(\d+ of \d+) saved and checked ·", output) or [None, None]
        )[1],
        "act3": (
            re.search(r"OpenAdapt: +(.+?)  [\d.]+s$", output, re.MULTILINE)
            or [None, None]
        )[1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cold", type=int, default=1, help="cold runs (fixture removed first)"
    )
    parser.add_argument(
        "--warm", type=int, default=3, help="warm runs after the cold ones"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    launcher = _launcher()
    rows = []
    plan = ["cold"] * args.cold + ["warm"] * args.warm
    with tempfile.TemporaryDirectory() as work:
        env = {
            **os.environ,
            "OPENADAPT_SEAL_HOME": str(Path(work) / "seal"),
            "OPENADAPT_FLOW_SCRUB": "off",
            "BROWSER": "true",
        }
        for index, kind in enumerate(plan):
            if kind == "cold":
                _discard_fixture()
            images = _images_present()
            out = Path(work) / f"{kind}-{index}"
            load_before = os.getloadavg()
            started = time.monotonic()
            try:
                result = subprocess.run(
                    [
                        launcher,
                        "quickstart",
                        "--app",
                        "openemr",
                        "--headless",
                        "--out",
                        str(out),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    timeout=RUN_TIMEOUT_S,
                    check=False,
                )
                code, output = result.returncode, result.stdout + result.stderr
            except subprocess.TimeoutExpired as exc:
                code = "timeout"
                output = (
                    (exc.stdout or b"").decode()
                    if isinstance(exc.stdout, bytes)
                    else (exc.stdout or "")
                )
            wall = time.monotonic() - started
            row = {
                "kind": kind,
                "wall_s": round(wall, 1),
                "exit": code,
                "images_present_before": images,
                "load_before": [round(v, 1) for v in load_before],
                "load_after": [round(v, 1) for v in os.getloadavg()],
                **_reported(output),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
            (args.out.parent / f"{args.out.stem}-{kind}-{index}.log").write_text(output)
    args.out.write_text(
        json.dumps(
            {
                "command": "openadapt quickstart --app openemr --headless --out DIR",
                "machine": {
                    "platform": platform.platform(),
                    "cpus": os.cpu_count(),
                    "python": platform.python_version(),
                },
                "date": time.strftime("%Y-%m-%d"),
                "runs": rows,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
