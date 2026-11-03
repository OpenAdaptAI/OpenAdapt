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
    {
        "arm": "cua_alone",
        "kind": "agent",
        "dir": "claude-computer-use+readback/2026-10-19",
        "outcome": "outcome_alone",
        "setup": "cua_alone",
        "label": "4. Computer-use agent, screenshots only (Claude Sonnet 5.5)",
    },
    {
        "arm": "agent_tools",
        "timing_dir": "agent-tools/2026-10-24-timing",
        "kind": "agent",
        "dir": "agent-tools/2026-10-24",
        "outcome": "outcome",
        "setup": "agent_tools",
        "label": "1. Agent with structured tools and record read-back (Claude Sonnet 5.5)",
    },
    {
        "arm": "agent_script",
        "timing_dir": "agent-script/2026-10-24-timing",
        "kind": "script",
        "dir": "agent-script/2026-10-24",
        "outcome": "outcome",
        "setup": "agent_script",
        "label": "2. Agent-written Playwright script with read-back and duplicate guard, agent-maintained",
    },
    {
        "arm": "openadapt_program",
        "timing_dir": "openadapt-program/2026-10-24-timing",
        "kind": "program",
        "dir": "openadapt-program/2026-10-24",
        "outcome": "outcome",
        "setup": "openadapt",
        "label": "3. OpenAdapt admitted program, read-back on, no model calls",
    },
    {
        "arm": "openadapt_self_repair",
        "kind": "program",
        "dir": "self-repair/2026-10-19",
        "outcome": "outcome",
        "setup": "openadapt",
        "label": "3r. OpenAdapt program with self-repair (engine of 2026-10-19)",
    },
    {
        "arm": "cua_plus_openadapt_readback",
        "kind": "agent",
        "dir": "claude-computer-use+readback/2026-10-19",
        "outcome": "outcome",
        "setup": "cua_alone",
        "label": "4 + OpenAdapt read-back: the same agent runs, each claim gated on verify-run",
    },
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
        key = (
            "claimed_failure_save_possible"
            if save_possible
            else "claimed_failure_no_save_possible"
        )
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
            op += (
                ASSUME["operator_s"]["repair_review"]
                + ASSUME["operator_s"]["interrupt"]
            )
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
        return {
            "model_usd": model,
            "operator_s": op,
            "engineer_s": eng,
            "compute_usd": float(r["item_s"] or 0) * cpu,
        }

    def usd(c: dict[str, float]) -> float:
        return (
            c["model_usd"]
            + c["compute_usd"]
            + c["operator_s"] / 3600 * rate["operator"]
            + c["engineer_s"] / 3600 * rate["engineer"]
        )

    control = [r for r in rows if r["release"] == "baseline" and r["fault"] == "ok"]
    steady = {
        k: statistics.mean(cost(r)[k] for r in control)
        for k in ("model_usd", "operator_s", "engineer_s", "compute_usd")
    }
    steady_usd = usd(steady)
    st_machine = steady["model_usd"] + steady["compute_usd"]

    setup_rows = [
        r
        for r in rows
        if any(e.get("phase") == "setup" for e in json.loads(r["human_events"] or "[]"))
    ]
    setup_model = sum(float(r["cost_usd"] or 0) for r in setup_rows)
    setup_agent_min = sum(float(r["item_s"] or 0) for r in setup_rows) / 60
    sp = ASSUME["setup_people_min"][arm["setup"]]
    setup = {
        "model_usd": setup_model,
        "agent_min": setup_agent_min,
        "operator_min": sp["operator"],
        "engineer_min": sp["engineer"],
    }
    setup["usd"] = (
        setup_model
        + sp["operator"] / 60 * rate["operator"]
        + sp["engineer"] / 60 * rate["engineer"]
    )

    faults = [r for r in rows if r["fault"] != "ok"]
    fault_rows = []
    for r in faults:
        c = cost(r)
        fault_rows.append(
            {
                "item": int(r["item"]),
                "fault": r["fault"],
                "outcome": r[oc],
                "excess_usd": usd(c) - steady_usd,
                "excess_machine_usd": c["model_usd"] + c["compute_usd"] - st_machine,
                "excess_people_usd": _split(
                    (c["operator_s"] - steady["operator_s"]) / 60,
                    (c["engineer_s"] - steady["engineer_s"]) / 60,
                ),
                "excess_people_min": (
                    c["operator_s"]
                    + c["engineer_s"]
                    - steady["operator_s"]
                    - steady["engineer_s"]
                )
                / 60,
            }
        )

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
        excess_usd = (
            model
            + comp
            + op_s / 3600 * rate["operator"]
            + eng_s / 3600 * rate["engineer"]
        ) - n * steady_usd
        changes.append(
            {
                "change": rel,
                "drift": block[0]["drift"],
                "items": n,
                "outcomes": {o: outs.count(o) for o in sorted(set(outs))},
                "silent_wrong": outs.count("silent_wrong"),
                "recovered_by_block_end": last_ok,
                "people_fix_added": fix,
                "operator_min": round(op_s / 60, 2),
                "engineer_min": round(eng_s / 60, 2),
                "model_usd": round(model, 4),
                "excess_people_min": round(
                    (op_s + eng_s) / 60
                    - n * (steady["operator_s"] + steady["engineer_s"]) / 60,
                    2,
                ),
                "excess_usd": round(excess_usd, 3),
                "excess_machine_usd": model + comp - n * st_machine,
                "excess_people_usd": _split(
                    op_s / 60 - n * steady["operator_s"] / 60,
                    eng_s / 60 - n * steady["engineer_s"] / 60,
                ),
            }
        )

    outcomes = [r[oc] for r in rows]
    timing = control
    if arm.get("timing_dir") and (SS / arm["timing_dir"] / "items.csv").is_file():
        # A separate control-block pass at normal host load; the full run's times are not used.
        timing = [
            r
            for r in csv.DictReader((SS / arm["timing_dir"] / "items.csv").open())
            if r["release"] == "baseline" and r["fault"] == "ok"
        ]
    walls = sorted(float(r["item_s"]) for r in timing)
    loads = [float(r["host_load_1m"] or 0) for r in timing]
    measured = {
        "items": len(rows),
        "verified_of_saveable": sum(
            1
            for r in rows
            if r[oc] == "correct" and r["expect"] == "save" and r["fault"] == "ok"
        ),
        "saveable": sum(
            1 for r in rows if r["expect"] == "save" and r["fault"] == "ok"
        ),
        "silent_wrong": outcomes.count("silent_wrong"),
        "flagged_wrong": outcomes.count("flagged_wrong"),
        "false_stops": sum(
            1
            for r in rows
            if r[oc] in ("safe_stop", "false_alarm")
            and r["expect"] == "save"
            and r["fault"] == "ok"
        ),
        "faults_reported_as_success": sum(1 for r in faults if r[oc] == "silent_wrong"),
        "stopped_when_no_save_possible": sum(
            1 for r in rows if r["expect"] == "stop" and r[oc] == "safe_stop"
        ),
        "model_usd_total": round(sum(float(r["cost_usd"] or 0) for r in rows), 4),
        "model_calls_total": sum(int(r["model_calls"] or 0) for r in rows),
        "control_median_s": round(statistics.median(walls), 2),
        "control_median_host_load": round(statistics.median(loads), 1),
        "control_model_usd_per_item": round(steady["model_usd"], 5),
    }
    return {
        "arm": arm["arm"],
        "label": arm["label"],
        "kind": arm["kind"],
        "source": f"silent_saves/results/1.1.0/{arm['dir']}",
        "measured": measured,
        "setup": setup,
        "steady": steady,
        "steady_usd": steady_usd,
        "faults": fault_rows,
        "changes": changes,
    }


