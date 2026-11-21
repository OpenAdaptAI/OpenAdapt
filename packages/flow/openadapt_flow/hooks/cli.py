"""``openadapt hooks``: check a coding agent's writes to declared systems.

    openadapt hooks install --agent claude-code     # or codex
    openadapt hooks status
    openadapt hooks approve ap-1a2b3c4d5e6f         # a held call or write
    openadapt hooks uninstall

``install`` writes project-local hook settings only (never your global
``~/.claude`` or ``~/.codex``). Each tool call that writes to a system
declared in ``openadapt.systems.yaml`` is pre-checked, read back and given
a signed receipt. Hooks see tool calls, so they are advisory against an agent
that has its own credentials and shell. See ``docs/HOOKS.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from openadapt_flow.hooks.agents import AGENTS


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "hooks",
        help=(
            "Hook Claude Code or Codex so each write to a declared system of "
            "record is pre-checked, read back and given a signed receipt"
        ),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(p)
    p.set_defaults(func=cmd_hooks)


def _fill(p: argparse.ArgumentParser) -> None:
    verbs = p.add_subparsers(dest="hooks_verb", required=True)

    def project(q: argparse.ArgumentParser) -> None:
        q.add_argument(
            "--project",
            default=".",
            help="the project folder (default: the current folder)",
        )

    q = verbs.add_parser(
        "install",
        help="write the hook settings into the project",
        allow_abbrev=False,
    )
    q.add_argument("--agent", required=True, choices=AGENTS)
    q.add_argument(
        "--strict",
        action="store_true",
        help="require the credential broker (refused: not available yet)",
    )
    project(q)
    q = verbs.add_parser(
        "status",
        help="show what is installed, held and signed",
        allow_abbrev=False,
    )
    q.add_argument("--json", action="store_true", help="print JSON")
    project(q)
    q = verbs.add_parser(
        "uninstall",
        help="remove OpenAdapt's hook entries",
        allow_abbrev=False,
    )
    q.add_argument("--agent", choices=AGENTS, help="only this agent (default: both)")
    project(q)
    q = verbs.add_parser(
        "approve",
        help="approve a held call once, or release a held write",
        allow_abbrev=False,
    )
    q.add_argument("id", help="an approval id (ap-...) or a write id (hk-...)")
    q.add_argument("--deny", action="store_true", help="decline the call instead")
    project(q)
    q = verbs.add_parser("run", help=argparse.SUPPRESS, allow_abbrev=False)
    q.add_argument("--agent", required=True, choices=AGENTS)
    q.add_argument("--event", required=True)
    project(q)


def _run_hook(project: Path, agent: str, event: str) -> int:
    """One hook event: read the agent's JSON on stdin, answer on stdout."""
    from openadapt_flow.hooks.agents import HookInput, render
    from openadapt_flow.hooks.engine import Engine

    try:
        data = json.loads(sys.stdin.read() or "{}")
        inp = HookInput.parse(agent, event, data)  # type: ignore[arg-type]
        out = Engine(project).handle(inp)
        doc, code = render(agent, event, out)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - fail closed before a tool call
        print(
            f"OpenAdapt's hook failed ({type(exc).__name__}); "
            + (
                "the call is blocked."
                if event == "PreToolUse"
                else "nothing was checked."
            ),
            file=sys.stderr,
        )
        return 2 if event == "PreToolUse" else 1
    if doc is not None:
        sys.stdout.write(json.dumps(doc) + "\n")
    return code


def cmd_hooks(args: argparse.Namespace) -> int:
    from openadapt_flow.hooks import engine, install

    project = Path(args.project).resolve()
    verb = args.hooks_verb
    if verb == "run":
        return _run_hook(project, args.agent, args.event)
    try:
        if verb == "install":
            done = install.install(project, args.agent, strict=args.strict)
            print(f"Installed OpenAdapt hooks for {done['agent']} in {done['file']}.")
            print(f"Events: {', '.join(done['events'])}.")
            print(f"Declared systems: {', '.join(done['systems'])}.")
            if args.agent == "codex":
                print("Open Codex in this project and trust the hooks with /hooks.")
            print(
                "Advisory: hooks see tool calls only. An agent with its own "
                "credentials and shell can reach a system in ways no hook sees."
            )
            return 0
        if verb == "uninstall":
            for line in install.uninstall(project, args.agent) or ["nothing to remove"]:
                print(line)
            return 0
        if verb == "approve":
            print(engine.approve(project, args.id, deny=args.deny))
            return 0
        if verb == "status":
            info = install.status(project)
            info["state"] = engine.summary(project)
            if args.json:
                print(json.dumps(info, indent=2))
                return 0
            _print_status(info)
            return 0
    except (install.InstallError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 2


def _print_status(info: dict) -> None:
    for name, agent in info["agents"].items():
        if agent["installed"]:
            line = (
                f"{name}: installed in {agent['file']} ({', '.join(agent['events'])})"
            )
            if agent["missing_events"]:
                line += f"; missing {', '.join(agent['missing_events'])}"
        else:
            line = f"{name}: not installed"
        print(line)
        if agent.get("note"):
            print(f"  {agent['note']}")
    launcher = info["launcher"]
    if any(a["installed"] for a in info["agents"].values()):
        print(
            f"launcher: {'ok' if launcher['present'] else 'MISSING'}; python "
            f"{launcher['python']} {'ok' if launcher['python_present'] else 'MISSING'}"
        )
    if info["systems_error"]:
        print(f"systems: ERROR {info['systems_error']}")
    for system in info["systems"]:
        writes = ", ".join(system["writes"]) or "no writes declared"
        print(f"system {system['name']} ({system['kind']}): {writes}")
    state = info["state"]
    for entry in state["approvals"]:
        print(
            f"waiting for approval: {entry['id']} ({entry.get('system')}: {entry.get('detail')})"
        )
    for entry in state["held"]:
        print(
            f"held: {entry['write_id']} {entry.get('label')} "
            f"({entry.get('system')}.{entry.get('write')}); check the record, then "
            f"`openadapt hooks approve {entry['write_id']}`"
        )
    for run in state["receipts"]:
        print(
            f"receipt {run['id']}: {run['outcome']} {run.get('system')}.{run.get('write')}"
            + ("" if run["receipt"] else " (not signed)")
        )
    print(
        "Hooks are advisory: they see tool calls, not every way an agent can reach a system."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openadapt hooks",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cmd_hooks(args)
