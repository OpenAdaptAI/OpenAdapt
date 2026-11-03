"""CLI: ``openadapt-flow reconcile`` -- do the EHR, clearinghouse and payer
agree on one prior authorization or claim? See ``docs/RECONCILE.md``.

    openadapt-flow reconcile --spec pa.reconcile.yaml --param submission_id=PA-1042

``--worklist units.jsonl`` reconciles one unit per line (each line a JSON
object of parameters). The JSON reply holds closed fields only. Exit 0 when
every unit agrees, 1 when any shows a REFUTED break, 2 when none does but
any is UNCERTAIN.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reconcile",
        help="Check that the EHR, clearinghouse and payer agree on a prior "
        "authorization or claim, and report each break",
    )
    p.add_argument("--spec", required=True, help="an openadapt.reconcile/v1 YAML")
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="the unit's identity (repeatable)",
    )
    p.add_argument("--worklist", help="JSON Lines: one unit's parameters per line")
    p.add_argument("--now", help="judge due times at this ISO 8601 time")
    p.add_argument("--out", help="write the JSON reply here too")
    p.set_defaults(func=cmd_reconcile)


def cmd_reconcile(args: argparse.Namespace) -> int:
    from openadapt_flow.reconcile import ReconcileSpec, reconcile
    from openadapt_flow.verifier import Outcome, VerifierError

    try:
        spec = ReconcileSpec.load(args.spec)
        units: list[dict[str, object]] = []
        if args.worklist:
            for line in Path(args.worklist).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    units.append(json.loads(line))
        if args.param or not units:
            one: dict[str, object] = {}
            for item in args.param:
                name, eq, value = item.partition("=")
                if not eq or not name:
                    raise VerifierError(f"--param {item!r}: use NAME=VALUE")
                one[name] = value
            units.append(one)
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else None
        )
        results = [reconcile(spec, unit, now=now) for unit in units]
    except (VerifierError, ValueError, OSError) as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    body = [r.to_dict() for r in results]
    text = json.dumps(body[0] if len(body) == 1 else body, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    outcomes = {r.outcome for r in results}
    if Outcome.REFUTED in outcomes:
        return 1
    return 2 if Outcome.UNCERTAIN in outcomes else 0
