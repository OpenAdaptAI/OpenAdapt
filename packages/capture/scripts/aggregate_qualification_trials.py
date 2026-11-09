#!/usr/bin/env python3
"""Aggregate counted qualification trials into a fail-closed evidence summary.

The production qualification campaign runs the complete live recorder
qualification three times per operating system against one exact candidate.
This tool reads the JUnit XML of every trial and emits one JSON summary that
records the counts the evidence standard requires:

- ``trials_run`` vs ``required_trials_per_os``,
- summed ``tests`` / ``failures`` / ``errors`` / ``skipped``,
- ``over_halt``: failed or errored test cases. These trials inject no fault,
  so any halt is an unjustified halt and must be zero,
- ``silent_incorrect_success``: trials whose JUnit shows failures, errors, or
  skips while the run was reported successful. The workflow fails any trial
  whose process exits non-zero, so a parsed trial implies a success-shaped
  run; any dirty count is therefore exactly that defect class and must be
  zero.

The aggregation fails closed: any missing file, unparsable XML, skip,
failure, error, duplicate, or count below the required number of trials
raises :class:`QualificationEvidenceError`.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class QualificationEvidenceError(ValueError):
    """The counted qualification evidence is incomplete or unclean."""


def _sum_suite(path: Path) -> dict[str, int]:
    """Sum testcase counts across every <testsuite> in one JUnit XML file."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise QualificationEvidenceError(f"{path}: unreadable JUnit XML: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise QualificationEvidenceError(f"{path}: no <testsuite> element found")
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in counts:
            raw = suite.get(key)
            if raw is None:
                raise QualificationEvidenceError(
                    f"{path}: <testsuite> is missing {key!r}"
                )
            try:
                counts[key] += max(0, int(float(raw)))
            except ValueError as exc:
                raise QualificationEvidenceError(
                    f"{path}: invalid {key!r} value {raw!r}"
                ) from exc
    if counts["tests"] <= 0:
        raise QualificationEvidenceError(f"{path}: a qualification trial ran zero tests")
    return counts


def aggregate(
    junit_files: list[Path],
    *,
    os_name: str,
    candidate_sha: str = "",
    required_trials: int = 3,
) -> dict:
    """Build the fail-closed per-OS campaign summary."""
    names = [path.name for path in junit_files]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise QualificationEvidenceError(f"duplicate trial files: {duplicates}")
    if len(junit_files) != required_trials:
        raise QualificationEvidenceError(
            f"{os_name}: expected {required_trials} trials, got {len(junit_files)}"
        )
    missing = [str(path) for path in junit_files if not path.is_file()]
    if missing:
        raise QualificationEvidenceError(f"{os_name}: missing trial evidence: {missing}")

    trials = []
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    silent_incorrect_success = 0
    for path in sorted(junit_files, key=lambda item: item.name):
        counts = _sum_suite(path)
        # A parsed trial implies its process exited zero (the workflow fails
        # non-zero trials first), so any dirty count is a success shape over
        # broken evidence.
        dirty = counts["failures"] + counts["errors"] + counts["skipped"]
        if dirty > 0:
            silent_incorrect_success += 1
        for key in totals:
            totals[key] += counts[key]
        trials.append({"file": path.name, **counts})

    over_halt = totals["failures"] + totals["errors"]
    violations = []
    if silent_incorrect_success:
        violations.append(
            f"{silent_incorrect_success} success-shaped trial(s) with failures/errors/skips"
        )
    if over_halt:
        violations.append(f"{over_halt} unjustified halt(s) (no fault injected)")
    if totals["skipped"]:
        violations.append(f"{totals['skipped']} skipped test case(s)")

    return {
        "os": os_name,
        "candidate_sha": candidate_sha,
        "required_trials_per_os": required_trials,
        "trials_run": len(junit_files),
        "per_trial": trials,
        "totals": totals,
        "silent_incorrect_success": silent_incorrect_success,
        "over_halt": over_halt,
        "passed": not violations,
        "violations": violations,
    }


def write_summary(summary: dict, output: Path) -> None:
    """Persist the summary JSON, creating its parent directory."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", dest="os_name", required=True, help="Operating-system label")
    parser.add_argument("--candidate-sha", default="", help="Qualified commit SHA")
    parser.add_argument(
        "--expected-trials",
        type=int,
        default=3,
        help="Required number of trial files (default: 3)",
    )
    parser.add_argument("junit_files", nargs="+", help="Trial JUnit XML paths")
    parser.add_argument("--output", required=True, help="Summary JSON destination")
    args = parser.parse_args()

    try:
        summary = aggregate(
            [Path(name) for name in args.junit_files],
            os_name=args.os_name,
            candidate_sha=args.candidate_sha,
            required_trials=args.expected_trials,
        )
    except QualificationEvidenceError as exc:
        print(json.dumps({"passed": False, "violations": [str(exc)]}, indent=2))
        raise SystemExit(1) from exc

    write_summary(summary, Path(args.output))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary["passed"]:
        raise SystemExit(
            f"{summary['os']}: qualification evidence rejected: "
            + "; ".join(summary["violations"])
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive fail-closed shell
        print(f"qualification aggregation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
