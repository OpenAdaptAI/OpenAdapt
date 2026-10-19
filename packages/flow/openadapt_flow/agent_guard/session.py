"""One guarded task: attach to the agent's tab, guard its save, record run 1.

:func:`guard_task` attaches the browser recorder
(:class:`~openadapt_flow.interactive_recorder.InteractiveRecorder`, the
``record --browser-cdp-endpoint`` mode) to the agent's own Chromium with the
network capture on, puts the commit guard on the same tab, and runs both from
one loop until the task's save has been read back (or the tab closes, or the
time runs out). The agent keeps driving its browser; nothing in it changes
except that it was started with a DevTools port.

The guard covers one task, from when it attaches until the save is read back.
A save the agent sends before the guard attaches is not seen, so start the
guard before the agent starts the task. Only the attached tab is guarded.

Output, under ``out_dir``::

    recording/          run 1, in the format ``compile`` reads (with network.jsonl)
    guard/              guarded-save state: baseline, approvals, receipt
    guard_events.jsonl  each decision, with no record values
    summary.json        the outcome (``openadapt.agent-guard-run/v1``)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.agent_guard.cdp import CdpGuardPage
from openadapt_flow.agent_guard.commit_guard import CommitGuard
from openadapt_flow.agent_guard.config import GuardConfig
from openadapt_flow.guard import GuardedSave, GuardTask
from openadapt_flow.verify_run import load_verify_config

__all__ = ["GuardRun", "guard_task"]


@dataclass
class GuardRun:
    """What one guarded task did."""

    out_dir: Path
    verdict: Optional[str]
    summary: dict[str, Any]
    recording: Optional[Path] = None
    #: Why the session ended: ``read_back``, ``tab_closed``, ``timeout``,
    #: ``stopped``.
    ended: str = "read_back"
    seconds: float = 0.0
    handoff: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.verdict == "VERIFIED"

    @property
    def blocked(self) -> list[dict[str, Any]]:
        return list(self.summary.get("blocked") or [])


def _attach(
    cfg: GuardConfig,
    recording_dir: Path,
    *,
    cdp_endpoint: str,
    page_url: Optional[str],
    attach_wait_s: float,
    reader: Optional[Callable[[], Any]],
    secret_fields: tuple[str, ...],
) -> Any:
    """Start the recorder on the agent's tab, waiting for the tab to exist."""

    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        InteractiveRecorder,
    )

    deadline = time.monotonic() + attach_wait_s
    while True:
        recorder = InteractiveRecorder(
            cfg.app_url,
            recording_dir,
            cdp_endpoint=cdp_endpoint,
            browser_page_url=page_url,
            capture_network=True,
            system_of_record_reader=reader,
            secret_fields=secret_fields,
        )
        try:
            recorder.start()
            return recorder
        except BrowserAttachError:
            recorder.abort()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def guard_task(
    cfg: GuardConfig,
    task: GuardTask,
    *,
    cdp_endpoint: str,
    out_dir: Path | str,
    page_url: Optional[str] = None,
    timeout_s: Optional[float] = None,
    attach_wait_s: float = 60.0,
    linger_s: float = 1.0,
    agent_id: str = "attached-agent",
    secret_fields: tuple[str, ...] = (),
    on_attached: Optional[Callable[[], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> GuardRun:
    """Guard one task's save in the agent's own browser and record the run.

    ``on_attached`` is called once the guard holds the tab's requests (a
    harness starts its agent then). ``stop`` ends the session early when it
    returns True. After the save is read back the session keeps recording for
    ``linger_s`` so the save's result is on the last frame.
    """

    from openadapt_flow.record_sor import RecordingObservation, RecordSorError

    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    observation: Optional[RecordingObservation] = None
    reader: Optional[Callable[[], Any]] = None
    try:
        # The record reads around run 1 (``record --verify``): run 2's
        # record check and compiled effects are proposed from them.
        observation = RecordingObservation.start(cfg.verify)
        reader = lambda: observation.observer.system_of_record  # noqa: E731
    except RecordSorError as exc:
        # The save is still read back by verify-run; only the recording's
        # record snapshots are missing, so run 1 can't become a program.
        notes.append(f"no record snapshots in the recording: {exc}")
    guarded = GuardedSave(
        load_verify_config(cfg.verify), out / "guard", approval=cfg.approval
    )
    started = time.monotonic()
    recorder = _attach(
        cfg,
        out / "recording",
        cdp_endpoint=cdp_endpoint,
        page_url=page_url,
        attach_wait_s=attach_wait_s,
        reader=reader,
        secret_fields=secret_fields,
    )
    page = None
    guard = None
    ended = "stopped"
    recording: Optional[Path] = None
    try:
        page = CdpGuardPage(recorder.page, screen_selector=cfg.screen_selector)
        guard = CommitGuard(
            guarded,
            task,
            page,
            app_origin=cfg.app_origin,
            not_saves=cfg.not_saves,
            observe_only=cfg.observe_only,
            agent_id=agent_id,
            response_wait_s=cfg.response_wait_s,
            events_path=out / "guard_events.jsonl",
        )
        if on_attached is not None:
            on_attached()
        finished_at: Optional[float] = None
        while True:
            alive = recorder.pump()
            page.drain(guard)
            now = time.monotonic()
            if guard.finished and finished_at is None:
                finished_at = now
            if finished_at is not None and now - finished_at >= linger_s:
                ended = "read_back"
                break
            if not alive:
                ended = "tab_closed"
                break
            if timeout_s is not None and now - started >= timeout_s:
                ended = "timeout"
                break
            if stop is not None and stop():
                ended = "stopped"
                break
        # Let the last frame and any request the save left queued through
        # the guard before the session lets go of the tab.
        page.drain(guard)
    except KeyboardInterrupt:
        ended = "stopped"
    except BaseException:
        if page is not None:
            page.release()
        recorder.abort()
        raise
    if page is not None:
        page.release()
    try:
        if ended != "tab_closed":
            finished: Path = recorder.finish()
            recording = finished
            if observation is not None:
                observation.finish(
                    finished, {k: str(v) for k, v in task.params.items()}
                )
        else:
            recorder.abort()
            notes.append("the agent's tab closed; run 1 was not kept")
    except Exception as exc:  # noqa: BLE001 - the guard's verdict stands
        notes.append(f"the recording could not be finished ({type(exc).__name__})")
        recording = None
    assert guard is not None
    summary = guard.summary.to_dict()
    summary.update(
        {
            "ended": ended,
            "seconds": round(time.monotonic() - started, 3),
            "recording": str(recording) if recording else None,
            "notes": notes,
        }
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return GuardRun(
        out_dir=out,
        verdict=guard.summary.verdict,
        summary=summary,
        recording=recording,
        ended=ended,
        seconds=summary["seconds"],
        notes=notes,
    )
