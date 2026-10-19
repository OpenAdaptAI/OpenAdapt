#!/usr/bin/env python3
"""Print the before/after table for the display-scaling sweep.

Usage::

    python benchmark/pixel_scale/compare.py results_before.json results_after.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATES = {
    "layout_drift": "layout check",
    "target_resolution": "target not found",
    "identity": "identity check",
    "postcondition": "postcondition",
    "input_verification": "input check",
    "other": "other",
}


def _cell(point: dict) -> str:
    if point["outcome"] == "pass":
        return "pass"
    stop = point.get("stopped_at") or {}
    gate = GATES.get(stop.get("gate", ""), stop.get("gate", "?"))
    step = stop.get("step_id", "?").replace("step_00", "step ")
    if point["outcome"] == "halted":
        return f"halt: {gate}, {step}"
    return point["outcome"]


def table(before: dict, after: dict) -> str:
    def key(p: dict) -> tuple:
        return (p["kiosk"], p["label"], p["campaign"])

    old = {key(p): p for p in before["points"]}
    rows = [
        "| Kiosk | Render | Run | Before | After |",
        "|---|---|---|---|---|",
    ]
    for point in after["points"]:
        prior = old.get(key(point))
        rows.append(
            f"| {point['kiosk']} | {point['label']} | {point['campaign']} | "
            f"{_cell(prior) if prior else 'not run'} | {_cell(point)} |"
        )
    return "\n".join(rows)


def main() -> int:
    before = json.loads(Path(sys.argv[1]).read_text())
    after = json.loads(Path(sys.argv[2]).read_text())
    print(table(before, after))
    print()
    for name, data in (("before", before), ("after", after)):
        print(name, json.dumps(data["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
