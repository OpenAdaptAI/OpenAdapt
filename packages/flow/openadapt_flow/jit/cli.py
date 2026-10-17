"""``openadapt-flow jit``: review, admit or reject a compiled CUA run.

The canary is not here: it runs the draft against the target app with the
task's verifier, which are Python objects. Run it with
:meth:`openadapt_flow.jit.CompiledAgent.canary`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openadapt_flow.jit import lifecycle


def _program_dir(path: str) -> Path:
    p = Path(path)
    if (p / lifecycle.CANDIDATE_FILE).is_file() or (p / "bundle").is_dir():
        return p
    if (p / "program").is_dir():
        return p / "program"
    raise SystemExit(f"{path}: no JIT draft here (expected candidate.json)")


def _key_dir(args: argparse.Namespace) -> Path | None:
    return Path(args.key_dir) if args.key_dir else None


def _confirm(args: argparse.Namespace, prompt: str) -> str:
    if args.yes:
        return "ok"
    return input(f"{prompt} Type ok to continue: ")


def cmd_jit(args: argparse.Namespace) -> int:
    program = _program_dir(args.program)
    try:
        if args.jit_cmd == "status":
            status = lifecycle.admission_status(program, key_dir=_key_dir(args))
            print(json.dumps(status, indent=2))
            return 0
        if args.jit_cmd == "review":
            status = lifecycle.admission_status(program, key_dir=_key_dir(args))
            for i, step in enumerate(status["steps"], 1):
                print(f"{i:3}. {step['id']}  {step['action']}  {step['intent'] or ''}")
            steps = (
                [s["id"] for s in status["steps"]]
                if args.all_steps_shown
                else list(args.step or [])
            )
            record = lifecycle.record_review(
                program,
                principal_id=args.principal,
                reviewed_steps=steps,
                accept_params=list(args.accept_param or []),
                confirm=_confirm(args, f"{args.principal} reviewed every step above?"),
            )
            print(f"reviewed by {record['principal_id']}; digest {record['digest']}")
            return 0
        if args.jit_cmd == "admit":
            manifest = lifecycle.admit(
                program,
                principal_id=args.principal,
                confirm=_confirm(args, f"Admit as {args.principal}?"),
                key_dir=_key_dir(args),
            )
            print(
                f"ADMITTED by {manifest['admitted_by_principal_id']} at "
                f"{manifest['admitted_at']}; digest {manifest['digest']}"
            )
            return 0
        if args.jit_cmd == "reject":
            kept = lifecycle.reject(
                program, principal_id=args.principal, reason=args.reason or ""
            )
            print(f"REJECTED; kept at {kept}")
            return 0
    except lifecycle.AdmissionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    raise SystemExit(f"unknown jit command {args.jit_cmd!r}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "jit",
        help="Review, admit or reject a DRAFT compiled from a computer-use agent run",
    )
    verbs = parser.add_subparsers(dest="jit_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("program", help="The task's directory or its program/ directory")
        p.add_argument(
            "--key-dir",
            help="Admission signing key directory (default: $OPENADAPT_JIT_KEY_DIR "
            "or ~/.openadapt/jit)",
        )

    p = verbs.add_parser(
        "status", help="Print the state, steps and what admission needs"
    )
    common(p)
    p = verbs.add_parser("review", help="Record that you reviewed every step")
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument(
        "--accept-param",
        action="append",
        help="A task parameter to confirm (repeat; must be all of them)",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--step", action="append", help="A step id you reviewed (repeat)"
    )
    group.add_argument(
        "--all-steps-shown",
        action="store_true",
        help="You reviewed every step this command prints",
    )
    p.add_argument(
        "--yes", action="store_true", help="Skip the prompt (you still decide)"
    )
    p = verbs.add_parser(
        "admit", help="Admit a reviewed draft whose canary passed; signs the manifest"
    )
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument(
        "--yes", action="store_true", help="Skip the prompt (you still decide)"
    )
    p = verbs.add_parser("reject", help="Set the draft aside")
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument("--reason")
    for v in verbs.choices.values():
        v.set_defaults(func=cmd_jit)
