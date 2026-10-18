"""``openadapt-flow compile-agent``: compile an agent's first verified run.

A computer-use agent does the task once in the app at ``--url``. That run is
real: the agent makes the entry in that app. Point ``--url`` at a test copy
with test records. If the system of record shows exactly the record the task
named, the run is compiled into a program that later runs with no model calls.

::

    openadapt-flow compile-agent "Open the referral for Jane Sample and save this note: {note}" \\
        --url http://localhost:8080/#tasks --verify verify.yaml \\
        --param note="Follow up in two weeks" \\
        --expect patient_id=p1 --on-screen "Jane Sample" --on-screen "MRN P1" \\
        --admit-key agent.pem --admission-policy signed-policy.json

    openadapt-flow compile-agent run <task dir> --param note="Recheck in ten days"

The command is re-entrant. Run it again with the same arguments and it picks
up where it stopped: the agent runs only when the task has no draft and no
admitted program. Confirming the proposed record checks is a person's step at
an interactive terminal; without one, the command stops with the draft
waiting and says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["add_parser", "cmd_compile_agent"]


def _pairs(items: Optional[list[str]], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise SystemExit(f"{flag} takes NAME=VALUE, got {item!r}")
        out[name] = value
    return out


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "compile-agent",
        help=(
            "Run a computer-use agent once against --url (it writes to that app; "
            "use a test copy), check the record it wrote, and compile the run "
            "into a program that runs with no model calls"
        ),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "prompt",
        help='the task, with {name} for each --param; or "run" to run a compiled task',
    )
    p.add_argument("task_dir", nargs="?", help="with run: the compiled task directory")
    p.add_argument("--param", action="append", metavar="NAME=VALUE")
    p.add_argument(
        "--url",
        help="the app's entry URL; the agent makes real entries there, so use a test copy",
    )
    p.add_argument(
        "--backend",
        choices=["web", "windows", "macos", "linux", "rdp", "citrix"],
        default="web",
        help=(
            "the surface the app runs on, as for record --backend. Only web is "
            "supported so far; any other is refused before anything runs"
        ),
    )
    p.add_argument(
        "--config",
        help="deployment.yaml; its backend.kind must be web (others are refused)",
    )
    p.add_argument(
        "--verify",
        help="verify.yaml whose verifier: section reads the system of record",
    )
    p.add_argument(
        "--expect",
        action="append",
        metavar="FIELD=VALUE",
        help="a field the new record must have, such as patient_id=p1",
    )
    p.add_argument(
        "--on-screen",
        action="append",
        metavar="TEXT",
        help="the record's identity as the screen shows it; checked before every save",
    )
    p.add_argument(
        "--agent",
        default="claude",
        help="claude (default), or a JSON file of scripted steps (no model calls)",
    )
    p.add_argument("--vision", choices=["selective", "every_turn"], default="selective")
    p.add_argument("--model", default=None, help="model id (default claude-opus-5-5)")
    p.add_argument("--effort", default="medium")
    p.add_argument("--max-cost", type=float, default=2.0, help="USD cap for run 1")
    p.add_argument("--task-id", default=None)
    p.add_argument("--cache", default=".openadapt-compile-agent")
    p.add_argument("--key-dir", default=None, help="JIT admission key directory")
    p.add_argument("--policy", default="clinical-write", help="Flow risk policy")
    p.add_argument(
        "--drop-field",
        action="append",
        default=[],
        help="a proposed match field to leave out of the checks (changes every save)",
    )
    p.add_argument(
        "--admit-key",
        help="encrypted key of a registered admitter, or of an agent with --admission-policy",
    )
    p.add_argument("--admission-policy", help="signed admission policy for --admit-key")
    p.add_argument(
        "--canary-param",
        action="append",
        metavar="NAME=VALUE",
        help=(
            "the value each canary saves; a canary is a real save on the record "
            "--expect names, so use a value that is safe there (required to admit)"
        ),
    )
    p.add_argument(
        "--admit-as",
        help="a person who reviews, attends the canary and admits at this terminal",
    )
    p.add_argument(
        "--hold",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="ask at this terminal before every save in run 1 (default: on at a terminal)",
    )
    p.add_argument("--show-browser", action="store_true")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(func=cmd_compile_agent)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _agent(args: argparse.Namespace) -> Any:
    from openadapt_flow.compile_agent.agents import (
        DEFAULT_MODEL,
        ClaudeAgent,
        ScriptedAgent,
    )

    if args.agent == "claude":
        return ClaudeAgent(
            vision=args.vision,
            model=args.model or DEFAULT_MODEL,
            effort=args.effort,
            max_cost_usd=args.max_cost,
        )
    raw = json.loads(Path(args.agent).read_text())
    return ScriptedAgent(steps=raw["steps"], principal_id=raw.get("principal_id"))


def _admitter(args: argparse.Namespace) -> Any:
    if not args.admit_key:
        return None
    from openadapt_flow.jit import authority, lifecycle

    key_dir = Path(args.key_dir) if args.key_dir else lifecycle.default_key_dir()
    policy = (
        authority.SignedAdmissionPolicy.load(args.admission_policy)
        if args.admission_policy
        else None
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(Path(args.admit_key)), key_dir, policy=policy
    )


def _terminal_confirm(
    ask: Callable[[str], str],
) -> Callable[[dict[str, Any]], Optional[str]]:
    def confirm(proposed: dict[str, Any]) -> Optional[str]:
        print("Proposed record checks (from the records read around run 1):")
        for effect in proposed["effects"]:
            print(f"  {effect['kind']}:")
            for name, value in effect["match"].items():
                print(f"    {name} = {value}")
        print(f"  file: {proposed['file']}")
        print(
            "Parameters (each run supplies a new value): "
            + (", ".join(proposed["params"]) or "none")
        )
        name = ask("Your name (the person confirming these checks): ").strip()
        if not name:
            return None
        if ask("Type ok to confirm them: ").strip().lower() != "ok":
            return None
        return name

    return confirm


def _terminal_review(
    ask: Callable[[str], str],
) -> Callable[[str, dict[str, Any]], bool]:
    def review(stage: str, shown: dict[str, Any]) -> bool:
        if stage == "review":
            print("The compiled program's steps:")
            for step in shown["steps"]:
                print(
                    f"  {step.get('id')}: {step.get('action')} {step.get('intent') or ''}"
                )
            print(f"Parameters: {', '.join(shown['params']) or 'none'}")
            prompt = "Type ok to confirm you reviewed every step: "
        else:
            for canary in shown["canaries"]:
                print(
                    f"Canary: {canary['status']}, receipt {canary['receipt_outcome']}"
                )
            prompt = "Type ok to admit this program: "
        return ask(prompt).strip().lower() == "ok"

    return review


def _terminal_hold(ask: Callable[[str], str]) -> Callable[[Any], bool]:
    def hold(request: Any) -> bool:
        print(
            f"The agent is about to press {request.control!r} with "
            f"{', '.join(request.identity)} on screen."
        )
        if request.screenshot:
            print(f"  screen: {request.screenshot}")
        return ask("Allow this save? [y/N] ").strip().lower() in ("y", "yes")

    return hold


def _print(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    for key, value in result.items():
        if value in (None, {}, []):
            continue
        if isinstance(value, dict):
            value = json.dumps(value, sort_keys=True, default=str)
        print(f"{key}: {value}")


def cmd_compile_agent(
    args: argparse.Namespace,
    *,
    ask: Callable[[str], str] = input,
    interactive: Callable[[], bool] = _interactive,
) -> int:
    from openadapt_flow.compile_agent.pipeline import compile_first_run, run_compiled

    params = _pairs(args.param, "--param")
    if args.prompt == "run":
        if not args.task_dir:
            raise SystemExit("compile-agent run needs the compiled task directory")
        replay = run_compiled(args.task_dir, **params)
        _print(replay.to_dict(), args.json)
        return 0 if replay.verified else 1
    from openadapt_flow.compile_agent.pipeline import UnsupportedTarget, check_target

    backend = args.backend
    if args.config:
        from openadapt_flow.deployment import load_deployment

        backend = load_deployment(args.config, allow_unresolved=True).backend.kind
    try:
        check_target(args.url or "http://unset", backend)
    except UnsupportedTarget as exc:
        print(f"compile-agent: {exc}", file=sys.stderr)
        return 2
    missing = [f for f in ("url", "verify") if not getattr(args, f)]
    if missing or not args.expect or not args.on_screen:
        raise SystemExit(
            "compile-agent needs --url, --verify, at least one --expect FIELD=VALUE "
            "and at least one --on-screen TEXT"
        )
    tty = interactive()
    hold = (
        _terminal_hold(ask) if (args.hold if args.hold is not None else tty) else None
    )
    if hold is not None and not tty:
        raise SystemExit("--hold asks a person at this terminal; there is none")
    model_key = None
    agent = _agent(args)
    if getattr(agent, "requires_model_key", True):
        from openadapt_flow.jit.keys import load_customer_model_key

        model_key = load_customer_model_key("anthropic")
    result = compile_first_run(
        args.prompt,
        params=params,
        url=args.url,
        verify=args.verify,
        expect=_pairs(args.expect, "--expect"),
        on_screen=args.on_screen,
        agent=agent,
        confirm_checks=_terminal_confirm(ask) if tty else None,
        drop_fields=args.drop_field,
        admitter=_admitter(args),
        person=args.admit_as,
        review=_terminal_review(ask) if (tty and args.admit_as) else None,
        canary_params=(
            [_pairs(args.canary_param, "--canary-param")] if args.canary_param else None
        ),
        hold=hold,
        task_id=args.task_id,
        policy=args.policy,
        cache_dir=args.cache,
        key_dir=args.key_dir,
        model_key=model_key,
        headless=not args.show_browser,
    )
    out = result.to_dict()
    if result.status == "draft" and not tty:
        out["next"] = (
            "a person confirms the proposed checks: run this same command again at "
            "an interactive terminal"
        )
    _print(out, args.json)
    return (
        0
        if result.admitted
        else (3 if result.status in ("draft", "needs_person") else 1)
    )
