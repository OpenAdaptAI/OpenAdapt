"""Summarize ``compare.py`` rows: medians and spread per arm, checks compared.

Medians are over saves the SQL read-back judged correct (r6, the possible
duplicate, is reported separately as a safe stop, as in the scoreboard).
The check comparison requires every arm to prove the same thing at every
step of the same referral: identity status and mode, input read-back,
postconditions, submit binding and the save's record check.

    python -m benchmark.screen_path_speed.analyze ROWS.jsonl > summary.json
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from typing import Any


def spread(values: list[float]) -> dict[str, Any]:
    values = sorted(values)
    if not values:
        return {"n": 0}
    q = statistics.quantiles(values, n=4) if len(values) >= 2 else [values[0]] * 3
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "p25": round(q[0], 3),
        "p75": round(q[2], 3),
        "min": round(values[0], 3),
        "max": round(values[-1], 3),
    }


def main() -> None:
    rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
    arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        arms[row["arm"]].append(row)
    out: dict[str, Any] = {"arms": {}, "checks": {}}
    for arm, items in arms.items():
        saves = [r for r in items if not r["mismatch"] and r["correct"]]
        stops = [r for r in items if r["mismatch"]]
        buckets: dict[str, list[float]] = defaultdict(list)
        for r in saves:
            for k, v in (r.get("timings_ms") or {}).items():
                buckets[k].append(float(v) / 1000.0)
        out["arms"][arm] = {
            "engine_head": sorted({r["engine_head"] for r in items}),
            "items": len(items),
            "saves_correct": f"{len(saves)} of {sum(1 for r in items if not r['mismatch'])}",
            "duplicate_safe_stops": f"{sum(1 for r in stops if r['correct'])} of {len(stops)}",
            "duplicate_halt_steps": sorted(
                {s[0] for r in stops for s in (r.get("step_errors") or [])}
            ),
            "load_1m_max": max(max(r["load_start"], r["load_end"]) for r in items),
            "wall_s": spread([r["wall_s"] for r in saves]),
            "setup_s": spread([r["setup_s"] for r in saves]),
            "engine_s": spread([r["engine_s"] for r in saves]),
            "harness_readback_s": spread([r["readback_s"] for r in saves]),
            "engine_buckets_median_s": {
                k: round(statistics.median(v), 3)
                for k, v in sorted(
                    buckets.items(), key=lambda kv: -statistics.median(kv[1])
                )
            },
        }
    # Same referral, same checks, in every arm.
    by_ref: dict[str, dict[str, set[tuple[str, ...]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for row in rows:
        by_ref[row["ref"]][row["arm"]].add(tuple(row.get("checks") or ()))
    differing = {}
    for ref, per_arm in by_ref.items():
        variants = {v for vs in per_arm.values() for v in vs}
        if len(variants) > 1:
            differing[ref] = {arm: len(vs) for arm, vs in per_arm.items()}
    out["checks"] = {
        "referrals": len(by_ref),
        "identical_across_arms_and_runs": not differing,
        "referrals_with_differing_checks": differing,
    }
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
