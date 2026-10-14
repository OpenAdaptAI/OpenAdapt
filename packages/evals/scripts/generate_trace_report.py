#!/usr/bin/env python3
"""Generate a markdown execution trace report from experiment screenshots and trajectory data.

Reads PNG screenshots (from ``--save-screenshots``) and optionally a JSONL
trajectory file (from :class:`PlannerTrajectoryLogger`) and produces a
self-contained markdown report with embedded screenshot references.

Usage::

    python scripts/generate_trace_report.py \\
        --screenshots /tmp/experiment_run/ \\
        --trajectory /tmp/trajectories.jsonl \\
        --output docs/traces/notepad_hello_2026_03_18.md \\
        --task-name "Open Notepad and type Hello World" \\
        --score 0.5
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("generate_trace_report")


def _load_trajectory(path: Path) -> dict[int, dict]:
    """Load trajectory JSONL and index by step_index.

    Returns a dict mapping ``step_index`` -> record dict.
    """
    steps: dict[int, dict] = {}
    if not path.exists():
        logger.warning("Trajectory file not found: %s", path)
        return steps
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            idx = record.get("step_index")
            if idx is not None:
                steps[int(idx)] = record
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Skipping malformed JSONL line: %s", exc)
    return steps


def _collect_screenshots(directory: Path) -> list[Path]:
    """Return sorted list of PNG files in *directory*."""
    pngs = sorted(directory.glob("*.png"))
    if not pngs:
        # Check one level deeper (trajectory logger stores in episode subdirs)
        pngs = sorted(directory.glob("*/*.png"))
    return pngs


def _step_number_from_filename(filename: str) -> int | None:
    """Extract step number from filenames like ``step_01.png`` or ``step_00_reset.png``."""
    import re

    m = re.search(r"step_(\d+)", filename)
    if m:
        return int(m.group(1))
    return None


def generate_report(
    *,
    screenshots_dir: Path,
    trajectory_path: Path | None,
    output_path: Path,
    task_name: str,
    score: float | None,
    run_date: str | None,
) -> Path:
    """Generate the markdown trace report.

    Screenshots are copied into the same directory as *output_path* so the
    markdown can reference them with relative paths.

    Returns the path to the generated report.
    """
    screenshots = _collect_screenshots(screenshots_dir)
    if not screenshots:
        logger.error("No PNG files found in %s", screenshots_dir)
        sys.exit(1)

    # Load trajectory data if available
    traj_steps: dict[int, dict] = {}
    if trajectory_path:
        traj_steps = _load_trajectory(trajectory_path)

    # Prepare output directory and copy screenshots
    output_path = output_path.resolve()
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    copied_filenames: list[tuple[int | None, str]] = []
    for src in screenshots:
        dst = output_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        step_num = _step_number_from_filename(src.name)
        copied_filenames.append((step_num, src.name))

    # Build markdown
    report_date = run_date or date.today().isoformat()
    total_steps = len([s for s, _ in copied_filenames if s is not None and s > 0])

    lines: list[str] = []
    lines.append(f"# Execution Trace: {task_name}")
    lines.append("")
    lines.append(f"> Date: {report_date}")
    if score is not None:
        lines.append(f"> Score: {score}")
    lines.append(f"> Steps: {total_steps}")
    lines.append("")

    for step_num, filename in copied_filenames:
        if step_num is None:
            lines.append("## Screenshot")
            lines.append(f"![Screenshot]({filename})")
            lines.append("")
            continue

        if step_num == 0:
            lines.append("## Step 0 (Reset)")
            lines.append(f"![Reset]({filename})")
            lines.append("")
            continue

        lines.append(f"## Step {step_num}")

        # Add trajectory metadata if available
        traj = traj_steps.get(step_num)
        if traj is None:
            # Trajectory logger uses 0-based indexing; steps in filenames
            # from run_planner_grounder are 1-based offset by 1.
            traj = traj_steps.get(step_num - 1)

        if traj:
            planner_out = traj.get("planner_output", {})
            if isinstance(planner_out, dict):
                instruction = planner_out.get("instruction", "")
                reasoning = planner_out.get("reasoning", "")
                decision = planner_out.get("decision", "")
                if instruction:
                    lines.append(f"**Planner**: {instruction}")
                if reasoning:
                    lines.append(f"**Reasoning**: {reasoning}")
                if decision:
                    lines.append(f"**Decision**: {decision}")

        lines.append(f"![Step {step_num}]({filename})")
        lines.append("")

    report_text = "\n".join(lines) + "\n"
    output_path.write_text(report_text, encoding="utf-8")
    logger.info("Report written to %s (%d screenshots, %d trajectory entries)",
                output_path, len(copied_filenames), len(traj_steps))
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a markdown execution trace from screenshots and trajectory data"
    )
    parser.add_argument(
        "--screenshots", required=True, type=Path,
        help="Directory containing step PNG screenshots",
    )
    parser.add_argument(
        "--trajectory", default=None, type=Path,
        help="Path to trajectories.jsonl from PlannerTrajectoryLogger",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Output markdown file path (e.g., docs/traces/run_report.md)",
    )
    parser.add_argument(
        "--task-name", default="Untitled Task",
        help="Human-readable task name for the report header",
    )
    parser.add_argument(
        "--score", type=float, default=None,
        help="Final evaluation score (0.0-1.0)",
    )
    parser.add_argument(
        "--date", default=None,
        help="Run date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Git-add and commit the report and screenshots after generation",
    )

    args = parser.parse_args()

    output = generate_report(
        screenshots_dir=args.screenshots,
        trajectory_path=args.trajectory,
        output_path=args.output,
        task_name=args.task_name,
        score=args.score,
        run_date=args.date,
    )

    if args.commit:
        import subprocess

        report_dir = output.parent
        subprocess.run(
            ["git", "add", str(output)] +
            [str(report_dir / f) for f in report_dir.iterdir() if f.suffix == ".png"],
            check=True,
        )
        msg = f"docs: add execution trace for {args.task_name}"
        subprocess.run(["git", "commit", "-m", msg], check=True)
        logger.info("Committed trace report: %s", msg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
