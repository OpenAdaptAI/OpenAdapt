#!/usr/bin/env python3
"""Update the weekly north-star metrics row in STATUS.md.

Computes hard-task success rates from benchmark summary artifacts and writes
the matching Monday row in the "Weekly North-Star Updates" table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_RESULTS_DIR = REPO_ROOT / "benchmark_results"
DEFAULT_STATUS_FILE = WORKSPACE_ROOT / "STATUS.md"

# Allow running as a direct script (e.g., `uv run python scripts/...`) without
# requiring PYTHONPATH to be manually set.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_evals.constants import HARDER_TASK_IDS


@dataclass(frozen=True)
class Trial:
    """Single task trial extracted from a run summary."""

    task_id: str
    condition: str  # "zs" or "dc"
    success: bool


def _parse_week_start(week_of: str | None) -> date:
    """Parse optional date and normalize to Monday."""
    if week_of:
        dt = date.fromisoformat(week_of)
    else:
        dt = datetime.now().date()
    return dt - timedelta(days=dt.weekday())


def _detect_condition(run_name: str) -> str | None:
    if run_name.startswith("val_zs_"):
        return "zs"
    if run_name.startswith("val_dc_"):
        return "dc"
    return None


def _load_trials(results_dir: Path, task_set: set[str]) -> list[Trial]:
    """Load trials from summary.json files under benchmark_results."""
    trials: list[Trial] = []
    for summary_path in sorted(results_dir.rglob("summary.json")):
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        run_name = str(data.get("run_name", ""))
        condition = _detect_condition(run_name)
        if condition is None:
            continue

        for task in data.get("tasks", []):
            task_id = str(task.get("task_id", ""))
            if task_id not in task_set:
                continue
            success = bool(task.get("success", False))
            # Fallback for runs that only report score.
            if not success:
                try:
                    success = float(task.get("score", 0.0)) > 0.0
                except Exception:
                    success = False
            trials.append(Trial(task_id=task_id, condition=condition, success=success))
    return trials


def _fmt_rate(value: float | None) -> str:
    if value is None:
        return "TBD"
    return f"{value * 100:.1f}%"


def _fmt_delta(zs_rate: float | None, dc_rate: float | None) -> str:
    if zs_rate is None or dc_rate is None:
        return "TBD"
    delta = (dc_rate - zs_rate) * 100.0
    return f"{delta:+.1f}"


def _compute_metrics(
    trials: list[Trial],
    task_ids: list[str],
    target_trials: int,
) -> tuple[str, str, str, str, str]:
    """Return (trials_col, zs_rate, dc_rate, delta, status)."""
    counts = {
        "zs": {tid: 0 for tid in task_ids},
        "dc": {tid: 0 for tid in task_ids},
    }
    success = {"zs": 0, "dc": 0}
    totals = {"zs": 0, "dc": 0}

    for trial in trials:
        counts[trial.condition][trial.task_id] += 1
        totals[trial.condition] += 1
        if trial.success:
            success[trial.condition] += 1

    zs_rate = (
        success["zs"] / totals["zs"] if totals["zs"] > 0 else None
    )
    dc_rate = (
        success["dc"] / totals["dc"] if totals["dc"] > 0 else None
    )

    zs_min = min(counts["zs"].values()) if counts["zs"] else 0
    zs_max = max(counts["zs"].values()) if counts["zs"] else 0
    dc_min = min(counts["dc"].values()) if counts["dc"] else 0
    dc_max = max(counts["dc"].values()) if counts["dc"] else 0
    trials_col = (
        f"ZS {zs_min}-{zs_max}; DC {dc_min}-{dc_max} "
        f"(target {target_trials})"
    )

    if totals["zs"] == 0 and totals["dc"] == 0:
        status = "PLANNED"
    else:
        zs_complete = all(v >= target_trials for v in counts["zs"].values())
        dc_complete = all(v >= target_trials for v in counts["dc"].values())
        status = "COMPLETE" if zs_complete and dc_complete else "IN PROGRESS"

    return (
        trials_col,
        _fmt_rate(zs_rate),
        _fmt_rate(dc_rate),
        _fmt_delta(zs_rate, dc_rate),
        status,
    )


def _upsert_week_row(
    status_md: Path,
    week_monday: date,
    task_label: str,
    trials_col: str,
    zs_rate: str,
    dc_rate: str,
    delta: str,
    status: str,
    notes: str,
) -> None:
    """Replace or insert the weekly row in STATUS.md."""
    text = status_md.read_text(encoding="utf-8")
    lines = text.splitlines()

    heading = "### Weekly North-Star Updates (Scaffold)"
    try:
        heading_idx = lines.index(heading)
    except ValueError as exc:
        raise RuntimeError(f"Could not find heading: {heading}") from exc

    # Find table boundaries.
    table_header_idx = None
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].startswith("| Week Of (Monday) |"):
            table_header_idx = i
            break
    if table_header_idx is None:
        raise RuntimeError("Could not find weekly metrics table header in STATUS.md")

    table_start = table_header_idx + 2  # skip header + separator
    table_end = table_start
    while table_end < len(lines) and lines[table_end].startswith("|"):
        table_end += 1

    week_str = week_monday.isoformat()
    new_row = (
        f"| {week_str} | {task_label} | {trials_col} | {zs_rate} | "
        f"{dc_rate} | {delta} | {status} | {notes} |"
    )

    week_row_regex = re.compile(rf"^\|\s*{re.escape(week_str)}\s*\|")
    replaced = False
    for i in range(table_start, table_end):
        if week_row_regex.match(lines[i]):
            lines[i] = new_row
            replaced = True
            break

    if not replaced:
        lines.insert(table_end, new_row)

    status_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help=f"Benchmark results directory (default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=DEFAULT_STATUS_FILE,
        help=f"STATUS.md file to update (default: {DEFAULT_STATUS_FILE})",
    )
    parser.add_argument(
        "--week-of",
        type=str,
        default=None,
        help="Week date (YYYY-MM-DD). Normalized to Monday. Default: current week.",
    )
    parser.add_argument(
        "--target-trials",
        type=int,
        default=3,
        help="Target trials per task per condition (default: 3).",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default="Auto-updated from benchmark_results summaries.",
        help="Notes text for the row.",
    )
    args = parser.parse_args()

    task_ids = list(HARDER_TASK_IDS)
    task_set = set(task_ids)
    week_monday = _parse_week_start(args.week_of)

    trials = _load_trials(args.results_dir, task_set)
    trials_col, zs_rate, dc_rate, delta, status = _compute_metrics(
        trials=trials,
        task_ids=task_ids,
        target_trials=args.target_trials,
    )

    _upsert_week_row(
        status_md=args.status_file,
        week_monday=week_monday,
        task_label=f"Fixed {len(task_ids)} hard WAA tasks",
        trials_col=trials_col,
        zs_rate=zs_rate,
        dc_rate=dc_rate,
        delta=delta,
        status=status,
        notes=args.notes,
    )

    print(f"Updated weekly north-star row for {week_monday.isoformat()}")
    print(f"  STATUS: {args.status_file}")
    print(f"  Trials: {trials_col}")
    print(f"  ZS: {zs_rate}")
    print(f"  DC: {dc_rate}")
    print(f"  Delta (pp): {delta}")
    print(f"  Row status: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
