"""Score the held-out runs exactly as PREREGISTRATION.md defines the metrics.

    python benchmark/form_tx_heldout_run/summarize.py results/arm_B.jsonl results/arm_C.jsonl

Prints one JSON object per arm with every pre-registered count, pooled and
per portal. Denominators come from the answer key's expected_behavior.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import importlib  # noqa: E402

SUITE = "heldout"
for _arg in list(sys.argv[1:]):
    if _arg.startswith("--suite="):
        SUITE = _arg.split("=", 1)[1]
        sys.argv.remove(_arg)
_PKG = {"heldout": "benchmark.form_tx_heldout", "heldout2": "benchmark.form_tx_heldout2"}[SUITE]
_PACKS = {
    "heldout": "benchmark.form_tx_heldout_run.packs",
    "heldout2": "benchmark.form_tx_heldout_run.packs2",
}[SUITE]
PORTALS = importlib.import_module(_PKG + ".portals").PORTALS
load_answer_key = importlib.import_module(_PKG + ".scoring").load_answer_key


def _popup_keys(pid: str) -> set[str]:
    """Keys typed in a popup or returned from one, per the portal's pack."""
    try:
        pack = importlib.import_module(_PACKS).PACKS[pid][0]
    except Exception:  # noqa: BLE001 - arm A rows have no pack
        return set()
    keys: set[str] = set()
    for sub in pack.popup_subroutines:
        keys.add(sub.return_output_key)
        keys.update(b.dossier_key for b in sub.bindings)
    return keys

FLAGGED = {"readback_refuted", "readback_incomplete"}
TRAP = {"must_stop_before_submit", "needs_operator_answer"}


def summarize(rows: list[dict[str, Any]], key: dict[str, Any]) -> dict[str, Any]:
    def blank() -> dict[str, Any]:
        return defaultdict(int)

    pooled = blank()
    by_portal: dict[str, Any] = defaultdict(blank)
    for row in rows:
        case = key["cases"][row["case_id"]]
        behavior = case["expected_behavior"]
        score = row["score"]
        halts = row.get("halts", [])
        stops_before = [h for h in halts if h.get("before_submit")]
        submitted = bool(score.get("submitted"))
        outcome = row.get("outcome", "")
        for bucket in (pooled, by_portal[row["portal"]]):
            bucket["cases"] += 1
            bucket["trap_cases"] += behavior in TRAP
            bucket["clean_cases"] += behavior == "submit_clean"
            bucket["correct_submissions"] += bool(score.get("correct"))
            bucket["correct_and_verified"] += (
                bool(score.get("correct")) and outcome == "verified_done"
            )
            bucket["submitted"] += submitted
            bucket["unsubmitted"] += not submitted
            if submitted and not score.get("record_correct"):
                bucket["wrong_submitted"] += 1
                if outcome == "verified_done":
                    bucket["wrong_submitted_verified"] += 1
                elif outcome in FLAGGED:
                    bucket["wrong_submitted_flagged_after_submit"] += 1
                else:
                    bucket["wrong_submitted_silent"] += 1
            if behavior in TRAP and stops_before:
                bucket["trap_cases_stopped_before_submit"] += 1
                bucket["trap_stopped_then_correct"] += bool(score.get("correct"))
            if behavior == "submit_clean" and stops_before:
                bucket["false_stop_cases"] += 1
                bucket["false_stop_halts"] += len(stops_before)
            if (
                behavior in TRAP
                and stops_before
                and "submit_clean" in case.get("also_acceptable", [])
            ):
                bucket["stops_on_cases_where_clean_submit_was_acceptable"] += 1
            nc = len((row.get("readback") or {}).get("not_checked", []))
            bucket["fields_not_checked"] += nc
            bucket["cases_readback_incomplete"] += outcome == "readback_incomplete"
            bucket["operator_handbacks_halts"] += len(halts)
            bucket["operator_handbacks_post_submit_flags"] += (
                outcome in FLAGGED and submitted
            )
            bucket["harness_errors"] += outcome == "harness_error"
            bucket["cases_with_stop_before_submit"] += bool(stops_before)
            reached = submitted or bool(stops_before)
            bucket["reached"] += reached
            bucket["clean_reached"] += behavior == "submit_clean" and reached
            bucket["false_stop_cases_reached"] += (
                behavior == "submit_clean" and reached and bool(stops_before)
            )
            bucket["submitted_verified_with_receipt"] += (
                submitted and outcome == "verified_done" and bool(row.get("receipt"))
            )
            bucket["trap_cases_unreached"] += behavior in TRAP and not reached
            ncs = set((row.get("readback") or {}).get("not_checked", []))
            if submitted and outcome == "readback_incomplete" and ncs:
                if ncs <= _popup_keys(row["portal"]):
                    bucket["incomplete_only_from_popup_inputs"] += 1
            bucket[f"outcome:{outcome}"] += 1
    for b in (pooled, *by_portal.values()):
        b["every_case_stopped"] = int(
            b["cases"] > 0 and b["cases"] == b.get("cases_with_stop_before_submit", 0)
        )
    return {
        "pooled": dict(pooled),
        "by_portal": {k: dict(v) for k, v in by_portal.items()},
    }


def main(paths: list[str]) -> None:
    key = load_answer_key()
    for path in paths:
        rows = [
            json.loads(line)
            for line in Path(path).read_text().splitlines()
            if line.strip()
        ]
        expected = sum(len(PORTALS[p].cases) for p in PORTALS)
        out = summarize(rows, key)
        out["file"] = Path(path).name
        out["rows"] = len(rows)
        out["expected_rows"] = expected
        print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
