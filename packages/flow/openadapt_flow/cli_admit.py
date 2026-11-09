"""CLI: ``openadapt-flow admit``.

- ``admit <recordings...>`` (or ``admit --candidate ID``): compile, induce,
  propose the checks, stop once with a short summary, and on ``accept`` apply
  the checks and reseal the bundle. Same as ``admit run``.
- ``admit decide DIR``: decide an admission that stopped without a terminal.
- ``admit status``: read the published release-admission ledger.

A person decides at an interactive terminal. A delegated agent approver
decides over MCP (``openadapt-agent serve --delegate-approvals``); see
:mod:`openadapt_flow.admit_pipeline`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.cli_hints import command
from openadapt_flow.release_admission import (
    ADMITTED,
    DEFAULT_LEDGER_URL,
    LedgerError,
    load_ledger,
    render_status,
    status_report,
)

#: Verbs of ``admit``; anything else after ``admit`` means ``admit run``.
ADMIT_VERBS = ("run", "decide", "status")
#: Exit code when the command stopped for a decision or a recording.
EXIT_STOPPED = 3


def admit_default_form(argv: list[str]) -> list[str]:
    """``admit <recordings...>`` and ``admit --candidate ID`` mean ``admit run``."""

    if argv[:1] == ["admit"] and len(argv) >= 2:
        first = argv[1]
        if first not in ADMIT_VERBS and first not in ("-h", "--help"):
            return ["admit", "run", *argv[1:]]
    return argv


def register_admit_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "admit",
        help=(
            "Turn recordings (or a shadow candidate) into a program with checks: "
            "compile, induce, propose checks, stop once for a decision. "
            "`admit status` reads the release-admission ledger."
        ),
    )
    verbs = parser.add_subparsers(dest="admit_cmd", required=True)
    run = verbs.add_parser(
        "run",
        help=(
            "Compile, induce, propose checks, and stop once for a person's "
            "decision (the default form: `admit <recordings...>`)"
        ),
    )
    run.add_argument(
        "recordings",
        nargs="*",
        help="Recording directories (or compiled bundles) of the same task",
    )
    run.add_argument(
        "--candidate",
        default=None,
        metavar="ID",
        help=(
            "A shadow candidate id under --candidates-root, or its folder. Its "
            "recordings/ folder is used when no recordings are given"
        ),
    )
    run.add_argument(
        "--candidates-root",
        default="shadow-candidates",
        help="Where shadow candidates are kept (default: ./shadow-candidates)",
    )
    run.add_argument("--out", required=True, help="New admission directory")
    run.add_argument("--name", required=True, help="Workflow name")
    run.add_argument(
        "--policy-pack",
        choices=("community", "cloud", "regulated"),
        default="community",
    )
    run.add_argument(
        "--by",
        default=None,
        help="Your name for the record (default: your login name)",
    )
    run.set_defaults(func=cmd_admit_run)
    decide = verbs.add_parser(
        "decide",
        help="Show a stopped admission's summary and decide it (a person, at a terminal)",
    )
    decide.add_argument("admission", help="Admission directory from `admit`")
    decide.add_argument("--by", default=None, help="Your name for the record")
    decide.set_defaults(func=cmd_admit_decide)
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


def _person(by: Optional[str]) -> "object":
    from openadapt_flow.admit_pipeline import DECIDER_ID_PATTERN, Decider

    name = by or getpass.getuser() or "person"
    name = re.sub(r"[^A-Za-z0-9._:@/-]", "-", name).lstrip("-._:@/") or "person"
    if not re.match(DECIDER_ID_PATTERN, name):
        raise SystemExit("admit: --by must be a short name")
    return Decider(kind="person", id=name, route="cli")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_once(
    out: Path,
    by: Optional[str],
    *,
    read: Callable[[str], str] = input,
    interactive: Optional[bool] = None,
) -> int:
    """The single stop: show the summary, take one decision from a person."""

    from openadapt_flow.admit_pipeline import (
        AdmissionError,
        decide_admission,
        load_pending,
        render_summary,
    )

    pending = load_pending(out)
    sys.stdout.write(render_summary(pending))
    if pending.status != "needs_decision":
        return EXIT_STOPPED if pending.status == "needs_recording" else 2
    if not (_interactive() if interactive is None else interactive):
        print(
            "Stopped for a decision. A person decides with "
            f"`{command(f'admit decide {out}')}` at a terminal; a delegated "
            "agent approver decides with the decide_admission MCP tool."
        )
        return EXIT_STOPPED
    answer = read("Type accept to apply these checks, or reject: ").strip().lower()
    reason = None
    if answer == "reject":
        reason = read("Why? ").strip() or "rejected at the terminal"
    elif answer != "accept":
        print(
            f"No decision recorded. Decide later with `{command(f'admit decide {out}')}`."
        )
        return EXIT_STOPPED
    try:
        result = decide_admission(
            out,
            pending_sha256=pending.sha256(),
            decision="accept" if answer == "accept" else "reject",
            decided_by=_person(by),  # type: ignore[arg-type]
            reason=reason,
        )
    except AdmissionError as exc:
        print(f"admit: {exc}", file=sys.stderr)
        return 2
    if result.decision == "reject":
        print(f"Rejected by {result.decided_by.label()}. Nothing was applied.")
        return 1
    print(
        f"Accepted by {result.decided_by.label()}. Checks applied to {out / 'bundle'}."
    )
    bundle = out / "bundle"
    print(f"Next: `{command(f'qualify {bundle} --verify verify.yaml')}`")
    return 0


def cmd_admit_run(args: argparse.Namespace) -> int:
    from openadapt_flow.admit_pipeline import (
        AdmissionError,
        prepare_admission,
        resolve_candidate,
    )

    if not args.recordings and not args.candidate:
        raise SystemExit("admit: give recording directories or --candidate ID")
    try:
        candidate = (
            resolve_candidate(args.candidate, args.candidates_root)
            if args.candidate
            else None
        )
        prepare_admission(
            args.recordings,
            out=args.out,
            name=args.name,
            prepared_by=_person(args.by),  # type: ignore[arg-type]
            candidate=candidate,
            policy_pack=args.policy_pack,
        )
    except AdmissionError as exc:
        print(f"admit: {exc}", file=sys.stderr)
        return 2
    return _ask_once(Path(args.out), args.by)


def cmd_admit_decide(args: argparse.Namespace) -> int:
    from openadapt_flow.admit_pipeline import AdmissionError

    if not _interactive():
        raise SystemExit(
            "admit decide: a person decides at an interactive terminal; a "
            "delegated agent approver uses the decide_admission MCP tool"
        )
    try:
        return _ask_once(Path(args.admission), args.by, interactive=True)
    except AdmissionError as exc:
        print(f"admit: {exc}", file=sys.stderr)
        return 2
