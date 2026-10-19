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
- ``seal verify`` / ``seal keys pin`` — check a Seal offline against keys a
  person pinned (trust on first use, explicit rotation).
- ``emit-skill`` — write a PHI-safe Agent Skill for one workflow from its
  card: purpose, inputs, and how to read the four outcomes.

``serve`` stays local stdio. Hosted ChatGPT.com reaches this computer through
``authoring connect`` (outbound HTTPS), not a port-forwarded MCP server.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from openadapt_flow.agent import __version__
from openadapt_flow.agent.contract import APPROVAL_MODES
from openadapt_flow.agent.runner import RunnerConfig, default_runs_dir

__all__ = ["build_parser", "main"]


def _authoring_section(args: argparse.Namespace) -> object:
    """The ``backend:`` section to pin for authoring, with
    ``--authoring-from-config``; otherwise None.

    A ``--config`` given only for runs never pins authoring by itself. A web
    section is pinned with ``--url --headed`` instead (a fresh browser window).
    """

    from openadapt_flow.agent.authoring import AuthoringError

    if not getattr(args, "authoring_from_config", False):
        return None
    if not args.config:
        raise AuthoringError("--authoring-from-config needs --config deploy.yaml")
    from openadapt_flow.deployment import load_deployment

    try:
        section = load_deployment(args.config).backend
    except (OSError, ValueError) as exc:
        raise AuthoringError(f"could not read {args.config}: {exc}") from exc
    if (section.kind or "web").strip().lower() == "web":
        raise AuthoringError(
            "the deployment file names a web app; pass --url URL --headed to "
            "record it in a new Chromium window"
        )
    return section


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt-agent",
        description=(
            "Your AI agent decides what to enter. OpenAdapt enters it in the app "
            "and checks that it saved. 'serve' with no flags starts a sandbox."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
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
        "--authoring-from-config",
        action="store_true",
        help=(
            "With --authoring and --config: record the app the deployment "
            "file's backend: section names (windows, macos, linux, a local "
            "rdp or citrix client window), the same app its runs drive. "
            "Network RDP (rdp_host) is refused for authoring."
        ),
    )
    p.add_argument(
        "--windows-agent-url",
        metavar="URL",
        help=(
            "With --authoring: drive a native Windows app through the Flow "
            "Windows agent server running on the Windows computer (for example "
            "http://localhost:5001, or an SSH-forwarded port from another "
            "computer). "
            "The agent's input goes through the same synthetic-input path "
            "replay uses. The token comes from OAFLOW_AGENT_TOKEN."
        ),
    )
    p.add_argument(
        "--remote-window",
        metavar="APP",
        help=(
            "With --authoring: drive the RDP or Citrix session shown in this "
            "local client window (app name, for example 'Microsoft Remote "
            "Desktop'). Screen-only: the agent clicks pixels and presses keys."
        ),
    )
    p.add_argument(
        "--remote-window-title",
        metavar="TITLE",
        help="With --remote-window or --citrix: pick one window by its title.",
    )
    p.add_argument(
        "--citrix",
        action="store_true",
        help=(
            "With --authoring: drive the local Citrix Workspace window "
            "(--remote-window overrides the app name). Screen-only."
        ),
    )
    p.add_argument(
        "--authoring-screens",
        choices=("off", "scrubbed", "unscrubbed"),
        default="off",
        help=(
            "With --authoring: let the agent see the pinned window through the "
            "screen tool, so it can drive a native Windows app or a remote "
            "session from pixels. scrubbed removes identifiers first and "
            "shares nothing if that fails. Default off."
        ),
    )
    p.add_argument(
        "--guard",
        default=None,
        metavar="VERIFY_YAML",
        help=(
            "Serve only the guarded-save tools (guard_begin, guard_commit, "
            "guard_confirm) for any agent's own writes. VERIFY_YAML is the "
            "verify-run file: the system-of-record reader and the record the "
            "save must produce. Needs --guard-tasks and --guard-state."
        ),
    )
    p.add_argument(
        "--teach",
        default=None,
        metavar="VERIFY_YAML",
        help=(
            "Add the teach tools: the agent does a task once in the app at --url "
            "(or the --config backend: Windows, macOS, Linux, RDP, Citrix), "
            "Flow checks the saved record through VERIFY_YAML's verifier, "
            "compiles the run, and once it is admitted the agent calls it as a "
            "tool with no model calls. Run 1 makes a real entry: use a test copy."
        ),
    )
    p.add_argument(
        "--teach-dir",
        default=None,
        metavar="DIR",
        help="With --teach: where drafts and taught programs live (default RUNS_DIR/teach)",
    )
    p.add_argument(
        "--teach-key",
        default=None,
        metavar="PEM",
        help=(
            "With --teach and --teach-policy: the encrypted key of the registered "
            "agent that may admit what it taught (passphrase from "
            "$OPENADAPT_JIT_KEY_PASSPHRASE). Without it, a person admits at a "
            "terminal (openadapt-flow compile-agent admit)."
        ),
    )
    p.add_argument(
        "--teach-policy",
        default=None,
        metavar="JSON",
        help=(
            "With --teach-key: the admission policy a person signed "
            "(openadapt-flow jit sign-policy). The agent admits its own draft "
            "only if it sets allow_self_admission."
        ),
    )
    p.add_argument(
        "--key-dir",
        default=None,
        metavar="DIR",
        help="With --teach: the JIT key directory (default $OPENADAPT_JIT_KEY_DIR or ~/.openadapt/jit)",
    )
    p.add_argument(
        "--teach-hold",
        action="store_true",
        help=(
            "With --teach on a web app: a person also approves every save in "
            "the client's form (always on for other surfaces)."
        ),
    )
    p.add_argument(
        "--teach-screens",
        choices=("off", "scrubbed", "unscrubbed"),
        default="off",
        help=(
            "With --teach: add teach_screen so the agent sees the app (RDP and "
            "Citrix need it). scrubbed shares nothing if scrubbing fails."
        ),
    )
    p.add_argument(
        "--guard-tasks",
        default=None,
        metavar="DIR",
        help="With --guard: folder of <task_id>.json task files the operator wrote",
    )
    p.add_argument(
        "--guard-state",
        default=None,
        metavar="DIR",
        help=(
            "With --guard: folder for baselines, approval requests, receipts and "
            "the per-task ledger"
        ),
    )
    p.add_argument(
        "--guard-approval",
        choices=("each_item", "not_gated"),
        default="each_item",
        help="With --guard: each_item (default) holds every save for a person",
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
        help=(
            "Deployment config YAML forwarded to `openadapt-flow run --config`. "
            "With --authoring-from-config, its backend: section also names the "
            "app to record."
        ),
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
            "Directory for run records and evidence (default: "
            "$OPENADAPT_AGENT_RUNS_DIR, else ~/.openadapt/runs; the sandbox "
            "uses ~/.openadapt/agent-sandbox). Never relative to the folder "
            "the client starts the server in."
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
        "--approval-mode",
        choices=APPROVAL_MODES,
        default=None,
        help=(
            "Per-save approval passed to every `openadapt-flow run`. Unset "
            "(the default): Flow decides, and each save waits for approval "
            "(each_item) under every profile; a run waiting "
            "for that approval reports needs_review. not_gated turns the pause "
            "off for every run this server starts, and each run's result and "
            "Flow's report record that. Callers can't change it."
        ),
    )
    p.add_argument(
        "--admissions",
        default=None,
        metavar="DIR",
        help=(
            "Add prepare_admission, list_admissions and decide_admission: the "
            "`openadapt-flow admit` command over recordings in DIR/recordings "
            "and shadow candidates in DIR/shadow-candidates. A person decides "
            "in the client's form, or, with --delegate-approvals, the agent "
            "decides as that approver. An agent can't decide an admission it "
            "prepared."
        ),
    )
    p.add_argument(
        "--approver-grant",
        default=None,
        metavar="FILE",
        help=(
            "With --admissions and --delegate-approvals: the approver grant a "
            "person signed (openadapt-flow approver-grant sign) that lets "
            "AGENT_ID decide admissions. It must name AGENT_ID, cover the "
            "workflow and let an agent approve saves; Flow verifies it on "
            "every decision. Without it only a person decides."
        ),
    )
    p.add_argument(
        "--delegate-approvals",
        default=None,
        metavar="AGENT_ID",
        help=(
            "Let the calling agent answer saves that wait for an approval, as "
            "the opaque id AGENT_ID (for example agent:intake-bot). Adds the "
            "list_approval_requests and answer_approval_request tools. Flow "
            "accepts the agent's answer only under a person-signed approver "
            "grant (openadapt-flow run --approver-policy) that names AGENT_ID "
            "and allows an agent for that step; by default only a person may "
            "answer. Runs this server starts record AGENT_ID as their "
            "initiator, so the agent can never approve its own runs. Flow "
            "records the agent and the grant's digest as the approver."
        ),
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
    p.add_argument(
        "--repair-access",
        default=None,
        metavar="FILE",
        help=(
            "Screen access setting for repairs, written by `openadapt-agent "
            "repair access`. Without it, halted runs still return needs_repair "
            "but no screens, and teach_repair is not offered."
        ),
    )
    p.add_argument(
        "--repair-dir",
        default=None,
        metavar="DIR",
        help="Where repair tokens, approvals, and candidates live (default RUNS_DIR/.repair).",
    )
    p.add_argument(
        "--hold-on-halt",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help=(
            "With --repair-access, keep a halted browser run's page open this "
            "long so teach_repair acts on it (default 300; 0 turns it off)."
        ),
    )
    p.set_defaults(func=_cmd_serve)

    repair = sub.add_parser(
        "repair",
        help="Control whether calling agents may help repair halted runs (local only).",
    )
    repair_sub = repair.add_subparsers(dest="repair_command", required=True)
    access = repair_sub.add_parser(
        "access",
        help="Set screen access for repairs for one workflow or the whole organization.",
    )
    scope = access.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workflow", help="Workflow name from list_workflows")
    scope.add_argument("--org", action="store_true", help="Default for every workflow")
    access.add_argument(
        "--screens",
        required=True,
        choices=("off", "scrubbed_only", "ask_each_time", "allow_unscrubbed"),
    )
    access.add_argument(
        "--by", required=True, help="Your name, recorded with the setting"
    )
    access.add_argument("--file", required=True, help="The --repair-access file")
    access.set_defaults(func=_cmd_repair_access)
    approve = repair_sub.add_parser(
        "approve-screen",
        help="Look at the exact screen a calling agent would get for one repair, and approve it.",
    )
    approve.add_argument("token_id", help="token_id from the needs_repair result")
    approve.add_argument(
        "--by", required=True, help="Your name, recorded with the approval"
    )
    approve.add_argument("--repair-access", required=True, metavar="FILE")
    approve.add_argument("--repair-dir", required=True, metavar="DIR")
    approve.set_defaults(func=_cmd_repair_approve)

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

    from openadapt_flow.agent.seal_cli import add_seal_parser

    add_seal_parser(sub)

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


def _legacy_runs_dir(args: argparse.Namespace) -> Optional[Path]:
    """An earlier version's implicit ``./runs`` that this default would skip.

    Before ~/.openadapt/runs, the default was ./runs in whatever folder the
    client started the server. Its request-id ledger and pauses live there;
    silently using a new folder would let a resent request_id run again. Only
    checked when the operator named no folder.
    """
    if args.runs_dir or os.environ.get("OPENADAPT_AGENT_RUNS_DIR", "").strip():
        return None
    legacy = Path("runs").resolve()
    if not (legacy / "openadapt-agent").is_dir():
        return None
    if legacy == default_runs_dir().expanduser().resolve():
        return None
    return legacy


def _cmd_serve(args: argparse.Namespace) -> int:
    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.flow_service import open_attended_service
    from openadapt_flow.agent.mcp import serve
    from openadapt_flow.agent.runner import default_flow_cli
    from openadapt_flow.agent.tutorial import TutorialError

    if args.guard:
        return _serve_guard(args, serve)
    if args.teach:
        return _serve_teach(args, serve)
    if (
        args.teach_key
        or args.teach_policy
        or args.teach_dir
        or args.teach_hold
        or args.teach_screens != "off"
    ):
        print("serve: the --teach-* flags need --teach VERIFY_YAML", file=sys.stderr)
        return 2
    desktop_pin = bool(
        args.windows_agent_url
        or args.remote_window
        or args.remote_window_title
        or args.citrix
        or args.authoring_screens != "off"
    )
    if desktop_pin and not args.authoring:
        print(
            "serve: --windows-agent-url, --remote-window, --remote-window-title, "
            "--citrix, and --authoring-screens need --authoring",
            file=sys.stderr,
        )
        return 2
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
    if args.approval_mode and mode not in {"production", "attended"}:
        print(
            "serve: --approval-mode applies to runs of your workflows; it needs "
            "--mode production or --mode attended with --bundles",
            file=sys.stderr,
        )
        return 2
    if args.approval_mode and not args.flow_cli:
        from openadapt_flow.agent import approval

        if not approval.flow_supports_approval_modes():
            print(
                "serve: --approval-mode needs an openadapt-flow with approval "
                "modes; the one installed here has none, so its runs have no "
                "per-save pause to set",
                file=sys.stderr,
            )
            return 2
    if getattr(args, "approver_grant", None) is not None and (
        args.delegate_approvals is None or getattr(args, "admissions", None) is None
    ):
        print(
            "serve: --approver-grant needs --admissions and --delegate-approvals",
            file=sys.stderr,
        )
        return 2
    if args.delegate_approvals is not None:
        import re as _re

        from openadapt_flow.agent.delegated import DELEGATE_ID_PATTERN

        if mode not in {"production", "attended"}:
            print(
                "serve: --delegate-approvals needs --mode production or --mode "
                "attended with --bundles",
                file=sys.stderr,
            )
            return 2
        if not _re.match(DELEGATE_ID_PATTERN, args.delegate_approvals):
            print(
                "serve: --delegate-approvals needs an opaque id such as "
                "agent:intake-bot, never a name or email",
                file=sys.stderr,
            )
            return 2
    if args.allow_synthetic_recorded_defaults and not (args.allow_run and args.bundles):
        print(
            "serve: --allow-synthetic-recorded-defaults requires --allow-run",
            file=sys.stderr,
        )
        return 2
    legacy = _legacy_runs_dir(args) if mode in {"production", "attended"} else None
    if legacy is not None:
        default = default_runs_dir().expanduser().resolve()
        print(
            f"serve: {legacy} holds run records from an earlier version, which "
            "kept them in the folder the server started in. This version keeps "
            f"them in {default}, so it wouldn't know the request_ids sent before "
            f"and could enter one of those requests again. Pass --runs-dir "
            f"{legacy} to keep using that folder, or --runs-dir {default} to "
            "start a new one.",
            file=sys.stderr,
        )
        return 2

    sandbox_engine = None
    authoring_bridge = None
    try:
        if args.authoring:
            from openadapt_flow.agent.authoring import (
                AuthoringBridge,
                AuthoringError,
                open_authoring_session,
            )

            if args.runs_dir:
                authoring_root = Path(args.runs_dir)
            elif mode == "sandbox":
                from openadapt_flow.agent.sandbox import default_sandbox_dir

                authoring_root = default_sandbox_dir()
            else:
                authoring_root = default_runs_dir()
            authoring_dir = authoring_root.expanduser().resolve() / "authoring"
            try:
                authoring_bridge = AuthoringBridge(
                    open_authoring_session(
                        out_dir=authoring_dir,
                        url=args.url,
                        headed=args.headed,
                        windows_agent_url=args.windows_agent_url,
                        windows_agent_token=os.environ.get("OAFLOW_AGENT_TOKEN")
                        or None,
                        remote_window=args.remote_window,
                        remote_window_title=args.remote_window_title,
                        citrix=args.citrix,
                        deployment_backend=_authoring_section(args),
                    ),
                    out_dir=authoring_dir,
                    screens=args.authoring_screens,
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
            from openadapt_flow.agent.copy import TUTORIAL_SERVE_COMMAND
            from openadapt_flow.agent.sandbox import SandboxEngine, default_sandbox_dir

            runs_dir = (
                Path(args.runs_dir).expanduser()
                if args.runs_dir
                else default_sandbox_dir()
            )
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
                    f"install the tutorial extra: {TUTORIAL_SERVE_COMMAND}"
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
        # Resolve once, so every part of the server (runs, pauses, the
        # request-id ledger) uses the same absolute folder.
        runs_dir = (
            (Path(args.runs_dir) if args.runs_dir else default_runs_dir())
            .expanduser()
            .resolve()
        )
        runner_config = RunnerConfig(
            flow_cli=(
                tuple(shlex.split(args.flow_cli))
                if args.flow_cli
                else default_flow_cli()
            ),
            runs_dir=runs_dir,
            url=args.url,
            deployment_config=args.config,
            policy=args.policy,
            timeout_s=args.timeout,
            allow_url_override=args.allow_url_override,
            extra_run_args=tuple(extra_run_args),
            approval_mode=args.approval_mode,
            delegate_approver_id=args.delegate_approvals,
            hold_on_halt_s=(
                max(0.0, min(args.hold_on_halt, 1800.0)) if args.repair_access else 0.0
            ),
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
                repair_desk=_repair_desk(args, runner_config.runs_dir),
                admission_desk=_admission_desk(args),
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
                f"authoring {'enabled' if authoring_bridge is not None else 'disabled'}; "
                + (
                    f"approval mode {args.approval_mode} (chosen with --approval-mode); "
                    if args.approval_mode
                    else "approval mode set by Flow (each save waits for approval); "
                )
                + f"runs folder {runs_dir}",
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


def _serve_guard(args: argparse.Namespace, serve) -> int:
    """Serve only the guarded-save tools over local stdio."""
    from openadapt_flow.agent.guard_tools import GuardBridge
    from openadapt_flow.guard import GuardedSave
    from openadapt_flow.verify_run import VerifyRunError

    if args.authoring or args.bundles or args.tutorial:
        print(
            "serve: --guard serves only the guarded-save tools; it cannot be "
            "combined with --authoring, --bundles or --tutorial",
            file=sys.stderr,
        )
        return 2
    if not args.guard_tasks or not args.guard_state:
        print("serve: --guard needs --guard-tasks and --guard-state", file=sys.stderr)
        return 2
    try:
        guarded = GuardedSave.from_file(
            args.guard,
            Path(args.guard_state).expanduser().resolve(),
            approval=args.guard_approval,
        )
    except VerifyRunError as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    print(
        f"openadapt-agent {__version__}: guarded-save tools over local stdio; "
        f"approval {args.guard_approval}; tasks {args.guard_tasks}",
        file=sys.stderr,
    )
    serve(None, guard=GuardBridge(guarded, Path(args.guard_tasks).expanduser()))
    return 0


def _teach_app(args: argparse.Namespace) -> tuple[Optional[str], Any]:
    """The app to teach in: (url, None) for a web app, (None, backend section)
    for any other surface."""

    if not args.config:
        if not args.url:
            raise ValueError("--teach needs the app: --url, or --config deploy.yaml")
        return args.url, None
    from openadapt_flow.deployment import load_deployment

    section = load_deployment(args.config).backend
    if (section.kind or "web").strip().lower() == "web":
        url = args.url or getattr(section, "url", None)
        if not url:
            raise ValueError("the deployment names a web app; pass --url")
        return url, None
    if args.url:
        raise ValueError("--url names a web app; the deployment names another surface")
    if section.kind == "rdp" and section.rdp_host:
        raise ValueError(
            "network RDP (rdp_host) can't be taught; use the local client window "
            "(rdp_window), as for authoring"
        )
    return None, section


def _serve_teach(args: argparse.Namespace, serve) -> int:
    """Serve the teach tools next to the workflow tools over local stdio."""
    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.runner import default_flow_cli
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig

    if args.authoring or args.tutorial or args.mode in ("sandbox", "attended"):
        print(
            "serve: --teach has its own recording tools; it can't be combined "
            "with --authoring, --tutorial, or --mode sandbox/attended",
            file=sys.stderr,
        )
        return 2
    if bool(args.teach_key) != bool(args.teach_policy):
        print("serve: --teach-key and --teach-policy go together", file=sys.stderr)
        return 2
    if args.bundles and args.mode != "production":
        print(
            "serve: with --teach, --bundles needs --mode production",
            file=sys.stderr,
        )
        return 2
    try:
        from openadapt_flow.record_sor import load_observer

        url, surface = _teach_app(args)
        # Read the verifier now: a typo fails here, not at the agent's first task.
        load_observer(Path(args.teach).expanduser().resolve())
        key_dir = Path(args.key_dir).expanduser().resolve() if args.key_dir else None
        admitter = None
        if args.teach_key:
            from openadapt_flow.jit import authority, lifecycle

            admitter = authority.principal_from_key(
                authority.load_encrypted_key(Path(args.teach_key)),
                key_dir or lifecycle.default_key_dir(),
                policy=authority.SignedAdmissionPolicy.load(args.teach_policy),
            )
        runs_dir = (
            (Path(args.runs_dir) if args.runs_dir else default_runs_dir())
            .expanduser()
            .resolve()
        )
        config = TeachConfig(
            verify=Path(args.teach).expanduser().resolve(),
            cache_dir=(
                Path(args.teach_dir).expanduser().resolve()
                if args.teach_dir
                else runs_dir / "teach"
            ),
            url=url,
            surface=surface,
            key_dir=key_dir,
            admitter=admitter,
            hold=args.teach_hold,
            headless=not args.headed,
            screens=args.teach_screens,
        )
    except Exception as exc:  # noqa: BLE001 - every setup problem is a refusal
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    runner_config = RunnerConfig(
        flow_cli=(
            tuple(shlex.split(args.flow_cli)) if args.flow_cli else default_flow_cli()
        ),
        runs_dir=runs_dir,
        url=args.url,
        deployment_config=args.config,
        policy=args.policy,
        timeout_s=args.timeout,
        approval_mode=args.approval_mode,
    )
    bridge = AgentBridge(
        Path(args.bundles) if args.bundles else None,
        runner_config,
        allow_run=True,
        mode="production",
    )
    teach = TeachBridge(config, workflows=bridge)
    print(
        f"openadapt-agent {__version__}: teach tools over local stdio on the "
        f"{teach.backend_kind} app; admission "
        + (
            f"by agent {admitter.principal_id} under policy "
            f"{admitter.policy.policy.policy_id}"
            if admitter is not None
            else "by a person at a terminal"
        )
        + f"; {len(teach.taught)} taught workflow(s); runs folder {runs_dir}",
        file=sys.stderr,
    )
    serve(bridge, teach=teach)
    return 0


def _serve(serve, bridge, authoring):
    """Call serve without surprising 1-arg monkeypatches in existing tests."""
    if authoring is None:
        serve(bridge)
        return
    serve(bridge, authoring=authoring)


def _cmd_authoring_connect(args: argparse.Namespace) -> int:
    from openadapt_flow.agent.mailbox import MailboxError, connect_mailbox

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
    from openadapt_flow.agent.skill import emit_agent_skill

    try:
        skill_dir = emit_agent_skill(
            Path(args.bundle), Path(args.out), include_bundle=args.include_bundle
        )
    except ValueError as exc:
        print(f"emit-skill: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote Agent Skill folder: {skill_dir}")
    return 0


def _admission_desk(args: argparse.Namespace) -> Optional[Any]:
    """The admission tools, only when the operator passed ``--admissions``."""
    if getattr(args, "admissions", None) is None:
        return None
    from openadapt_flow.agent.admission import AdmissionDesk

    return AdmissionDesk(
        args.admissions,
        delegate_id=args.delegate_approvals,
        approver_grant=getattr(args, "approver_grant", None),
    )


def _repair_desk(args: argparse.Namespace, runs_dir: Path):
    from openadapt_flow.agent.repair import (
        LazyBackendDemonstrator,
        RepairAccessPolicy,
        RepairDesk,
    )

    if not args.repair_access:
        policy = RepairAccessPolicy(None)
        demonstrator = None
    else:
        policy = RepairAccessPolicy(args.repair_access)
        demonstrator = LazyBackendDemonstrator(url=args.url, headed=args.headed)
    return RepairDesk(
        Path(args.repair_dir) if args.repair_dir else Path(runs_dir) / ".repair",
        policy,
        demonstrator=demonstrator,
    )


def _cmd_repair_access(args: argparse.Namespace) -> int:
    from openadapt_flow.agent.repair import RepairError, confirm_at_terminal, set_access

    if args.screens == "allow_unscrubbed" and not confirm_at_terminal(
        "Calling agents will see screens with names, dates of birth, and record "
        "numbers left in. Type 'share unscrubbed' to confirm: ",
        "share unscrubbed",
    ):
        print(
            "Not changed. allow_unscrubbed needs confirmation at a terminal.",
            file=sys.stderr,
        )
        return 1
    try:
        set_access(
            args.file,
            workflow=None if args.org else args.workflow,
            mode=args.screens,
            enabled_by=args.by,
        )
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    target = "every workflow" if args.org else args.workflow
    print(f"Screen access for repairs on {target}: {args.screens} (set by {args.by}).")
    return 0


def _cmd_repair_approve(args: argparse.Namespace) -> int:
    from openadapt_flow.agent.repair import (
        LazyBackendDemonstrator,
        RepairAccessPolicy,
        RepairDesk,
        RepairError,
        confirm_at_terminal,
    )

    desk = RepairDesk(
        args.repair_dir,
        RepairAccessPolicy(args.repair_access),
        demonstrator=LazyBackendDemonstrator(),
    )
    try:
        sha, record = desk.find(args.token_id)
        if desk.policy.for_workflow(record["workflow"]).mode != "ask_each_time":
            raise RepairError(
                "this workflow isn't set to ask_each_time; nothing to approve"
            )
        image, evidence = desk.prepare_preview(sha, record)
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    preview = Path(args.repair_dir) / "approvals" / f"preview-{args.token_id}.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(image)
    os.chmod(preview, 0o600)
    print(f"The calling agent would get exactly this image: {preview}")
    print(f"Personal data removed: {'yes' if evidence else 'NO'}.")
    if not confirm_at_terminal(
        f"Type the token id ({args.token_id}) to share it for this one repair: ",
        args.token_id,
    ):
        print("Not approved. Nothing was shared.", file=sys.stderr)
        return 1
    try:
        desk.approve_screen(sha, approved_by=args.by, image=image, evidence=evidence)
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Approved for this repair only.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
