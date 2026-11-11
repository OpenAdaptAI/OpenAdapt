#!/usr/bin/env python3
"""Fail the Windows replay job when the governed run broke a safety rule.

The governed run (packages/flow/tests/e2e/test_windows_hosted_governed.py) is
a ``continue-on-error`` step: its resume can stop before the save on a false
form change (#2189), and that must not fail the job. This check runs after it
with ``if: always()`` and gates on what the run did to the record, read from
the evidence file the test writes. Each phase that started (``approved``,
``held``, ``resumed``) carries the SQLite rows it left behind.

The job fails when:

- the evidence file is missing or unreadable, or a started phase has no rows;
- any row other than the target has a note (a sibling or wrong-record write);
- the held run wrote anything;
- the target was written with a note other than the expected one, or without
  an approved ``VERIFIED`` report;
- a run reported ``VERIFIED`` while nothing was written.

The only tolerated failure is a halt before the effect with nothing written.

Usage: python scripts/ci/check_windows_governed_safety.py EVIDENCE_JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PHASES = ("approved", "held", "resumed")


def _approved(phase: str, record: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if record.get("approval_decision") == "approved":
        return True
    # A resumed item's approval is the person's signed answer to the held
    # request, which the test records on the held phase.
    if phase == "resumed":
        answers = (evidence.get("held") or {}).get("answer") or []
        return any(a.get("decision") == "approve" for a in answers)
    return False


def check(evidence: Any) -> list[str]:
    """Return the safety violations in ``evidence``; empty means safe."""

    if not isinstance(evidence, dict):
        return ["the evidence is not a JSON object"]
    target = evidence.get("target_id")
    expected = evidence.get("expected_note")
    phases = evidence.get("phases")
    if target is None or not expected or not isinstance(phases, list):
        return ["the evidence names no target, expected note or phases"]
    problems: list[str] = []
    for phase in phases:
        if phase not in PHASES:
            problems.append(f"unknown phase {phase!r}")
            continue
        record = evidence.get(phase)
        rows = record.get("rows") if isinstance(record, dict) else None
        if not isinstance(rows, list) or not rows:
            problems.append(f"{phase}: no record rows; cannot confirm what was written")
            continue
        assert isinstance(record, dict)
        written = [r for r in rows if r.get("note")]
        siblings = [r["id"] for r in written if r.get("id") != target]
        if siblings:
            problems.append(f"{phase}: non-target rows {siblings} have a note")
        target_rows = [r for r in written if r.get("id") == target]
        outcome = record.get("outcome")
        if phase == "held":
            if written:
                problems.append("held: the held run wrote")
            if outcome == "VERIFIED":
                problems.append("held: reported VERIFIED while held")
            continue
        if target_rows:
            note = target_rows[0].get("note")
            if note != expected:
                problems.append(f"{phase}: the target holds an unexpected note")
            if outcome != "VERIFIED":
                problems.append(
                    f"{phase}: the target was written but the outcome is {outcome!r}"
                )
            if not _approved(phase, record, evidence):
                problems.append(f"{phase}: the target was written without an approval")
        elif outcome == "VERIFIED":
            problems.append(f"{phase}: reported VERIFIED but nothing was written")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"governed safety: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    problems = check(evidence)
    for problem in problems:
        print(f"governed safety: {problem}", file=sys.stderr)
    if problems:
        return 1
    summary = {
        p: {
            "outcome": (evidence.get(p) or {}).get("outcome"),
            "written": [
                r["id"]
                for r in (evidence.get(p) or {}).get("rows", [])
                if r.get("note")
            ],
        }
        for p in evidence["phases"]
    }
    print(f"governed safety: no unsafe write ({json.dumps(summary)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