def _split(c_people_min_op: float, c_people_min_eng: float) -> float:
    rate = ASSUME["labor_usd_per_hour"]
    return (
        c_people_min_op / 60 * rate["operator"]
        + c_people_min_eng / 60 * rate["engineer"]
    )


def project(a: dict[str, Any], n: int, people_factor: float = 1.0) -> dict[str, float]:
    """Totals for n items. people_factor scales every people-time assumption."""

    vol = ASSUME["volume"]
    ch_rate = vol["ui_changes_per_1000_items"] / 1000
    f_rate = vol["server_faults_per_1000_items"] / 1000
    st = a["steady"]
    s = a["setup"]
    # (machine $, people $, people min) for each part
    setup = (
        s["model_usd"],
        _split(s["operator_min"], s["engineer_min"]),
        s["operator_min"] + s["engineer_min"],
    )
    per_item = (
        st["model_usd"] + st["compute_usd"],
        _split(st["operator_s"] / 60, st["engineer_s"] / 60),
        (st["operator_s"] + st["engineer_s"]) / 60,
    )

    def mean(xs: list[float]) -> float:
        return statistics.mean(xs) if xs else 0.0

    fault = (
        mean([f["excess_machine_usd"] for f in a["faults"]]),
        mean([f["excess_people_usd"] for f in a["faults"]]),
        mean([f["excess_people_min"] for f in a["faults"]]),
    )
    change = (
        mean([c["excess_machine_usd"] for c in a["changes"]]),
        mean([c["excess_people_usd"] for c in a["changes"]]),
        mean([c["excess_people_min"] for c in a["changes"]]),
    )
    parts = {
        "setup": (1.0, setup),
        "per_item": (n, per_item),
        "faults": (n * f_rate, fault),
        "ui_changes": (n * ch_rate, change),
    }
    out: dict[str, Any] = {}
    total = people_min = 0.0
    for k, (mult, (mach, ppl, mins)) in parts.items():
        usd = mult * (mach + people_factor * ppl)
        out[f"{k}_usd"] = round(usd, 2)
        total += usd
        people_min += mult * mins * people_factor
    out.update(
        total_usd=round(total, 2),
        people_min=round(people_min, 1),
        model_usd=round(s["model_usd"] + n * st["model_usd"], 2),
        silent_wrong_expected=round(
            n
            * f_rate
            * mean(
                [1.0 if f["outcome"] == "silent_wrong" else 0.0 for f in a["faults"]]
            )
            + n * ch_rate * mean([c["silent_wrong"] for c in a["changes"]]),
            2,
        ),
    )
    return out


