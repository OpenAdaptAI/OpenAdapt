"""Derive figures.json from runs.csv (the 20 + 20 matrix).

    python -m benchmark.openemr_api_tier.compute_figures [--check]
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "results" / "runs.csv"
OUT = HERE / "results" / "figures.json"


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(xs: list[float]) -> dict[str, float | int]:
    return {"n": len(xs), "median": round(statistics.median(xs), 3), "p10": round(pct(xs, 0.1), 3),
            "p90": round(pct(xs, 0.9), 3), "min": round(min(xs), 3), "max": round(max(xs), 3),
            "mean": round(statistics.fmean(xs), 3)}


def boot_ratio(a: list[float], b: list[float], n: int = 10_000, seed: int = 0) -> list[float]:
    rnd = random.Random(seed)
    rs = sorted(statistics.median(rnd.choices(a, k=len(a))) / statistics.median(rnd.choices(b, k=len(b)))
                for _ in range(n))
    return [round(rs[int(0.025 * n)], 2), round(rs[int(0.975 * n)], 2)]


def compute() -> dict:
    rows = list(csv.DictReader(RUNS.open()))
    fig: dict = {"source": "results/runs.csv", "rows": len(rows), "arms": {}}
    for arm in ("browser", "api"):
        rs = [r for r in rows if r["arm"] == arm]
        normal = [r for r in rs if r["mismatch"] == "False"]
        dup = [r for r in rs if r["mismatch"] == "True"]
        ok = [r for r in normal if r["correct"] == "True"]
        fig["arms"][arm] = {
            "runs": len(rs),
            "correct": sum(r["correct"] == "True" for r in rs),
            "normal_runs": len(normal), "normal_saved_12_of_12": len(ok),
            "duplicate_runs": len(dup), "duplicate_no_new_chart": sum(r["correct"] == "True" for r in dup),
            "wall_s_correct_normal": summary([float(r["wall_s"]) for r in ok]),
            "task_s_correct_normal": summary([float(r["task_s"]) for r in ok]),
            "setup_s_correct_normal": summary([float(r["setup_s"]) for r in ok]),
            # runs.csv task_s runs from the clock start; engine time is after setup.
            "engine_s_correct_normal": summary([float(r["task_s"]) - float(r["setup_s"]) for r in ok]),
            "wall_s_all_runs": summary([float(r["wall_s"]) for r in rs]),
            "failures": [{k: r[k] for k in ("block", "ref", "readback", "outcome", "error")}
                         for r in rs if r["correct"] != "True"],
        }
    b, a = fig["arms"]["browser"], fig["arms"]["api"]
    bw = [float(r["wall_s"]) for r in rows if r["arm"] == "browser" and r["mismatch"] == "False" and r["correct"] == "True"]
    aw = [float(r["wall_s"]) for r in rows if r["arm"] == "api" and r["mismatch"] == "False" and r["correct"] == "True"]
    fig["speedup"] = {
        "wall_median_ratio": round(b["wall_s_correct_normal"]["median"] / a["wall_s_correct_normal"]["median"], 2),
        "wall_median_ratio_ci95": boot_ratio(bw, aw),
        "wall_median_diff_s": round(b["wall_s_correct_normal"]["median"] - a["wall_s_correct_normal"]["median"], 3),
        "task_median_ratio": round(b["task_s_correct_normal"]["median"] / a["task_s_correct_normal"]["median"], 2),
        "engine_median_ratio": round(b["engine_s_correct_normal"]["median"] / a["engine_s_correct_normal"]["median"], 2),
    }
    return fig


def main() -> int:
    fig = compute()
    text = json.dumps(fig, indent=1) + "\n"
    if "--check" in sys.argv:
        if OUT.read_text() != text:
            print("figures.json is stale; rerun compute_figures.py")
            return 1
        return 0
    OUT.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
