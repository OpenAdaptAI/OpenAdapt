"""Write value-free results_*.json summaries of run 3 from the drivers' outputs.

Only counts, verdicts, codes, field names and booleans are kept: no field
value, no patient name, no step intent, no screen text. copy_evidence.py's
gate greps these files too. Usage: make_results.py RUN_DIR
"""

import json
import sys
from pathlib import Path

R = Path(sys.argv[1])


def load(p):
    p = R / p
    return json.loads(p.read_text()) if p.exists() else None


def card_counts(c):
    keys = ("agent_label", "task_type", "scope_ok", "failed_scope", "cases_total", "matched",
            "mismatched", "not_comparable", "not_checked", "risks", "landed_writes", "egress",
            "model_cost_usd", "agent_time_s", "human_time_s")
    out = {k: c.get(k) for k in keys}
    s = c.get("savings") or {}
    out["savings"] = {k: s.get(k) for k in ("estimate", "minutes_saved_per_task", "est_value_usd")}
    out["field_errors"] = c.get("field_errors")
    return out


s1 = load("1-shadow/step1_result.json")
if s1:
    s1 = {**s1, "card": card_counts(load("1-shadow/card/scorecard.json")),
          "adversarial_card": card_counts(load("1-shadow/adversarial/card/scorecard.json"))}
    (R / "results_step1.json").write_text(json.dumps(s1, indent=2) + "\n")

s2 = load("2-gated/step2_results.json")
if s2:
    rows = []
    for r in s2:
        p = r.get("person") or {}
        card = p.get("card") or {}
        row = r.get("ledger_row") or {}
        rows.append({
            "task_id": r["task_id"], "scenario": r["scenario"], "write_adapter": r["adapter"],
            "person_decision": r["decision"], "guard_verdict": r["verdict"], "blocked": r["blocked"],
            "write_requests": r["writes"], "truth_fault_class": (r.get("truth") or {}).get("fault_class"),
            "card_shown": bool(card), "card_values_checked": card.get("values_checked"),
            "card_fields_after_not_read": [f.get("label") for f in card.get("fields") or [] if f.get("after") == "not read"],
            "card_says_exactly": p.get("card_says_exactly"), "portal_says_exactly": p.get("portal_says_exactly"),
            "answered": p.get("answered"), "person_error": p.get("error"),
            "ledger_row": {k: row.get(k) for k in ("verdict", "approver_kind", "fields_checked", "fields_differing", "readback_tier")},
        })
    (R / "results_step2.json").write_text(json.dumps(rows, indent=2) + "\n")

s2b = load("2b-watch/step2b_result.json")
if s2b:
    w = s2b.get("watch") or {}
    out = {k: s2b.get(k) for k in ("card_kind", "save_while_open", "writes_while_open", "guard_verdict",
                                   "writes_after_answer", "events", "note_in_log", "run_ref")}
    out["watch"] = {"exit": w.get("exit"), "stopped": w.get("stopped"),
                    "outcomes": [{k: o.get(k) for k in ("kind", "answer", "proceed")} for o in w.get("outcomes") or []]}
    (R / "results_step2b.json").write_text(json.dumps(out, indent=2) + "\n")

s4 = load("4-fallback/step4_result.json")
if s4:
    out = {"compile_admit_s": s4.get("compile_admit_s")}
    for k, v in s4.items():
        if isinstance(v, dict):
            out[k] = {x: v.get(x) for x in ("path", "outcome", "status", "verified", "model_calls", "cost_usd",
                                            "approvals", "repair_opened", "saved", "saved_rows", "seconds")}
            out[k]["gate_answers"] = [{x: a.get(x) for x in ("decision", "code", "route")} for a in v.get("gate_answers") or []]
            out[k]["person_answered"] = (v.get("person") or {}).get("answered")
    (R / "results_step4.json").write_text(json.dumps(out, indent=2) + "\n")

s5 = load("5-claude/step5_result.json")
if s5:
    tasks = []
    for d in sorted((R / "5-claude/runs").glob("readiness-*")):
        run, case = json.loads((d / "run.json").read_text()), json.loads((d / "case.json").read_text())
        eg = json.loads((d / "egress.json").read_text()) if (d / "egress.json").exists() else {}
        us = json.loads((d / "usage.json").read_text()) if (d / "usage.json").exists() else {}
        tasks.append({"task_id": d.name, "status": run["status"], "ended": run["ended"],
                      "case": case["status"], "reason": case["reason"],
                      "fields": [{"name": f.get("name"), "kind": f.get("kind")} for f in case.get("fields") or []],
                      "identity_ok": (case.get("identity") or {}).get("ok") if isinstance(case.get("identity"), dict) else case.get("identity"),
                      "model_calls_metered": eg.get("model_calls"), "cost_usd_metered": eg.get("cost_usd"),
                      "agent_calls": us.get("calls"), "agent_stopped": us.get("stopped"),
                      "agent_final_line": us.get("final_line"), "agent_wall_s": run.get("agent_wall_s")})
    out = {**s5, "tasks": tasks, "total_metered_usd": round(sum(t["cost_usd_metered"] or 0 for t in tasks), 6),
           "card": card_counts(load("5-claude/card/scorecard.json"))}
    (R / "results_step5.json").write_text(json.dumps(out, indent=2) + "\n")
print(sorted(p.name for p in R.glob("results_*.json")))
