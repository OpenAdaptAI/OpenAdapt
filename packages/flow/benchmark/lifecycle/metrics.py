"""The metric, computed only from the results ledger.

Headline (``headline``): verified completed items per operator-minute, with
every pause, approval, reconciliation and repair review charged as operator
time, beside false stops per 100 items, operator minutes per stop, and the
maintenance minutes each app change cost until the program ran again.
Verified completed items per operator-hour (``bands``) is the same number
times 60.

- A *verified completed item* is one whose final state the independent
  oracle judges right (the intended chart exactly once, or no new chart when
  none is wanted) and that the operator did not have to do by hand.
- *Operator hours* are every charged second: setup, per-item requests
  (approvals, stops, reconciliations, repair reviews, questions, items done by
  hand) and maintenance after each UI change.

Charges are recomputed here from the ledger's request kinds and the cost
model, so the sensitivity bands (x0.5, x2) use the same rows.

Outcome of an item, from the oracle's read after the arm returned (before any
by-hand fix), crossed with what the arm claimed:

=====================  ==================================================
``correct``            right end state, arm claimed done
``right_stop``         no chart wanted, arm stopped, nothing written
``false_stop``         chart wanted, item data right, no item-level fault,
                       arm stopped with nothing written (split by whether a
                       UI change was in force)
``fault_stop``         chart wanted, an item-level fault was injected
                       (expiry, slow or failed save, concurrent edit), arm
                       stopped with nothing written
``stopped_saved``      arm stopped but the right chart is there
``silent_wrong``       arm claimed done; the oracle disagrees
``flagged_wrong``      a wrong write landed and the arm stopped
``error``              the arm crashed; judged like a stop
=====================  ==================================================
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from .cost_model import BANDS, CostModel

ITEM_FAULTS = {
    "session_expiry",
    "slow_save",
    "failed_save",
    "concurrent_same",
    "concurrent_other",
}
DATA_EXCEPTIONS = {
    "dup_existing",
    "dup_resend",
    "ambiguous_same_person",
    "ambiguous_new_person",
    "missing_required",
    "missing_optional",
    "malformed_dob",
    "malformed_case",
}


def classify(*, claimed: str, oracle: str, chart_wanted: bool, exception: str) -> str:
    """One item's outcome. ``oracle`` is "correct", "absent", "wrong" or
    "duplicate" (for no-chart items "correct" means no new chart)."""

    done = claimed == "done"
    if oracle == "correct":
        if done:
            return "correct"
        return "stopped_saved" if chart_wanted else "right_stop"
    if not chart_wanted:
        # a new chart landed where none was wanted
        return "silent_wrong" if done else "flagged_wrong"
    if oracle == "absent":
        if done:
            return "silent_wrong"
        if exception in ITEM_FAULTS:
            return "fault_stop"
        if exception in DATA_EXCEPTIONS:
            return "data_stop"
        return "false_stop"
    return "silent_wrong" if done else "flagged_wrong"


def stop_cause(row: dict[str, Any]) -> str:
    """Why an item stopped, from the product's own halt text (for root causes)."""

    detail = row.get("detail") or {}
    first = detail.get("first_run") or {}
    halt = str(
        first.get("halt") or detail.get("error") or row.get("product_status") or ""
    )
    if "no_duplicate_candidate" in halt or "No matches were found" in halt:
        return "duplicate check listed a chart"
    if "waiting for a person to approve" in halt and "switched_off" in halt:
        return "problems_only switch off; stopped unasked"
    if "Identity check failed" in halt:
        return "identity check failed (relabeled control)"
    if "Missing" in halt or "missing" in halt and "param" in halt:
        return "missing value"
    outcome = first.get("transaction_outcome") or ""
    return f"other: {outcome} {halt[:80]}".strip()


def wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, (c - r) / d), 4), round(min(1.0, (c + r) / d), 4))