# -- OpenEMR: the lifecycle soak's ledgers ------------------------------------------

LIFE = BENCH / "lifecycle" / "results"
#: (arm in the ledger, ledger dir, setup key, label)
OPENEMR_ARMS: list[dict[str, Any]] = [
    {
        "arm": "openadapt-production-relabel",
        "dir": "production-2026-10-24/after",
        "setup": "openadapt_regulated_openemr",
        "kind": "program",
        "label": "3. OpenAdapt, Regulated + problems_only, durable, pipelined read-back, relabel repair "
        "(engine of measure/production-lifecycle; the API step is refused on this form, so the GUI save runs)",
    },
    {
        "arm": "openadapt-production",
        "dir": "production-2026-10-24/before",
        "setup": "openadapt_regulated_openemr",
        "kind": "program",
        "label": "3 on main. Same configuration, current main's engine",
    },
    {
        "arm": "agent_tools",
        "dir": "economics-2026-10-24/agent_tools",
        "setup": "agent_tools",
        "kind": "agent",
        "label": "1. Agent with structured tools and REST read-back (Claude Sonnet 5.5)",
    },
    {
        "arm": "agent_script",
        "dir": "economics-2026-10-24/agent_script",
        "setup": "agent_script",
        "kind": "script",
        "label": "2. Agent-written Playwright + REST script with read-back and duplicate check, agent-maintained (Claude Sonnet 5.5)",
    },
    {
        "arm": "human_script",
        "dir": "economics-2026-10-24/human_script",
        "setup": "human_script",
        "kind": "script",
        "label": "2h. Hand-written script, same checks, not maintained during the run",
    },
    {
        "arm": "cua_alone",
        "dir": "economics-2026-10-24/cua_alone",
        "setup": "cua_alone",
        "kind": "agent",
        "label": "4. Computer-use agent, screenshots only (Claude Sonnet 5.5), first 12 items",
    },
]


