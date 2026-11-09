"""Read every recorded stop and name why it stopped and why repair did not fix it.

    python benchmark/auto_repair/stop_causes.py --runs-root ROOT --out results/causes/<name>.csv \\
        [--bundle BUNDLE] [--label-depth N]

For every halted ``report.json`` under ``ROOT`` (the item's own run, not a
self-repair attempt or probe inside it), this writes one row with:

- ``stop``: what the halted step's error says, by rule (:func:`stop_kind`);
- ``blocker``: what kept repair from resuming the item, from the stop's
  transaction outcome and, when self-repair ran, its own evidence
  (``self-repair/self-repair.json``) (:func:`repair_blocker`);
- ``unsettled``: the steps whose effect the transaction classifier could not
  settle (a write may exist), when the run ended ``RECONCILIATION_REQUIRED``.

The root cause of each group of stops, which sometimes needs the
benchmark's own knowledge of what it changed, is in ``STOP_CAUSES.md``.
No record values are copied: rows carry step ids, rule names and the first
200 characters of the engine's own messages.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

#: Directories that hold a repair's own runs, not the item's.
_INNER = {"self-repair", "probe", "halt", "resume", "campaigns"}


def stop_kind(error: str) -> str:
    """What kind of stop the halted step's error describes."""

    if error.startswith("Postconditions failed"):
        failed = error.split("failed:", 1)[-1]
        kinds = set(
            re.findall(
                r"\b(region_stable|text_present|text_absent|"
                r"local_edit|field_value|url_changed|"
                r"title_changed|new_tab_opened)\b",
                failed,
            )
        )
        if kinds == {"region_stable"}:
            return "postcondition:region_only"
        if "text_present" in kinds and "region_stable" in kinds:
            return "postcondition:text_and_region"
        if "text_present" in kinds:
            return "postcondition:text"
        return "postcondition:other"
    if error.startswith("Identity check failed"):
        return "identity:mismatch"
    if "identity could not be read" in error:
        return "identity:unreadable"
    if "DOM locator is ambiguous" in error:
        return "target:ambiguous"
    if "not an identity-bearing actionable DOM element" in error:
        return "target:moved"
    if "only resolved via the 'geometry' rung" in error:
        return "target:geometry_only"
    if (
        error.startswith("Could not resolve target")
        or "all resolution rungs failed" in error
    ):
        return "target:not_found"
    if "closed-loop scroll exhausted" in error:
        return "target:scroll_budget"
    return "other"


def repair_blocker(outcome: Optional[str], repair: Optional[dict[str, Any]]) -> str:
    """What kept the stop from being resumed by repair."""

    if repair is not None and repair.get("status") == "resumed":
        return "none:resumed"
    if outcome == "RECONCILIATION_REQUIRED":
        return "reconciliation:effect_not_settled"
    if outcome == "REJECTED_POLICY":
        return "person:rejected_policy"
    if repair is None:
        return "not_run"
    steps = repair.get("steps") or []
    details = [str(s.get("detail") or "") for s in steps if not s.get("passed")]
    text = " | ".join(details)
    if "stopped before resolving its target for another reason" in text:
        return "person:not_a_missing_target"
    if "does not read as a save" in text:
        return "patch:proposed_control_not_a_save"
    if "reads as a save" in text:
        return "patch:proposed_control_write_shaped"
    if "the probe stopped" in text:
        return "probe:stopped_before_save"
    if "the agent gave up" in text:
        return "agent:gave_up"
    if text:
        return "other:" + details[-1][:60]
    return "other"


def _halted(report: dict[str, Any]) -> Optional[dict[str, Any]]:
    for result in reversed(report.get("results") or []):
        if not result.get("ok") and not result.get("skipped"):
            return result
    return None


def _unsettled(path: Path) -> list[str]:
    try:
        from openadapt_flow.ir import RunReport
        from openadapt_flow.transaction import steps_requiring_reconciliation

        report = RunReport.model_validate_json(path.read_text())
    except Exception:  # noqa: BLE001 - an old or partial report: unknown
        return []
    return steps_requiring_reconciliation(report)


def rows_for(root: Path, label_depth: int) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("report.json")):
        rel = path.parent.relative_to(root)
        if _INNER & set(rel.parts):
            continue
        report = json.loads(path.read_text())
        halted = _halted(report)
        if halted is None or report.get("success"):
            continue
        repair_path = path.parent / "self-repair" / "self-repair.json"
        repair = json.loads(repair_path.read_text()) if repair_path.is_file() else None
        outcome = report.get("transaction_outcome")
        error = str(halted.get("error") or "")
        rows.append(
            {
                "run": str(rel),
                "label": "/".join(rel.parts[:label_depth]) if label_depth else "",
                "outcome": outcome,
                "halted_step": halted.get("step_id"),
                "delivery_attempted": halted.get("delivery_attempted"),
                "risk": halted.get("risk"),
                "stop": stop_kind(error),
                "blocker": repair_blocker(outcome, repair),
                "unsettled": " ".join(_unsettled(path))
                if outcome == "RECONCILIATION_REQUIRED"
                else "",
                "error": error[:200],
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-root", required=True, action="append")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--label-depth",
        type=int,
        default=1,
        help="how many leading path parts name the stop's group (mode, block)",
    )
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []
    for root in args.runs_root:
        rows.extend(rows_for(Path(root).expanduser(), args.label_depth))
    out = Path(args.out)
    if not out.is_absolute():
        out = HERE / out
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["run"]
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    groups: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (row["label"], row["stop"], row["blocker"])
        groups[key] = groups.get(key, 0) + 1
    for (label, stop, blocker), count in sorted(groups.items()):
        print(f"{count:3d}  {label:28s} {stop:30s} {blocker}")
    print(f"{len(rows)} stops -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
