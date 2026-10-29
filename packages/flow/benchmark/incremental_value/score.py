"""Score both arms from their ledgers, with the pre-registered rule.

    python -m incremental_value.score DIR [--json OUT]

Outcomes per item (``PREREGISTRATION.md``):

- ``correct``: the end state is right and the claim matches it;
- ``missed_harm``: harm left, and the arm said done;
- ``caught_harm``: harm left, and the arm stopped;
- ``false_stop``: a chart was wanted, the end state is right, and the arm
  stopped;
- ``failed_stop``: no chart, and the arm stopped (the person enters it);
- ``silent_absent``: no chart, and the arm said done;
- ``infra``: the harness failed; excluded, and reported.

People time is **modelled** with cost model V1
(``economic_comparison/assumptions.json``), never measured.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ASSUMPTIONS = json.loads((HERE.parent / "economic_comparison" / "assumptions.json").read_text())
S = ASSUMPTIONS["operator_s"]
STOP, RECONCILE, REDO, INTERRUPT = S["stop"], S["reconcile"], S["manual_entry_openemr"], S["interrupt"]
#: Pre-registered setup people time (minutes, engineer).
SETUP_MIN = {"A_agent_readback": 60.0, "B_agent_openadapt": 90.0}
#: People seconds per outcome, before the x0.5 / x2 sensitivity.
PEOPLE_S = {
    "correct": 0.0,
    "correct_stop": STOP + INTERRUPT,
    "false_stop": STOP + RECONCILE + INTERRUPT,
    "caught_harm": STOP + RECONCILE + REDO + INTERRUPT,
    "failed_stop": STOP + REDO + INTERRUPT,
    "missed_harm": RECONCILE + REDO + INTERRUPT,
    "silent_absent": RECONCILE + REDO + INTERRUPT,
}
CORRECT_AFTER_HANDLING = {"correct", "correct_stop", "false_stop", "caught_harm", "failed_stop"}


def outcome(row: dict[str, Any]) -> str:
    if row.get("infra_error") or row.get("result") is None or row.get("prepare_error"):
        return "infra"
    done = row["result"]["claimed"] == "done"
    v = row["oracle"]["verdict"]
    if v == "harmful":
        return "missed_harm" if done else "caught_harm"
    if v == "absent":
        return "silent_absent" if done else "failed_stop"
    # correct end state
    if row["intended"]:
        return "correct" if done else "false_stop"
    return "correct_stop" if not done else "correct"


def arm_summary(rows: list[dict[str, Any]], arm: str, factor: float = 1.0) -> dict[str, Any]:
    outs = [outcome(r) for r in rows]
    counted = [(r, o) for r, o in zip(rows, outs) if o != "infra"]
    n = len(counted)
    count = {k: sum(1 for _, o in counted if o == k) for k in PEOPLE_S}
    people_s = sum(PEOPLE_S[o] for _, o in counted) * factor
    setup_min = SETUP_MIN[arm]
    correct_after = sum(1 for _, o in counted if o in CORRECT_AFTER_HANDLING)
    walls = [r["result"]["wall_s"] for r, _ in counted]
    cost = sum(r["result"]["cost_usd"] for r, _ in counted)
    dups = sum(1 for r, _ in counted if r["oracle"].get("duplicate"))
    verify = [r["result"]["detail"].get("verify_s", 0) for r, _ in counted]
    by_kind: dict[str, dict[str, int]] = {}
    for r, o in counted:
        by_kind.setdefault(r["kind"], {}).setdefault(o, 0)
        by_kind[r["kind"]][o] += 1
    total_min = setup_min + people_s / 60
    return {
        "items": n,
        "infra_excluded": len(rows) - n,
        "correct_completions": count["correct"] + count["correct_stop"],
        "missed_harmful_changes": count["missed_harm"],
        "caught_harmful_changes": count["caught_harm"],
        "false_stops": count["false_stop"],
        "failed_stops": count["failed_stop"],
        "silent_absent": count["silent_absent"],
        "duplicates": dups,
        "correct_after_handling": correct_after,
        "people_min_items_modelled": round(people_s / 60, 1),
        "setup_people_min_modelled": setup_min,
        "people_min_per_correct_txn_modelled": round(total_min / correct_after, 3) if correct_after else None,
        "people_min_per_correct_txn_items_only_modelled": round(people_s / 60 / correct_after, 3) if correct_after else None,
        "model_usd_total": round(cost, 3),
        "model_usd_per_item": round(cost / n, 4) if n else None,
        "wall_s_median": round(statistics.median(walls), 1) if walls else None,
        "wall_s_p90": round(sorted(walls)[int(0.9 * (len(walls) - 1))], 1) if walls else None,
        "verify_s_median": round(statistics.median(verify), 2) if verify else None,
        "by_kind": by_kind,
    }


def verdict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    n = max(a["items"], b["items"])
    c1 = b["missed_harmful_changes"] < a["missed_harmful_changes"]
    c2 = b["false_stops"] <= a["false_stops"] + math.ceil(n / 100)
    pa, pb = a["people_min_per_correct_txn_modelled"], b["people_min_per_correct_txn_modelled"]
    c3 = pa is not None and pb is not None and pb < pa
    return {
        "1_fewer_missed_harms": c1,
        "2_false_stops_within": c2,
        "false_stop_allowance": math.ceil(n / 100),
        "3_fewer_people_min_per_correct_txn": c3,
        "B_wins": c1 and c2 and c3,
    }


def load(d: Path, arm: str) -> list[dict[str, Any]]:
    p = d / arm / "ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    rows = {arm: load(a.dir, arm) for arm in SETUP_MIN}
    out: dict[str, Any] = {"cost_model": "V1 (economic_comparison/assumptions.json); modelled, not measured"}
    for f in (1.0, 0.5, 2.0):
        s = {arm: arm_summary(r, arm, f) for arm, r in rows.items() if r}
        if len(s) == 2:
            out[f"x{f:g}"] = {**s, "verdict": verdict(s["A_agent_readback"], s["B_agent_openadapt"])}
        else:
            out[f"x{f:g}"] = s
    text = json.dumps(out, indent=1)
    print(text)
    if a.json:
        a.json.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
