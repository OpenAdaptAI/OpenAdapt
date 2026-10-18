"""Derive every published referral-timing figure from runs.csv.

Writes figures.json next to this file. Standard library only, fixed bootstrap
seed, so the output is the same on every machine:

    python packages/flow/benchmark/openemr_referral_timing/compute_figures.py

Pass --check to fail (exit 1) when the committed figures.json is stale.

The published comparison is the warm runs of blocks 2 to 10 of engine commit
0c642bc0. Block 1 is left out because another process competed for the CPU
during it (CAVEATS.md); blocks 11 to 15 of the DOM bundle were run later, paired
with a different engine commit, and the agent was not run then.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = "main-0c642bc0"
ARMS = {
    # Published as "OpenAdapt": the referral program compiled with DOM element
    # identity (PROTOCOL.md, arm 2).
    "openadapt": "replay-dom",
    # The same task compiled from the 10 Oct bundle (PROTOCOL.md, arm 1).
    "openadapt_main_bundle": "replay-main",
    "agent": "agent-sonnet",
}
SETS = {
    "published": range(2, 11),
    "blocks_1_to_10": range(1, 11),
}
BOOTSTRAP = 10_000
SEED = 20261014


def quantile(xs: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (the protocol's p10/p90)."""
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def r(x: float, n: int = 3) -> float:
    return round(x, n)


def arm_figures(rows: list[dict], arm: str, mode: str, blocks: range) -> dict:
    runs = [
        x
        for x in rows
        if x["engine_tag"] == ENGINE
        and x["arm"] == arm
        and x["mode"] == mode
        and int(x["block"]) in blocks
    ]
    normal = [x for x in runs if x["mismatch"] == "False"]
    dup = [x for x in runs if x["mismatch"] == "True"]
    ok = [x for x in normal if x["correct"] == "True"]
    wall = [float(x["wall"]) for x in ok]
    cost_all = [float(x["cost_usd"]) for x in runs]
    cost_ok = [float(x["cost_usd"]) for x in ok]
    return {
        "blocks": [blocks.start, blocks.stop - 1],
        "runs": len(runs),
        "normal_runs": len(normal),
        "normal_saved_correct": len(ok),
        "readback_12_of_12": sum(x["readback"] == "12/12" for x in ok),
        "duplicate_runs": len(dup),
        "duplicate_no_new_chart": sum(x["new_charts"] == "0" for x in dup),
        "misses": [
            {
                "block": int(x["block"]),
                "ref": x["ref"],
                "outcome": x["outcome"],
                "new_charts": int(x["new_charts"]),
            }
            for x in normal
            if x["correct"] != "True"
        ],
        "wall_s": {
            "n": len(wall),
            "median": r(statistics.median(wall)),
            "mean": r(statistics.fmean(wall)),
            "p10": r(quantile(wall, 0.1)),
            "p90": r(quantile(wall, 0.9)),
            "min": r(min(wall)),
            "max": r(max(wall)),
        },
        "phase_median_s": {
            k: r(statistics.median(float(x[k]) for x in ok))
            for k in (
                "browser_launch",
                "signin_form",
                "recorder_start",
                "task",
                "readback_s",
            )
        },
        "actions_median": statistics.median(int(x["actions"]) for x in ok),
        "actions_min": min(int(x["actions"]) for x in ok),
        "actions_max": max(int(x["actions"]) for x in ok),
        "screens_sent_median": statistics.median(int(x["screens_sent"]) for x in ok),
        "model_calls_median": statistics.median(int(x["model_calls"]) for x in ok),
        "cost_usd": {
            "median_all_runs": r(statistics.median(cost_all), 4),
            "median_correct_normal": r(statistics.median(cost_ok), 4),
            "min": r(min(cost_all), 4),
            "max": r(max(cost_all), 4),
            "total": r(sum(cost_all)),
        },
    }


def diff_of_medians(a: list[float], b: list[float]) -> dict:
    rng = random.Random(SEED)
    boot = sorted(
        statistics.median(rng.choices(a, k=len(a)))
        - statistics.median(rng.choices(b, k=len(b)))
        for _ in range(BOOTSTRAP)
    )
    d = statistics.median(a) - statistics.median(b)
    return {
        "median_difference_s": r(d),
        "ci95_s": [r(quantile(boot, 0.025)), r(quantile(boot, 0.975))],
        "percent_faster": r(-100 * d / statistics.median(b), 1),
    }


def walls(rows: list[dict], arm: str, blocks: range) -> list[float]:
    return [
        float(x["wall"])
        for x in rows
        if x["engine_tag"] == ENGINE
        and x["arm"] == arm
        and x["mode"] == "warm"
        and int(x["block"]) in blocks
        and x["mismatch"] == "False"
        and x["correct"] == "True"
    ]


def compute() -> dict:
    with open(HERE / "runs.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert all(x["valid"] == "True" for x in rows), "an invalid run is in runs.csv"
    out: dict = {
        "source": "packages/flow/benchmark/openemr_referral_timing/runs.csv",
        "engine_commit": "0c642bc0",
        "dates": "2026-10-14 22:27 to 2026-10-15 05:35 ET",
        "system": "OpenEMR 8.0.0.3, local test copy, 8 synthetic referrals (7 normal, 1 possible duplicate)",
        "agent_model": "claude-sonnet-5-5",
        "mode": "warm",
        "sets": {},
    }
    for set_name, blocks in SETS.items():
        out["sets"][set_name] = {
            name: arm_figures(rows, arm, "warm", blocks) for name, arm in ARMS.items()
        }
        out["sets"][set_name]["openadapt_vs_agent"] = diff_of_medians(
            walls(rows, "replay-dom", blocks), walls(rows, "agent-sonnet", blocks)
        )
    # Every DOM-bundle warm block on this engine, including 11 to 15 (run later,
    # paired with a PR build; the agent was not run then). Not a comparison set.
    out["sets"]["openadapt_blocks_1_to_15"] = {
        "openadapt": arm_figures(rows, "replay-dom", "warm", range(1, 16))
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = json.dumps(compute(), indent=1) + "\n"
    target = HERE / "figures.json"
    if args.check:
        if target.read_text() != text:
            print("figures.json is stale: run compute_figures.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
