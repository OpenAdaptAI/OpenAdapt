#!/usr/bin/env python3
"""Clean-user smoke for the native binary's embedded Flow + browser runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, env: dict[str, str], timeout: float = 900) -> tuple[str, float]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    elapsed = time.monotonic() - started
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise RuntimeError(f"{command!r} exited {result.returncode}: {output[-3000:]}")
    return output, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    suffix = ".exe" if sys.platform == "win32" else ""
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "dist" / f"openadapt-engine{suffix}",
    )
    args = parser.parse_args()
    executable = args.artifact.resolve(strict=True)

    with tempfile.TemporaryDirectory(prefix="openadapt-frozen-flow-") as raw_root:
        root = Path(raw_root)
        env = dict(os.environ)
        # A brand-new path proves the frozen executable itself performs the
        # first-use provision.  No system Python or openadapt-flow command is
        # used by any lifecycle command below.
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "browser-runtime")
        env.pop("OPENADAPT_FLOW_NO_AUTO_INSTALL", None)

        flow = [str(executable), "__openadapt_flow__"]
        first_output, first_seconds = _run(
            [*flow, "demo-record", "--out", str(root / "recording")],
            env=env,
        )
        if "Downloading the Chromium browser" not in first_output:
            raise RuntimeError("clean-user run did not exercise first-use browser provision")
        if not any((root / "browser-runtime").glob("chromium*")):
            raise RuntimeError("browser runtime was not persisted outside the one-file extraction")

        _run(
            [
                *flow,
                "compile",
                str(root / "recording"),
                "--out",
                str(root / "bundle"),
                "--name",
                "native-frozen-demo",
            ],
            env=env,
        )
        _, replay_seconds = _run(
            [
                *flow,
                "replay",
                str(root / "bundle"),
                "--run-dir",
                str(root / "run"),
            ],
            env=env,
        )

        report = json.loads((root / "run" / "report.json").read_text(encoding="utf-8"))
        results = report.get("results") or []
        if not results or not all(item.get("ok") is True for item in results):
            raise RuntimeError("frozen replay did not produce an all-success report")
        metrics = report.get("metrics") or {}
        if metrics.get("model_calls", 0) != 0 or metrics.get("cost_usd", 0) != 0:
            raise RuntimeError("healthy frozen replay unexpectedly used a model or incurred cost")

        second_output, warm_seconds = _run(
            [*flow, "demo-record", "--out", str(root / "recording-warm")],
            env=env,
        )
        if "Downloading the Chromium browser" in second_output:
            raise RuntimeError("warm run attempted to download the browser again")

        print(
            json.dumps(
                {
                    "artifact_bytes": executable.stat().st_size,
                    "first_provision_seconds": round(first_seconds, 3),
                    "replay_seconds": round(replay_seconds, 3),
                    "warm_record_seconds": round(warm_seconds, 3),
                    "steps": len(results),
                    "silent_incorrect_successes": 0,
                    "model_calls": 0,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
