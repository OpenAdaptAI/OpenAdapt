"""Run the candidate on every reference episode, stopped before each save.

``openadapt-flow readiness run --episodes DIR --out RUNS --app-url URL
(--agent-cmd CMD | --program BUNDLE)``

For each episode, one at a time:

1. The operator's ``--reset-cmd`` (if any) puts the sandbox back to its
   synthetic "before" state.
2. The runner starts a headless Chromium with a DevTools port, opens one tab
   at ``--app-url``, and puts :class:`~openadapt_flow.agent_guard.shadow_hold.ShadowHoldGuard`
   on it over CDP (every request of the tab is paused and decided).
3. It starts the candidate through an :class:`~openadapt_flow.readiness.launcher.AgentLauncher`
   and decides the tab's requests until the save was held (and then
   ``--linger`` seconds more), the candidate exits, or ``--timeout`` passes.
4. It stops the candidate *before* the guard lets go of the tab, so a retried
   save can't slip through once requests are no longer paused, then blocks
   anything still paused and closes the browser.

Per task, ``RUNS/<task_id>/`` holds ``run.json`` (no values), the held
requests (``held/``), ``proposed_write.json`` and ``before_screen.txt``
(values, mode 0600), the guard's event log and the agent's log.
``RUNS/run.json`` records the mode and the scope statement.

Replay state. The primary mode (``--mode sandbox``) runs on a sandbox or
test tenant seeded with synthetic "before" state, reset before each task.
``--mode run-ahead`` runs the candidate on a live queue item first; the
person then does the item, and ``readiness import`` pairs the two by task
id. Contamination detection is on in both modes (see
:mod:`openadapt_flow.readiness.compare`).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow.agent_guard.requests import NotASave, origin_of
from openadapt_flow.readiness.launcher import AgentLauncher, AgentResult, LaunchJob

__all__ = [
    "MODES",
    "RUN_SCHEMA",
    "SCOPE_STATEMENT",
    "RunnerConfig",
    "run_episodes",
]

RUN_SCHEMA = "openadapt.readiness-run/v1"
TASK_RUN_SCHEMA = "openadapt.readiness-task-run/v1"
MODES = ("sandbox", "run-ahead")
SCOPE_STATEMENT = (
    "No non-GET request to the app origin left the browser except listed "
    "sign-in requests. Not covered: GET requests with side effects, WebSocket "
    "writes, other tabs, writes before attach."
)
POLL_MS = 25
SCREEN_EVERY_S = 0.1


@dataclass
class RunnerConfig:
    app_url: str
    mode: str = "sandbox"
    not_saves: Sequence[NotASave] = ()
    screen_selector: Optional[str] = None
    timeout_s: float = 300.0
    linger_s: float = 1.5
    reset_cmd: Optional[Sequence[str]] = None
    verify: Optional[Path] = None
    lens: Any = None
    headless: bool = True
    viewport: tuple[int, int] = (1280, 800)
    #: Called before each task, after ``reset_cmd`` (tests use it).
    reset: Optional[Callable[[], None]] = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("mode must be sandbox or run-ahead")
        if not origin_of(self.app_url):
            raise ValueError("app_url must be an absolute http(s) URL")


def _write_private(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    )
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _reset(cfg: RunnerConfig) -> None:
    if cfg.reset_cmd:
        subprocess.run(list(cfg.reset_cmd), check=True, timeout=120)
    if cfg.reset is not None:
        cfg.reset()


def _program_proposal(
    guard: Any, result: AgentResult, page: Any, run_dir: Path
) -> None:
    """A program stopped before its save sent nothing; its approval request's
    form values are the proposed write, checked against the screen now."""

    from openadapt_flow.agent_guard.shadow_hold import PROPOSED_WRITE_SCHEMA
    from openadapt_flow.guard import check_identity

    if guard.proposed is not None or not result.proposed_fields:
        return
    screen = page.screen_text()
    status, _ = check_identity(guard.task.expected_record, screen)
    record = {
        "schema": PROPOSED_WRITE_SCHEMA,
        "task_id": guard.task.task_id,
        "request_index": None,
        "method": None,
        "url_path": None,
        "origin": guard.app_origin,
        "held_at": _now(),
        "identity": {
            "ok": status == "verified",
            "code": "verified" if status == "verified" else f"identity_{status}",
        },
        "fields": list(result.proposed_fields),
        "withheld_fields": 0,
        "source": "approval_request",
        "refused": True,
    }
    guard.proposed = record
    _write_private(run_dir / "proposed_write.json", record)


def run_one(
    episode_dir: Path,
    episode: dict[str, Any],
    out: Path,
    launcher: AgentLauncher,
    cfg: RunnerConfig,
) -> dict[str, Any]:
    """One task in its own browser and guard session."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow.agent_guard.cdp import CdpGuardPage
    from openadapt_flow.agent_guard.shadow_hold import ShadowHoldGuard
    from openadapt_flow.guard import GuardedSave, load_task

    task_id = str(episode["task_id"])
    run_dir = out / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    task = load_task(episode_dir / "guard_task.json")
    inputs = json.loads((episode_dir / "inputs.json").read_text(encoding="utf-8"))
    save = episode.get("save") or None
    record: dict[str, Any] = {
        "schema": TASK_RUN_SCHEMA,
        "task_id": task_id,
        "status": "AGENT_ERROR",
        "ended": None,
        "agent_wall_s": None,
        "exit_code": None,
        "usage": None,
        "model_cost_usd": None,
        "landed_writes": 0,
        "held": 0,
        "double_save": False,
        "other_origin_refused": 0,
        "proposed": False,
        "before_screen": False,
        "notes": [],
    }
    _reset(cfg)
    port = _free_port()
    pw = sync_playwright().start()
    browser = None
    agent = None
    cdp_page = None
    guard = None
    try:
        browser = pw.chromium.launch(
            headless=cfg.headless, args=[f"--remote-debugging-port={port}"]
        )
        width, height = cfg.viewport
        tab = browser.new_page(viewport={"width": width, "height": height})
        tab.goto(cfg.app_url)
        guarded = (
            GuardedSave.from_file(cfg.verify, run_dir / "guard", approval="not_gated")
            if cfg.verify
            else None
        )
        cdp_page = CdpGuardPage(
            tab, screen_selector=cfg.screen_selector, all_resources=True
        )
        guard = ShadowHoldGuard(
            guarded,
            task,
            cdp_page,
            app_origin=cfg.app_url,
            run_dir=run_dir,
            not_saves=cfg.not_saves,
            lens=cfg.lens,
            save_target=(save["method"], save["url_path"]) if save else None,
            events_path=run_dir / "guard_events.jsonl",
        )
        job = LaunchJob(
            task_id=task_id,
            instruction=str(inputs.get("instruction") or ""),
            inputs=dict(inputs.get("inputs") or {}),
            cdp_url=f"http://127.0.0.1:{port}",
            page_url=cfg.app_url,
            run_dir=run_dir,
        )
        started = time.monotonic()
        agent = launcher.start(job)
        held_at: Optional[float] = None
        exited_at: Optional[float] = None
        last_screen = 0.0
        ended = "timeout"
        while True:
            tab.wait_for_timeout(POLL_MS)
            cdp_page.drain(guard)
            now = time.monotonic()
            if held_at is None and now - last_screen >= SCREEN_EVERY_S:
                guard.observe_screen()
                last_screen = now
            if guard.finished and held_at is None:
                held_at = now
            if held_at is not None and now - held_at >= cfg.linger_s:
                ended = "held"
                break
            if exited_at is None and agent.poll() is not None:
                exited_at = now
            if exited_at is not None and now - exited_at >= 0.5:
                ended = "held" if held_at is not None else "agent_exited"
                break
            if now - started >= cfg.timeout_s:
                ended = "held" if held_at is not None else "timeout"
                break
        end = held_at if held_at is not None else (exited_at or time.monotonic())
        record["agent_wall_s"] = round(end - started, 3)
        record["ended"] = ended
        # The candidate goes first: once the tab is released nothing is held.
        agent.stop()
        cdp_page.drain(guard)
        result = agent.result()
        _program_proposal(guard, result, cdp_page, run_dir)
        record["exit_code"] = result.exit_code
        record["usage"] = result.usage
        record["model_cost_usd"] = (result.usage or {}).get("cost_usd")
        record["notes"] += result.notes
    except Exception as exc:  # noqa: BLE001 - recorded; the next task still runs
        record["notes"].append(f"runner error: {type(exc).__name__}: {exc}")
    finally:
        if agent is not None:
            try:
                agent.stop()
            except Exception:  # noqa: BLE001
                pass
        if cdp_page is not None:
            cdp_page.release()
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        pw.stop()
    if guard is not None:
        outcome = guard.outcome()
        record.update(
            {
                "landed_writes": outcome["landed_writes"],
                "held": outcome["held"],
                "double_save": outcome["double_save"],
                "other_origin_refused": outcome["other_origin_refused"],
                "proposed": guard.proposed is not None,
                "requests": outcome["requests"],
            }
        )
        if guard.before_screen is not None:
            _write_private(run_dir / "before_screen.txt", guard.before_screen)
            record["before_screen"] = True
        if outcome["landed_writes"]:
            record["status"] = "FAILED_SCOPE"
        elif guard.proposed is not None:
            record["status"] = "HELD"
        elif record["ended"] == "timeout":
            record["status"] = "TIMEOUT"
        elif record["ended"] is not None:
            record["status"] = "NO_WRITE_HELD"
    _write_private(run_dir / "run.json", record)
    return record


def run_episodes(
    episodes: Path | str,
    out: Path | str,
    launcher: AgentLauncher,
    cfg: RunnerConfig,
    *,
    only: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Run every episode under ``episodes``; returns the run manifest."""

    from openadapt_flow.readiness.reference import load_episodes

    episodes, out = Path(episodes), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "agent_label": launcher.label,
        "mode": cfg.mode,
        "app_origin": origin_of(cfg.app_url),
        "scope_statement": SCOPE_STATEMENT,
        "started_at": _now(),
        "reset_cmd": shlex.join(cfg.reset_cmd) if cfg.reset_cmd else None,
        "tasks": [],
    }
    for folder, episode in load_episodes(episodes):
        if only and episode["task_id"] not in only:
            continue
        result = run_one(folder, episode, out, launcher, cfg)
        manifest["tasks"].append(
            {"task_id": result["task_id"], "status": result["status"]}
        )
    manifest["finished_at"] = _now()
    manifest["landed_writes"] = sum(
        json.loads((out / t["task_id"] / "run.json").read_text("utf-8"))["landed_writes"]
        for t in manifest["tasks"]
    )
    _write_private(out / "run.json", manifest)
    return manifest
