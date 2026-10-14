#!/usr/bin/env python3
"""Extract a compact over-halt regression artifact from a benchmark results.json.

``run_current_flow_local_benchmark.py`` emits a regression artifact for the
``theme`` condition only, because that is where the over-halt lived when the
runner was written.  Over-halts move: the ``region_stable`` postcondition
failure that fired under ``theme`` on Flow 1.16.1 fires under ``clean`` on
1.24.0.  Pinning the artifact to one condition would have hidden that.

This reads an already-written ``results.json`` and emits the same shape for any
condition, so the runner that produced the measurement stays byte-identical
across releases and the artifact still follows the finding.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_regression(
    report: dict[str, Any],
    *,
    condition: str,
    arm: str = "compiled",
    title: str | None = None,
) -> dict[str, Any]:
    """Build the regression artifact for ``arm`` under ``condition``."""

    if condition not in report["conditions"]:
        raise ValueError(f"unknown condition {condition!r}: {report['conditions']}")
    if arm not in report["arms"]:
        raise ValueError(f"unknown arm {arm!r}: {report['arms']}")

    observations = [
        {
            "trial": row["trial"],
            "replayer_success": row.get("replayer_success"),
            "oracle_success": row.get("success"),
            "primary_outcome": row["primary_outcome"],
            "first_failure": row.get("first_failure"),
            "steady_wall_s": row["steady_wall_s"],
            "end_to_end_wall_s": row["end_to_end_wall_s"],
            "note_sha256": row["note_sha256"],
            "final_screenshot_sha256": row["final_screenshot_sha256"],
        }
        for row in report["runs"]
        if row["arm"] == arm
        and row["condition"] == condition
        and row["primary_outcome"] == "over_halt"
    ]
    flow = report["source"]["flow"]
    wheel = flow["artifact"]["filename"]
    return {
        "schema_version": 1,
        "title": title or (f"{condition} effect succeeds but the runtime reports halt ({arm} arm)"),
        "flow": flow,
        "runner_sha256": report["source"]["runner_sha256"],
        "task": report["task"],
        "condition": {
            "query": "" if condition == "clean" else f"?drift={condition}",
            "kind": condition,
        },
        "arm": arm,
        "oracle": report["oracle"],
        "expected": (
            "if the independently verified effect succeeded, the runtime must "
            "not leave the run in an unresumable false-incomplete state"
        ),
        "observed_count": len(observations),
        "counted_trials": report["trials_per_arm_condition"],
        "observations": observations,
        "reproduce": (
            "python scripts/run_current_flow_local_benchmark.py "
            f"--flow-source <{flow['release_tag']}-checkout> "
            f"--flow-wheel <{wheel}> --out <new-output-directory>; "
            "python scripts/extract_over_halt_regression.py "
            f"--results <new-output-directory>/results.json --condition {condition}"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract an over-halt regression artifact from results.json."
    )
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--arm", default="compiled")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--title")
    args = parser.parse_args(argv)

    report = json.loads(args.results.read_text(encoding="utf-8"))
    regression = build_regression(report, condition=args.condition, arm=args.arm, title=args.title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(regression, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({regression['observed_count']} observation(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
