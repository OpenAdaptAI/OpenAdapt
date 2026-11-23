"""CLI runner for the OpenAdapt Real-App Verification Matrix.

Usage:
    python -m benchmarks.real_apps run --app all --mode contract \\
        --output-json /tmp/scoreboard.json --output-md /tmp/scoreboard.md
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Literal, cast

from benchmarks.real_apps.harness import (
    RealAppHarness,
    RunMode,
    render_scoreboard_json,
    render_scoreboard_markdown,
)
from benchmarks.real_apps.registry import list_app_specs, load_adapters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.real_apps",
        description="Run the OpenAdapt Real-App Verification Matrix.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List registered Wave 1 real applications.")

    p_run = sub.add_parser("run", help="Run contract or live verification.")
    p_run.add_argument(
        "--app",
        default="all",
        help="App ID to run (e.g. openemr, odoo17, twenty_crm, gnucash) or 'all'.",
    )
    p_run.add_argument(
        "--mode",
        choices=("contract", "live"),
        default="contract",
        help="Verification mode: 'contract' (in-process) or 'live' (Docker/desktop).",
    )
    p_run.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Optional working directory (defaults to a temporary directory).",
    )
    p_run.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write JSON scoreboard to this path.",
    )
    p_run.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Write Markdown scoreboard to this path.",
    )

    args = parser.parse_args(argv)

    if args.command == "list":
        for spec in list_app_specs():
            print(
                f"{spec.app_id:12s}  {spec.display_name:20s}  "
                f"{spec.surface:8s}  {spec.tier:7s}  {spec.persistence_oracle}"
            )
        return 0

    mode = cast(RunMode, cast(Literal["contract", "live"], args.mode))
    adapters = load_adapters(args.app)
    harness = RealAppHarness(adapters)

    if args.work_dir is not None:
        results = harness.run_all(mode=mode, work_dir=args.work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="oa-real-apps-") as tmp:
            results = harness.run_all(mode=mode, work_dir=Path(tmp))

    json_text = render_scoreboard_json(results, mode=mode)
    md_text = render_scoreboard_markdown(results, mode=mode)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json_text, encoding="utf-8")
    if args.output_md is not None:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(md_text, encoding="utf-8")

    print(md_text)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
