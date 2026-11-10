#!/usr/bin/env python3
"""Fail when a change grows a file in the decomposition plan past its budget.

The budgets live in one table in packages/flow/docs/design/DECOMPOSITION.md,
between the ``size-ratchet:begin`` and ``size-ratchet:end`` markers. A file may
shrink; it may not grow. When a change shrinks a file, lower its budget in the
same change so the gain is kept. Raising a budget is a reviewed edit to that
table.

With ``--base REF`` (a pull request or the merge queue compares against the
base it merges into), a file over budget fails only when this change grew it
or changed its budget row. A file the change did not touch never fails it, so
two pull requests that each fit cannot turn every later one red once both
merge. Without a base the check is absolute; ``--report-only`` prints those
failures as warnings and exits 0, which is what a push to main does. The
nightly run and a local run enforce the absolute check. ``policy`` maps a CI
event to these options for the root test.

Usage: python scripts/check_file_size_ratchet.py [--root PATH] [--base REF]
       [--report-only]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Mapping
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


def read_at(root: Path, ref: str, path: str) -> bytes | None:
    """``path`` as it is at ``ref``, or None when it does not exist there."""
    result = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        # Fail closed: an unreadable base must not pass as "nothing grew".
        raise ValueError(f"base {ref!r} is not a commit in {root}")
    return None


def base_budgets(root: Path, base: str) -> dict[str, int]:
    """The budget table at ``base``; empty when the base has none."""
    text = read_at(root, base, DOC.as_posix())
    if text is None:
        return {}
    try:
        return {b.path: b.lines for b in parse_budgets(text.decode("utf-8"))}
    except ValueError:
        return {}


def policy(env: Mapping[str, str]) -> tuple[str | None, bool]:
    """(base, report_only) for a CI event; nightly and local runs are strict.

    A pull request's and the merge queue's checkout is the merge commit, so
    its first parent is the base it merges into.
    """
    event = env.get("GITHUB_EVENT_NAME")
    if event in ("pull_request", "merge_group"):
        return "HEAD^1", False
    return None, event == "push"


def check(root: Path, base: str | None = None) -> tuple[list[str], list[str]]:
    """Return (failures, notes) for the budgets in ``root``'s plan.

    With ``base``, an over-budget or missing file fails only when the change
    from ``base`` grew it, removed it or changed its budget row.
    """
    budgets = parse_budgets((root / DOC).read_text(encoding="utf-8"))
    before = base_budgets(root, base) if base else {}
    failures: list[str] = []
    notes: list[str] = []
    for budget in budgets:
        path = root / budget.path
        old = read_at(root, base, budget.path) if base else None
        row_changed = base is not None and before.get(budget.path) != budget.lines
        if not path.is_file():
            message = (
                f"{budget.path}: listed in {DOC} but missing. If it was split or "
                "removed, delete its row in the same change."
            )
            if base is None or old is not None or row_changed:
                failures.append(message)
            else:
                notes.append(f"{message} (Already missing on the base.)")
            continue
        lines = count_lines(path)
        if lines > budget.lines:
            message = (
                f"{budget.path}: {lines} lines, budget {budget.lines} "
                f"(+{lines - budget.lines}). Move code out instead of adding it, "
                f"or raise the budget in {DOC} with a reason in the pull request."
            )
            grew = old is None or lines > old.count(b"\n")
            if base is None or grew or row_changed:
                failures.append(message)
            else:
                notes.append(
                    f"{message} Already over budget on the base; this change "
                    "did not grow it."
                )
        elif lines < budget.lines:
            notes.append(
                f"{budget.path}: {lines} lines, budget {budget.lines}. "
                f"Lower the budget to {lines} in {DOC}."
            )
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--base",
        help="fail only for files the change since REF grew; a pull request "
        "and the merge queue pass HEAD^1, the base of their merge commit",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="print failures as warnings and exit 0",
    )
    args = parser.parse_args(argv)
    try:
        failures, notes = check(args.root, args.base)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"note: {note}")
    level = "warning" if args.report_only else "error"
    for failure in failures:
        print(f"{level}: {failure}", file=sys.stderr)
    return 1 if failures and not args.report_only else 0


if __name__ == "__main__":
    sys.exit(main())
