"""``openadapt-flow silent-saves`` (or ``python -m silent_saves``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import protocol, runner

EPILOG = """\
executors (--agent):
  screen-script                 clicks through and believes the screen ($0)
  claude-computer-use           the scoreboard's agent (paid; needs --allow-paid)
  openadapt-program:<cache>     an admitted OpenAdapt program (no model calls)
  command:'<shell command>'     any executor; see README.md for the contract
  python:<module>:<class>       a Python class with name and run(task) -> Report

examples:
  openadapt-flow silent-saves --agent screen-script
  openadapt-flow silent-saves --agent screen-script --readback
  openadapt-flow silent-saves --agent command:'node my_agent.js' --per-fault 10
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openadapt-flow silent-saves",
        description=(
            "Score a GUI executor on injected bad saves: how often it reports "
            "success while the system of record holds a wrong, partial, "
            f"duplicate or missing save. Fault set {protocol.VERSION} "
            f"({protocol.PROTOCOL})."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--agent", required=True, help="The executor (see below)")
    p.add_argument(
        "--readback",
        action="store_true",
        help="Gate the executor's claim on OpenAdapt's read-back (verify-run)",
    )
    p.add_argument(
        "--per-fault",
        type=int,
        default=protocol.FAULT_SET["per_fault_default"],
        help="Items per fault kind (default %(default)s)",
    )
    p.add_argument(
        "--faults",
        default=",".join(protocol.FAULTS),
        help="Comma-separated subset of the fault set (default: all)",
    )
    p.add_argument(
        "--run-id", default=None, help="Results go to results/<agent>/<run-id>"
    )
    p.add_argument("--out", default=None, help="Results directory (overrides --run-id)")
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for the app the executor opens (default: any free port)",
    )
    p.add_argument(
        "--headed", action="store_true", help="Show the screen-script browser"
    )
    p.add_argument(
        "--allow-paid",
        action="store_true",
        help="Allow an executor that calls a paid model",
    )
    p.add_argument(
        "--max-cost",
        type=float,
        default=0.5,
        help="Per-item spend cap for claude-computer-use (USD)",
    )
    p.add_argument("--json", action="store_true", help="Print the summary as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.agent.startswith("claude-computer-use") and not args.allow_paid:
        print(
            "claude-computer-use calls a paid model; pass --allow-paid to run it",
            file=sys.stderr,
        )
        return 2
    if args.agent.startswith("python:") and str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    faults = tuple(f.strip() for f in args.faults.split(",") if f.strip())
    out = (
        Path(args.out)
        if args.out
        else runner.default_out(args.agent, args.readback, args.run_id)
    )
    summary = runner.run(
        args.agent,
        out=out,
        per_fault=args.per_fault,
        faults=faults,
        readback=args.readback,
        port=args.port,
        headed=args.headed,
        max_cost_usd=args.max_cost,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        runner.print_summary(summary)
        print(f"rows: {out / 'items.csv'}")
    return 0
