"""``openadapt plan``: dry-run, apply and undo a batch of writes to external systems.

    openadapt plan create --tx openadapt.tx.yaml --task T-1 writes.json
    openadapt plan show PLAN
    openadapt plan approve PLAN           # a person, at a terminal
    openadapt plan apply PLAN
    openadapt plan undo PLAN
    openadapt plan release PLAN           # irreversible writes after holdback
    openadapt plan cancel PLAN            # drop held writes before release
    openadapt plan status

``create`` reads every record the batch touches, predicts the diff (a staged
transaction for a database, a computed shadow for a REST API, the holdback
queue for email) and prints what would change. ``approve`` binds to that exact
plan digest, once. ``apply`` refuses a plan whose records moved since, and
each write carries an idempotency key derived from the task and the record,
never from the payload: a retried or regenerated write with a different
payload is refused. ``undo`` compensates each write and proves it by reading
every record and the whole frame back. See ``docs/TRANSACTIONS.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "plan",
        help="Dry-run a batch of writes to external systems, then apply it, undo it or release it",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(p)
    p.set_defaults(func=cmd_plan)


def _fill(p: argparse.ArgumentParser) -> None:
    verbs = p.add_subparsers(dest="plan_verb", required=True)

    def common(q: argparse.ArgumentParser, tx: bool = True) -> None:
        if tx:
            q.add_argument("--tx", default="openadapt.tx.yaml", help="the tx file (default: ./openadapt.tx.yaml)")
        q.add_argument("--state", help="the state folder (default: $OPENADAPT_TX_STATE or ~/.openadapt/tx)")
        q.add_argument("--json", action="store_true", help="print JSON")

    q = verbs.add_parser("create", help="read the baseline and print the predicted diff")
    q.add_argument("--task", required=True, help="the task this batch belongs to (OpenAdapt's id, not the model's)")
    q.add_argument("writes", help="a JSON file: a list of writes, or {\"writes\": [...]}; - for stdin")
    common(q)
    for verb, text in (
        ("show", "print a plan"),
        ("apply", "apply an approved plan"),
        ("undo", "undo an applied plan and prove it by read-back"),
        ("release", "release held irreversible writes whose holdback passed"),
        ("cancel", "cancel held writes before release"),
    ):
        q = verbs.add_parser(verb, help=text)
        q.add_argument("plan", help="a plan digest or its prefix")
        common(q, tx=verb not in ("show", "cancel"))
    q = verbs.add_parser("approve", help="approve one plan digest, once (a person at a terminal)")
    q.add_argument("plan")
    q.add_argument("--as", dest="approver", default=None, help="who approves (default: $USER)")
    q.add_argument(
        "--automation",
        action="store_true",
        help="record the approval as automation (lab runs; the receipt then says agent)",
    )
    common(q, tx=False)
    q = verbs.add_parser("status", help="list held writes and ledger entries")
    common(q, tx=False)


def _ctx(args: argparse.Namespace, need_spec: bool = True):  # type: ignore[no-untyped-def]
    from openadapt_flow.txplan.engine import Ctx
    from openadapt_flow.txplan.spec import load_spec
    from openadapt_flow.txplan.state import State

    state = State(Path(args.state) if args.state else None)
    spec = load_spec(Path(args.tx)) if need_spec else None
    return Ctx(spec=spec, state=state)  # type: ignore[arg-type]


def cmd_plan(args: argparse.Namespace) -> int:
    from openadapt_flow.txplan import engine
    from openadapt_flow.txplan.adapters import AdapterError
    from openadapt_flow.txplan.spec import TxSpecError
    from openadapt_flow.txplan.state import StateError

    verb = args.plan_verb
    try:
        ctx = _ctx(args, need_spec=verb in ("create", "apply", "undo", "release"))
        if verb == "status":
            out = {
                "held": ctx.state.held(),
                "ledger": {e.key: e.status for e in ctx.state.entries()},
            }
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0
        if verb == "create":
            raw = sys.stdin.read() if args.writes == "-" else Path(args.writes).read_text(encoding="utf-8")
            data = json.loads(raw)
            writes = data.get("writes") if isinstance(data, dict) else data
            plan = engine.make_plan(ctx, args.task, writes or [])
            if args.json:
                print(json.dumps({k: v for k, v in plan.items() if k != "anomaly_details"}, indent=2, sort_keys=True))
            else:
                print("\n".join(engine.summary_lines(plan)))
                print(f"approve: openadapt plan approve {plan['digest'][:16]}")
            return 0
        digest = ctx.state.find_plan(args.plan)
        if verb == "show":
            plan, priv = engine.load_plan(ctx.state, digest)
            plan["anomaly_details"] = priv.get("anomalies", [])
            print(json.dumps(plan, indent=2, sort_keys=True) if args.json else "\n".join(engine.summary_lines(plan)))
            return 0
        if verb == "approve":
            if args.automation:
                decided_by = "automation"
            else:
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    print("approve needs a person at a terminal (or --automation for a lab run)", file=sys.stderr)
                    return 2
                plan, priv = engine.load_plan(ctx.state, digest)
                plan["anomaly_details"] = priv.get("anomalies", [])
                print("\n".join(engine.summary_lines(plan)))
                typed = input(f"Type the first 8 characters of the plan digest ({digest[:4]}...) to approve: ")
                if typed.strip() != digest[:8]:
                    print("not approved", file=sys.stderr)
                    return 1
                decided_by = "human"
            approver = args.approver or os.environ.get("USER") or "unknown"
            engine.approve_plan(ctx.state, digest, approver=approver, decided_by=decided_by)
            print(f"approved plan {digest[:12]} once")
            return 0
        fn = {"apply": engine.apply_plan, "undo": engine.undo_plan, "release": engine.release}.get(verb)
        if verb == "cancel":
            n = engine.cancel(ctx, digest)
            print(f"cancelled {n} held write(s)")
            return 0
        assert fn is not None
        result = fn(ctx, digest)
        print(json.dumps({k: result[k] for k in ("outcome", "tx", "run_dir")}, indent=2, sort_keys=True))
        return 0 if result["outcome"] in ("VERIFIED", "ROLLED_BACK", "COMPLETED_UNVERIFIED") else 1
    except engine.TxRefused as exc:
        print(f"refused: {exc.code}" + (f" ({exc.detail})" if exc.detail else ""), file=sys.stderr)
        if exc.report:
            print(json.dumps(exc.report, indent=2, sort_keys=True))
        return 3
    except (TxSpecError, StateError, AdapterError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openadapt plan",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cmd_plan(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