def _charge(cost: CostModel, row: dict[str, Any]) -> float:
    s = cost.per_request(row["kind"])
    if (
        row["action"] == "do_by_hand"
        and row["kind"] != "manual_entry"
        and row.get("by_hand")
    ):
        s += cost.manual_entry_s
    return s


def _phase_charge(cost: CostModel, row: dict[str, Any]) -> float:
    fixed = {
        "teach": cost.teach_prep_s,
        "compile": cost.compile_review_s,
        "admit": cost.admit_s,
    }.get(row["phase"], 0.0)
    return float(row.get("attended_s", 0.0)) + fixed


def compute(rows: Iterable[dict[str, Any]], cost: CostModel) -> dict[str, Any]:
    rows = list(rows)
    items = [r for r in rows if r["type"] == "item_result"]
    ops = [r for r in rows if r["type"] == "operator"]
    setup = [r for r in rows if r["type"] == "phase" and r.get("stage") == "setup"]
    maint = [
        r for r in rows if r["type"] == "phase" and r.get("stage") == "maintenance"
    ]
    drifts = [r for r in rows if r["type"] == "drift_applied"]

    by_hand = {
        r["item_id"] for r in ops if r["action"] == "do_by_hand" and r.get("item_id")
    }
    outcomes = Counter(r["outcome"] for r in items)
    final_ok = [r for r in items if r["final_oracle"] == "correct"]
    completed = [
        r
        for r in final_ok
        if r["item_id"] not in by_hand and r["outcome"] != "silent_wrong"
    ]

    # operator requests attributed to maintenance: repair reviews and any
    # request raised in the first `maintenance_window` items after a drift
    item_ops = [r for r in ops if r["kind"] != "repair_review"]
    repair_ops = [r for r in ops if r["kind"] == "repair_review"]

    silent = [r for r in items if r["outcome"] == "silent_wrong"]

    def bands() -> dict[str, Any]:
        out = {}
        for factor in BANDS:
            c = cost.scaled(factor) if factor != 1.0 else cost
            setup_s = sum(_phase_charge(c, r) for r in setup)
            maint_s = sum(_phase_charge(c, r) for r in maint) + sum(
                _charge(c, r) for r in repair_ops
            )
            item_s = sum(_charge(c, r) for r in item_ops)
            hours = (setup_s + maint_s + item_s) / 3600.0
            # Silent wrongs cost nobody anything during the run. If each is
            # found later, someone must look it up and put it right.
            cleanup_s = len(silent) * (c.per_request("reconcile") + c.manual_entry_s)
            hours_cleanup = hours + cleanup_s / 3600.0
            n_all = len(items)
            manual_hours = n_all * c.manual_entry_s / 3600.0
            out[f"x{factor:g}"] = {
                "setup_min": round(setup_s / 60, 2),
                "maintenance_min": round(maint_s / 60, 2),
                "per_item_operator_min_total": round(item_s / 60, 2),
                "per_item_operator_min": round(item_s / 60 / n_all, 3)
                if n_all
                else None,
                "operator_hours": round(hours, 3),
                "verified_completed_per_operator_hour": (
                    round(len(completed) / hours, 2) if hours else None
                ),
                "operator_hours_with_cleanup": round(hours_cleanup, 3),
                "verified_completed_per_operator_hour_with_cleanup": (
                    round(len(completed) / hours_cleanup, 2) if hours_cleanup else None
                ),
                "manual_only_items_per_hour": (
                    round(3600.0 / c.manual_entry_s, 2) if c.manual_entry_s else None
                ),
                "manual_only_hours": round(manual_hours, 3),
                "work_removed_fraction": (
                    round(1 - hours / manual_hours, 3) if manual_hours else None
                ),
            }
        return out

    stops = [r for r in items if r["claimed"] != "done"]
    wanted = [r for r in items if r["chart_wanted"]]
    clean_wanted = [
        r
        for r in wanted
        if r["exception"] not in ITEM_FAULTS and r["exception"] not in DATA_EXCEPTIONS
    ]
    false_stops = [r for r in items if r["outcome"] == "false_stop"]
    done_claims = [r for r in items if r["claimed"] == "done"]
    silent = [r for r in items if r["outcome"] == "silent_wrong"]

    per_drift: dict[str, Any] = {}
    for d in drifts:
        kind = d["kind"]
        window = [r for r in items if r["seq"] >= d["first_seq"]]
        nxt = [x["first_seq"] for x in drifts if x["first_seq"] > d["first_seq"]]
        if nxt:
            window = [r for r in window if r["seq"] < min(nxt)]
        per_drift[kind] = {
            "first_seq": d["first_seq"],
            "items": len(window),
            "outcomes": dict(Counter(r["outcome"] for r in window)),
            "repair_reviews": sum(
                1
                for r in repair_ops
                if window and window[0]["seq"] <= r.get("seq", -1) <= window[-1]["seq"]
            ),
        }

    def headline() -> dict[str, Any]:
        out: dict[str, Any] = {}
        stopped_ids = {r["item_id"] for r in items if r["claimed"] != "done"}
        for factor in BANDS:
            c = cost.scaled(factor) if factor != 1.0 else cost
            setup_s = sum(_phase_charge(c, r) for r in setup)
            maint_s = sum(_phase_charge(c, r) for r in maint) + sum(
                _charge(c, r) for r in repair_ops
            )
            item_s = sum(_charge(c, r) for r in item_ops)
            run_min = (maint_s + item_s) / 60.0
            all_min = run_min + setup_s / 60.0
            stop_s = sum(
                _charge(c, r)
                for r in ops
                if r["kind"] in ("stop", "reconcile")
                and r.get("item_id") in stopped_ids
            )
            drift_rows = []
            for d in drifts:
                nxt = [
                    x["first_seq"] for x in drifts if x["first_seq"] > d["first_seq"]
                ]
                end = min(nxt) if nxt else 10**9
                window = [r for r in items if d["first_seq"] <= r["seq"] < end]
                # Stops the change caused: clean items that wanted a chart
                # and stopped with nothing written (a right stop on a
                # duplicate is not maintenance).
                ids = {r["item_id"] for r in window if r["outcome"] == "false_stop"}
                spent = sum(
                    _charge(c, r)
                    for r in ops
                    if (r["kind"] in ("stop", "reconcile") and r.get("item_id") in ids)
                    or (
                        r["kind"] == "repair_review"
                        and d["first_seq"] <= r.get("seq", -1) < end
                    )
                ) + sum(
                    _phase_charge(c, r)
                    for r in maint
                    if d["first_seq"] <= r.get("seq", -1) < end
                )
                first_done = next((r for r in window if r["claimed"] == "done"), None)
                drift_rows.append(
                    {
                        "kind": d["kind"],
                        "first_seq": d["first_seq"],
                        "items_in_window": len(window),
                        "items_false_stopped": len(ids),
                        "maintenance_operator_min": round(spent / 60, 2),
                        "first_verified_seq": first_done["seq"] if first_done else None,
                        "seconds_to_running_again": (
                            round(first_done["t"] - d["t"], 1)
                            if first_done and "t" in d and "t" in first_done
                            else None
                        ),
                    }
                )
            out[f"x{factor:g}"] = {
                "verified_per_operator_minute": (
                    round(len(completed) / run_min, 3) if run_min else None
                ),
                "verified_per_operator_minute_with_setup": (
                    round(len(completed) / all_min, 3) if all_min else None
                ),
                "operator_min_run": round(run_min, 2),
                "operator_min_setup": round(setup_s / 60, 2),
                "operator_min_per_stop": (
                    round(stop_s / 60 / len(stopped_ids), 2) if stopped_ids else None
                ),
                "after_app_change": drift_rows,
            }
        n = len(items)
        dup = [
            r
            for r in items
            if r["claimed"] != "done"
            and stop_cause(r) == "duplicate check listed a chart"
        ]

        def matched(r: dict[str, Any]) -> str:
            shares = r.get("shares_with_existing") or {}
            hit = [k for k in ("lname", "DOB") if shares.get(k)]
            if hit:
                return "+".join(hit)
            return "first name only" if shares.get("fname") else "none at start"

        return {
            "duplicate_rule_stops": {
                "items": len(dup),
                "by_shared_field": dict(Counter(matched(r) for r in dup)),
                "by_outcome": dict(Counter(r["outcome"] for r in dup)),
                "by_exception": dict(Counter(r["exception"] for r in dup)),
                "note": "fields the item shared with a chart that existed when "
                "it started; charts created during the run by earlier items "
                "count too",
            },
            "stops_by_cause": {
                outcome: dict(
                    Counter(stop_cause(r) for r in items if r["outcome"] == outcome)
                )
                for outcome in sorted(
                    {r["outcome"] for r in items if r["claimed"] != "done"}
                )
            },
            "verified_completed": len(completed),
            "items": n,
            "false_stops_per_100_items": round(100 * len(false_stops) / n, 2)
            if n
            else None,
            "stops_per_100_items": round(100 * len(stops) / n, 2) if n else None,
            "manual_entry_min": round(cost.manual_entry_s / 60, 2),
            "bands": out,
            "definition": "verified completed items (oracle-correct, not done by "
            "hand) per operator-minute; operator minutes are every charged "
            "approval, stop, reconciliation, resume, repair review and item done "
            "by hand, plus maintenance; *_with_setup also charges admission",
        }

    wall = [r["wall_s"] for r in items if r.get("wall_s") is not None]
    loads = [r["load_1m"] for r in items if r.get("load_1m") is not None]
    return {
        "cost_model": cost.version,
        "items": len(items),
        "outcomes": dict(outcomes),
        "verified_completed": len(completed),
        "final_correct": len(final_ok),
        "done_by_hand": len(by_hand),
        "operator_requests": dict(Counter(r["kind"] for r in ops)),
        "operator_actions": dict(Counter(r["action"] for r in ops)),
        "false_stop_rate": {
            "k": len(false_stops),
            "n": len(clean_wanted),
            "rate": round(len(false_stops) / len(clean_wanted), 4)
            if clean_wanted
            else None,
            "ci95": wilson(len(false_stops), len(clean_wanted)),
            "with_ui_change_in_force": sum(
                1 for r in false_stops if r.get("drift_active")
            ),
            "definition": "stops with nothing written on items whose data was right and "
            "that carried no item-level fault, over all such items",
        },
        "silent_wrong_rate": {
            "k": len(silent),
            "n": len(done_claims),
            "rate": round(len(silent) / len(done_claims), 4) if done_claims else None,
            "ci95": wilson(len(silent), len(done_claims)),
            "of_all_items": round(len(silent) / len(items), 4) if items else None,
            "definition": "items the arm claimed done that the independent oracle judged wrong, "
            "over all items the arm claimed done",
        },
        "stops": len(stops),
        "by_exception": {
            k: dict(Counter(r["outcome"] for r in items if r["exception"] == k))
            for k in sorted({r["exception"] for r in items})
        },
        "per_drift": per_drift,
        "wall": {
            "total_s": round(sum(wall), 1),
            "median_item_s": round(sorted(wall)[len(wall) // 2], 2) if wall else None,
            "load_1m_median": round(sorted(loads)[len(loads) // 2], 1)
            if loads
            else None,
            "load_1m_max": round(max(loads), 1) if loads else None,
        },
        "model": {
            "calls": sum(r.get("model_calls", 0) for r in items),
            "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in items), 4),
        },
        "bands": bands(),
        "headline": headline(),
    }


def group_by_arm(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r.get("arm", "")].append(r)
    return dict(out)