def openemr_arm(spec: dict[str, Any]) -> Optional[dict[str, Any]]:
    sys.path.insert(0, str(BENCH))
    from lifecycle.arms import economics as econ

    ledger = LIFE / spec["dir"] / "ledger.jsonl"
    if not ledger.is_file():
        return None
    rows = econ.read_rows([ledger])
    res = econ.compute(rows)
    a = next((x for x in res["arms"] if x["arm"] == spec["arm"]), None)
    if a is None:
        return None
    # A run that ended short of its planned items is labelled, never silently
    # shrunk: its rates come from fewer items than the stream it was started on.
    partial = bool(a.get("short_run"))
    label = spec["label"]
    if partial:
        label += f" [short run: {a['items']} of {a['planned_items']} planned items; {a['short_why']}]"
    m = a["measured"]
    n = a["items"]
    op = ASSUME["operator_s"]
    mine = [r for r in rows if r.get("arm") == spec["arm"]]
    fixes = [
        r
        for r in mine
        if r.get("type") == "phase"
        and r.get("stage") == "maintenance"
        and r.get("phase") in ("fix_script",)
    ]
    reviews = [
        r
        for r in mine
        if r.get("type") == "operator" and r.get("kind") == "repair_review"
    ]
    setup_walls = [
        float(r.get("wall_s", 0) or 0)
        for r in mine
        if r.get("type") == "phase" and r.get("stage") == "setup"
    ]
    sp = ASSUME["setup_people_min"][spec["setup"]]
    # Script fixes are reviewed by an engineer (assumption), not charged as an operator repair review.
    eng_maint_min = (
        len(fixes) * ASSUME["maintenance_people_min"]["script_fix_review_engineer"]
        if spec["kind"] == "script"
        else 0.0
    )
    op_maint_min = 0.0 if spec["kind"] == "script" else m["maintenance_human_min"]
    if spec["kind"] == "script" and spec["arm"] == "human_script" and a["drift_events"]:
        # Not maintained during the run: an engineer fixes it once per change (prices.json, 30 min).
        broke = any(
            r.get("type") == "item_result" and (r.get("detail") or {}).get("broke")
            for r in mine
        )
        eng_maint_min = (
            ASSUME["maintenance_people_min"]["script_fix_by_hand_engineer"]
            if broke
            else 0.0
        )
    d = max(a["drift_events"], 1)
    sw = a["silent_wrong"]
    cleanup_s = sw * (
        op["reconcile"] + op["manual_entry_openemr"] + 2 * op["interrupt"]
    )
    item_op_min = m["operator_min"] - (
        len(reviews) * (op["repair_review"] + op["interrupt"]) / 60
        if spec["kind"] == "script"
        else 0.0
    )
    out = {
        "arm": spec["arm"],
        "label": label,
        "kind": spec["kind"],
        "source": f"lifecycle/results/{spec['dir']}",
        "partial": partial,
        "items": n,
        "verified": a["verified"],
        "automated": a["automated"],
        "by_hand": a["by_hand"],
        "outcomes": a["outcomes"],
        "silent_wrong": sw,
        "false_stop": a["false_stop"],
        "false_stop_of": a["false_stop_denominator"],
        "drift_events": a["drift_events"],
        "median_s": m["median_wall_s_automated"],
        "p90_s": m["p90_wall_s_automated"],
        "model_usd_total": round(
            m["item_model_usd"] + m["setup_model_usd"] + m["maintenance_model_usd"], 4
        ),
        "setup": {
            "model_usd": m["setup_model_usd"],
            "agent_min": round(sum(setup_walls) / 60, 1),
            "operator_min": sp["operator"],
            "engineer_min": sp["engineer"],
        },
        "per_item": {
            "model_usd": m["item_model_usd"] / n,
            "compute_usd": m["compute_usd"] / n,
            "operator_min": item_op_min / n,
        },
        "per_change": {
            "model_usd": m["maintenance_model_usd"] / d,
            "operator_min": op_maint_min / d,
            "engineer_min": eng_maint_min / d,
            "fixes": len(fixes),
        },
        "per_item_cleanup_min": cleanup_s / 60 / n,
    }
    if not a["drift_events"]:
        # The run never reached the app change: its fixes were first-use debugging, so they are
        # setup, and maintenance after a change was not measured.
        out["setup"]["model_usd"] += m["maintenance_model_usd"]
        out["setup"]["engineer_min"] += eng_maint_min
        out["setup"]["debug_fixes"] = len(fixes)
        out["per_change"] = {
            "model_usd": 0.0,
            "operator_min": 0.0,
            "engineer_min": 0.0,
            "fixes": 0,
            "not_measured": True,
        }
    return out


