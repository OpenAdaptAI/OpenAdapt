"""``openadapt-flow service``: the unattended on-prem service."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _cmd_run(args: argparse.Namespace) -> int:
    from openadapt_flow.onprem_service import (
        ServiceConfig,
        default_queue_script,
        run_service,
    )

    cfg = ServiceConfig(
        root=Path(args.root).resolve(),
        queue_script=Path(args.queue_script)
        if args.queue_script
        else default_queue_script(),
        poll_seconds=args.poll_seconds,
        allow_outbound=args.allow_outbound,
        max_cycles=args.max_cycles,
    )
    return run_service(cfg)


def _cmd_health(args: argparse.Namespace) -> int:
    from openadapt_flow.onprem_service import health, restart

    report = health(
        Path(args.root), max_age_s=args.max_age, stuck_after_s=args.stuck_after
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["healthy"]:
        return 0
    if args.restart_on_fail:
        rc = restart(args.label)
        print(f"restart requested ({args.label}): exit {rc}", file=sys.stderr)
    return 1


def _cmd_restart(args: argparse.Namespace) -> int:
    from openadapt_flow.onprem_service import restart

    return restart(args.label)


def _cmd_egress_audit(args: argparse.Namespace) -> int:
    from openadapt_flow.onprem_service import egress_audit

    report = egress_audit(
        Path(args.root), pid=args.pid, samples=args.samples, interval_s=args.interval
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("verdict") == "clean" else 1


def _cmd_boot_check(args: argparse.Namespace) -> int:
    import getpass

    from openadapt_flow.onprem_reboot import (
        SettingsError,
        boot_check,
        boot_id,
        default_session_probe,
        load_settings,
    )

    root = Path(args.root)
    try:
        settings = load_settings(root)
    except SettingsError as exc:
        print(f"service: {exc}", file=sys.stderr)
        return 2
    account = args.account or getpass.getuser()
    result = boot_check(
        root,
        boot_id=boot_id(),
        probe=default_session_probe(account),
        settings=settings,
    )
    print(
        json.dumps(
            {
                "boot_id": result.boot_id,
                "logged_in": result.logged_in,
                "waited_s": round(result.waited_s, 1),
                "notice": result.notice["id"] if result.notice else None,
                "alerts": result.alerts,
            },
            sort_keys=True,
        )
    )
    return 0 if result.logged_in else 1


def _cmd_notices(args: argparse.Namespace) -> int:
    import getpass

    from openadapt_flow.onprem_reboot import acknowledge, list_notices, summarize

    root = Path(args.root)
    if args.ack:
        notice = acknowledge(root, args.ack, by=args.by or getpass.getuser())
        if notice is None:
            print(f"no notice {args.ack}", file=sys.stderr)
            return 1
        print(json.dumps(summarize([notice])[0], indent=2, sort_keys=True))
        return 0
    notices = list_notices(root, include_closed=args.all)
    print(json.dumps(summarize(notices), indent=2, sort_keys=True))
    return 0


def _cmd_pf_render(args: argparse.Namespace) -> int:
    from openadapt_flow.no_outbound_allow import AllowList, AllowListError
    from openadapt_flow.pf_firewall import PfRuleError, plan

    try:
        allow = AllowList.load_or_empty(Path(args.allow_file))
        text = plan(args.user, allow.entries).anchor_text
    except (AllowListError, PfRuleError) as exc:
        print(f"service pf: {exc}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_pf_check(args: argparse.Namespace) -> int:
    from openadapt_flow.pf_firewall import missing_pf_conf_lines, validate_anchor

    problems = validate_anchor(Path(args.anchor).read_text(encoding="utf-8"), args.user)
    if args.pf_conf:
        for line in missing_pf_conf_lines(Path(args.pf_conf).read_text("utf-8")):
            problems.append(f"pf.conf lacks: {line}")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "service", help="Unattended on-prem service: run, health, restart, egress audit"
    )
    s = p.add_subparsers(dest="service_cmd", required=True)

    def root(a: argparse.ArgumentParser) -> None:
        a.add_argument(
            "--root", required=True, help="On-prem storage root (jobs/, runs/, audit/)"
        )

    r = s.add_parser(
        "run", help="Run the queue loop (what launchd/systemd keeps alive)"
    )
    root(r)
    r.add_argument(
        "--queue-script", default=None, help="Default: deploy/on-prem/bin/run-queue.sh"
    )
    r.add_argument("--poll-seconds", type=float, default=10.0)
    r.add_argument("--max-cycles", type=int, default=None, help=argparse.SUPPRESS)
    r.add_argument(
        "--allow-outbound",
        action="store_true",
        help="Run without strict no-outbound mode (recorded in the heartbeat)",
    )
    r.set_defaults(func=_cmd_run)

    h = s.add_parser("health", help="Check the service; exit 1 when unhealthy")
    root(h)
    h.add_argument(
        "--max-age", type=float, default=None, help="Heartbeat age limit (s)"
    )
    h.add_argument(
        "--stuck-after",
        type=float,
        default=3600.0,
        help="Job in processing/ too long (s)",
    )
    h.add_argument("--restart-on-fail", action="store_true")
    h.add_argument("--label", default="ai.openadapt.flow-service")
    h.set_defaults(func=_cmd_health)

    t = s.add_parser("restart", help="Ask the service manager to restart the service")
    t.add_argument("--label", default="ai.openadapt.flow-service")
    t.set_defaults(func=_cmd_restart)

    e = s.add_parser(
        "egress-audit",
        help="Sample the service process tree's sockets; exit 0 only if no peer is off the machine",
    )
    root(e)
    e.add_argument(
        "--pid", type=int, default=None, help="Default: pid from the heartbeat"
    )
    e.add_argument("--samples", type=int, default=5)
    e.add_argument("--interval", type=float, default=2.0)
    e.set_defaults(func=_cmd_egress_audit)

    b = s.add_parser(
        "boot-check",
        help="After a restart: wait for the service account to sign in; alert if late",
    )
    root(b)
    b.add_argument("--account", default=None, help="Default: the current user")
    b.set_defaults(func=_cmd_boot_check)

    n = s.add_parser("notices", help="List or acknowledge service notices")
    root(n)
    n.add_argument("--ack", default=None, metavar="ID")
    n.add_argument("--by", default=None, help="Who acknowledges (default: you)")
    n.add_argument("--all", action="store_true", help="Include acknowledged notices")
    n.set_defaults(func=_cmd_notices)

    pf = s.add_parser("pf", help="macOS pf rules for the service account (text only)")
    pfs = pf.add_subparsers(dest="pf_cmd", required=True)
    pr = pfs.add_parser("render", help="Render the anchor from the allow list")
    pr.add_argument("--user", required=True, help="Service account")
    pr.add_argument("--allow-file", required=True)
    pr.add_argument("--out", default=None)
    pr.set_defaults(func=_cmd_pf_render)
    pc = pfs.add_parser("check", help="Check a rendered anchor (and pf.conf)")
    pc.add_argument("anchor")
    pc.add_argument("--user", required=True)
    pc.add_argument("--pf-conf", default=None)
    pc.set_defaults(func=_cmd_pf_check)
