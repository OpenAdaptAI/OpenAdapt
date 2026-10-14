"""``openadapt-agent`` CLI: serve workflows over MCP, emit Agent Skills.

Subcommands:

- ``serve`` — with no flags, a sandbox: one synthetic workflow that shows
  every result your agent can get back. ``--mode production --bundles DIR``
  serves an operator's compiled workflows; ``--mode attended`` also lets a
  person at this computer answer paused runs. ``--bundles DIR`` alone stays
  read-only. ``--authoring`` adds first-demo stdio tools and does not imply
  runs.
- ``authoring connect`` — outbound mailbox client for hosted ChatGPT.com /
  Claude.ai (claim ``oab_``, poll wait=0, Allow-per-sub). Not an HTTP
  listener. Overlay chrome stays Desktop-only.
- ``emit-skill`` — write a PHI-safe Agent Skill for one workflow from its
  card: purpose, inputs, and how to read the four outcomes.

``serve`` stays local stdio. Hosted ChatGPT.com reaches this computer through
``authoring connect`` (outbound HTTPS), not a port-forwarded MCP server.
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
            "Your AI agent decides what to enter. OpenAdapt enters it in the app "
            "and checks that it saved. 'serve' with no flags starts a sandbox."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "serve",
        help=(
            "Serve workflows to your AI agent over MCP (stdio). With no flags, "
            "serve the sandbox."
        ),
    )
    p.add_argument(
        "--mode",
        choices=("sandbox", "attended", "production"),
        default=None,
        help=(
            "sandbox (default with no --bundles): one synthetic workflow that "
            "shows every result. production: run the compiled workflows in "
            "--bundles. attended: production plus decisions on paused runs by a "
            "person at this computer. --bundles without --mode stays read-only."
        ),
    )
    p.add_argument(
        "--bundles",
        default=None,
        help=(
            "Bundle directory: either one compiled bundle, or a directory "
            "whose immediate subdirectories are bundles. Needed for "
            "--mode production and --mode attended."
        ),
    )
    p.add_argument(
        "--sandbox-engine",
        choices=("auto", "flow", "simulated"),
        default="auto",
        help=(
            "Advanced. flow drives the synthetic app in a hidden browser (needs "
            "the tutorial extra); simulated returns the same results without "
            "opening it. auto picks flow when it can."
        ),
    )
    p.add_argument(
        "--authoring",
        action="store_true",
        help=(
            "Register first-demo authoring tools over local stdio: observe, "
            "start_record, click, halt. Local Claude Code path is the first "
            "authoring UI. Pass --url --headed to pin Playwright Chromium "
            "with empty cookies; pause_for_input is how a person signs in "
            "there. --url is not the Chrome window you already signed into. "
            "Omit --url to pin a unique frontmost Chrome window after login "
            "(macOS; no DOM identity). Local stdio may also type through the "
            "recorder; hosted MCP remains pause-only. Does not enable run "
            "tools. This process stays stdio and must not be served over HTTP."
        ),
    )
    p.add_argument(
        "--tutorial",
        action="store_true",
        help=(
            "Older name for --mode sandbox. Synthetic only. Cannot be combined "
            "with --bundles, --url, or --config."
        ),
    )
    p.add_argument(
        "--allow-run",
        action="store_true",
        help=(
            "Older name for --mode production (with --bundles) or --mode sandbox "
            "(without). Without a mode or this flag, --bundles stays read-only."
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
            "Part of --mode attended. Register governed Reject/Teach/Escalate tools "
            "for signed durable pauses. With --config, also register Continue/Skip "
            "through Flow's deployment-bound live verifier and deterministic "
            "resume path."
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
        default=None,
        help=(
            "Directory for run records and evidence (default: ./runs; the "
            "sandbox uses ~/.openadapt/agent-sandbox)"
        ),
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

    authoring = sub.add_parser(
        "authoring",
        help=(
            "Hosted authoring mailbox client (ChatGPT.com / Claude.ai). "
            "Outbound HTTPS only; not an HTTP listener."
        ),
    )
    authoring_sub = authoring.add_subparsers(dest="authoring_command", required=True)
    connect = authoring_sub.add_parser(
        "connect",
        help=(
            "Claim an openadapt://runner link or pack URL, poll wait=0, "
            "and prompt Allow per chat account"
        ),
        description=(
            "Claim an openadapt://runner link or pack URL, poll wait=0, "
            "and prompt Allow per chat account. Overlay chrome stays "
            "Desktop-only. Continue uses record_observed; it does not type "
            "secrets. This process only makes outbound HTTPS."
        ),
    )
    connect.add_argument(
        "target",
        help=(
            "openadapt://runner?pack=…&bind=oab_…&origin=https://openadapt.ai "
            "or https://openadapt.ai/j/{id} (bind required to claim)"
        ),
    )
    connect.add_argument(
        "--url",
        default=None,
        help=(
            "Launch Playwright Chromium with empty cookies at this URL. "
            "Not the browser you are already signed into."
        ),
    )
    connect.add_argument(
        "--headed",
        action="store_true",
        help="Keep the Playwright window visible (required to sign in in the app).",
    )
    connect.set_defaults(func=_cmd_authoring_connect)

    p = sub.add_parser(
        "emit-skill",
        help=(
            "Write an Agent Skill for one workflow: what it does, its inputs, and "
            "how to read the result. No recorded values, step text, or secrets."
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--out", required=True, help="Parent directory for the skill folder")
    p.add_argument(
        "--include-bundle",
        action="store_true",
        help=(
            "Also copy the compiled bundle next to SKILL.md. The copy is protected "
            "workflow data; install it only where that data may live."
        ),
    )
    p.set_defaults(func=_cmd_emit_skill)

    return parser


def _resolve_mode(args: argparse.Namespace) -> Optional[str]:
    """Pick the server mode from --mode and the older flags it replaces.

    Returns None for an authoring-only server. Raises ValueError with the
    message to print when the flags conflict.
    """
    if args.mode is not None:
        if args.mode == "sandbox":
            if args.bundles:
                raise ValueError("--mode sandbox cannot be combined with --bundles")
            return "sandbox"
        if not args.bundles:
            raise ValueError(f"--mode {args.mode} needs --bundles")
        args.allow_run = True
        if args.mode == "attended":
            args.allow_attended_actions = True
        return args.mode
    if args.tutorial:
        return "sandbox"
    if args.bundles:
        # Older flags: a person can answer paused runs with or without runs.
        return "attended" if args.allow_attended_actions else "production"
    if args.authoring:
        return None
    return "sandbox"


def _cmd_serve(args: argparse.Namespace) -> int:
    from openadapt_agent.bridge import AgentBridge
    from openadapt_agent.flow_service import open_attended_service
    from openadapt_agent.mcp import serve
    from openadapt_agent.runner import default_flow_cli
    from openadapt_agent.tutorial import TutorialError

    if args.authoring and args.tutorial:
        print("serve: --authoring cannot be combined with --tutorial", file=sys.stderr)
        return 2
    if args.authoring and args.allow_run and not args.bundles:
        print(
            "serve: --authoring does not imply --allow-run; --allow-run still "
            "requires --bundles",
            file=sys.stderr,
        )
        return 2
    if args.tutorial and (args.bundles or args.url or args.config):
        print(
            "serve: --tutorial cannot be combined with --bundles, --url, or --config",
            file=sys.stderr,
        )
        return 2
    try:
        mode = _resolve_mode(args)
    except ValueError as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    # After mode resolution, so --mode attended is covered too.
    if args.allow_attended_actions and args.flow_cli:
        print(
            "serve: attended actions require the openadapt-flow installed in "
            "this interpreter; --flow-cli cannot select a different runtime",
            file=sys.stderr,
        )
        return 2
    if mode == "sandbox" and (args.url or args.config):
        print(
            "serve: the sandbox runs a synthetic app; --url and --config need "
            "--mode production --bundles DIR",
            file=sys.stderr,
        )
        return 2
    if args.allow_synthetic_recorded_defaults and not (args.allow_run and args.bundles):
        print(
            "serve: --allow-synthetic-recorded-defaults requires --allow-run",
            file=sys.stderr,
        )
        return 2

    sandbox_engine = None
    authoring_bridge = None
    try:
        if args.authoring:
            from openadapt_agent.authoring import AuthoringBridge, AuthoringError
            from openadapt_agent.authoring import open_authoring_session

            if args.runs_dir:
                authoring_root = Path(args.runs_dir)
            elif mode == "sandbox":
                from openadapt_agent.sandbox import default_sandbox_dir

                authoring_root = default_sandbox_dir()
            else:
                authoring_root = Path("runs")
            authoring_dir = authoring_root.expanduser().resolve() / "authoring"
            try:
                authoring_bridge = AuthoringBridge(
                    open_authoring_session(
                        out_dir=authoring_dir,
                        url=args.url,
                        headed=args.headed,
                    ),
                    out_dir=authoring_dir,
                )
            except AuthoringError as exc:
                print(f"serve: {exc}", file=sys.stderr)
                return 2

        if mode is None:
            print(
                f"openadapt-agent {__version__}: authoring tools enabled over "
                "local stdio; run tools disabled; --authoring does not imply "
                "--allow-run",
                file=sys.stderr,
            )
            _serve(serve, None, authoring_bridge)
            return 0

        if mode == "sandbox":
            from openadapt_agent.sandbox import SandboxEngine, default_sandbox_dir

            runs_dir = Path(args.runs_dir).expanduser() if args.runs_dir else default_sandbox_dir()
            runs_dir.mkdir(parents=True, exist_ok=True)
            sandbox_engine = SandboxEngine(
                runs_dir, engine=args.sandbox_engine, headed=args.headed
            )
            bridge = AgentBridge(
                None,
                RunnerConfig(runs_dir=runs_dir, timeout_s=args.timeout),
                sandbox=sandbox_engine,
                mode="sandbox",
            )
            note = ""
            if sandbox_engine.unavailable_reason == "browser_extra_missing":
                note = (
                    " To drive the synthetic app in a hidden browser instead, "
                    "install the tutorial extra."
                )
            print(
                f"openadapt-agent {__version__}: sandbox mode. One workflow "
                f"({next(iter(bridge.catalog))}) on a synthetic app; engine "
                f"{sandbox_engine.engine}. Tools: list_workflows, run_workflow, "
                f"get_run. Nothing real changes.{note}",
                file=sys.stderr,
            )
            _serve(serve, bridge, authoring_bridge)
            return 0

        extra_run_args = list(args.extra_run_arg)
        runner_config = RunnerConfig(
            flow_cli=(tuple(shlex.split(args.flow_cli)) if args.flow_cli else default_flow_cli()),
            runs_dir=Path(args.runs_dir or "runs"),
            url=args.url,
            deployment_config=args.config,
            policy=args.policy,
            timeout_s=args.timeout,
            allow_url_override=args.allow_url_override,
            extra_run_args=tuple(extra_run_args),
        )
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
                mode=mode,
            )
            n = len(bridge.workflows)
            print(
                f"openadapt-agent {__version__}: {mode} mode; serving {n} workflow(s) "
                "over local stdio; run tools "
                f"{'enabled' if args.allow_run else 'disabled (read-only)'}; attended "
                f"decisions {'enabled' if args.allow_attended_actions else 'disabled'}; "
                "live Continue/Skip "
                f"{'ready' if bridge.attended.live_actions_ready else 'not configured'}; "
                "protected MCP export "
                f"{'ENABLED' if args.allow_protected_export else 'disabled'}; "
                "synthetic recorded defaults "
                f"{'ENABLED' if args.allow_synthetic_recorded_defaults else 'disabled'}; "
                f"authoring {'enabled' if authoring_bridge is not None else 'disabled'}",
                file=sys.stderr,
            )
            _serve(serve, bridge, authoring_bridge)
    except TutorialError as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    finally:
        if sandbox_engine is not None:
            sandbox_engine.close()
        if authoring_bridge is not None:
            closer = getattr(authoring_bridge.session, "close", None)
            if callable(closer):
                closer()
    return 0


def _serve(serve, bridge, authoring):
    """Call serve without surprising 1-arg monkeypatches in existing tests."""
    if authoring is None:
        serve(bridge)
        return
    serve(bridge, authoring=authoring)


def _cmd_authoring_connect(args: argparse.Namespace) -> int:
    from openadapt_agent.mailbox import MailboxError, connect_mailbox

    try:
        return connect_mailbox(
            args.target,
            url=args.url,
            headed=args.headed,
        )
    except MailboxError as exc:
        print(f"authoring connect: {exc}", file=sys.stderr)
        return 2


def _cmd_emit_skill(args: argparse.Namespace) -> int:
    from openadapt_agent.skill import emit_agent_skill

    try:
        skill_dir = emit_agent_skill(
            Path(args.bundle), Path(args.out), include_bundle=args.include_bundle
        )
    except ValueError as exc:
        print(f"emit-skill: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote Agent Skill folder: {skill_dir}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