def project_openemr(a: dict[str, Any], n: int, f: float = 1.0) -> dict[str, float]:
    rate = ASSUME["labor_usd_per_hour"]
    ch = ASSUME["volume"]["ui_changes_per_1000_items"] / 1000 * n
    s, pi, pc = a["setup"], a["per_item"], a["per_change"]
    op_usd = rate["operator"] / 60
    eng_usd = rate["engineer"] / 60
    setup_usd = s["model_usd"] + f * (
        s["operator_min"] * op_usd + s["engineer_min"] * eng_usd
    )
    items_usd = n * (
        pi["model_usd"] + pi["compute_usd"] + f * pi["operator_min"] * op_usd
    )
    change_usd = ch * (
        pc["model_usd"]
        + f * (pc["operator_min"] * op_usd + pc["engineer_min"] * eng_usd)
    )
    cleanup_usd = n * f * a["per_item_cleanup_min"] * op_usd
    people = f * (
        s["operator_min"]
        + s["engineer_min"]
        + n * pi["operator_min"]
        + ch * (pc["operator_min"] + pc["engineer_min"])
        + n * a["per_item_cleanup_min"]
    )
    return {
        "setup_usd": round(setup_usd, 2),
        "per_item_usd": round(items_usd, 2),
        "ui_changes_usd": round(change_usd, 2),
        "silent_wrong_cleanup_usd": round(cleanup_usd, 2),
        "total_usd": round(setup_usd + items_usd + change_usd + cleanup_usd, 2),
        "people_min": round(people, 1),
        "model_usd": round(
            s["model_usd"] + n * pi["model_usd"] + ch * pc["model_usd"], 2
        ),
        "silent_wrong_expected": round(n * a["silent_wrong"] / a["items"], 2)
        if a["items"]
        else 0.0,
    }


