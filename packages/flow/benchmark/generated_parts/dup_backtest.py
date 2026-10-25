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


_REFERENCE = r"""
import json, sys
sys.path = [p for p in sys.path if "openadapt-evals" not in p]
from benchmark.dup_check import measure
from openadapt_flow.runtime.checks.duplicate_record import TableSource, check_duplicate
doc = json.load(sys.stdin)
profile = doc["profile"]
out = []
for case in doc["cases"]:
    table = profile if case["skip"] is None else profile[: case["skip"]] + profile[case["skip"] + 1 :]
    records = [measure.record_of(i, p) for i, p in enumerate(table)]
    decision = check_duplicate(TableSource(records), case["new"]).decision
    out.append(decision != "none")
json.dump(out, sys.stdout)
"""


def reference_on(checkout: Path, calset: Any) -> Any:
    """#1516's record check on ``calset``, as ``decide(new, table) -> bool``."""

    import os
    import subprocess

    cases = [{"new": new, "skip": None} for _k, new, _s in calset.positives]
    cases += [{"new": calset.profile[i], "skip": i} for i in calset.negatives]
    flow = checkout / "packages" / "flow"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(flow), str(checkout / "packages" / "types")]
        ),
    }
    decisions = json.loads(
        subprocess.run(
            [sys.executable, "-c", _REFERENCE],
            input=json.dumps({"profile": calset.profile, "cases": cases}),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(flow),
            check=True,
        ).stdout
    )
    lookup: dict[tuple[str, int], bool] = {}
    for case, stop in zip(cases, decisions):
        key = (
            json.dumps(case["new"], sort_keys=True, default=str),
            len(calset.profile) - (case["skip"] is not None),
        )
        lookup[key] = stop

    def decide(new: dict, table: list) -> bool:
        return lookup[(json.dumps(new, sort_keys=True, default=str), len(table))]

    return decide


def _same_first_and_dob(a: dict, b: dict) -> bool:
    return str(a.get("fname", "")).casefold() == str(
        b.get("fname", "")
    ).casefold() and a.get("DOB") == b.get("DOB")


def run(
    stream: dict[str, Any],
    *,
    seed: int,
    budget: float,
    name_variants: bool,
    checkout: Path,
    assumed_labels: bool = False,
) -> dict[str, Any]:
    from openadapt_flow.generate.identity import adopt, generate_rule

    profile = [dict(p, DOB=_iso(p.get("DOB", ""))) for p in stream["profile"]]
    # The table a new record meets grows to the stream's size: the known
    # operational volume, not any outcome.
    expected = len(profile) + sum(
        1 for i in stream["items"] if i["intended"] is not None
    )
    started = time.perf_counter()
    rule = generate_rule(
        profile,
        seed=seed,
        expected_records=expected,
        false_stop_budget=budget,
        name_variants=name_variants,
        # ASSUMED labels, not a person's: two existing charts with the same
        # first name and date of birth are judged one person. A sensitivity
        # arm only.
        labeller=(
            (lambda item: _same_first_and_dob(*item["records"]))
            if assumed_labels
            else None
        ),
    )
    gen_s = time.perf_counter() - started
    adoption = adopt(rule, reference_on(checkout, rule.calibration_set))
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
    return {
        "rule": rule,
        "rows": rows,
        "generation_s": round(gen_s, 3),
        "adoption": adoption,
    }


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
        default="0.005,0.01,0.02,0.05,0.1",
        help="estimated false stops per new record the rule may spend (a sweep)",
    )
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()
    streams = dump_streams(args.dup_check, args.long_items)
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    budgets = [float(b) for b in args.budgets.split(",")]
    for name, stream in streams.items():
        for variants, labels in ((False, False), (True, False), (True, True)):
            for budget in budgets:
                result = run(
                    stream,
                    seed=args.seed,
                    budget=budget,
                    name_variants=variants,
                    checkout=args.dup_check,
                    assumed_labels=labels,
                )
                rows = result["rows"]
                key = f"{name}|variants={variants}|labels={labels}|budget={budget}"
                adoption = result["adoption"]
                report[key] = {
                    "stream": name,
                    "name_variants": variants,
                    "assumed_labels": labels,
                    "budget": budget,
                    "profile_records": len(stream["profile"]),
                    "generation_s": result["generation_s"],
                    "rule": result["rule"].as_dict(),
                    "rule_text": result["rule"].describe(),
                    "adoption": {
                        "use": adoption.use,
                        "why": adoption.why,
                        "calibration_generated": adoption.generated,
                        "calibration_reference": adoption.reference,
                    },
                    "generated": summarize(
                        rows, "generated", lambda r: r["generated"] != "none"
                    ),
                    "hand_built_record_check": summarize(
                        rows, "record", lambda r: r["record"] != "none"
                    ),
                }
                g = report[key]["generated"]
                h = report[key]["hand_built_record_check"]
                print(
                    f"{key}: test missed {g['missed_duplicates']}/{g['true_duplicates']} "
                    f"false stops {g['false_stops']} ({g['false_stops_per_100_items']}/100) "
                    f"| hand missed {h['missed_duplicates']} fs {h['false_stops']} "
                    f"| calib gen missed {adoption.generated['missed']} ref "
                    f"{adoption.reference['missed'] if adoption.reference else '-'} "
                    f"of {adoption.generated['duplicates']} -> use {adoption.use}",
                    flush=True,
                )
    (args.out / "duplicates.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
