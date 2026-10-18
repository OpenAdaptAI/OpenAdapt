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
