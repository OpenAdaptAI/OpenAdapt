"""Turn recorded soak stops into the stop-queue cards Desktop would show.

Input: a lifecycle soak arm directory (``ledger.jsonl`` and ``workload.jsonl``
from ``packages/flow/benchmark/lifecycle/results/<run>/<arm>/``). Output: a
drill file with the first N stops the soak's operator handled, each as:

* the card Desktop's ``stop_queue`` builds for it (the same code path the app
  uses), from the item's values and the stop Flow reported;
* the possible matches the app's duplicate list held at that moment,
  reconstructed from the soak: the charts that existed before the run
  (``existing_chart``), the charts earlier items created, and a chart another
  clerk created just before the item. A row is listed when it shares the
  item's last name or date of birth, the rule the duplicate check applies;
* the answer the ground truth calls right, and what the soak's scripted
  operator did and was charged.

The patients are the soak's synthetic ones. Run from ``packages/desktop``::

    uv run python benchmark/stop_queue/build_drill.py \\
        --arm PATH/TO/after --out benchmark/stop_queue/drill-2026-10-24.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from openadapt_desktop import stop_queue
from openadapt_desktop.work_items import allowed_actions

#: Columns the app's possible-duplicate list shows.
LIST_FIELDS = ("lname", "fname", "DOB", "sex", "street")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _transaction(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def _expected(kind: str, *, chart_wanted: bool, final_right: bool) -> set[str]:
    """The answers the ground truth calls right for one card."""

    if kind == "same_record":
        return {"different_by_hand"} if chart_wanted else {"same", "decline"}
    if kind == "confirm_save":
        return {"saved"} if final_right else {"fixed_by_hand"}
    # A stop before saving that isn't a duplicate question: the inputs were
    # refused (missing or malformed values).
    return {"by_hand"} if chart_wanted else {"skip", "decline"}


def build(arm: Path, *, count: int = 20) -> dict[str, Any]:
    ledger = _rows(arm / "ledger.jsonl")
    workload = _rows(arm / "workload.jsonl")
    items = {r["item_id"]: r for r in workload if r.get("type") == "item"}
    existing = [r for r in workload if r.get("type") == "existing_chart"]
    results = {r["item_id"]: r for r in ledger if r.get("type") == "item_result"}
    stops = [
        r
        for r in ledger
        if r.get("type") == "operator" and r.get("kind") in {"stop", "reconcile"}
    ][:count]

    out = []
    for stop in stops:
        item = items[stop["item_id"]]
        truth = item["truth"]
        result = results[stop["item_id"]]
        reason = str(stop.get("reason") or "")
        transaction = _transaction(reason)
        nothing_written = transaction in {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY"}
        # Charts in the app when this item ran: the starting set, every
        # earlier item that made one, and another clerk's just before it.
        charts = [dict(c) for c in existing]
        for earlier in sorted(results.values(), key=lambda r: r["seq"]):
            if earlier["seq"] >= item["seq"]:
                break
            made = (earlier.get("final") or {}).get("new_charts") or 0
            if made:
                charts.append(items[earlier["item_id"]]["truth"]["person"])
        for event in truth.get("inject") or []:
            if event.get("event") == "other_clerk_creates" and event.get("person"):
                charts.append(event["person"])
        params = item["params"]
        lname = str(params.get("lname") or "").strip().casefold()
        dob = str(params.get("DOB") or "").strip()
        candidates = [
            {f: str(c.get(f) or "") for f in LIST_FIELDS}
            for c in charts
            if (lname and str(c.get("lname") or "").strip().casefold() == lname)
            or (dob and c.get("DOB") == dob)
        ]
        desktop_item = {
            "item_id": stop["item_id"],
            "source": "item",
            "workflow_id": "openemr-referral",
            "task_name": "New patient referral",
            "label": f"Referral {stop['item_id']}",
            # No retained frames from the soak: the card shows no screenshot.
            "run_id": None,
            "status": "open",
            "nothing_written": nothing_written,
            "result": "stopped_before_saving" if nothing_written else "check_record",
            "approval": None,
            "halt_kind": "screen_changed" if nothing_written else "save_not_confirmed",
            "halt_reason": reason[:500],
            "halt_step": "",
            "expected": "",
            "observed": "",
            "field_diffs": [],
            "inputs": {k: str(v) for k, v in params.items() if v is not None},
            "candidates": candidates if "no_duplicate_candidate" in reason else [],
            "created_at": None,
        }
        desktop_item["actions"] = allowed_actions(desktop_item)
        card = stop_queue.card_for_item(desktop_item)
        if card is None:
            continue
        chart_wanted = bool(result.get("chart_wanted"))
        final_right = (result.get("final") or {}).get("verdict") == "correct"
        out.append(
            {
                "item_id": stop["item_id"],
                "exception": truth.get("exception"),
                "rationale": truth.get("rationale"),
                "card": card,
                "expected": sorted(
                    _expected(card["kind"], chart_wanted=chart_wanted, final_right=final_right)
                ),
                # A card a quick glance would get wrong: a possible match that
                # is a different person, or a save that went in wrong.
                "hard": card["kind"] == "same_record" and chart_wanted
                or card["kind"] == "confirm_save" and not final_right,
                "soak_operator": {
                    "action": stop.get("action"),
                    "rule": stop.get("rule"),
                    "charged_s": stop.get("charged_s"),
                    "cost_model": stop.get("cost_model"),
                },
            }
        )
    return {
        "schema": "openadapt.stop-queue-drill/v1",
        "source": str(arm),
        "selection": f"the first {count} stops the soak's operator handled, in order",
        "stops": out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    drill = build(args.arm, count=args.count)
    args.out.write_text(json.dumps(drill, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(drill['stops'])} stops to {args.out}")


if __name__ == "__main__":
    main()
