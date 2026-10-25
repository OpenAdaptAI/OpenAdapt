"""Back-test: the generated duplicate rule against #1516's record check.

    python -m generated_parts.dup_backtest --dup-check <checkout of #1516>

Uses #1516's own streams (``benchmark/dup_check/measure.py``): the soak's 55
items and the 500-item seed-7 stream, each with the planted true duplicates,
676 items with 223 true duplicates on the long stream. The generated rule is
calibrated only on the charts that exist before the stream starts (the
system's own data) with the generic error model in
``openadapt_flow/generate/identity.py``; it never sees the stream, the
planted kinds or their labels. Both checks then meet the same items in the
same order, with each earlier item's right outcome added to the table.

No app, no model calls.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


_DUMP = r"""
import dataclasses, json, sys
sys.path = [p for p in sys.path if "openadapt-evals" not in p]
from benchmark.dup_check import measure
long_items = int(sys.argv[1])
out = {}
for name, (existing, items) in (
    ("soak-seed7-55", measure.from_ledger(measure.SOAK / "workload.jsonl")),
    (f"seed7-{long_items}", measure.from_generator(7, long_items, 10)),
):
    extra, extra_items = measure.extra_existing(existing)
    profile = existing + extra
    stream = measure.plant(existing, items + extra_items, 7)
    hand = measure.measure(profile, stream)
    out[name] = {
        "profile": profile,
        "items": [dataclasses.asdict(i) for i in stream],
        "duplicate_kinds": sorted(measure.DUPLICATE_KINDS),
        "hand_rows": hand["rows"],
    }
json.dump(out, sys.stdout)
"""


def dump_streams(checkout: Path, long_items: int) -> dict[str, Any]:
    """#1516's streams and its record check's decisions, from its own checkout."""

    import os
    import subprocess

    flow = checkout / "packages" / "flow"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(flow), str(checkout / "packages" / "types")]
        ),
    }
    out = subprocess.run(
        [sys.executable, "-c", _DUMP, str(long_items)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(flow),
        check=True,
    ).stdout
    return json.loads(out)


def _iso(value: str) -> str:
    import re

    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else (value or "")


def run(stream: dict[str, Any], *, seed: int, budget: float = 0.005) -> dict[str, Any]:
    from openadapt_flow.generate.identity import generate_rule

    profile = [dict(p, DOB=_iso(p.get("DOB", ""))) for p in stream["profile"]]
    started = time.perf_counter()
    # The table a new record meets grows to the stream's size: the known
    # operational volume, not any outcome.
    expected = len(profile) + sum(
        1 for i in stream["items"] if i["intended"] is not None
    )
    rule = generate_rule(
        profile, seed=seed, expected_records=expected, false_stop_budget=budget
    )
    gen_s = time.perf_counter() - started
    table: list[dict] = [dict(p) for p in profile]
    kinds = set(stream["duplicate_kinds"])
    hand = {r["item"]: r for r in stream["hand_rows"]}
    rows = []
    for item in stream["items"]:
        table += [dict(p, DOB=_iso(p.get("DOB", ""))) for p in item["before"]]
        params = {
            k: (v.strip() if isinstance(v, str) else v)
            for k, v in item["params"].items()
        }
        params["DOB"] = _iso(params.get("DOB", ""))
        decision, _why = rule.decide(params, table)
        rows.append(
            {
                "item": item["item_id"],
                "exception": item["exception"],
                "right": "new chart"
                if item["intended"] is not None
                else (
                    "no chart: duplicate" if item["exception"] in kinds else "no chart"
                ),
                "generated": decision,
                "record": hand[item["item_id"]]["record"],
            }
        )
        if item["intended"] is not None:
            table.append(
                dict(item["intended"], DOB=_iso(item["intended"].get("DOB", "")))
            )
    return {"rule": rule, "rows": rows, "generation_s": round(gen_s, 3)}


def summarize(rows: list[dict], key: str, stops: Any) -> dict:
    new = [r for r in rows if r["right"] == "new chart"]
    dups = [r for r in rows if r["right"] == "no chart: duplicate"]
    false = [r for r in new if stops(r)]
    missed = [r for r in dups if not stops(r)]

    def count(rs: list[dict]) -> dict:
        out: dict[str, int] = {}
        for r in rs:
            out[r["exception"]] = out.get(r["exception"], 0) + 1
        return dict(sorted(out.items()))

    return {
        "items": len(rows),
        "new_chart_items": len(new),
        "true_duplicates": len(dups),
        "false_stops": len(false),
        "false_stops_per_100_items": round(100 * len(false) / max(1, len(rows)), 1),
        "missed_duplicates": len(missed),
        "missed_by_exception": count(missed),
        "false_stops_by_exception": count(false),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dup-check", type=Path, required=True)
    parser.add_argument("--long-items", type=int, default=500)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--budgets",
        default="0.005,0.01,0.02,0.05",
        help="estimated false stops per new record the rule may spend (a sweep)",
    )
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()
    streams = dump_streams(args.dup_check, args.long_items)
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    budgets = [float(b) for b in args.budgets.split(",")]
    for name, stream in streams.items():
        for budget in budgets:
            result = run(stream, seed=args.seed, budget=budget)
            rows = result["rows"]
            report[f"{name}@{budget}"] = {
                "budget": budget,
                "profile_records": len(stream["profile"]),
                "generation_s": result["generation_s"],
                "rule": result["rule"].as_dict(),
                "rule_text": result["rule"].describe(),
                "generated": summarize(
                    rows, "generated", lambda r: r["generated"] != "none"
                ),
                "hand_built_record_check": summarize(
                    rows, "record", lambda r: r["record"] != "none"
                ),
            }
            g = report[f"{name}@{budget}"]["generated"]
            h = report[f"{name}@{budget}"]["hand_built_record_check"]
            print(
                f"{name} budget={budget}: generated missed {g['missed_duplicates']}/"
                f"{g['true_duplicates']}, false stops {g['false_stops']} "
                f"({g['false_stops_per_100_items']}/100); hand-built missed "
                f"{h['missed_duplicates']}, false stops {h['false_stops']} "
                f"({h['false_stops_per_100_items']}/100)"
            )
    (args.out / "duplicates.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
