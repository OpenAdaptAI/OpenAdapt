"""CLI entrypoint for `python -m benchmarks.osworld_v2`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmarks.osworld_v2.metrics import (
    evaluate_contract_suites,
    render_markdown_scoreboard,
)


def _run_command(args: argparse.Namespace) -> int:
    if args.mode == "live":
        try:
            __import__("desktop_env")
        except ImportError:
            print(
                "Live OSWorld/OS-Harm mode requires the optional `desktop_env` VM runner "
                "(not installed in this environment). Run with `--mode contract` for offline "
                "contract verification.",
                file=sys.stderr,
            )
            return 2

    board = evaluate_contract_suites(suite=args.suite)
    md = render_markdown_scoreboard(board)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(board.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.output_md is not None:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(md, encoding="utf-8")

    print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.osworld_v2",
        description="Run OpenAdapt OSWorld 2.0 + OS-Harm benchmark suites.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Execute benchmark suite and render scoreboard.")
    run_p.add_argument(
        "--suite",
        choices=("osworld2", "os_harm", "all"),
        default="all",
    )
    run_p.add_argument(
        "--mode",
        choices=("contract", "live"),
        default="contract",
    )
    run_p.add_argument("--output-json", type=Path, default=None)
    run_p.add_argument("--output-md", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
