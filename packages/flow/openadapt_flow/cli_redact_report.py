"""``openadapt-flow redact-report``: remove session tokens from an existing run's report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _cmd_redact_report(args: argparse.Namespace) -> int:
    from openadapt_flow.report_redaction import (
        RedactionPolicyError,
        RedactRunError,
        load_policy,
        redact_run,
    )

    try:
        policy = load_policy(Path(args.policy) if args.policy else None)
        result = redact_run(Path(args.run_dir), policy)
    except (RedactionPolicyError, RedactRunError) as exc:
        print(f"not redacted: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "run_dir": str(result.run_dir),
                    "changed": result.changed,
                    "kept": str(result.kept) if result.kept else None,
                    "moved_seals": list(result.moved_seals),
                    "redactions": {
                        name: [r.wire() for r in found]
                        for name, found in result.redactions.items()
                    },
                },
                indent=2,
            )
        )
        return 0
    if not result.changed:
        print(f"nothing to redact in {result.run_dir}")
        return 0
    for name, found in result.redactions.items():
        if not found:
            continue
        print(f"redacted {len(found)} value(s) in {name}:")
        for r in found:
            print(f"  {r.pointer}  {r.reason}")
    print(f"original kept on this machine (never publish it): {result.kept}")
    if result.moved_seals:
        print(
            "the old Seal covered the original bytes and was moved there too; "
            f"seal again: openadapt-flow local-seal run {result.run_dir}"
        )
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "redact-report",
        help=(
            "Remove session tokens and credentials from an existing run's "
            "report.json, keeping the original locally, so it can be sealed again"
        ),
    )
    p.add_argument("run_dir", help="Run directory holding report.json")
    p.add_argument(
        "--policy",
        default=None,
        help=(
            "Redaction policy JSON (default OPENADAPT_FLOW_REPORT_REDACTION, "
            "else $OPENADAPT_HOME/report-redaction.json, else the built-in list)"
        ),
    )
    p.add_argument("--json", action="store_true", help="Print the result as JSON")
    p.set_defaults(func=_cmd_redact_report)
