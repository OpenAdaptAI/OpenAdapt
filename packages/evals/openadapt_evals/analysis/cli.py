"""CLI for trace analysis.

Usage::

    # Print summary to stdout
    python -m openadapt_evals.analysis path/to/traces/

    # Generate HTML report
    python -m openadapt_evals.analysis path/to/traces/ --report output.html

    # Compare two runs
    python -m openadapt_evals.analysis path/to/traces/ --compare path/to/other/

    # Compare with HTML report
    python -m openadapt_evals.analysis path/to/traces/ --compare path/to/other/ --report diff.html

    # JSON output (for scripting)
    python -m openadapt_evals.analysis path/to/traces/ --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for trace analysis."""
    parser = argparse.ArgumentParser(
        prog="openadapt_evals.analysis",
        description="Analyze OpenAdapt evaluation traces",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m openadapt_evals.analysis results.jsonl\n"
            "  python -m openadapt_evals.analysis traces/ --report report.html\n"
            "  python -m openadapt_evals.analysis run1.jsonl --compare run2.jsonl\n"
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to trace file (.jsonl) or directory",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="FILE",
        help="Generate an HTML report to this path",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        metavar="PATH",
        help="Compare against another trace file/directory",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=15,
        help="Max steps per episode for timeout classification (default: 15)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output summary as JSON instead of formatted text",
    )

    args = parser.parse_args(argv)

    # Validate paths
    if not args.path.exists():
        print(f"Error: path does not exist: {args.path}", file=sys.stderr)
        return 1

    if args.compare and not args.compare.exists():
        print(f"Error: compare path does not exist: {args.compare}", file=sys.stderr)
        return 1

    # Import here to keep CLI fast for --help
    from openadapt_evals.analysis.trace_analyzer import TraceAnalyzer

    analyzer = TraceAnalyzer(args.path, max_steps=args.max_steps)

    compare_analyzer = None
    if args.compare:
        compare_analyzer = TraceAnalyzer(args.compare, max_steps=args.max_steps)

    # Summary
    summary = analyzer.summary()
    failures = analyzer.failure_modes()

    if args.json_output:
        output = {
            "summary": summary,
            "failure_modes": failures,
        }
        if compare_analyzer:
            output["comparison"] = analyzer.compare(compare_analyzer)
        print(json.dumps(output, indent=2, default=str))
    else:
        _print_summary(summary)
        _print_failures(failures)

        if compare_analyzer:
            comparison = analyzer.compare(compare_analyzer)
            _print_comparison(comparison)

    # HTML report
    if args.report:
        report_path = analyzer.generate_report(
            args.report,
            compare_with=compare_analyzer,
        )
        print(f"\nHTML report: {report_path}")

    return 0


def _print_summary(summary: dict) -> None:
    """Print formatted summary to stdout."""
    total_time = summary["total_time"]
    if total_time > 3600:
        time_str = f"{total_time / 3600:.1f}h"
    elif total_time > 60:
        time_str = f"{total_time / 60:.1f}m"
    else:
        time_str = f"{total_time:.0f}s"

    print()
    print("=" * 56)
    print("  TRACE ANALYSIS SUMMARY")
    print("=" * 56)
    print(f"  Model:            {summary.get('model') or 'unknown'}")
    print(f"  Episodes:         {summary['total_episodes']}")
    print(f"  Success rate:     {summary['success_rate']:.1%}")
    print(f"  Avg score:        {summary['avg_score']:.3f}")
    print(f"  Total steps:      {summary['total_steps']}")
    print(f"  Avg steps/ep:     {summary['avg_steps_per_episode']:.1f}")
    print(f"  Total time:       {time_str}")
    print(f"  Avg time/ep:      {summary['avg_time_per_episode']:.1f}s")
    print(f"  Est. cost:        ${summary['cost_estimate_usd']:.2f}")

    by_status = summary.get("episodes_by_status", {})
    if by_status:
        parts = [f"{k}: {v}" for k, v in by_status.items()]
        print(f"  Status breakdown: {', '.join(parts)}")

    print("=" * 56)


def _print_failures(failures: list[dict]) -> None:
    """Print failure mode breakdown."""
    if not failures:
        return

    print()
    print("Failure Modes:")
    print("-" * 44)
    for fm in failures:
        label = fm["mode"].replace("_", " ").title()
        print(f"  {label:24s}  {fm['count']:3d}  ({fm['percentage']:.0f}%)")
    print("-" * 44)


def _print_comparison(comparison: dict) -> None:
    """Print comparison results."""
    sd = comparison["summary_diff"]

    print()
    print("=" * 56)
    print("  RUN COMPARISON")
    print("=" * 56)
    sr_delta = sd["success_rate_delta"]
    arrow = "+" if sr_delta > 0 else ""
    print(f"  Success rate delta: {arrow}{sr_delta:.1%}")

    score_delta = sd["avg_score_delta"]
    arrow = "+" if score_delta > 0 else ""
    print(f"  Avg score delta:    {arrow}{score_delta:.3f}")

    print(f"  Improved:           {len(comparison['improved'])} tasks")
    print(f"  Regressed:          {len(comparison['regressed'])} tasks")
    print(f"  Unchanged:          {len(comparison['unchanged'])} tasks")
    print(f"  New tasks:          {len(comparison['new_tasks'])}")
    print(f"  Removed tasks:      {len(comparison['removed_tasks'])}")

    if comparison["improved"]:
        print()
        print("  Improved tasks:")
        for item in comparison["improved"][:10]:
            tid = item["task_id"][:20]
            print(
                f"    {tid:22s}  {item['old_score']:.2f} -> {item['new_score']:.2f}  (+{item['score_delta']:.2f})"
            )
        if len(comparison["improved"]) > 10:
            print(f"    ... and {len(comparison['improved']) - 10} more")

    if comparison["regressed"]:
        print()
        print("  Regressed tasks:")
        for item in comparison["regressed"][:10]:
            tid = item["task_id"][:20]
            print(
                f"    {tid:22s}  {item['old_score']:.2f} -> {item['new_score']:.2f}  ({item['score_delta']:.2f})"
            )
        if len(comparison["regressed"]) > 10:
            print(f"    ... and {len(comparison['regressed']) - 10} more")

    print("=" * 56)
