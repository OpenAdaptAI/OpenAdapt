"""``openadapt-flow flight``: record any agent's run, gate its commits.

    openadapt-flow flight [--task-id ID] [--cdp URL] [--max-cost-usd N] -- CMD ...

Starts a loopback proxy in front of the model API and runs CMD with
ANTHROPIC_BASE_URL and OPENAI_BASE_URL pointing at it, so every model call
is metered and priced, and every screenshot and text block is scrubbed of
personal data on this machine before it is sent. The provider the agent
already targets (its own *_BASE_URL, else the provider's API) is the only
place anything is sent; the agent's key passes through untouched and is
never logged.

CMD also gets OPENADAPT_FLIGHT_GATE_URL: an executor wrapped with
GatedComputer or GatedComputerCall (one line, see FLIGHT_RECORDER.md) logs
its actions here and holds each click on Save, Submit, Send... (or on a
target it can't read) for a person: --gate terminal asks y/N on this
terminal, --gate file writes an item-approval request Desktop or a paired
phone answers, --gate refuse holds every one.

With --cdp (or when CMD starts Chromium with --remote-debugging-port), the
existing `openadapt-flow guard` also attaches to the browser, given
--guard-config and --guard-task.

Writes flight.json (openadapt.flight-run/v1), timeline.jsonl, scrubbed frame
thumbnails and timeline.html in the run directory, and prints its path.
Exit status: CMD's own; 2 when the run could not start (no scrubber, a bad
option, CMD not found).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

__all__ = ["add_parser", "cmd_flight"]

_UNSCRUBBED_PURPOSE = "flight: model API requests"
_PORT_FLAG = re.compile(r"^--remote-debugging-port=(\d+)$")


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "flight",
        help=(
            "Record any agent you already run: meter and price its model calls, "
            "scrub its screenshots before they leave the machine, log its actions, "
            "and hold each Save/Submit/Send for a person"
        ),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--task-id", help="the task this run does (default: generated)")
    p.add_argument(
        "--out", help="the run directory (default: flight-runs/<time>-<task>)"
    )
    p.add_argument(
        "--label", help="names the agent or worker in flight.json (default: CMD's name)"
    )
    p.add_argument(
        "--gate",
        choices=["terminal", "file", "refuse"],
        default="terminal",
        help="who decides a held action: y/N on this terminal (refuses with no "
        "terminal), an item-approval request for Desktop or a phone, or nobody",
    )
    p.add_argument(
        "--approval-timeout",
        type=float,
        default=300.0,
        help="seconds --gate file waits for a person before the action stays held",
    )
    p.add_argument("--cdp", help="the agent's Chromium DevTools URL, to run the guard")
    p.add_argument("--guard-config", help="guard.yaml for the guard")
    p.add_argument(
        "--guard-task", help="the guard's task file (openadapt.guard-task/v1)"
    )
    p.add_argument(
        "--max-cost-usd",
        type=float,
        help="refuse model calls (HTTP 429) once this much is spent; an unpriced "
        "model is refused too, since the cap can't be enforced",
    )
    p.add_argument(
        "--no-scrub",
        action="store_true",
        help="send screenshots and text unscrubbed (needs --i-accept-unscrubbed)",
    )
    p.add_argument(
        "--i-accept-unscrubbed",
        action="store_true",
        help="confirm --no-scrub; recorded in flight.json and the local ledger",
    )
    p.add_argument("--json", action="store_true", help="print flight.json")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD ...")
    p.set_defaults(func=cmd_flight)


def _err(message: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return 2


def _task_id(value: Optional[str]) -> str:
    if value:
        return value
    return "flight-" + secrets.token_hex(4)


def _run_dir(out: Optional[str], task_id: str) -> Path:
    if out:
        return Path(out)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id)[:64]
    return Path("flight-runs") / f"{stamp}-{safe}"


def _detect_cdp(command: list[str]) -> Optional[str]:
    for arg in command:
        match = _PORT_FLAG.match(arg)
        if match:
            return f"http://127.0.0.1:{match.group(1)}"
    return None


def _upstreams() -> dict[str, str]:
    """The providers the agent already targets: its own base URLs, if set."""
    out: dict[str, str] = {}
    for provider, env in (
        ("anthropic", "ANTHROPIC_BASE_URL"),
        ("openai", "OPENAI_BASE_URL"),
    ):
        value = os.environ.get(env)
        if value:
            out[provider] = value
    return out


def cmd_flight(args: argparse.Namespace) -> int:
    from openadapt_flow.flight.gate import FileGate, RefuseGate, TerminalGate
    from openadapt_flow.flight.proxy import FlightProxy
    from openadapt_flow.flight.timeline import Timeline

    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return _err(
            "give the command to run after --: openadapt-flow flight -- CMD ..."
        )
    if args.no_scrub and not args.i_accept_unscrubbed:
        return _err(
            "--no-scrub sends personal data unscrubbed; add --i-accept-unscrubbed"
        )
    if args.max_cost_usd is not None and args.max_cost_usd < 0:
        return _err("--max-cost-usd must be zero or more")

    scrubber: Any = None
    if args.no_scrub:
        from openadapt_flow.privacy import outbound_opt_out

        reason = outbound_opt_out(
            _UNSCRUBBED_PURPOSE, "--no-scrub --i-accept-unscrubbed"
        )
        scrub_record: dict[str, Any] = {"mode": "off", "reason": reason}
        scrub_image = scrub_text = None
    else:
        from openadapt_flow.privacy import (
            get_scrubber,
            scrub_outbound_image,
            scrub_outbound_text,
        )

        scrubber = get_scrubber()
        if scrubber is None:
            return _err(
                "openadapt-privacy is not installed, so screenshots can't be scrubbed "
                "before they are sent; nothing was started. Install it with "
                "pip install 'openadapt-flow[privacy]' and python -m spacy download "
                "en_core_web_sm."
            )
        scrub_record = {"mode": "on", "provider": type(scrubber).__name__}

        def scrub_image(png: bytes) -> bytes:
            return scrub_outbound_image(png, scrubber)

        def scrub_text(text: str) -> str:
            return scrub_outbound_text(text, scrubber) or ""

    cdp = args.cdp or _detect_cdp(command)
    want_guard = cdp is not None and bool(args.guard_config and args.guard_task)
    if args.cdp and not want_guard:
        return _err("--cdp runs the guard, which needs --guard-config and --guard-task")

    task_id = _task_id(args.task_id)
    run_dir = _run_dir(args.out, task_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        return _err(f"{run_dir} is not empty; give a new --out")
    label = args.label or Path(command[0]).name
    try:
        timeline = Timeline(
            run_dir,
            task_id=task_id,
            executor_label=label,
            scrub_image=scrub_image,
            scrub_text=scrub_text,
            scrub_record=scrub_record,
        )
    except Exception as exc:  # noqa: BLE001 - e.g. strict at-rest mode
        return _err(str(exc))

    if args.gate == "file":
        gate: Any = FileGate(
            run_dir,
            run_id=task_id,
            workflow_name=task_id,
            timeout_s=args.approval_timeout,
        )
    elif args.gate == "refuse":
        gate = RefuseGate("hold")
    else:
        gate = TerminalGate()

    try:
        proxy = FlightProxy(
            timeline,
            upstreams=_upstreams(),
            scrubber=scrubber,
            scrub=not args.no_scrub,
            max_cost_usd=args.max_cost_usd,
            gate=gate,
        ).start()
    except ValueError as exc:
        return _err(str(exc))

    env = {**os.environ, **proxy.child_env(), "OPENADAPT_FLIGHT_RUN_DIR": str(run_dir)}
    guard_proc: Optional[subprocess.Popen[bytes]] = None
    guard_info: dict[str, Any] = {"cdp": cdp, "started": False}
    if want_guard:
        guard_cmd = [
            sys.executable,
            "-m",
            "openadapt_flow",
            "guard",
            "--cdp",
            str(cdp),
            "--config",
            args.guard_config,
            "--task",
            args.guard_task,
            "--out",
            str(run_dir / "guard"),
        ]
        try:
            guard_proc = subprocess.Popen(guard_cmd)
            guard_info["started"] = True
        except OSError as exc:
            guard_info["error"] = str(exc)
    elif cdp:
        guard_info["note"] = (
            "a DevTools port was seen; give --guard-config and --guard-task to guard it"
        )
        print(f"[openadapt flight] {guard_info['note']}", file=sys.stderr)

    timeline.record(
        "run_started", command_name=label, gate=args.gate, guard=bool(guard_proc)
    )
    exit_code = 2
    child: Optional[subprocess.Popen[bytes]] = None
    try:
        try:
            child = subprocess.Popen(command, env=env)
        except OSError as exc:
            print(f"Error: could not start {command[0]!r}: {exc}", file=sys.stderr)
            child = None
        if child is not None:
            previous = signal.getsignal(signal.SIGINT)

            def forward(signum: int, _frame: Any) -> None:
                if child is not None and child.poll() is None:
                    child.send_signal(signum)

            try:
                signal.signal(signal.SIGINT, forward)
            except ValueError:  # not the main thread (tests)
                previous = None
            try:
                exit_code = child.wait()
            finally:
                if previous is not None:
                    signal.signal(signal.SIGINT, previous)
    finally:
        if guard_proc is not None:
            if guard_proc.poll() is None:
                guard_proc.send_signal(signal.SIGTERM)
            try:
                guard_info["exit_code"] = guard_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                guard_proc.kill()
                guard_info["exit_code"] = guard_proc.wait()
        proxy.stop()
        timeline.record("run_ended", exit_code=exit_code)
        summary = timeline.finish(
            details={
                "exit_code": exit_code,
                "gate": args.gate,
                "guard": guard_info,
                "spend_cap_usd": args.max_cost_usd,
                "providers": {
                    name: _host(url) for name, url in proxy.upstreams.items()
                },
            }
        )

    if summary["model"]["calls"] == 0:
        print(
            "[openadapt flight] no model call came through the proxy: does the agent "
            "read ANTHROPIC_BASE_URL / OPENAI_BASE_URL?",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        model = summary["model"]
        cost = model["cost_usd"]
        held = sum(1 for e in summary["gate_events"] if e["kind"] != "allowed")
        print(
            f"{run_dir}  {model['calls']} model calls, "
            f"{'cost unknown' if cost is None else f'${cost:.4f}'}, "
            f"{summary['actions_count']} actions, {held} held"
        )
    return exit_code


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc
