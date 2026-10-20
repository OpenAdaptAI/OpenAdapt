"""``openadapt-flow guard``: guard an agent you already run.

Start your agent's Chromium with a DevTools port (the one line you add), then
start the guard for the task before the agent starts it::

    openadapt-flow guard --cdp http://127.0.0.1:9222 \\
        --config guard.yaml --task task.json --out runs/task-0001

The guard attaches to the agent's tab, holds every save until the record on
screen is the one ``task.json`` names, reads the save back, and records the
run. With a ``teach:`` section, a run that reads back correctly becomes a
draft program a person confirms and admits (``compile-agent admit``).

Exit status: 0 the save was read back VERIFIED; 3 a save was blocked and none
went through; 4 the save went through and was not verified (REFUTED or
RECONCILIATION_REQUIRED: a person checks the record); 5 no save was seen;
2 the config, the task or the browser could not be used.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

__all__ = ["add_parser", "cmd_guard"]


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "guard",
        help=(
            "Guard an agent you already run: attach to its Chromium over CDP, "
            "hold each save until the right record is on screen, read it back, "
            "and record the run for a compiled program"
        ),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cdp",
        required=True,
        help="the agent's Chromium DevTools endpoint on this computer "
        "(http://127.0.0.1:9222)",
    )
    p.add_argument("--config", required=True, help="guard.yaml")
    p.add_argument(
        "--task",
        required=True,
        help="the task file (openadapt.guard-task/v1) from whoever assigned it",
    )
    p.add_argument("--out", required=True, help="new folder for this task's run")
    p.add_argument(
        "--page-url",
        help="the tab's exact URL, when more than one tab is open on the app",
    )
    p.add_argument(
        "--observe-only",
        action="store_true",
        help="never block: log what the guard would block, still read back",
    )
    p.add_argument(
        "--approval",
        choices=["not_gated", "each_item"],
        help="override guard.yaml: each_item holds every save for a person",
    )
    p.add_argument(
        "--timeout", type=float, default=None, help="seconds before the guard stops"
    )
    p.add_argument(
        "--attach-wait",
        type=float,
        default=60.0,
        help="seconds to wait for the agent's tab on the app",
    )
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="FIELD",
        help="a field name or id whose value is never recorded (repeatable)",
    )
    p.add_argument(
        "--no-teach",
        action="store_true",
        help="don't hand a verified run to the teach dir",
    )
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(func=cmd_guard)


def cmd_guard(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from openadapt_flow.agent_guard.config import ConfigError, load_config
    from openadapt_flow.agent_guard.handoff import HandoffError, handoff
    from openadapt_flow.agent_guard.session import guard_task
    from openadapt_flow.guard import GuardRefused, load_task
    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        validate_browser_cdp_endpoint,
    )

    try:
        cfg = load_config(args.config)
        if args.observe_only:
            cfg = replace(cfg, observe_only=True)
        if args.approval:
            cfg = replace(cfg, approval=args.approval)
        task = load_task(args.task)
        validate_browser_cdp_endpoint(args.cdp)
    except (ConfigError, GuardRefused, BrowserAttachError) as exc:
        print(f"guard: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(
            f"guard: {out} is not empty; give each task its own --out", file=sys.stderr
        )
        return 2
    mode = "observe-only (never blocks)" if cfg.observe_only else "fail-closed"
    print(
        f"guard: waiting for the agent's tab on {cfg.app_origin} ({mode}, "
        f"approval {cfg.approval})",
        file=sys.stderr,
        flush=True,
    )
    # Ctrl-C or SIGTERM ends the session between requests, so a paused save
    # is blocked and the recording is finished instead of cut off.
    stopping: list[int] = []

    def _stop(signum: int, _frame: Any) -> None:
        stopping.append(signum)

    previous = {
        sig: signal.signal(sig, _stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        run = guard_task(
            cfg,
            task,
            cdp_endpoint=args.cdp,
            out_dir=out,
            page_url=args.page_url,
            timeout_s=args.timeout,
            attach_wait_s=args.attach_wait,
            secret_fields=tuple(args.secret),
            on_attached=lambda: print(
                "guard: attached; saves are held until the right record is on screen",
                file=sys.stderr,
                flush=True,
            ),
            stop=lambda: bool(stopping),
        )
    except BrowserAttachError as exc:
        print(f"guard: {exc}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    result: dict[str, Any] = {"run": run.summary, "out": str(run.out_dir)}
    if run.verified and cfg.teach is not None and not args.no_teach:
        if run.left_scope:
            # Page content or the agent reached outside the task's scope:
            # what was recorded is not a run to compile.
            result["handoff"] = {
                "status": "refused",
                "detail": "the run went outside the task's scope; no draft",
            }
        elif run.recording is None:
            result["handoff"] = {"status": "no_recording"}
        else:
            try:
                result["handoff"] = handoff(run.recording, cfg=cfg, task=task).to_dict()
            except HandoffError as exc:
                result["handoff"] = {"status": "refused", "detail": str(exc)}
    _print(result, as_json=args.json)
    if run.verified:
        return 0
    if run.verdict is not None:
        return 4
    return 3 if run.blocked else 5


def _print(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    run = result["run"]
    print(f"verdict: {run.get('verdict') or 'no save went through'}")
    for item in run.get("blocked") or []:
        print(f"  blocked a save: {item['code']}")
    for item in run.get("scope_blocked") or []:
        print(f"  blocked outside the task's scope: {item['code']} ({item['origin']})")
    for item in run.get("would_block") or []:
        print(f"  would have blocked (observe-only): {item['code']}")
    if run.get("hold_ms"):
        print(f"  held the save {run['hold_ms'][0]} ms for the checks")
    if run.get("receipt_path"):
        print(f"  receipt: {run['receipt_path']}")
    print(f"  run folder: {result['out']}")
    handed = result.get("handoff")
    if handed:
        print(f"run 2: {handed.get('status')}: {handed.get('detail') or ''}".rstrip())
        for step in handed.get("next_steps") or []:
            print(f"  next: {step}")