def render_openemr(arms: list[dict[str, Any]]) -> str:
    L = [
        "## OpenEMR referral intake (seed 7, 55 items, one app change: the save button relabeled at item 26)",
        "",
        "| Arm | Items | Verified in the end | Automated (no hand entry) | Silent wrong | False stops | Median s/item | Model $/item | Operator min/item | Maintenance for the change (people min / model $) | Setup people min | Setup agent min / $ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in arms:
        pc, s = a["per_change"], a["setup"]
        L.append(
            f"| {a['label']} | {a['items']} | {a['verified']} | {a['automated']} | {a['silent_wrong']} | "
            f"{a['false_stop']} of {a['false_stop_of']} | {_f(a['median_s'], 1)} | {_f(a['per_item']['model_usd'], 3)} | "
            f"{_f(a['per_item']['operator_min'], 2)} | "
            + (
                "not reached | "
                if pc.get("not_measured") and a["kind"] != "manual"
                else f"{_f(pc['operator_min'] + pc['engineer_min'], 1)} / {_f(pc['model_usd'], 2)} | "
            )
            + f"{_f(s['operator_min'] + s['engineer_min'], 0)} | {_f(s['agent_min'], 1)} / {_f(s['model_usd'], 2)} |"
        )
    L += [
        "",
        "### Totals (people time priced at the assumed rates; x0.5 and x2 in brackets)",
        "",
        "| Arm | "
        + " | ".join(f"{n} items: total $ | people min" for n in SIZES)
        + " |",
        "|---|" + "---|---|" * len(SIZES),
    ]
    for a in arms:
        cells = []
        for n in SIZES:
            t, lo, hi = (
                a["totals"][str(n)],
                a["totals_people_x0.5"][str(n)],
                a["totals_people_x2"][str(n)],
            )
            cells += [
                f"{_f(t['total_usd'], 0)} [{_f(lo['total_usd'], 0)}-{_f(hi['total_usd'], 0)}]",
                _f(t["people_min"], 0),
            ]
        L.append(f"| {a['arm']} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    arms = [x for x in (mockmed_arm(a) for a in MOCKMED_ARMS) if x]
    missing = [
        a["arm"] for a in MOCKMED_ARMS if not any(x["arm"] == a["arm"] for x in arms)
    ]
    for a in arms:
        a["totals"] = {str(n): project(a, n) for n in SIZES}
        a["totals_people_x0.5"] = {str(n): project(a, n, 0.5) for n in SIZES}
        a["totals_people_x2"] = {str(n): project(a, n, 2.0) for n in SIZES}
    oe = [x for x in (openemr_arm(a) for a in OPENEMR_ARMS) if x]
    op = ASSUME["operator_s"]
    manual_min = (op["manual_entry_openemr"] + op["interrupt"]) / 60
    oe.append(
        {
            "arm": "all_manual",
            "label": "Reference: a person enters every item (cost model V1)",
            "kind": "manual",
            "source": "cost model",
            "partial": False,
            "items": None,
            "verified": None,
            "automated": 0,
            "by_hand": None,
            "outcomes": {},
            "silent_wrong": 0,
            "false_stop": None,
            "false_stop_of": None,
            "drift_events": 0,
            "median_s": manual_min * 60,
            "p90_s": None,
            "model_usd_total": 0.0,
            "setup": {
                "model_usd": 0.0,
                "agent_min": 0.0,
                "operator_min": 0.0,
                "engineer_min": 0.0,
            },
            "per_item": {
                "model_usd": 0.0,
                "compute_usd": 0.0,
                "operator_min": manual_min,
            },
            "per_change": {
                "model_usd": 0.0,
                "operator_min": 0.0,
                "engineer_min": 0.0,
                "fixes": 0,
            },
            "per_item_cleanup_min": 0.0,
        }
    )
    for a in oe:
        a["totals"] = {str(n): project_openemr(a, n) for n in SIZES}
        a["totals_people_x0.5"] = {str(n): project_openemr(a, n, 0.5) for n in SIZES}
        a["totals_people_x2"] = {str(n): project_openemr(a, n, 2.0) for n in SIZES}
    result = {
        "assumptions": ASSUME["version"],
        "openemr": oe,
        "mockmed": arms,
        "mockmed_missing": missing,
        "openemr_missing": [
            a["arm"] for a in OPENEMR_ARMS if not any(x["arm"] == a["arm"] for x in oe)
        ],
    }
    (out / "totals.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    text = render_openemr(oe) + "\n" + render(result)
    (out / "TABLE.md").write_text(text)
    print(text)
    return 0


def _f(x: Any, nd: int = 2) -> str:
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else str(x)


def render(result: dict[str, Any]) -> str:
    arms = result["mockmed"]
    L = [
        "## MockMed, fault set 1.1.0 (100 items, 10 UI changes, 5 server faults)",
        "",
        "| Arm | Verified (of 90) | Silent wrong | False stops | Faults claimed saved (of 5) | Stopped when no save possible (of 5) | Control s/item (load) | Model $/item | Setup people min | Setup agent min / $ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in arms:
        m, s = a["measured"], a["setup"]
        L.append(
            f"| {a['label']} | {m['verified_of_saveable']} | {m['silent_wrong']} | {m['false_stops']} | "
            f"{m['faults_reported_as_success']} | {m['stopped_when_no_save_possible']} | "
            f"{_f(m['control_median_s'], 1)} ({_f(m['control_median_host_load'], 0)}) | {_f(m['control_model_usd_per_item'], 4)} | "
            f"{_f(s['operator_min'] + s['engineer_min'], 0)} | {_f(s['agent_min'], 1)} / {_f(s['model_usd'], 3)} |"
        )
    L += [
        "",
        "### Each UI change: excess people minutes (silent wrongs in the block)",
        "",
        "| Change | " + " | ".join(a["arm"] for a in arms) + " |",
        "|---|" + "---|" * len(arms),
    ]
    for i, c in enumerate(arms[0]["changes"]):
        cells = []
        for a in arms:
            ch = a["changes"][i]
            cells.append(
                f"{_f(ch['excess_people_min'], 1)}"
                + (f" ({ch['silent_wrong']} SW)" if ch["silent_wrong"] else "")
                + (" +fix" if ch["people_fix_added"] else "")
            )
        L.append(f"| {c['change']} | " + " | ".join(cells) + " |")
    L += [
        "",
        "### Totals (people time priced at the assumed rates; x0.5 and x2 in brackets)",
        "",
        "| Arm | "
        + " | ".join(f"{n} items: total $ | people min | silent wrongs" for n in SIZES)
        + " |",
        "|---|" + "---|---|---|" * len(SIZES),
    ]
    for a in arms:
        cells = []
        for n in SIZES:
            t, lo, hi = (
                a["totals"][str(n)],
                a["totals_people_x0.5"][str(n)],
                a["totals_people_x2"][str(n)],
            )
            cells += [
                f"{_f(t['total_usd'], 0)} [{_f(lo['total_usd'], 0)}-{_f(hi['total_usd'], 0)}]",
                _f(t["people_min"], 0),
                _f(t["silent_wrong_expected"], 1),
            ]
        L.append(f"| {a['arm']} | " + " | ".join(cells) + " |")
    if result.get("mockmed_missing"):
        L += ["", f"Not yet run: {', '.join(result['mockmed_missing'])}."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
