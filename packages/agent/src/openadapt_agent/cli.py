"""``openadapt-agent`` CLI: serve bundles over MCP, emit Agent Skills.

Subcommands:

- ``serve`` — expose a directory of compiled openadapt-flow bundles as an
  MCP server over stdio. Read-only tools are always on; ``run_*`` tools
  require the explicit ``--allow-run`` flag.
- ``emit-skill`` — emit a Claude Agent Skill folder for one bundle
  (wraps ``openadapt-flow emit-skill`` and appends MCP + halt guidance).

EXPERIMENTAL: see the README's honest-limits section before deploying.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Optional, Sequence

from openadapt_agent import __version__
from openadapt_agent.runner import RunnerConfig

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt-agent",
        description=(
            "EXPERIMENTAL agent-facing bridge for openadapt-flow: expose "
            "compiled workflow bundles as MCP tools and Agent Skills. "
            "Execution always shells out to the governed `openadapt-flow "
            "run` CLI."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "serve",
        help="Serve a directory of workflow bundles as an MCP server (stdio)",
    )
    p.add_argument(
        "--bundles",
        required=True,
        help=(
            "Bundle directory: either one compiled bundle, or a directory "
            "whose immediate subdirectories are bundles"
        ),
    )
    p.add_argument(
        "--allow-run",
        action="store_true",
        help=(
            "Register run_* tools (default: read-only tools only). Without "
            "this flag no MCP client can execute anything."
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help="Target app URL passed to every `openadapt-flow run` (operator-fixed)",
    )
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help="Deployment config YAML forwarded to `openadapt-flow run --config`",
    )
    p.add_argument(
        "--policy",
        default=None,
        metavar="NAME-OR-PATH",
        help=(
            "Certifying policy forwarded to `openadapt-flow run --policy` "
            "and used by get_workflow's certification check"
        ),
    )
    p.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory for per-run output directories (default: ./runs)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="Per-call timeout for each governed run (default: 600)",
    )
    p.add_argument(
        "--allow-url-override",
        action="store_true",
        help=(
            "Permit MCP callers to pass a per-call `url` argument (default: "
            "the target URL is fixed by --url/--config at server start)"
        ),
    )
    p.add_argument(
        "--flow-cli",
        default=None,
        help=(
            "Command used to invoke the flow CLI (default: this "
            "interpreter's `python -m openadapt_flow`, so the flow "
            "installed alongside this server is always the one that runs); "
            "may contain spaces, e.g. 'openadapt-flow'"
        ),
    )
    p.add_argument(
        "--extra-run-arg",
        action="append",
        default=[],
        metavar="ARG",
        help=(
            "Extra argument appended to every `openadapt-flow run` "
            "invocation (repeatable; operator-fixed, e.g. "
            "--extra-run-arg=--allow-unencrypted for a demo bundle)"
        ),
    )
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser(
        "emit-skill",
        help=(
            "Emit a Claude Agent Skill folder for a bundle (wraps "
            "`openadapt-flow emit-skill`, appends MCP + halt guidance)"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--out", required=True, help="Parent directory for the skill folder"
    )
    p.set_defaults(func=_cmd_emit_skill)

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    from openadapt_agent.bridge import AgentBridge
    from openadapt_agent.mcp import serve

    from openadapt_agent.runner import default_flow_cli

    runner_config = RunnerConfig(
        flow_cli=(
            tuple(shlex.split(args.flow_cli)) if args.flow_cli else default_flow_cli()
        ),
        runs_dir=Path(args.runs_dir),
        url=args.url,
        deployment_config=args.config,
        policy=args.policy,
        timeout_s=args.timeout,
        allow_url_override=args.allow_url_override,
        extra_run_args=tuple(args.extra_run_arg),
    )
    try:
        bridge = AgentBridge(
            Path(args.bundles), runner_config, allow_run=args.allow_run
        )
    except FileNotFoundError as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    n = len(bridge.workflows)
    print(
        f"openadapt-agent {__version__} (EXPERIMENTAL): serving {n} "
        f"workflow(s) over stdio; run tools "
        f"{'ENABLED' if args.allow_run else 'disabled (read-only)'}",
        file=sys.stderr,
    )
    serve(bridge)
    return 0


def _cmd_emit_skill(args: argparse.Namespace) -> int:
    from openadapt_agent.skill import emit_agent_skill

    skill_dir = emit_agent_skill(Path(args.bundle), Path(args.out))
    print(f"Wrote Agent Skill folder: {skill_dir}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
