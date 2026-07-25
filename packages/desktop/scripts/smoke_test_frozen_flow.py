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
        env["OPENADAPT_VISION_RUNTIME_ROOT"] = str(root / "vision-runtime")
        env.pop("OPENADAPT_FLOW_NO_AUTO_INSTALL", None)

        flow = [str(executable), "__openadapt_flow__"]
        first_output, first_seconds = _run(
            [*flow, "demo-record", "--out", str(root / "recording")],
            env=env,
        )
        if "local vision runtime is ready" not in first_output:
            raise RuntimeError("clean-user run did not exercise first-use vision provision")
        if not any((root / "vision-runtime").glob("*/*/.complete.json")):
            raise RuntimeError("vision runtime was not persisted outside the one-file extraction")
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
        if report.get("execution_outcome") != "COMPLETED_UNVERIFIED":
            raise RuntimeError(
                "Demo replay did not preserve the precise COMPLETED_UNVERIFIED outcome"
            )
        if (
            report.get("execution_profile") != "demo"
            or report.get("production_eligible") is not False
            or report.get("execution_completed") is not True
        ):
            raise RuntimeError(
                "Demo replay did not remain completed and explicitly non-production"
            )
        envelope = report.get("outcome_envelope")
        if not isinstance(envelope, dict) or (
            envelope.get("version") != "openadapt.execution-outcome/v1"
            or envelope.get("outcome") != report.get("execution_outcome")
            or envelope.get("profile") != report.get("execution_profile")
            or envelope.get("production_eligible") != report.get("production_eligible")
            or envelope.get("execution_completed") != report.get("execution_completed")
        ):
            raise RuntimeError("frozen replay did not emit a bound v1 outcome envelope")
        for contract_counts in ("required_contracts", "passed_contracts"):
            counts = envelope.get(contract_counts)
            if not isinstance(counts, dict) or set(counts) != {
                "authorization",
                "identity",
                "postcondition",
                "effect",
            }:
                raise RuntimeError(
                    f"frozen replay emitted invalid {contract_counts} evidence counts"
                )
        if any(
            envelope["passed_contracts"][contract]
            > envelope["required_contracts"][contract]
            for contract in envelope["required_contracts"]
        ):
            raise RuntimeError("frozen replay reported more passed than required contracts")
        metrics = report.get("metrics") or {}
        if (
            report.get("model_calls") != 0
            or envelope.get("model_calls") != 0
            or metrics.get("model_calls", 0) != 0
            or metrics.get("cost_usd", 0) != 0
        ):
            raise RuntimeError("healthy frozen replay unexpectedly used a model or incurred cost")
        network_observation = report.get("external_network_calls")
        if (
            network_observation not in {"none", "observed", "unknown"}
            or envelope.get("external_network_calls") != network_observation
        ):
            raise RuntimeError(
                "frozen replay did not bind its external-network observation to the outcome"
            )
        from engine.flow_bridge import FlowBridge

        if FlowBridge.classify_outcome(0, report) != "COMPLETED_UNVERIFIED":
            raise RuntimeError(
                "Desktop rejected the precise outcome emitted by its frozen Flow runtime"
            )

        second_output, warm_seconds = _run(
            [*flow, "demo-record", "--out", str(root / "recording-warm")],
            env=env,
        )
        if "Downloading the Chromium browser" in second_output:
            raise RuntimeError("warm run attempted to download the browser again")
        if any(
            marker in second_output
            for marker in (
                "preparing the separately licensed local vision runtime",
                "downloading rapidocr-onnxruntime",
                "downloading numpy",
                "downloading opencv-python",
            )
        ):
            raise RuntimeError("warm run attempted to download the vision runtime again")

        print(
            json.dumps(
                {
                    "artifact_bytes": executable.stat().st_size,
                    "first_provision_seconds": round(first_seconds, 3),
                    "replay_seconds": round(replay_seconds, 3),
                    "warm_record_seconds": round(warm_seconds, 3),
                    "steps": len(results),
                    "outcome": report["execution_outcome"],
                    "production_eligible": report["production_eligible"],
                    "external_network_calls": network_observation,
                    "silent_incorrect_successes": 0,
                    "model_calls": 0,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
