#!/usr/bin/env python3
"""Print the README tables from the sweep and probe results.

Usage::

    python benchmark/pixel_grounding/summarize.py \\
        results_template.json results_model.json results_grounding.json
    python benchmark/pixel_grounding/summarize.py --latency results_latency.json
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

UNSAFE = ("wrong_target", "wrong_write", "false_completion", "crash")
ICONS = ("tb-attach", "save")


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def end_to_end(template: dict, model: dict) -> None:
    print("| Condition | Campaign | Template ladder | With grounding model |")
    print("|---|---|---|---|")
    pairs = zip(template["points"], model["points"])
    for old, new in pairs:
        assert old["condition"]["name"] == new["condition"]["name"]
        assert old["campaign"] == new["campaign"]

        def cell(point: dict) -> str:
            stop = point["stopped_at"]
            where = f" ({stop['step_id']}, {stop['gate']})" if stop else ""
            return point["outcome"] + where

        print(
            f"| {old['condition']['name']} | {old['campaign']} | "
            f"{cell(old)} | {cell(new)} |"
        )
    print()
    for name, data in (("template", template), ("model", model)):
        outcomes = Counter(p["outcome"] for p in data["points"])
        grounder_steps = sum(
            p["rung_counts"].get("grounder", 0) for p in data["points"]
        )
        unsafe = sum(outcomes[o] for o in UNSAFE)
        print(
            f"{name}: {dict(outcomes)}; unsafe {unsafe}; "
            f"steps resolved by the model {grounder_steps}; "
            f"dead clicks {sum(p['dead_clicks'] for p in data['points'])}"
        )
    print()


def per_target(probe: dict) -> None:
    rows = probe["rows"]
    conditions = list(dict.fromkeys(r["condition"] for r in rows))
    print("| Condition | Target | Template matcher | Grounding model rung |")
    print("|---|---|---|---|")
    for condition in conditions:
        for row in rows:
            if row["condition"] != condition or row["target"] not in ICONS:
                continue
            template = (
                "found"
                if row["template_found"]
                else ("wrong target" if row["template_wrong"] else "not found")
            )
            print(
                f"| {condition} | {row['target']} | {template} | "
                f"{row['rung_outcome']} |"
            )
    print()
    for label, subset in (
        ("icons", [r for r in rows if r["target"] in ICONS]),
        ("all click targets", rows),
    ):
        template = Counter(
            "found"
            if r["template_found"]
            else "wrong"
            if r["template_wrong"]
            else "none"
            for r in subset
        )
        rung = Counter(r["rung_outcome"].split(":")[0] for r in subset)
        print(f"{label} ({len(subset)}): template {dict(template)}; rung {dict(rung)}")
    by_frame: dict[tuple, list[float]] = {}
    for row in rows:
        by_frame.setdefault(tuple(row["frame"]), []).append(row["ms"])
    print("latency (ms), median / max by frame size:")
    for frame, values in sorted(by_frame.items()):
        print(
            f"  {frame[0]}x{frame[1]}: {statistics.median(values):.0f} / {max(values):.0f}"
        )
    print(f"  all: {probe['latency_ms']}")


def latency(data: dict) -> None:
    print(
        "| Provider | Capture | Target | p50 ms | p95 ms | Decode | Edge search | Model |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for row in data["rows"]:
        if not row["queried_in_replay"]:
            continue
        provider = row["provider"].replace("ExecutionProvider", "")
        print(
            f"| {provider} | {row['capture']} ({row['frame'][0]}x{row['frame'][1]}) "
            f"| {row['target']} | {row['warm_ms']['p50']:.0f} | "
            f"{row['warm_ms']['p95']:.0f} | {row['decode_ms']['p50']:.0f} | "
            f"{row['edge_search_ms']['p50']:.0f} | {row['model_ms']['p50']:.0f} |"
        )
    print()
    print("| Input side | 1 crop | 4 crops | 8 crops | 12 crops |")
    print("|---|---|---|---|---|")
    by_side: dict[int, dict[int, float]] = {}
    for entry in data["model_by_batch"]:
        if "p50" in entry:
            by_side.setdefault(entry["input_side"], {})[entry["batch"]] = entry["p50"]
    for side, cells in by_side.items():
        print(
            f"| {side} px | "
            + " | ".join(f"{cells.get(n, float('nan')):.0f}" for n in (1, 4, 8, 12))
            + " |"
        )


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--latency":
        latency(_load(sys.argv[2]))
        return 0
    template, model, probe = (_load(p) for p in sys.argv[1:4])
    end_to_end(template, model)
    per_target(probe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
