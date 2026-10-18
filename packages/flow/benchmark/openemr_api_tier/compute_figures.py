"""Derive results/figures.json from results/runs.csv (the feature matrix).

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
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "median": round(statistics.median(xs), 3), "p10": round(pct(xs, 0.1), 3),
            "p90": round(pct(xs, 0.9), 3), "min": round(min(xs), 3), "max": round(max(xs), 3)}


def boot_ratio(a: list[float], b: list[float], n: int = 10_000, seed: int = 0) -> list[float]:
    rnd = random.Random(seed)
    rs = sorted(statistics.median(rnd.choices(a, k=len(a))) / statistics.median(rnd.choices(b, k=len(b)))
                for _ in range(n))
    return [round(rs[int(0.025 * n)], 2), round(rs[int(0.975 * n)], 2)]


def compute() -> dict:
    rows = list(csv.DictReader(RUNS.open()))
    fig: dict = {"source": "results/runs.csv", "rows": len(rows), "arms": {}}
    wall: dict[str, list[float]] = {}
    for arm in ("browser", "api"):
        rs = [r for r in rows if r["arm"] == arm]
        normal = [r for r in rs if r["mismatch"] == "False"]
        dup = [r for r in rs if r["mismatch"] == "True"]
        ok = [r for r in normal if r["correct"] == "True"]
        wall[arm] = [float(r["wall_s"]) for r in ok]
        fig["arms"][arm] = {
            "runs": len(rs),
            "correct": sum(r["correct"] == "True" for r in rs),
            "normal_runs": len(normal),
            "normal_saved_12_of_12": len(ok),
            "duplicate_runs": len(dup),
            "duplicate_no_new_chart": sum(r["correct"] == "True" for r in dup),
            "save_actuation": sorted({r["actuation"] for r in normal}),
            "api_fallbacks": sorted({r["api_fallback"] for r in rs if r["api_fallback"]}),
            "wall_s_correct_normal": summary(wall[arm]),
            "setup_s_correct_normal": summary([float(r["setup_s"]) for r in ok]),
            "engine_s_correct_normal": summary([float(r["engine_s"]) for r in ok]),
            "gate_gave_up": sum(r["gate_gave_up"] == "True" for r in rs),
            "failures": [{k: r[k] for k in ("block", "ref", "readback", "outcome", "error")}
                         for r in rs if r["correct"] != "True"],
        }
    b, a = fig["arms"]["browser"], fig["arms"]["api"]
    if wall["browser"] and wall["api"]:
        fig["speedup"] = {
            "wall_median_ratio": round(b["wall_s_correct_normal"]["median"] / a["wall_s_correct_normal"]["median"], 2),
            "wall_median_ratio_ci95": boot_ratio(wall["browser"], wall["api"]),
            "wall_median_diff_s": round(b["wall_s_correct_normal"]["median"] - a["wall_s_correct_normal"]["median"], 3),
            "engine_median_ratio": round(
                b["engine_s_correct_normal"]["median"] / a["engine_s_correct_normal"]["median"], 2),
        }
    return fig


def main() -> int:
    text = json.dumps(compute(), indent=1) + "\n"
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
