"""``openadapt-flow handoff list``: open handoffs by state, and who follows up.

A handoff is one thing a run sent to another party's system (a claim to a
payer, a referral to a receiving clinic). ``handoff list`` reads handoff
ledgers and shows which ones still need someone to follow up:

    openadapt-flow handoff list --ledger /var/lib/openadapt/deferred
    openadapt-flow handoff list --config deployment.yaml --all
    openadapt-flow handoff list --ledger DIR --run runs/claim-0412 --json

A deferred-check ledger (``effects.deferred_report.ledger``) is read as the
payer handoffs beside it (``<ledger>/handoffs``), created on first read. Any
other directory is read as a handoff ledger. See ``docs/COUNTERPARTY_CONFIRMATIONS.md``.

The ``send`` and ``verify`` subcommands belong to the same group and are
registered elsewhere; this module adds only ``list``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.handoffs.ledger import Handoff, HandoffLedger

_STATE_WORDS = {
    "sent": "sent, no answer yet",
    "acknowledged": "acknowledged",
    "recorded_as_intended": "recorded as intended",
    "recorded_differently": "recorded differently",
}
_REASON_WORDS = {
    "rejected_by_counterparty": "they refused it",
    "conflicting_report": "their reports disagree",
    "ambiguous_key": "the key matches more than one item",
    "receiver_unreachable": "their system could not be read",
    "not_found": "not in their system",
    "no_answer_by_deadline": "no answer by the follow-up time",
}
_GROUPS = (
    ("needs_follow_up", "Needs follow-up"),
    ("waiting", "Waiting"),
    ("done", "Done"),
)


def _handoff_verbs(sub: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The ``handoff`` group's subcommands, made if no one registered it yet.

    ``handoff send`` and ``handoff verify`` live in another module; whichever
    registers first makes the group, and the other adds its verbs to it.
    """

    parser = sub.choices.get("handoff")
    if parser is None:
        parser = sub.add_parser(
            "handoff",
            help="Handoffs to other parties: what they recorded, and who follows up",
            allow_abbrev=False,
        )
        return parser.add_subparsers(dest="handoff_command", required=True)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return parser.add_subparsers(dest="handoff_command", required=True)


