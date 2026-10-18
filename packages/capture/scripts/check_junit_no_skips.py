#!/usr/bin/env python3
"""Reject an empty, skipped, failed, or errored qualification JUnit report."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


class JUnitQualificationError(RuntimeError):
    """A qualification result is incomplete."""


def check_reports(
    paths: list[Path], *, expected_tests: int | None = None
) -> dict[str, int]:
    if not paths:
        raise JUnitQualificationError("no JUnit report was supplied")
    totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise JUnitQualificationError(f"cannot read JUnit report {path}: {exc}") from exc
        cases = root.findall(".//testcase")
        totals["tests"] += len(cases)
        totals["skipped"] += sum(case.find("skipped") is not None for case in cases)
        totals["failures"] += sum(case.find("failure") is not None for case in cases)
        totals["errors"] += sum(case.find("error") is not None for case in cases)
    if totals["tests"] == 0:
        raise JUnitQualificationError("the qualification ran zero tests")
    # A caller that selects named tests states how many it selected. A renamed,
    # deleted, or newly deselected test then fails the gate instead of quietly
    # qualifying a smaller set than the workflow claims to run.
    if expected_tests is not None and totals["tests"] != expected_tests:
        raise JUnitQualificationError(
            f"the qualification ran {totals['tests']} tests; "
            f"exactly {expected_tests} were required"
        )
    if totals["skipped"]:
        raise JUnitQualificationError(
            f"the qualification skipped {totals['skipped']} of {totals['tests']} tests"
        )
    if totals["failures"] or totals["errors"]:
        raise JUnitQualificationError(
            "the qualification contains "
            f"{totals['failures']} failures and {totals['errors']} errors"
        )
    return totals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "--expected-tests",
        type=int,
        default=None,
        help="Exact number of test cases the reports must contain",
    )
    args = parser.parse_args()
    totals = check_reports(args.reports, expected_tests=args.expected_tests)
    print(
        "qualification JUnit is complete: "
        f"{totals['tests']} tests, no skips, no failures, no errors"
    )


if __name__ == "__main__":
    main()
