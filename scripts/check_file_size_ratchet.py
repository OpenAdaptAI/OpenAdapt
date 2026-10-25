#!/usr/bin/env python3
"""Fail when a file listed in the decomposition plan grows past its budget.

The budgets live in one table in packages/flow/docs/design/DECOMPOSITION.md,
between the ``size-ratchet:begin`` and ``size-ratchet:end`` markers. A file may
shrink; it may not grow. When a change shrinks a file, lower its budget in the
same change so the gain is kept. Raising a budget is a reviewed edit to that
table.

Usage: python scripts/check_file_size_ratchet.py [--root PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DOC = Path("packages/flow/docs/design/DECOMPOSITION.md")
BEGIN = "<!-- size-ratchet:begin -->"
END = "<!-- size-ratchet:end -->"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*$")


@dataclass(frozen=True)
class Budget:
    path: str
    lines: int


def parse_budgets(doc_text: str) -> list[Budget]:
    """Read the budget rows between the markers; refuse a missing or empty table."""
    try:
        start = doc_text.index(BEGIN) + len(BEGIN)
        end = doc_text.index(END, start)
    except ValueError:
        raise ValueError(f"{DOC}: size-ratchet markers not found") from None
    budgets = []
    for line in doc_text[start:end].splitlines():
        match = ROW.match(line.strip())
        if match:
            budgets.append(Budget(match.group(1), int(match.group(2))))
    if not budgets:
        raise ValueError(f"{DOC}: size-ratchet table has no rows")
    paths = [b.path for b in budgets]
    duplicates = sorted({p for p in paths if paths.count(p) > 1})
    if duplicates:
        raise ValueError(f"{DOC}: listed more than once: {', '.join(duplicates)}")
    return budgets


def count_lines(path: Path) -> int:
    """Count lines the way ``wc -l`` does."""
    return path.read_bytes().count(b"\n")


def check(root: Path) -> tuple[list[str], list[str]]:
    """Return (failures, notes) for the budgets in ``root``'s plan."""
    budgets = parse_budgets((root / DOC).read_text(encoding="utf-8"))
    failures: list[str] = []
    notes: list[str] = []
    for budget in budgets:
        path = root / budget.path
        if not path.is_file():
            failures.append(
                f"{budget.path}: listed in {DOC} but missing. If it was split or "
                "removed, delete its row in the same change."
            )
            continue
        lines = count_lines(path)
        if lines > budget.lines:
            failures.append(
                f"{budget.path}: {lines} lines, budget {budget.lines} "
                f"(+{lines - budget.lines}). Move code out instead of adding it, "
                f"or raise the budget in {DOC} with a reason in the pull request."
            )
        elif lines < budget.lines:
            notes.append(
                f"{budget.path}: {lines} lines, budget {budget.lines}. "
                f"Lower the budget to {lines} in {DOC}."
            )
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    try:
        failures, notes = check(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"note: {note}")
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
