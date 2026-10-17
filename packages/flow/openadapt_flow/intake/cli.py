"""``openadapt-flow intake``: document -> confirmed ``--params-file`` JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openadapt_flow.cli_hints import command
from openadapt_flow.intake.document import read_document
from openadapt_flow.intake.extract import (
    IntakeResult,
    apply_agent_response,
    build_agent_request,
    extract_fields,
)
from openadapt_flow.ir import ParamSpec


def _specs_for_bundle(bundle: str) -> dict[str, ParamSpec]:
    from openadapt_flow.ir import Workflow

    wf = Workflow.load(bundle)
    specs = dict(wf.param_specs)
    for name in wf.params:
        specs.setdefault(name, ParamSpec(name=name))
    if not specs:
        raise SystemExit(f"{bundle} declares no parameters; nothing to extract")
    return specs


def _parse_aliases(pairs: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pair in pairs:
        name, sep, label = pair.partition("=")
        if not sep or not name or not label:
            raise SystemExit(f"--alias expects name=Label, got {pair!r}")
        out.setdefault(name, []).append(label)
    return out


def _prompt(result: IntakeResult) -> None:
    for name in result.needs_confirmation():
        f = result.fields[name]
        while True:
            hint = f" [{f.value}]" if f.value else ""
            why = f.error or f"confidence {f.confidence:.2f}"
            answer = input(f"{name} ({why}){hint}: ").strip()
            try:
                result.confirm(name, answer or None)
                break
            except ValueError as exc:
                print(exc, file=sys.stderr)


def cmd_intake(args: argparse.Namespace) -> int:
    specs = _specs_for_bundle(args.bundle)
    doc = read_document(args.document)
    if args.agent_request:
        print(json.dumps(build_agent_request(doc, specs), indent=2))
        return 0
    if args.agent_response:
        response = json.loads(Path(args.agent_response).read_text())
        result = apply_agent_response(doc, specs, response, threshold=args.threshold)
    else:
        result = extract_fields(
            doc, specs, aliases=_parse_aliases(args.alias), threshold=args.threshold
        )
    for name in args.accept:
        result.confirm(name)
    if result.needs_confirmation() and sys.stdin.isatty() and not args.no_input:
        _prompt(result)
    pending = result.needs_confirmation()
    if pending:
        print(json.dumps(result.to_dict(), indent=2))
        print(
            "Not written. Confirm these fields (interactively, or with --accept "
            "NAME after checking the value): " + ", ".join(pending),
            file=sys.stderr,
        )
        return 2
    Path(args.out).write_text(json.dumps(result.to_params(), indent=2) + "\n")
    print(
        f"Wrote {args.out}. Run: "
        + command(f"run {args.bundle} --params-file {args.out}")
    )
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "intake",
        help="Fill a workflow's parameters from a referral PDF, fax image, or email",
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("document", help="PDF, image (.png/.tif/...), .eml, or .txt")
    p.add_argument("--out", default="params.json", help="Output --params-file path")
    p.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="NAME=LABEL",
        help="Extra document label for a parameter (repeatable)",
    )
    p.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="NAME",
        help="Confirm a proposed value without prompting (repeatable)",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Fields below this confidence need confirmation",
    )
    p.add_argument("--no-input", action="store_true", help="Never prompt")
    p.add_argument(
        "--agent-request",
        action="store_true",
        help="Print a privacy-scrubbed request for the calling agent and exit",
    )
    p.add_argument(
        "--agent-response",
        metavar="JSON",
        help="Apply the calling agent's answer to --agent-request",
    )
    p.set_defaults(func=cmd_intake)
