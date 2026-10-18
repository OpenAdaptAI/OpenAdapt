"""``openadapt-flow deferred``: settle saves that a later report confirms.

``deferred settle`` reads a report (an X12 999 or 277CA, a CSV export, a saved
status page) and settles the waiting checks it mentions, then expires the ones
past their deadline. ``deferred status RUN`` gives a run's outcome after its
checks settle. See :mod:`openadapt_flow.runtime.effects.deferred_report`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from openadapt_flow.reports.parse import ReportError, ReportFormat

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.runtime.effects.deferred_report import (
        DeferredReportSpec,
        PendingLedger,
    )


def register_deferred_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "deferred",
        help="Settle saves that a later report confirms (claims acks, exports)",
    )
    verbs = parser.add_subparsers(dest="deferred_cmd", required=True)

    settle = verbs.add_parser(
        "settle",
        help="Match a report against the waiting checks; expire overdue ones",
    )
    settle.add_argument(
        "--config",
        default=None,
        help="deployment.yaml whose effects.deferred_report names the ledger "
        "and the report format",
    )
    settle.add_argument("--ledger", default=None, help="Pending-check ledger directory")
    settle.add_argument(
        "--report", default=None, help="The report file (omit to only expire)"
    )
    settle.add_argument(
        "--format",
        default=None,
        choices=["csv", "x12-999", "x12-277ca", "text"],
        help="Report format (default: from --config)",
    )
    settle.add_argument("--key-column", default=None)
    settle.add_argument("--status-column", default=None)
    settle.add_argument("--accept", action="append", default=[])
    settle.add_argument("--reject", action="append", default=[])
    settle.add_argument("--pattern", default=None)
    settle.add_argument(
        "--report-time",
        default=None,
        help="When the report was produced (ISO 8601 with a time zone). "
        "Required for csv and text reports; X12 reports carry their own",
    )
    settle.set_defaults(func=cmd_deferred_settle)

    status = verbs.add_parser(
        "status", help="A run's outcome after its deferred checks settle"
    )
    status.add_argument("run", help="Run directory (holds report.json)")
    status.add_argument("--config", default=None)
    status.add_argument("--ledger", default=None)
    status.set_defaults(func=cmd_deferred_status)


def _from_config(path: Optional[str]) -> Optional["DeferredReportSpec"]:
    if not path:
        return None
    from openadapt_flow.deployment import load_deployment

    cfg = load_deployment(path)
    sections = [cfg.effects, *cfg.effects.candidates]
    for section in sections:
        if section.deferred_report is not None:
            return section.deferred_report
    raise ValueError(f"{path} declares no effects.deferred_report")


def _ledger(
    args: argparse.Namespace, spec: Optional["DeferredReportSpec"]
) -> "PendingLedger":
    from openadapt_flow.runtime.effects.deferred_report import PendingLedger

    directory = args.ledger or (spec.ledger if spec is not None else None)
    if not directory:
        raise ValueError("name the ledger with --ledger or --config")
    return PendingLedger(directory)


def _format(
    args: argparse.Namespace, spec: Optional["DeferredReportSpec"]
) -> Optional[ReportFormat]:
    if args.format:
        return ReportFormat(
            format=args.format,
            key_column=args.key_column,
            status_column=args.status_column,
            accept=list(args.accept),
            reject=list(args.reject),
            pattern=args.pattern,
        )
    return spec.report if spec is not None else None


def cmd_deferred_settle(args: argparse.Namespace) -> int:
    from openadapt_flow.runtime.effects.deferred_report import settle_report_file

    try:
        spec = _from_config(args.config)
        ledger = _ledger(args, spec)
        fmt = _format(args, spec)
        if args.report:
            if fmt is None:
                raise ValueError("name the report format with --format or --config")
            issued_at = None
            if args.report_time:
                issued_at = datetime.fromisoformat(args.report_time)
                if issued_at.tzinfo is None:
                    raise ValueError("--report-time needs a time zone")
            settled = settle_report_file(
                ledger, Path(args.report), fmt, issued_at=issued_at
            )
        else:
            settled = ledger.settle(None, None)
    except (OSError, ValueError, ReportError) as exc:
        print(f"deferred settle: {exc}", file=sys.stderr)
        return 2
    waiting = [e for e in ledger.entries() if e.state == "waiting"]
    for item in settled:
        word = "VERIFIED" if item.state == "verified" else "RECONCILIATION_REQUIRED"
        print(f"{item.id}: {word} ({item.reason}; code {item.code})")
    print(f"Settled {len(settled)}; {len(waiting)} still waiting.")
    return 0


def cmd_deferred_status(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import RunReport
    from openadapt_flow.runtime.effects.deferred_report import settled_outcome

    try:
        spec = _from_config(args.config)
        ledger = _ledger(args, spec)
        report = RunReport.model_validate_json(
            (Path(args.run) / "report.json").read_text()
        )
    except (OSError, ValueError) as exc:
        print(f"deferred status: {exc}", file=sys.stderr)
        return 2
    outcome = settled_outcome(report, ledger)
    billable = "billable" if outcome == "VERIFIED" else "not billable"
    print(f"run ended {report.transaction_outcome}; now {outcome} ({billable})")
    return 0 if outcome == "VERIFIED" else 1


__all__ = ["register_deferred_parser"]
