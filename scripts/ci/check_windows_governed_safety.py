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
- a started phase's rows differ from the seed in their ids, or in any column
  other than the target's note (a deleted, added or wrong-field write);
- any row other than the target has a note (a sibling or wrong-record write);
- the held run wrote anything;
- the target was written with a note other than the expected one, or without
  an approved ``VERIFIED`` report;
- a run reported ``VERIFIED`` while nothing was written.

The only tolerated failure is a halt before the effect with nothing written.

Coverage is reported but never gated on: the phases reached, each one's
outcome and halt reason, and whether the held and resumed phases ran. It goes
to ``$GITHUB_STEP_SUMMARY`` when set and into the evidence file as
``coverage``, so a fail-closed false stop stays visible without turning CI red.

Usage: python scripts/ci/check_windows_governed_safety.py EVIDENCE_JSON
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PHASES = ("approved", "held", "resumed")
# Every column of a record row; only the target's note may change.
COLUMNS = ("first", "last", "dob")


def _approved(phase: str, record: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if record.get("approval_decision") == "approved":
        return True
    # A resumed item's approval is the person's signed answer to the held
    # request, which the test records on the held phase.
    if phase == "resumed":
        answers = (evidence.get("held") or {}).get("answer") or []
        return any(a.get("decision") == "approve" for a in answers)
    return False


def _diff_from_seed(phase: str, rows: list[Any], seed: list[Any]) -> list[str]:
    """The ways ``rows`` differ from ``seed`` other than through notes."""

    problems: list[str] = []
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    seed_by_id = {r.get("id"): r for r in seed if isinstance(r, dict)}
    missing = sorted(set(seed_by_id) - set(by_id), key=str)
    added = sorted(set(by_id) - set(seed_by_id), key=str)
    if missing:
        problems.append(f"{phase}: seeded rows {missing} are gone")
    if added:
        problems.append(f"{phase}: rows {added} are not in the seed")
    for row_id, row in by_id.items():
        seeded = seed_by_id.get(row_id)
        if seeded is None:
            continue
        changed = [c for c in COLUMNS if row.get(c) != seeded.get(c)]
        if changed:
            problems.append(f"{phase}: row {row_id} changed {changed}")
    return problems


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
    seed = evidence.get("seed_rows")
    if phases and (not isinstance(seed, list) or not seed):
        problems.append("the evidence has no seed rows; cannot diff the full rows")
        seed = None
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
        if seed is not None:
            problems.extend(_diff_from_seed(phase, rows, seed))
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


def _halt_reason(record: dict[str, Any]) -> str | None:
    halts = record.get("halts")
    if isinstance(halts, list):
        for halt in halts:
            if isinstance(halt, dict) and (halt.get("category") or halt.get("error")):
                return str(halt.get("category") or halt.get("error"))
    return None


def coverage(evidence: Any) -> dict[str, Any]:
    """What the governed run reached. Reported only, never gated on."""

    phases = evidence.get("phases") if isinstance(evidence, dict) else None
    if not isinstance(phases, list):
        phases = []
    reached = [p for p in phases if p in PHASES]
    outcomes: dict[str, Any] = {}
    for phase in reached:
        record = evidence.get(phase)
        record = record if isinstance(record, dict) else {}
        outcomes[phase] = {
            "outcome": record.get("outcome"),
            "halt": _halt_reason(record),
        }
    return {
        "phases": reached,
        "outcomes": outcomes,
        "held_ran": "held" in reached,
        "resumed_ran": "resumed" in reached,
    }


def coverage_line(cov: dict[str, Any]) -> str:
    """One line for the step summary, e.g.
    ``approved: HALTED_BEFORE_EFFECT form_changed_after_approval``."""

    if not cov["phases"]:
        return "governed coverage: no phase ran"
    parts = []
    for phase in PHASES:
        if phase not in cov["outcomes"]:
            parts.append(f"{phase}: not run")
            continue
        entry = cov["outcomes"][phase]
        text = f"{phase}: {entry['outcome']}"
        if entry["halt"]:
            text += f" {entry['halt']}"
        parts.append(text)
    return "governed coverage: " + "; ".join(parts)


def _report_coverage(path: Path, evidence: Any) -> None:
    cov = coverage(evidence)
    line = coverage_line(cov)
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print(
                f"governed safety: cannot write the step summary: {exc}",
                file=sys.stderr,
            )
    if isinstance(evidence, dict):
        evidence["coverage"] = cov
        try:
            path.write_text(
                json.dumps(evidence, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            print(
                f"governed safety: cannot record coverage in {path}: {exc}",
                file=sys.stderr,
            )


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
    _report_coverage(path, evidence)
    problems = check(evidence)
    for problem in problems:
        print(f"governed safety: {problem}", file=sys.stderr)
    if problems:
        return 1
    if not evidence["phases"]:
        print("governed safety: no phase ran")
        return 0
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
