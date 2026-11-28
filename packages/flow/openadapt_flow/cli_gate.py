"""CLI: ``openadapt-flow gate`` -- shadow mode and gating, per contract.

    openadapt-flow gate status contract.yaml
    openadapt-flow gate label contract.yaml <write_id> caught|false_stop
    openadapt-flow gate enable contract.yaml --by "A. Reviewer"
    openadapt-flow gate disable contract.yaml --by "A. Reviewer"

See ``openadapt_flow.enforcement`` and ``docs/VERDICT_TIERS.md``.
"""

from __future__ import annotations

import argparse
import json
import sys


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "gate",
        help="Show a contract's shadow run of clean checks, label a stop, or "
        "turn gating on once it is offered",
        allow_abbrev=False,
    )
    gsub = p.add_subparsers(dest="gate_command", required=True)
    s = gsub.add_parser(
        "status",
        help="mode, clean checks in a row, whether gating is offered",
        allow_abbrev=False,
    )
    s.add_argument("contract")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_gate)
    s = gsub.add_parser(
        "label",
        help="label one stop: caught (a real harm) or false_stop",
        allow_abbrev=False,
    )
    s.add_argument("contract")
    s.add_argument("write_id")
    s.add_argument("verdict", choices=("caught", "false_stop"))
    s.set_defaults(func=cmd_gate)
    for name, text in (
        ("enable", "turn gating on (only once it is offered)"),
        ("disable", "go back to shadow"),
    ):
        s = gsub.add_parser(name, help=text, allow_abbrev=False)
        s.add_argument("contract")
        s.add_argument("--by", required=True, help="who decided")
        s.set_defaults(func=cmd_gate)


def cmd_gate(args: argparse.Namespace) -> int:
    from openadapt_flow import enforcement

    digest = enforcement.contract_digest(args.contract)
    if args.gate_command == "label":
        enforcement.label(digest, args.write_id, args.verdict)
        st = enforcement.status(digest)
    elif args.gate_command == "enable":
        try:
            st = enforcement.enable(digest, args.by)
        except enforcement.GateRefused as exc:
            print(f"gate: {exc}", file=sys.stderr)
            return 2
    elif args.gate_command == "disable":
        st = enforcement.disable(digest, args.by)
    else:
        st = enforcement.status(digest)
    if getattr(args, "json", False):
        print(json.dumps(st.to_dict(), indent=2))
        return 0
    print(f"mode: {st.mode}")
    print(f"clean checks in a row: {st.clean_in_a_row} of {st.needed}")
    print(f"checks recorded: {st.checks}; stops labelled false: {st.false_stops}")
    if st.offered:
        from openadapt_flow.cli_hints import command

        hint = command(f"gate enable {args.contract} --by <name>")
        print(f"gating is offered: run `{hint}` to let this contract block")
    return 0
