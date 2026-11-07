"""Aggregate the ledger into results.json and the tables in README.md.

    PYTHONPATH=. python benchmark/onboarding_effort/report.py [--ledger PATH]

For each (workflow, installation) the counted run is the LAST complete run in
the ledger (one with an ``end`` row). Earlier runs stay in the ledger and are
listed as superseded with the reason in the report.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
COUNTS = ("demos_scripted", "demos_agent", "corrections", "questions", "config_lines",
          "commands", "failed_commands", "tokens_in", "tokens_out")
SUCCESS = {"endpoint", "zero-touch", "repair-only", "re-teach"}


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def runs(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[list[dict[str, Any]]]]:
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_run[r.get("run") or f"{r['workflow']}/{r['installation']}"].append(r)
    out: dict[tuple[str, str], list[list[dict[str, Any]]]] = defaultdict(list)
    for rs in by_run.values():
        rs.sort(key=lambda r: r["ts"])
        out[(rs[0]["workflow"], rs[0]["installation"])].append(rs)
    for v in out.values():
        v.sort(key=lambda rs: rs[0]["ts"])
    return out


def summarize_run(rs: list[dict[str, Any]]) -> dict[str, Any]:
    s: dict[str, Any] = {k: sum(int(r.get(k) or 0) for r in rs) for k in COUNTS}
    s["engineer_min"] = round(sum(float(r.get("engineer_min") or 0) for r in rs), 1)
    s["engineer_hours"] = round(s["engineer_min"] / 60, 2)
    s["machine_min"] = round(sum(float(r.get("machine_s") or 0) for r in rs) / 60, 1)
    s["model_usd"] = round(sum(float(r.get("model_usd") or 0) for r in rs), 4)
    end = next((r for r in reversed(rs) if r["phase"] == "end"), None)
    s["outcome"] = end["action"] if end else "incomplete"
    s["success"] = s["outcome"] in SUCCESS
    held = [r for r in rs if r["phase"] == "heldout"]
    last = held[-1]["detail"] if held else {}
    s["heldout"] = {k: last.get(k) for k in ("n", "oracle_verified", "engine_verified",
                                              "silent_wrong", "wrong", "missing", "model_calls")}
    s["behavioral"] = last.get("behavioral")
    s["heldout_batches"] = len(held)
    s["qualify_attempts"] = sum(1 for r in rs if r["phase"] == "qualify")
    s["stuck"] = [
        {"where": r["action"], "reason": r["detail"].get("reason", ""), "remedy": r["detail"].get("remedy", "")}
        for r in rs if r["phase"] == "stuck"
    ]
    s["strategy_reached"] = [r["action"] for r in rs if r["phase"] == "correct"]
    s["run"] = rs[0].get("run")
    s["engine_commit"] = rs[0].get("engine_commit")
    s["manifest_sha256"] = rs[0].get("manifest_sha256")
    s["mean_load1"] = round(statistics.mean(float(r.get("load1") or 0) for r in rs), 1)
    return s


def quartiles(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    v = sorted(values)
    if len(v) == 1:
        return {"n": 1, "median": v[0], "q1": v[0], "q3": v[0], "iqr": 0.0}
    q = statistics.quantiles(v, n=4, method="inclusive")
    return {"n": len(v), "median": statistics.median(v), "q1": round(q[0], 2),
            "q3": round(q[2], 2), "iqr": round(q[2] - q[0], 2), "min": v[0], "max": v[-1]}


def build(ledger: Path) -> dict[str, Any]:
    from benchmark.onboarding_effort.registry import get, pool_ids

    allruns = runs(load(ledger))
    table: dict[str, dict[str, Any]] = {}
    superseded = []
    for wid in pool_ids():
        wf = get(wid)
        entry: dict[str, Any] = {"app": wf.app, "shape": wf.shape, "policy": wf.policy,
                                 "second_installation": wf.second_installation,
                                 "second_kind": wf.second_kind}
        for inst in ("first", "second"):
            rr = [rs for rs in allruns.get((wid, inst), []) if any(r["phase"] == "end" for r in rs)]
            if rr:
                entry[inst] = summarize_run(rr[-1])
                superseded += [{"workflow": wid, "installation": inst, "run": rs[0].get("run")}
                               for rs in rr[:-1]]
        table[wid] = entry
    firsts = [e["first"] for e in table.values() if "first" in e]
    ok = [f for f in firsts if f["success"]]
    agg = {
        "workflows": len(table),
        "first_attempted": len(firsts),
        "first_reached_endpoint": len(ok),
    }
    for key in ("engineer_min", "demos_scripted", "demos_agent", "corrections", "questions",
                "config_lines", "machine_min"):
        agg[f"first_{key}_endpoint_only"] = quartiles([f[key] for f in ok])
        agg[f"first_{key}_all"] = quartiles([f[key] for f in firsts])
    seconds = [e["second"] for e in table.values() if "second" in e]
    classes: dict[str, int] = defaultdict(int)
    for s in seconds:
        classes[s["outcome"] if s["outcome"] in SUCCESS else "failed"] += 1
    agg["second_attempted"] = len(seconds)
    agg["second_classes"] = dict(classes)
    for key in ("engineer_min", "demos_scripted", "corrections", "machine_min"):
        agg[f"second_{key}_all"] = quartiles([s[key] for s in seconds])
    agg["silent_wrong_total"] = sum(int((e.get(i) or {}).get("heldout", {}).get("silent_wrong") or 0)
                                    for e in table.values() for i in ("first", "second"))
    agg["model_usd_total"] = round(sum((e.get(i) or {}).get("model_usd", 0) for e in table.values()
                                       for i in ("first", "second")), 4)
    return {"aggregate": agg, "workflows": table, "superseded_runs": superseded}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path, default=HERE / "results" / "ledger.jsonl")
    ap.add_argument("--out", type=Path, default=HERE / "results" / "results.json")
    a = ap.parse_args()
    result = build(a.ledger)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result["aggregate"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(HERE.parents[1]))
    raise SystemExit(main())
