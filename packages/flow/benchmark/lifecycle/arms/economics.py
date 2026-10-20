"""Total lifecycle cost per 1,000 verified items, per arm, from the results ledgers alone.

Every number comes from ledger rows the harness wrote (items, operator
requests, setup and maintenance phases, drift events) priced with the soak's
versioned operator cost model and this directory's dated ``prices.json``.
Nothing is re-measured here, so ``python -m lifecycle.arms.economics <run dir>``
recomputes the report from committed data.

Definitions:

- *Verified item*: the oracle's final read says the item is right (the
  intended chart exactly once, or no new chart when none is wanted), whoever
  did it. Items a person did by hand count, and so does their time: this is
  the cost of getting the work done right, not of automating it.
- *Automated*: verified and not done by hand.
- *Setup*: one-time, per workflow. People's minutes (attended phase time plus
  the cost model's fixed charges for teach, compile review and admission) and
  model dollars.
- *Per item*: model dollars, nominal compute, and operator minutes (approvals,
  stops, questions, reconciliations, items done by hand).
- *Maintenance*: after a UI change. Model dollars spent repairing or
  re-authoring, and people's minutes reviewing the repair.
- *Silent-wrong cleanup*: each silent wrong is charged, as a lower bound,
  the people time to find and fix it later (one reconcile plus one manual
  entry, plus the interrupt charge each). Its clinical or business harm is
  not priced; the silent-wrong rate is reported beside every total.
- Projection to 1,000 verified items: setup once, per-item costs scaled by
  1,000 / verified, maintenance per UI change times the assumed changes per
  1,000 items (``prices.json`` drift rate).
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from . import budget

try:
    from ..cost_model import MODELS
    from ..metrics import wilson
except ImportError:  # pragma: no cover - the harness is not checked in yet
    MODELS = {}

    def wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:  # type: ignore[misc]
        import math

        if n == 0:
            return None
        p = k / n
        d = 1 + z * z / n
        c = p + z * z / (2 * n)
        r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (round((c - r) / d, 4), round((c + r) / d, 4))


FIXED_PHASES = {"teach": "teach_prep_s", "compile": "compile_review_s", "admit": "admit_s"}


def read_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in paths:
        for line in p.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _cost_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for r in rows:
        if r.get("type") == "run_start" and r.get("cost_model"):
            return r["cost_model"]
    if MODELS:
        return next(iter(MODELS.values())).to_dict()
    raise ValueError("no cost model in the ledger and none importable")


def _phase_human_s(cm: dict[str, Any], phase: dict[str, Any]) -> float:
    fixed = cm.get(FIXED_PHASES.get(phase.get("phase", ""), ""), 0.0)
    return float(phase.get("attended_s", 0.0) or 0.0) + float(fixed or 0.0)


def arm_economics(rows: list[dict[str, Any]], arm: str, cm: dict[str, Any], labor: dict[str, Any],
                  drift_per_1000: float) -> dict[str, Any]:
    mine = [r for r in rows if r.get("arm") == arm]
    items = [r for r in mine if r["type"] == "item_result"]
    ops = [r for r in mine if r["type"] == "operator"]
    setup = [r for r in mine if r["type"] == "phase" and r.get("stage") == "setup"]
    maint = [r for r in mine if r["type"] == "phase" and r.get("stage") == "maintenance"]
    for it in items:
        for p in (it.get("detail") or {}).get("maintenance_phases", []) or []:
            maint.append({**p, "stage": "maintenance", "item_id": it["item_id"]})
    drifts = [r for r in mine if r["type"] == "drift_applied"]
    describe = next((r.get("describe") for r in mine if r["type"] == "arm_start"), {}) or {}

    n = len(items)
    by_hand = {r["item_id"] for r in ops if r.get("action") == "do_by_hand" and r.get("item_id")}
    verified = [r for r in items if r.get("final_oracle") == "correct"]
    automated = [r for r in verified if r["item_id"] not in by_hand]
    outcomes = Counter(r["outcome"] for r in items)
    wanted = [r for r in items if r.get("chart_wanted")]
    clean = [r for r in wanted if r.get("exception") in ("clean", "missing_optional", "malformed_dob",
                                                         "malformed_case", "ambiguous_new_person")]

    setup_human_s = sum(_phase_human_s(cm, p) for p in setup)
    setup_model_usd = sum(float(p.get("cost_usd", 0.0) or 0.0) for p in setup)
    item_model_usd = sum(float(r.get("cost_usd", 0.0) or 0.0) for r in items)
    compute_usd = sum(float((r.get("detail") or {}).get("compute_usd") or budget.compute_usd(r.get("wall_s", 0.0)))
                      for r in items)
    item_op_s = sum(float(r["charged_s"]) for r in ops if r["kind"] != "repair_review")
    review_s = sum(float(r["charged_s"]) for r in ops if r["kind"] == "repair_review")
    maint_human_s = review_s + sum(_phase_human_s(cm, p) for p in maint)
    maint_model_usd = sum(float(p.get("cost_usd", 0.0) or 0.0) for p in maint)
    model_calls = sum(int(r.get("model_calls", 0) or 0) for r in items)

    op_rate = labor["operator_usd_per_hour"] / 3600.0
    sw = outcomes.get("silent_wrong", 0)
    cleanup_s = sw * (float(cm.get("reconcile_s", 0.0)) + float(cm.get("manual_entry_s", 0.0))
                      + 2 * float(cm.get("interrupt_s", 0.0)))
    v = len(verified)
    per_v = (1000.0 / v) if v else float("nan")
    items_per_1000v = n * per_v
    d = len(drifts)
    maint_per_event_usd = (maint_model_usd + maint_human_s * op_rate) / d if d else 0.0
    maint_per_event_min = maint_human_s / 60.0 / d if d else 0.0
    proj_events = drift_per_1000 * items_per_1000v / 1000.0 if v else float("nan")

    proj = {
        "setup_usd": setup_model_usd + setup_human_s * op_rate,
        "setup_model_usd": setup_model_usd,
        "setup_human_min": setup_human_s / 60.0,
        "item_model_usd": item_model_usd * per_v,
        "compute_usd": compute_usd * per_v,
        "operator_min": item_op_s / 60.0 * per_v,
        "operator_usd": item_op_s * op_rate * per_v,
        "maintenance_events": proj_events,
        "maintenance_usd": maint_per_event_usd * proj_events,
        "maintenance_min": maint_per_event_min * proj_events,
        "silent_wrong": sw * per_v,
        "cleanup_min": cleanup_s / 60.0 * per_v,
        "cleanup_usd": cleanup_s * op_rate * per_v,
    }
    proj["total_usd"] = (proj["setup_usd"] + proj["item_model_usd"] + proj["compute_usd"]
                         + proj["operator_usd"] + proj["maintenance_usd"] + proj["cleanup_usd"])
    proj["total_human_min"] = (proj["setup_human_min"] + proj["operator_min"] + proj["maintenance_min"]
                               + proj["cleanup_min"])
    walls = sorted(float(r["wall_s"]) for r in automated)
    return {
        "arm": arm,
        "describe": describe,
        "items": n,
        "verified": v,
        "automated": len(automated),
        "by_hand": len(by_hand),
        "outcomes": dict(outcomes),
        "silent_wrong": outcomes.get("silent_wrong", 0),
        "silent_wrong_rate": round(outcomes.get("silent_wrong", 0) / n, 4) if n else None,
        "silent_wrong_ci95": wilson(outcomes.get("silent_wrong", 0), n),
        "false_stop": outcomes.get("false_stop", 0),
        "false_stop_rate": round(outcomes.get("false_stop", 0) / len(clean), 4) if clean else None,
        "false_stop_denominator": len(clean),
        "false_stop_ci95": wilson(outcomes.get("false_stop", 0), len(clean)),
        "drift_events": d,
        "measured": {
            "setup_human_min": round(setup_human_s / 60.0, 2),
            "setup_model_usd": round(setup_model_usd, 4),
            "item_model_usd": round(item_model_usd, 4),
            "model_calls": model_calls,
            "compute_usd": round(compute_usd, 4),
            "operator_min": round(item_op_s / 60.0, 2),
            "maintenance_human_min": round(maint_human_s / 60.0, 2),
            "maintenance_model_usd": round(maint_model_usd, 4),
            "maintenance_per_event_usd": round(maint_per_event_usd, 4),
            "maintenance_per_event_min": round(maint_per_event_min, 2),
            "median_wall_s_automated": round(statistics.median(walls), 2) if walls else None,
            "p90_wall_s_automated": round(walls[int(0.9 * (len(walls) - 1))], 2) if walls else None,
            "per_item_model_usd": round(item_model_usd / n, 4) if n else None,
        },
        "per_1000_verified": {k: (round(x, 2) if isinstance(x, float) else x) for k, x in proj.items()},
    }


AXES = [
    # (key path, label, lower is better)
    (("per_1000_verified", "total_usd"), "Total $ per 1,000 verified", True),
    (("per_1000_verified", "setup_usd"), "Setup $ (people + model)", True),
    (("measured", "per_item_model_usd"), "Model $ per item", True),
    (("per_1000_verified", "operator_min"), "Operator minutes per 1,000 verified", True),
    (("measured", "maintenance_per_event_usd"), "Maintenance $ per UI change", True),
    (("silent_wrong_rate",), "Silent-wrong rate", True),
    (("false_stop_rate",), "False-stop rate", True),
    (("measured", "median_wall_s_automated"), "Median seconds per automated item", True),
]


def _get(d: dict[str, Any], path: tuple[str, ...]) -> Any:
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def winners(arms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for path, label, lower in AXES:
        vals = [(a["arm"], _get(a, path)) for a in arms if isinstance(_get(a, path), (int, float))]
        vals = [(k, v) for k, v in vals if v == v]  # drop NaN
        if not vals:
            continue
        best = min(v for _, v in vals) if lower else max(v for _, v in vals)
        best_arms = [k for k, v in vals if v == best]
        sw = {a["arm"]: a["silent_wrong"] for a in arms}
        caveat = [k for k in best_arms if sw.get(k)] if path[-1] != "silent_wrong_rate" else []
        out.append({"axis": label, "best": best_arms, "value": best, "all": dict(vals),
                    "best_has_silent_wrong": caveat})
    return out


def compute(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cm = _cost_model(rows)
    table = budget.prices()
    labor = table["labor"]
    drift_rate = float(table["drift"]["events_per_1000_items"])
    arms = []
    for name in dict.fromkeys(r["arm"] for r in rows if r.get("type") == "arm_start"):
        arms.append(arm_economics(rows, name, cm, labor, drift_rate))
    bands = {}
    for f in (0.5, 2.0):
        scaled = {**labor, "operator_usd_per_hour": labor["operator_usd_per_hour"] * f}
        bands[f"labor_x{f:g}"] = {a["arm"]: arm_economics(rows, a["arm"], cm, scaled, drift_rate)["per_1000_verified"]["total_usd"]
                                  for a in arms}
    return {
        "prices_as_of": table["as_of"],
        "labor": labor,
        "drift_events_per_1000_items": drift_rate,
        "cost_model": cm.get("version"),
        "arms": arms,
        "winners": winners(arms),
        "bands": bands,
    }


def _fmt(x: Any, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return str(x)


def render(result: dict[str, Any]) -> str:
    arms = result["arms"]
    lines = ["| Arm | Items | Verified | Automated | Silent wrong | False stops | Setup $ | Model $/item | Operator min /1k | Maint. $/change | Total $ /1k verified |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in arms:
        m, p = a["measured"], a["per_1000_verified"]
        lines.append(
            f"| {a['arm']} | {a['items']} | {a['verified']} | {a['automated']} | "
            f"{a['silent_wrong']} ({_fmt(a['silent_wrong_rate'], 3)}) | "
            f"{a['false_stop']} of {a['false_stop_denominator']} | {_fmt(p['setup_usd'])} | "
            f"{_fmt(m['per_item_model_usd'], 3)} | {_fmt(p['operator_min'], 0)} | "
            f"{_fmt(m['maintenance_per_event_usd'])} | {_fmt(p['total_usd'], 0)} |"
        )
    lines += ["", "| Axis | Best | Value |", "|---|---|---|"]
    for w in result["winners"]:
        flag = " (has silent wrongs)" if w.get("best_has_silent_wrong") else ""
        lines.append(f"| {w['axis']} | {', '.join(w['best'])}{flag} | {_fmt(w['value'], 3)} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    run_dir = Path(args[0])
    ledgers = sorted(run_dir.glob("**/ledger.jsonl"))
    result = compute(read_rows(ledgers))
    (run_dir / "economics.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
