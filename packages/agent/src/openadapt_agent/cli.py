"""``openadapt-agent`` CLI: serve bundles over MCP, emit Agent Skills.

Subcommands:

- ``serve`` — expose compiled openadapt-flow bundles and the local Needs
  Attention queue over MCP stdio. PHI-safe read-only tools are always on;
  workflow runs and attended decisions require separate operator flags.
- ``emit-skill`` — emit a Claude Agent Skill folder for one bundle
  (wraps ``openadapt-flow emit-skill`` and appends MCP + halt guidance).

The bridge is local stdio. Remote identity, tenancy, and transport are provided
by OpenAdapt Cloud rather than added to this process.
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
            "Agent-facing bridge for openadapt-flow: expose compiled workflow "
            "bundles, Needs Attention, and governed operator decisions as "
            "local MCP tools and Agent Skills."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
        "--allow-protected-export",
        action="store_true",
        help=(
            "DANGER: export raw workflow labels, recorded values, intents, "
            "local paths, reports, stdout, stderr, and exception detail to the "
            "MCP client. Off by default; enable only for an explicitly trusted "
            "local client inside the protected data boundary."
        ),
    )
    p.add_argument(
        "--allow-synthetic-recorded-defaults",
        action="store_true",
        help=(
            "DEMO ONLY: let omitted workflow parameters reuse recorded values. "
            "Use only with synthetic demonstrations; production requires every "
            "declared parameter so a run cannot silently target the recorded "
            "customer or record."
        ),
    )
    p.add_argument(
        "--allow-attended-actions",
        action="store_true",
        help=(
            "Register governed Teach/Escalate tools for signed durable pauses. "
            "With --config, also register Continue/Skip through Flow's "
            "deployment-bound live verifier and deterministic resume path."
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
        "--headed",
        action="store_true",
        help=(
            "Keep a deployment-configured attended web session visible to the "
            "local operator (required for web Continue/Skip)."
        ),
    )
    p.add_argument(
        "--allow-model-grounding",
        action="store_true",
        help=(
            "Explicit CLI opt-in for configured off-box model grounding. A "
            "deployment config may also opt in; otherwise verification remains local."
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
    p.add_argument("--out", required=True, help="Parent directory for the skill folder")
    p.set_defaults(func=_cmd_emit_skill)

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    from openadapt_agent.bridge import AgentBridge
    from openadapt_agent.flow_service import open_attended_service
    from openadapt_agent.mcp import serve

    from openadapt_agent.runner import default_flow_cli

    if args.allow_attended_actions and args.flow_cli:
        print(
            "serve: attended actions require the openadapt-flow installed in "
            "this interpreter; --flow-cli cannot select a different runtime",
            file=sys.stderr,
        )
        return 2
    if args.allow_synthetic_recorded_defaults and not args.allow_run:
        print(
            "serve: --allow-synthetic-recorded-defaults requires --allow-run",
            file=sys.stderr,
        )
        return 2

    runner_config = RunnerConfig(
        flow_cli=(tuple(shlex.split(args.flow_cli)) if args.flow_cli else default_flow_cli()),
        runs_dir=Path(args.runs_dir),
        url=args.url,
        deployment_config=args.config,
        policy=args.policy,
        timeout_s=args.timeout,
        allow_url_override=args.allow_url_override,
        extra_run_args=tuple(args.extra_run_arg),
    )
    try:
        with open_attended_service(
            enabled=args.allow_attended_actions,
            deployment_config=args.config,
            url=args.url,
            headed=args.headed,
            allow_model_grounding=args.allow_model_grounding,
        ) as attended_service:
            bridge = AgentBridge(
                Path(args.bundles),
                runner_config,
                allow_run=args.allow_run,
                allow_attended_actions=args.allow_attended_actions,
                attended_service=attended_service,
                allow_protected_export=args.allow_protected_export,
                allow_recorded_defaults=args.allow_synthetic_recorded_defaults,
            )
            n = len(bridge.workflows)
            print(
                f"openadapt-agent {__version__}: serving {n} workflow(s) "
                "over local stdio; run tools "
                f"{'enabled' if args.allow_run else 'disabled'}; attended "
                f"decisions {'enabled' if args.allow_attended_actions else 'disabled'}; "
                "live Continue/Skip "
                f"{'ready' if bridge.attended.live_actions_ready else 'not configured'}; "
                "protected MCP export "
                f"{'ENABLED' if args.allow_protected_export else 'disabled'}; "
                "synthetic recorded defaults "
                f"{'ENABLED' if args.allow_synthetic_recorded_defaults else 'disabled'}",
                file=sys.stderr,
            )
            serve(bridge)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
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
