"""``openadapt-flow jit``: review, admit or reject a compiled CUA run.

Admission has three modes (:mod:`openadapt_flow.jit.authority`):

* ``jit admit PROGRAM --key KEY``: a CI job or script, with an encrypted
  admitter key a person registered. The passphrase comes from
  ``$OPENADAPT_JIT_KEY_PASSPHRASE``; the principal comes from the key.
* ``jit admit PROGRAM --key KEY --policy POLICY``: a registered agent under
  an admission policy a person signed. A draft outside the policy goes to
  the ``needs_person`` queue (exit 3), which ``jit review --queue`` lists.
* ``jit admit PROGRAM --principal NAME``: a person at an interactive
  terminal, who types a confirmation.

Provisioning is a person's job and needs a terminal: ``jit register`` adds a
key to the registry and ``jit sign-policy`` signs a policy. ``jit keygen``
writes an encrypted key. There is no flag that skips a check. The canary is
not here: it runs the draft against the target app with the task's verifier,
which are Python objects. Run it with
:meth:`openadapt_flow.jit.CompiledAgent.canary`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from openadapt_flow.cli_hints import command
from openadapt_flow.jit import authority, lifecycle


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


def _needs_terminal(args: argparse.Namespace) -> bool:
    if args.jit_cmd in ("status", "keygen"):
        return False
    if args.jit_cmd == "review" and args.queue:
        return False
    if args.jit_cmd == "admit" and args.key:
        return False
    return True


def _keys(args: argparse.Namespace) -> Path:
    return _key_dir(args) or lifecycle.default_key_dir()


def cmd_jit(
    args: argparse.Namespace,
    *,
    ask: Callable[[str], str] = input,
    interactive: Callable[[], bool] = _interactive,
) -> int:
    if _needs_terminal(args) and not interactive():
        hint = (
            " A script admits with a registered key: jit admit --key."
            if args.jit_cmd == "admit"
            else ""
        )
        print(
            f"jit {args.jit_cmd}: a person does this at an interactive terminal; "
            f"it can't be done by a script or by the agent that recorded the run."
            f"{hint}",
            file=sys.stderr,
        )
        return 2

    def confirm(prompt: str) -> str:
        return ask(f"{prompt} Type ok to continue: ")

    try:
        if args.jit_cmd == "keygen":
            pub = authority.generate_encrypted_key(args.out)
            print(f"wrote {args.out}; public key {pub}")
            print(f"A person registers it: {command('jit register')} ...")
            return 0
        if args.jit_cmd == "register":
            if confirm(
                f"Register {args.principal} as {args.role} with key {args.public_key}?"
            ).strip().lower() not in ("ok", "yes"):
                print("Not registered.")
                return 1
            authority.register_principal(
                _keys(args),
                role=args.role,
                principal_id=args.principal,
                public_key_hex=args.public_key,
            )
            print(f"registered {args.principal} as {args.role} in {_keys(args)}")
            return 0
        if args.jit_cmd == "sign-policy":
            policy = authority.AdmissionPolicy.model_validate_json(
                Path(args.policy).read_text(encoding="utf-8")
            )
            print(policy.model_dump_json(indent=2, by_alias=True))
            if confirm(f"Sign this policy as {policy.signer}?").strip().lower() not in (
                "ok",
                "yes",
            ):
                print("Not signed.")
                return 1
            signed = authority.sign_policy(
                policy, authority.load_encrypted_key(args.key)
            )
            authority.verify_policy(
                signed, authority.PrincipalRegistry.load(_keys(args))
            )
            signed.save(args.out)
            print(f"signed policy {policy.digest()} written to {args.out}")
            return 0
        if args.jit_cmd == "review" and args.queue:
            print(json.dumps(lifecycle.needs_person_queue(args.queue), indent=2))
            return 0
        if not args.program:
            raise SystemExit(f"jit {args.jit_cmd}: name the program directory")
        program = _program_dir(args.program)
        if args.jit_cmd != "admit" and args.jit_cmd != "status" and not args.principal:
            raise SystemExit(f"jit {args.jit_cmd}: --principal is required")
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
                    confirm=confirm(
                        f"Make {accept} the task's parameters"
                        + (
                            f", with {args.idempotency_key} identifying one record?"
                            if args.idempotency_key
                            else "?"
                        )
                    ),
                    key_dir=_key_dir(args),
                    idempotency_key=args.idempotency_key,
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
                key_dir=_key_dir(args),
            )
            print(f"reviewed by {record['principal_id']}; digest {record['digest']}")
            return 0
        if args.jit_cmd == "admit" and args.key:
            if args.principal:
                raise SystemExit(
                    "jit admit --key: the principal comes from the key; drop --principal"
                )
            policy_doc = (
                authority.SignedAdmissionPolicy.load(args.policy)
                if args.policy
                else None
            )
            principal = authority.principal_from_key(
                authority.load_encrypted_key(args.key), _keys(args), policy=policy_doc
            )
            decision = lifecycle.admit_with_key(
                program, principal=principal, key_dir=_keys(args)
            )
            if decision["outcome"] == "needs_person":
                print(
                    "NEEDS PERSON: outside the admission policy; queued:\n  "
                    + "\n  ".join(decision["reasons"])
                )
                return 3
            manifest = decision["manifest"]
            print(
                f"ADMITTED by {manifest['admitted_by_principal_id']} "
                f"({manifest['admission_mode']}) at {manifest['admitted_at']}; "
                f"digest {manifest['digest']}"
            )
            return 0
        if args.jit_cmd == "admit":
            if not args.principal:
                raise SystemExit("jit admit: --principal NAME, or --key KEY")
            if args.policy:
                raise SystemExit("jit admit --policy needs --key (the agent's key)")
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
                program,
                principal_id=args.principal,
                reason=args.reason or "",
                key_dir=_key_dir(args),
            )
            print(f"REJECTED; kept at {kept}")
            return 0
    except (lifecycle.AdmissionError, authority.AuthorityError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    raise SystemExit(f"unknown jit command {args.jit_cmd!r}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "jit",
        help="Review, admit or reject a DRAFT compiled from a computer-use agent run",
    )
    verbs = parser.add_subparsers(dest="jit_cmd", required=True)

    def common(p: argparse.ArgumentParser, *, program: bool = True) -> None:
        if program:
            p.add_argument(
                "program",
                nargs="?",
                help="The task's directory or its program/ directory",
            )
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
    p.add_argument("--principal", help="Your name or user id")
    p.add_argument(
        "--queue",
        metavar="CACHE_DIR",
        help="List the drafts waiting for a person (needs_person) under CACHE_DIR",
    )
    p.add_argument(
        "--accept-param",
        action="append",
        help=(
            "A task parameter to confirm (repeat; must be all of them). Applied "
            "before the steps are shown, because it recompiles the program"
        ),
    )
    p.add_argument(
        "--idempotency-key",
        metavar="PARAM",
        help=(
            "The parameter that identifies one saved record (the app keeps it "
            "unique, such as a claim number). The record check selects by it "
            "and refuses a duplicate"
        ),
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--step", action="append", help="A step id you reviewed (repeat)"
    )
    group.add_argument(
        "--all-steps-shown",
        action="store_true",
        help="You reviewed every step this command prints",
    )
    p = verbs.add_parser(
        "admit",
        help="Admit a draft whose canary passed: with a registered key, as an "
        "agent under a signed policy, or as a person at a terminal",
    )
    common(p)
    p.add_argument("--principal", help="A person admitting at a terminal")
    p.add_argument(
        "--key",
        help="Encrypted admitter or agent key; passphrase from "
        f"${authority.KEY_PASSPHRASE_ENV}. The principal comes from the key",
    )
    p.add_argument("--policy", help="Signed admission policy, for an agent key")
    p.add_argument(
        "--require-distinct",
        action="store_true",
        help="Refuse if you also reviewed the draft",
    )
    p = verbs.add_parser("reject", help="Set the draft aside")
    common(p)
    p.add_argument("--principal", help="Your name or user id")
    p.add_argument("--reason")
    p = verbs.add_parser("keygen", help="Write a new encrypted Ed25519 key")
    common(p, program=False)
    p.add_argument("--out", required=True)
    p = verbs.add_parser(
        "register", help="Register a key as a policy signer, admitter key or agent"
    )
    common(p, program=False)
    p.add_argument(
        "--role", required=True, choices=["policy_signer", "admitter_key", "agent"]
    )
    p.add_argument("--principal", required=True)
    p.add_argument("--public-key", required=True, help="hex, from jit keygen")
    p = verbs.add_parser(
        "sign-policy", help="Sign an admission policy with your policy-signer key"
    )
    common(p, program=False)
    p.add_argument("policy", help="Unsigned AdmissionPolicy JSON")
    p.add_argument("--key", required=True, help="Your encrypted policy-signer key")
    p.add_argument("--out", required=True)
    for v in verbs.choices.values():
        v.set_defaults(func=cmd_jit)
