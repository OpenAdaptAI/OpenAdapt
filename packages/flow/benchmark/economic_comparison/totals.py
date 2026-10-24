"""Lifecycle totals over 10, 100 and 1,000 items, per arm, from raw results.

    cd packages/flow/benchmark
    python -m economic_comparison.totals            # writes results/totals.json and results/TABLE.md

Measured, per arm, from the raw rows (nothing here is re-run):

- per-item time, model cost and outcome on the control block (no UI change,
  no server fault);
- the excess people time and model cost of each server-fault item;
- the excess people time and model cost of each UI change (one block of
  items with that change on), including any repair or re-authoring the arm
  did, any silent wrongs it made under the change, and a person's fix when
  the arm had not recovered by the end of the block;
- setup: the agent's measured authoring time and cost, plus the people time
  in ``assumptions.json``.

People time is priced from ``assumptions.json`` (cost model V1 and the labor
rates), never measured; every total is also given at half and double it.

Totals for N items = setup + N x (control per-item cost)
+ N x (faults per item) x (mean excess per fault item)
+ N x (changes per item) x (mean excess per UI change).
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
SS = BENCH / "silent_saves" / "results" / "1.1.0"
ASSUME = json.loads((HERE / "assumptions.json").read_text())
SIZES = (10, 100, 1000)

#: MockMed arms: (name, kind, items.csv, outcome column, setup key, label)
MOCKMED_ARMS: list[dict[str, Any]] = [
    {"arm": "cua_alone", "kind": "agent", "dir": "claude-computer-use+readback/2026-10-19",
     "outcome": "outcome_alone", "setup": "cua_alone",
     "label": "4. Computer-use agent, screenshots only (Claude Sonnet 5.5)"},
    {"arm": "agent_tools", "kind": "agent", "dir": "agent-tools/2026-10-24", "outcome": "outcome",
     "setup": "agent_tools", "label": "1. Agent with structured tools and record read-back (Claude Sonnet 5.5)"},
    {"arm": "agent_script", "kind": "script", "dir": "agent-script/2026-10-24", "outcome": "outcome",
     "setup": "agent_script", "label": "2. Agent-written Playwright script with read-back and duplicate guard, agent-maintained"},
    {"arm": "openadapt_program", "kind": "program", "dir": "openadapt-program/2026-10-24", "outcome": "outcome",
     "setup": "openadapt", "label": "3. OpenAdapt admitted program, read-back on, no model calls"},
    {"arm": "openadapt_self_repair", "kind": "program", "dir": "self-repair/2026-10-19", "outcome": "outcome",
     "setup": "openadapt", "label": "3r. OpenAdapt program with self-repair (engine of 2026-10-19)"},
    {"arm": "cua_plus_openadapt_readback", "kind": "agent", "dir": "claude-computer-use+readback/2026-10-19",
     "outcome": "outcome", "setup": "cua_alone",
     "label": "4 + OpenAdapt read-back: the same agent runs, each claim gated on verify-run"},
]


def _people_s(steps: list[str], manual_key: str) -> float:
    op = ASSUME["operator_s"]
    total = 0.0
    for s in steps:
        total += op[manual_key] if s == "manual_entry" else op[s]
    if steps and "interrupt" not in steps:
        total += op["interrupt"]
    return total


def operator_s(row: dict[str, Any], outcome: str, manual_key: str) -> float:
    """People seconds after one item, from what the arm reported and the store."""

    per = ASSUME["per_outcome"]
    save_possible = row["expect"] == "save" and row["fault"] == "ok"
    if outcome == "correct":
        return 0.0
    if outcome == "safe_stop":
        key = "claimed_failure_save_possible" if save_possible else "claimed_failure_no_save_possible"
        return _people_s(per[key], manual_key)
    return _people_s(per[outcome], manual_key)


def _rows(arm: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    p = SS / arm["dir"] / "items.csv"
    if not p.is_file():
        return None
    rows = sorted(csv.DictReader(p.open()), key=lambda r: int(r["item"]))
    return rows if len(rows) == 100 else None


def _maint_events(row: dict[str, Any]) -> tuple[float, float]:
    """(engineer s, operator s) for the repair events an item logged."""

    eng = op = 0.0
    m = ASSUME["maintenance_people_min"]
    for e in json.loads(row["human_events"] or "[]"):
        if e.get("kind") == "script_review" and e.get("phase") == "maintenance":
            eng += m["script_fix_review_engineer"] * 60
        elif e.get("kind") == "repair_approval":
            op += ASSUME["operator_s"]["repair_review"] + ASSUME["operator_s"]["interrupt"]
    return eng, op


def mockmed_arm(arm: dict[str, Any]) -> Optional[dict[str, Any]]:
    rows = _rows(arm)
    if rows is None:
        return None
    oc = arm["outcome"]
    rate = ASSUME["labor_usd_per_hour"]
    cpu = ASSUME["compute_usd_per_hour"] / 3600.0

    def cost(r: dict[str, Any], *, setup_item: bool = False) -> dict[str, float]:
        model = float(r["cost_usd"] or 0)
        events = json.loads(r["human_events"] or "[]")
        if setup_item or any(e.get("phase") == "setup" for e in events):
            model = 0.0  # authoring cost is setup, counted there
        eng, op_maint = _maint_events(r)
        op = operator_s(r, r[oc], "manual_entry_mockmed") + op_maint
        return {"model_usd": model, "operator_s": op, "engineer_s": eng,
                "compute_usd": float(r["item_s"] or 0) * cpu}

    def usd(c: dict[str, float]) -> float:
        return (c["model_usd"] + c["compute_usd"] + c["operator_s"] / 3600 * rate["operator"]
                + c["engineer_s"] / 3600 * rate["engineer"])

    control = [r for r in rows if r["release"] == "baseline" and r["fault"] == "ok"]
    steady = {k: statistics.mean(cost(r)[k] for r in control) for k in ("model_usd", "operator_s", "engineer_s", "compute_usd")}
    steady_usd = usd(steady)
    st_machine = steady["model_usd"] + steady["compute_usd"]

    setup_rows = [r for r in rows if any(e.get("phase") == "setup" for e in json.loads(r["human_events"] or "[]"))]
    setup_model = sum(float(r["cost_usd"] or 0) for r in setup_rows)
    setup_agent_min = sum(float(r["item_s"] or 0) for r in setup_rows) / 60
    sp = ASSUME["setup_people_min"][arm["setup"]]
    setup = {"model_usd": setup_model, "agent_min": setup_agent_min,
             "operator_min": sp["operator"], "engineer_min": sp["engineer"]}
    setup["usd"] = setup_model + sp["operator"] / 60 * rate["operator"] + sp["engineer"] / 60 * rate["engineer"]

    faults = [r for r in rows if r["fault"] != "ok"]
    fault_rows = []
    for r in faults:
        c = cost(r)
        fault_rows.append({"item": int(r["item"]), "fault": r["fault"], "outcome": r[oc],
                           "excess_usd": usd(c) - steady_usd,
                           "excess_machine_usd": c["model_usd"] + c["compute_usd"] - st_machine,
                           "excess_people_usd": _split((c["operator_s"] - steady["operator_s"]) / 60, (c["engineer_s"] - steady["engineer_s"]) / 60),
                           "excess_people_min": (c["operator_s"] + c["engineer_s"] - steady["operator_s"] - steady["engineer_s"]) / 60})

    m = ASSUME["maintenance_people_min"]
    changes = []
    for rel in dict.fromkeys(r["release"] for r in rows if r["release"] != "baseline"):
        block = [r for r in rows if r["release"] == rel and r["fault"] == "ok"]
        outs = [r[oc] for r in block]
        cs = [cost(r) for r in block]
        op_s = sum(c["operator_s"] for c in cs)
        eng_s = sum(c["engineer_s"] for c in cs)
        model = sum(c["model_usd"] for c in cs)
        comp = sum(c["compute_usd"] for c in cs)
        expect_save = all(r["expect"] == "save" for r in block)
        last_ok = outs[-1] == "correct" if expect_save else outs[-1] in ("safe_stop",)
        fix = ""
        if not last_ok:
            # Still failing when the block ended: a person fixes it once.
            if arm["kind"] == "program":
                op_s += m["openadapt_reteach_operator"] * 60
                fix = "re-teach"
            elif arm["kind"] == "script":
                eng_s += m["script_fix_by_hand_engineer"] * 60
                fix = "engineer fixes the script"
            else:
                eng_s += m["agent_prompt_fix_engineer"] * 60
                fix = "engineer adjusts the prompt"
        n = len(block)
        excess_usd = (model + comp + op_s / 3600 * rate["operator"] + eng_s / 3600 * rate["engineer"]) - n * steady_usd
        changes.append({
            "change": rel, "drift": block[0]["drift"], "items": n,
            "outcomes": {o: outs.count(o) for o in sorted(set(outs))},
            "silent_wrong": outs.count("silent_wrong"),
            "recovered_by_block_end": last_ok, "people_fix_added": fix,
            "operator_min": round(op_s / 60, 2), "engineer_min": round(eng_s / 60, 2),
            "model_usd": round(model, 4),
            "excess_people_min": round((op_s + eng_s) / 60 - n * (steady["operator_s"] + steady["engineer_s"]) / 60, 2),
            "excess_usd": round(excess_usd, 3),
            "excess_machine_usd": model + comp - n * st_machine,
            "excess_people_usd": _split(op_s / 60 - n * steady["operator_s"] / 60, eng_s / 60 - n * steady["engineer_s"] / 60),
        })

    outcomes = [r[oc] for r in rows]
    walls = sorted(float(r["item_s"]) for r in control)
    loads = [float(r["host_load_1m"] or 0) for r in control]
    measured = {
        "items": len(rows),
        "verified_of_saveable": sum(1 for r in rows if r[oc] == "correct" and r["expect"] == "save" and r["fault"] == "ok"),
        "saveable": sum(1 for r in rows if r["expect"] == "save" and r["fault"] == "ok"),
        "silent_wrong": outcomes.count("silent_wrong"),
        "flagged_wrong": outcomes.count("flagged_wrong"),
        "false_stops": sum(1 for r in rows if r[oc] in ("safe_stop", "false_alarm") and r["expect"] == "save" and r["fault"] == "ok"),
        "faults_reported_as_success": sum(1 for r in faults if r[oc] == "silent_wrong"),
        "stopped_when_no_save_possible": sum(1 for r in rows if r["expect"] == "stop" and r[oc] == "safe_stop"),
        "model_usd_total": round(sum(float(r["cost_usd"] or 0) for r in rows), 4),
        "model_calls_total": sum(int(r["model_calls"] or 0) for r in rows),
        "control_median_s": round(statistics.median(walls), 2),
        "control_median_host_load": round(statistics.median(loads), 1),
        "control_model_usd_per_item": round(steady["model_usd"], 5),
    }
    return {"arm": arm["arm"], "label": arm["label"], "kind": arm["kind"], "source": f"silent_saves/results/1.1.0/{arm['dir']}",
            "measured": measured, "setup": setup, "steady": steady, "steady_usd": steady_usd,
            "faults": fault_rows, "changes": changes}


def _split(c_people_min_op: float, c_people_min_eng: float) -> float:
    rate = ASSUME["labor_usd_per_hour"]
    return c_people_min_op / 60 * rate["operator"] + c_people_min_eng / 60 * rate["engineer"]


def project(a: dict[str, Any], n: int, people_factor: float = 1.0) -> dict[str, float]:
    """Totals for n items. people_factor scales every people-time assumption."""

    rate = ASSUME["labor_usd_per_hour"]
    vol = ASSUME["volume"]
    ch_rate = vol["ui_changes_per_1000_items"] / 1000
    f_rate = vol["server_faults_per_1000_items"] / 1000
    st = a["steady"]
    s = a["setup"]
    # (machine $, people $, people min) for each part
    setup = (s["model_usd"], _split(s["operator_min"], s["engineer_min"]), s["operator_min"] + s["engineer_min"])
    per_item = (st["model_usd"] + st["compute_usd"], _split(st["operator_s"] / 60, st["engineer_s"] / 60),
                (st["operator_s"] + st["engineer_s"]) / 60)

    def mean(xs: list[float]) -> float:
        return statistics.mean(xs) if xs else 0.0

    fault = (mean([f["excess_machine_usd"] for f in a["faults"]]), mean([f["excess_people_usd"] for f in a["faults"]]),
             mean([f["excess_people_min"] for f in a["faults"]]))
    change = (mean([c["excess_machine_usd"] for c in a["changes"]]), mean([c["excess_people_usd"] for c in a["changes"]]),
              mean([c["excess_people_min"] for c in a["changes"]]))
    parts = {"setup": (1.0, setup), "per_item": (n, per_item), "faults": (n * f_rate, fault), "ui_changes": (n * ch_rate, change)}
    out: dict[str, Any] = {}
    total = people_min = 0.0
    for k, (mult, (mach, ppl, mins)) in parts.items():
        usd = mult * (mach + people_factor * ppl)
        out[f"{k}_usd"] = round(usd, 2)
        total += usd
        people_min += mult * mins * people_factor
    out.update(total_usd=round(total, 2), people_min=round(people_min, 1),
               model_usd=round(s["model_usd"] + n * st["model_usd"], 2),
               silent_wrong_expected=round(n * f_rate * mean([1.0 if f["outcome"] == "silent_wrong" else 0.0 for f in a["faults"]])
                                           + n * ch_rate * mean([c["silent_wrong"] for c in a["changes"]]), 2))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    arms = [x for x in (mockmed_arm(a) for a in MOCKMED_ARMS) if x]
    missing = [a["arm"] for a in MOCKMED_ARMS if not any(x["arm"] == a["arm"] for x in arms)]
    for a in arms:
        a["totals"] = {str(n): project(a, n) for n in SIZES}
        a["totals_people_x0.5"] = {str(n): project(a, n, 0.5) for n in SIZES}
        a["totals_people_x2"] = {str(n): project(a, n, 2.0) for n in SIZES}
    result = {"assumptions": ASSUME["version"], "mockmed": arms, "mockmed_missing": missing}
    (out / "totals.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    (out / "TABLE.md").write_text(render(result))
    print(render(result))
    return 0


def _f(x: Any, nd: int = 2) -> str:
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else str(x)


def render(result: dict[str, Any]) -> str:
    arms = result["mockmed"]
    L = ["## MockMed, fault set 1.1.0 (100 items, 10 UI changes, 5 server faults)", "",
         "| Arm | Verified (of 90) | Silent wrong | False stops | Faults claimed saved (of 5) | Stopped when no save possible (of 5) | Control s/item (load) | Model $/item | Setup people min | Setup agent min / $ |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for a in arms:
        m, s = a["measured"], a["setup"]
        L.append(f"| {a['label']} | {m['verified_of_saveable']} | {m['silent_wrong']} | {m['false_stops']} | "
                 f"{m['faults_reported_as_success']} | {m['stopped_when_no_save_possible']} | "
                 f"{_f(m['control_median_s'],1)} ({_f(m['control_median_host_load'],0)}) | {_f(m['control_model_usd_per_item'],4)} | "
                 f"{_f(s['operator_min'] + s['engineer_min'],0)} | {_f(s['agent_min'],1)} / {_f(s['model_usd'],3)} |")
    L += ["", "### Each UI change: excess people minutes (silent wrongs in the block)", "",
          "| Change | " + " | ".join(a["arm"] for a in arms) + " |", "|---|" + "---|" * len(arms)]
    for i, c in enumerate(arms[0]["changes"]):
        cells = []
        for a in arms:
            ch = a["changes"][i]
            cells.append(f"{_f(ch['excess_people_min'],1)}" + (f" ({ch['silent_wrong']} SW)" if ch["silent_wrong"] else "")
                         + (" +fix" if ch["people_fix_added"] else ""))
        L.append(f"| {c['change']} | " + " | ".join(cells) + " |")
    L += ["", "### Totals (people time priced at the assumed rates; x0.5 and x2 in brackets)", "",
          "| Arm | " + " | ".join(f"{n} items: total $ | people min | silent wrongs" for n in SIZES) + " |",
          "|---|" + "---|---|---|" * len(SIZES)]
    for a in arms:
        cells = []
        for n in SIZES:
            t, lo, hi = a["totals"][str(n)], a["totals_people_x0.5"][str(n)], a["totals_people_x2"][str(n)]
            cells += [f"{_f(t['total_usd'],0)} [{_f(lo['total_usd'],0)}-{_f(hi['total_usd'],0)}]",
                      _f(t["people_min"], 0), _f(t["silent_wrong_expected"], 1)]
        L.append(f"| {a['arm']} | " + " | ".join(cells) + " |")
    if result.get("mockmed_missing"):
        L += ["", f"Not yet run: {', '.join(result['mockmed_missing'])}."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
