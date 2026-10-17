"""``openadapt-flow jit``: review, admit or reject a compiled CUA run.

Every command that changes a draft needs an interactive terminal and a typed
confirmation; there is no flag to skip it. The canary is not here: it runs
the draft against the target app with the task's verifier, which are Python
objects. Run it with :meth:`openadapt_flow.jit.CompiledAgent.canary`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

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


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_jit(
    args: argparse.Namespace,
    *,
    ask: Callable[[str], str] = input,
    interactive: Callable[[], bool] = _interactive,
) -> int:
    program = _program_dir(args.program)
    if args.jit_cmd != "status" and not interactive():
        print(
            f"jit {args.jit_cmd}: a person does this at an interactive terminal; "
            "it can't be done by a script or by the agent that recorded the run",
            file=sys.stderr,
        )
        return 2

    def confirm(prompt: str) -> str:
        return ask(f"{prompt} Type ok to continue: ")

    try:
        if args.jit_cmd == "status":
            status = lifecycle.admission_status(program, key_dir=_key_dir(args))
            print(json.dumps(status, indent=2))
            return 0
        if args.jit_cmd == "review":
            status = lifecycle.admission_status(program, key_dir=_key_dir(args))
            params = status["requirements"]["params"]
            if params["task_params"] and not params["met"]:
                # Confirming the parameters recompiles the program, so it
                # comes first, and the steps shown are those of the result.
                accept = sorted(args.accept_param or [])
                lifecycle.confirm_params(
                    program,
                    principal_id=args.principal,
                    accept_params=accept,
                    confirm=confirm(f"Make {accept} the task's parameters?"),
                )
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
                confirm=confirm(f"{args.principal} reviewed every step above?"),
            )
            print(f"reviewed by {record['principal_id']}; digest {record['digest']}")
            return 0
        if args.jit_cmd == "admit":
            manifest = lifecycle.admit(
                program,
                principal_id=args.principal,
                confirm=confirm(f"Admit as {args.principal}?"),
                key_dir=_key_dir(args),
                require_distinct_reviewer_and_admitter=args.require_distinct,
            )
            print(
                f"ADMITTED by {manifest['admitted_by_principal_id']} at "
                f"{manifest['admitted_at']}; digest {manifest['digest']}"
            )
            return 0
        if args.jit_cmd == "reject":
            if confirm(
                f"Reject the draft as {args.principal}?"
            ).strip().lower() not in (
                "ok",
                "yes",
            ):
                print("Not rejected.")
                return 1
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
    p = verbs.add_parser(
        "review",
        help="Confirm the parameters, then record that you reviewed every step",
    )
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument(
        "--accept-param",
        action="append",
        help=(
            "A task parameter to confirm (repeat; must be all of them). Applied "
            "before the steps are shown, because it recompiles the program"
        ),
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
    p = verbs.add_parser(
        "admit", help="Admit a reviewed draft whose canary passed; signs the manifest"
    )
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument(
        "--require-distinct",
        action="store_true",
        help="Refuse if you also reviewed the draft",
    )
    p = verbs.add_parser("reject", help="Set the draft aside")
    common(p)
    p.add_argument("--principal", required=True, help="Your name or user id")
    p.add_argument("--reason")
    for v in verbs.choices.values():
        v.set_defaults(func=cmd_jit)
