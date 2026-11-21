"""``openadapt proxy``: hold the database credentials and give the agent scoped tools.

    openadapt proxy --postgres "$DATABASE_URL" -- claude
    openadapt proxy --sqlite app.db --auto-approve-max-rows 10 -- claude
    openadapt proxy --sqlite app.db            # serve here; y approve, u undo
    openadapt proxy --api systems.tx.yaml -- claude   # API systems, same endpoint

See ``openadapt_flow.txplan.proxy`` and ``docs/TRANSACTIONS.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openadapt proxy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(p)
    return p


def _fill(p: argparse.ArgumentParser) -> None:
    db = p.add_mutually_exclusive_group()
    db.add_argument(
        "--postgres", metavar="URL", help="a PostgreSQL URL (needs the psycopg package)"
    )
    db.add_argument("--sqlite", metavar="PATH", help="a SQLite file")
    p.add_argument(
        "--api",
        metavar="TX_FILE",
        help=(
            "a tx file of API systems (openadapt.tx/v1 REST systems with scoped "
            "verification): adds api_read and api_write, checked by read-back and "
            "the system's own log"
        ),
    )
    p.add_argument(
        "--task", help="the task these writes belong to (default: one per proxy run)"
    )
    p.add_argument(
        "--auto-approve-max-rows",
        type=int,
        default=0,
        metavar="N",
        help="approve by policy a plan that touches at most N rows and has no anomaly (default 0)",
    )
    p.add_argument(
        "--flag",
        action="append",
        default=[],
        metavar="TABLE.COLUMN=VALUE",
        help="show rows the statement touches where TABLE.COLUMN = VALUE (repeatable)",
    )
    p.add_argument(
        "--state",
        help="the state folder (default: $OPENADAPT_TX_STATE or ~/.openadapt/tx)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="the MCP port on 127.0.0.1 (default: random)",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "proxy",
        help=(
            "Hold the database credentials; give the agent MCP tools that preview, apply "
            "once and undo each write"
        ),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _fill(p)
    p.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- COMMAND ... (the agent)"
    )
    p.set_defaults(
        func=lambda a: _run(a, a.command[1:] if a.command[:1] == ["--"] else a.command)
    )


def _run(args: argparse.Namespace, command: Sequence[str]) -> int:
    import os

    from openadapt_flow.txplan.proxy import default_task, run, sql_spec
    from openadapt_flow.txplan.spec import RestSystem, TxSpec, TxSpecError, load_spec
    from openadapt_flow.txplan.state import State

    if not (args.postgres or args.sqlite or args.api):
        print("error: give --postgres, --sqlite or --api", file=sys.stderr)
        return 2
    api_spec = None
    hidden: list[str] = [args.postgres] if args.postgres else []
    try:
        if args.api:
            api_spec = load_spec(Path(args.api))
            for system in api_spec.systems:
                if isinstance(system, RestSystem) and system.auth is not None:
                    # The agent never gets the API credentials either.
                    hidden.append(os.environ.get(system.auth.value_env, ""))
        if args.postgres or args.sqlite:
            spec = sql_spec(postgres=args.postgres, sqlite=args.sqlite,
                            max_rows=args.auto_approve_max_rows, flags=args.flag)
        else:
            assert api_spec is not None
            spec = TxSpec(schema_version="openadapt.tx/v1", systems=api_spec.systems)
    except (ValueError, TxSpecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state = State(Path(args.state) if args.state else None)
    # One task per system and project folder, as default_task does for a database.
    target = args.postgres or str(Path(args.sqlite or args.api).resolve())
    return run(
        command,
        spec=spec,
        state=state,
        task=args.task or default_task(target),
        secrets_=[h for h in hidden if h],
        port=args.port,
        api_spec=api_spec,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1 :]
    args = _parser().parse_args(argv)
    return _run(args, command)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
