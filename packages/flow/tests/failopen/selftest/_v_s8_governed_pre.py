# vendored from 576f87b41483:scripts/ci/check_windows_governed_safety.py, lines 27-91, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S8 pre-fix: main's governed check reads '' as no write (#2258); pinned so a fix on main can't turn the control green.

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations


# vendored: 576f87b41483:scripts/ci/check_windows_governed_safety.py 27-91
# fmt: off
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
# fmt: on
