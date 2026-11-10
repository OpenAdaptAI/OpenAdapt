"""Summarize the frozen runs: one row per workflow, from results/frozen-*/<app>/.

    python -m benchmark.api_generalization.summarize > results/SUMMARY.md

For each workflow the latest frozen run that compiled it is used. Times are
engine seconds of correct runs (median, with the count), from the timing
runs, which interleave the two arms.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent / "results"


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open())) if path.exists() else []


def _median(rows: list[dict[str, str]], arm: str, actuation: str | None = None) -> str:
    xs = [
        float(r["engine_s"])
        for r in rows
        if r["arm"] == arm
        and r["correct"] == "True"
        and (actuation is None or r["save_actuation"] == actuation)
    ]
    return f"{statistics.median(xs):.2f} (n={len(xs)})" if xs else "-"


def main() -> None:
    latest: dict[str, Path] = {}
    for d in sorted(HERE.glob("frozen-*/*/compile.json"), key=lambda p: p.stat().st_mtime):
        latest[d.parent.name] = d.parent
    print("| Workflow | Engine | API step | Why not | Runs (matrix) | Correct | False success | Incorrect write | API saves | GUI s (median) | API s (median) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for app, d in sorted(latest.items()):
        comp = json.loads((d / "compile.json").read_text())
        matrix, timing = _rows(d / "matrix.csv"), _rows(d / "timing.csv")
        both = matrix + timing
        why = "" if comp["proposed"] else "; ".join(comp["reasons"])[:160]
        print(
            f"| {app} | {d.parent.name[7:]} | {'admitted' if comp.get('admitted') else 'no'} | {why} "
            f"| {len(matrix)} | {sum(r['correct'] == 'True' for r in both)}/{len(both)} "
            f"| {sum(r['false_success'] == 'True' for r in both)} | {sum(r['incorrect_write'] == 'True' for r in both)} "
            f"| {sum(r['save_actuation'] == 'api' for r in both)} "
            f"| {_median(timing, 'gui')} | {_median(timing, 'api', 'api')} |"
        )


if __name__ == "__main__":
    main()
