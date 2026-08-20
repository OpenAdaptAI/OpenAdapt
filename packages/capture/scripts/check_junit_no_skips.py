#!/usr/bin/env python3
"""Reject an empty, skipped, failed, or errored qualification JUnit report."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


class JUnitQualificationError(RuntimeError):
    """A qualification result is incomplete."""


def check_reports(paths: list[Path]) -> dict[str, int]:
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
    args = parser.parse_args()
    totals = check_reports(args.reports)
    print(
        "qualification JUnit is complete: "
        f"{totals['tests']} tests, no skips, no failures, no errors"
    )


if __name__ == "__main__":
    main()