def _group(sub: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The shared group: ``cli_handoff.handoff_group`` where it exists (the
    module with ``send`` and ``verify``), else the same rule here."""

    try:
        from openadapt_flow.cli_handoff import handoff_group
    except ImportError:
        return _handoff_verbs(sub)
    return handoff_group(sub)


def register_handoff_list(sub: argparse._SubParsersAction) -> None:
    verbs = _group(sub)
    listing = verbs.add_parser(
        "list",
        help="Open handoffs by state, with who still needs to follow up",
        allow_abbrev=False,
    )
    listing.add_argument(
        "--ledger",
        action="append",
        default=[],
        help="A handoff ledger, or a deferred-check ledger (repeatable)",
    )
    listing.add_argument(
        "--config",
        default=None,
        help="deployment.yaml whose effects.deferred_report names the ledger",
    )
    listing.add_argument(
        "--all", action="store_true", help="Also list the handoffs that are done"
    )
    listing.add_argument(
        "--run",
        default=None,
        help="Only this run's handoffs (its directory, holding report.json)",
    )
    listing.add_argument(
        "--json",
        action="store_true",
        help="Print JSON: each handoff's status and its receipt line",
    )
    listing.add_argument(
        "--sign-updates",
        action="store_true",
        help="With --run: sign a follow-up receipt for the handoffs whose state "
        "changed since the run's receipt (receipt.update-N.json)",
    )
    listing.set_defaults(func=cmd_handoff_list)


def _ledgers(args: argparse.Namespace) -> list["HandoffLedger"]:
    from openadapt_flow.handoffs.ledger import HandoffLedger
    from openadapt_flow.handoffs.payer import sync
    from openadapt_flow.runtime.effects.deferred_report import PendingLedger

    dirs = [Path(d) for d in args.ledger]
    if args.config:
        from openadapt_flow.cli_deferred import _from_config

        spec = _from_config(args.config)
        if spec is not None:
            dirs.append(Path(spec.ledger))
    if not dirs:
        raise ValueError("name a ledger with --ledger or --config")
    found: list[HandoffLedger] = []
    for directory in dirs:
        if not directory.is_dir():
            raise ValueError(f"no ledger at {directory}")
        pending = PendingLedger(directory)
        if any(e for e in _quiet_entries(pending)):
            found.append(sync(pending))
        else:
            found.append(HandoffLedger(directory))
    return found


def _quiet_entries(pending: object) -> list[object]:
    try:
        return list(pending.entries())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not a deferred ledger
        return []


def _describe(handoff: "Handoff") -> str:
    last = handoff.evidence
    words = _STATE_WORDS[handoff.state]
    if last is not None and last.counterparty_accepted and last.reason is None:
        words = "accepted (status only, fields not compared)"
    if handoff.state == "recorded_differently" and last is not None:
        words += ": " + ", ".join(last.differing_fields)
    latest = handoff.last
    if latest is not None and latest.reason is not None:
        if handoff.state == "sent":
            words = "sent"
        words += f"; {_REASON_WORDS[latest.reason]}"
    return words


def _when(text: str) -> str:
    return text.replace("T", " ").removesuffix(":00Z").removesuffix("Z")


def cmd_handoff_list(args: argparse.Namespace) -> int:
    from openadapt_flow.handoffs.ledger import now_utc
    from openadapt_flow.handoffs.line import handoffs_for_run, to_line

    try:
        ledgers = _ledgers(args)
        if args.run:
            report = json.loads((Path(args.run) / "report.json").read_text("utf-8"))
            handoffs = handoffs_for_run(report, ledgers, Path(args.run))
        else:
            handoffs = [h for ledger in ledgers for h in ledger.entries()]
    except (OSError, ValueError) as exc:
        print(f"handoff list: {exc}", file=sys.stderr)
        return 2
    if args.sign_updates and not args.run:
        print("handoff list: --sign-updates needs --run", file=sys.stderr)
        return 2
    history = _history(Path(args.run), ledgers, args.sign_updates) if args.run else None
    if history is not None and history.get("error"):
        print(f"handoff list: {history['error']}", file=sys.stderr)
        return 2
    # A signed history that doesn't verify fails the command, after listing.
    status_code = 1 if history is not None and not history.get("verified") else 0
    now: datetime = now_utc()
    if args.json:
        out = []
        for handoff in handoffs:
            status = handoff.status(now)
            if status == "done" and not args.all and not args.run:
                continue
            item = to_line(handoff)
            out.append(
                {
                    "id": handoff.id,
                    "status": status,
                    "state": handoff.state,
                    "counterparty": handoff.counterparty,
                    "follow_up": handoff.follow_up,
                    "follow_up_due": handoff.follow_up_due,
                    "sent_at": handoff.sent_at,
                    "differing_fields": list(
                        handoff.evidence.differing_fields if handoff.evidence else []
                    ),
                    "reason": handoff.last.reason if handoff.last else None,
                    "receipt_line": item.line,
                }
            )
        if history is not None:
            print(json.dumps({"handoffs": out, "signed_history": history}, indent=2))
        else:
            print(json.dumps(out, indent=2))
        return status_code
    if not handoffs:
        print("No handoffs.")
        if history is not None:
            _print_history(history)
        return status_code
    grouped: dict[str, list["Handoff"]] = {key: [] for key, _ in _GROUPS}
    for handoff in handoffs:
        grouped[handoff.status(now)].append(handoff)
    for key, title in _GROUPS:
        items = grouped[key]
        if key == "done" and not args.all:
            if items:
                print(f"{title}: {len(items)} (--all lists them)")
            continue
        print(f"{title} ({len(items)})")
        for handoff in items:
            overdue = " OVERDUE" if handoff.overdue(now) and key != "done" else ""
            print(f"  {handoff.counterparty}: {_describe(handoff)}")
            print(
                f"    sent {_when(handoff.sent_at)}, follow up by "
                f"{_when(handoff.follow_up_due)}{overdue}"
                + (f"; who: {handoff.follow_up}" if key == "needs_follow_up" else "")
            )
        print()
    if history is not None:
        _print_history(history)
    return status_code


def _history(
    run_dir: Path, ledgers: list["HandoffLedger"], sign: bool
) -> Optional[dict[str, object]]:
    """The run's signed handoff history: its receipt and follow-ups, verified
    against this computer's key and pinned keys. With ``sign``, first sign a
    follow-up for what changed."""

    from openadapt_types import receipt as r

    orphans = any(run_dir.glob("receipt.update-*.json"))
    if not (run_dir / r.RECEIPT_FILE).is_file() and not orphans:
        return {"error": "no receipt.json in the run"} if sign else None
    try:
        from openadapt_flow.handoffs.followup import sign_updates, verify_history
        from openadapt_flow.offline_seal import seal_home, signer_for
        from openadapt_flow.receipt_record import local_registry

        signed = None
        if sign:
            signed = sign_updates(run_dir, ledgers, signer=signer_for(seal_home()))
        chain = verify_history(run_dir, local_registry())
    except ImportError:
        return {"error": "openadapt-types has no follow-up receipts"}
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
    return {
        "signed_now": signed.name if signed is not None else None,
        "verified": bool(chain.ok),
        "reason": chain.reason,
        "updates": len(chain.updates),
        "lines": list(chain.lines),
    }


def _print_history(history: dict[str, object]) -> None:
    if history.get("signed_now"):
        print(f"Signed {history['signed_now']}")
    if not history["verified"]:
        print(f"Signed history: not verified ({history['reason']})")
        return
    print(f"Signed history (receipt and {history['updates']} update(s), verified)")
    for line in history["lines"]:  # type: ignore[attr-defined]
        print(f"  {line}")


__all__ = ["register_handoff_list"]
