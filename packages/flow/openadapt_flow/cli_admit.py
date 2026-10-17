"""CLI: ``openadapt-flow admit status``."""

from __future__ import annotations

import argparse
import json
import sys

from openadapt_flow.release_admission import (
    ADMITTED,
    DEFAULT_LEDGER_URL,
    LedgerError,
    load_ledger,
    render_status,
    status_report,
)


def register_admit_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "admit",
        help=(
            "Read the published release-admission ledger. "
            "Does not sign or issue an admission."
        ),
    )
    verbs = parser.add_subparsers(dest="admit_cmd", required=True)
    status = verbs.add_parser(
        "status",
        help=(
            "Print admitted vs not-admitted for Flow from the public ledger. "
            "Never mints a signature."
        ),
    )
    status.add_argument(
        "--ledger",
        default=DEFAULT_LEDGER_URL,
        help=(
            "Public production-lifecycle JSON (URL or path). "
            f"Default: {DEFAULT_LEDGER_URL}"
        ),
    )
    status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the status report as JSON",
    )
    status.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 unless Flow currently holds a live admission",
    )
    status.set_defaults(func=cmd_admit_status)


def cmd_admit_status(args: argparse.Namespace) -> int:
    try:
        ledger = load_ledger(args.ledger)
        report = status_report(ledger)
    except LedgerError as exc:
        print(f"admit status: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_status(report, ledger_source=str(args.ledger)))
    if args.check and report["flow"]["state"] != ADMITTED:
        return 1
    return 0
