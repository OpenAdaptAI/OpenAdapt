"""One guarded task: attach to the agent's tab, guard its save, record run 1.

:func:`guard_task` connects to the agent's own Chromium over CDP twice:

* **The guard**, on this thread, with its own connection. It puts the commit
  guard on the tab and runs until the task's save has been read back (or the
  tab closes, or the time runs out).
* **The recorder**, on its own thread with its own connection: the browser
  recorder (:class:`~openadapt_flow.interactive_recorder.InteractiveRecorder`,
  the ``record --browser-cdp-endpoint`` mode) with the network capture on.
  It records run 1 for run 2.

They are kept apart so that the recording can fail without the guard
stopping. The recorder refuses a recording it can't keep exactly (for
example when the agent acts faster than a frame can be kept); then run 1
can't become a program, but every save is still held and read back.

The agent keeps driving its browser; nothing in it changes except that it was
started with a DevTools port. The guard covers one task, from when it attaches
until the save is read back. A save the agent sends before the guard attaches
is not seen, so start the guard before the agent starts the task. Only the
attached tab is guarded.

Output, under ``out_dir``::

    recording/          run 1, in the format ``compile`` reads (with network.jsonl)
    guard/              guarded-save state: baseline, approvals, receipt
    guard_events.jsonl  each decision, with no record values
    summary.json        the outcome (``openadapt.agent-guard-run/v1``)
"""

from __future__ import annotations

import json
import threading
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

#: How often the guard's loop hands paused requests to the commit guard.
POLL_MS = 25


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
    notes: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.verdict == "VERIFIED"

    @property
    def blocked(self) -> list[dict[str, Any]]:
        return list(self.summary.get("blocked") or [])

    @property
    def left_scope(self) -> bool:
        """The tab sent or tried a request outside the task's teach scope.
        Such a run is never handed off as a draft."""

        return bool(self.summary.get("scope_blocked"))


def _connect(
    cfg: GuardConfig,
    *,
    cdp_endpoint: str,
    page_url: Optional[str],
    attach_wait_s: float,
) -> tuple[Any, Any, Any]:
    """The guard's own connection and the agent's tab, waiting for the tab."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        select_attached_page,
    )

    pw = sync_playwright().start()
    deadline = time.monotonic() + attach_wait_s
    try:
        while True:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_endpoint)
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise BrowserAttachError(
                        "could not connect to the agent's Chromium DevTools endpoint"
                    ) from exc
                time.sleep(0.25)
                continue
            try:
                page = select_attached_page(
                    browser, app_url=cfg.app_url, page_url=page_url
                )
                return pw, browser, page
            except BrowserAttachError:
                browser.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
    except BaseException:
        pw.stop()
        raise


class _RecorderThread:
    """The run-1 recorder on its own thread and its own connection."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.recording: Optional[Path] = None
        self.error: Optional[str] = None
        self._thread = threading.Thread(
            target=self._main, name="openadapt-guard-recorder", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _main(self) -> None:
        from openadapt_flow.interactive_recorder import InteractiveRecorder

        recorder = None
        try:
            recorder = InteractiveRecorder(**self._kwargs)
            recorder.start()
            self.ready.set()
            while not self._stop.is_set():
                if not recorder.pump():
                    break
            self.recording = recorder.finish()
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            self.error = str(exc) or type(exc).__name__
            if recorder is not None:
                try:
                    recorder.abort()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.ready.set()

    def stop(self, timeout_s: float = 60.0) -> Optional[Path]:
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            self.error = self.error or "the recorder did not finish in time"
        return self.recording


def task_scope(cfg: GuardConfig, task: GuardTask) -> Any:
    """The task's teach scope: the app's origin and the operator's other
    origins, the task's record and values, and the app's write adapter."""

    from openadapt_flow.teach_scope import TeachScope, load_write_adapter

    origins = [
        cfg.app_origin,
        *cfg.allow_origins,
        *(rule.origin for rule in cfg.not_saves if rule.origin),
    ]
    return TeachScope.from_guard_task(
        task,
        origins=origins,
        constants=cfg.allow_text,
        write_adapter=(
            load_write_adapter(cfg.write_adapter) if cfg.write_adapter else None
        ),
    )


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
    record: bool = True,
    on_attached: Optional[Callable[[], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> GuardRun:
    """Guard one task's save in the agent's own browser and record the run.

    ``on_attached`` is called once the guard holds the tab's requests and the
    recorder has started (or failed): a harness starts its agent then. ``stop``
    ends the session early when it returns True. After the save is read back
    the session keeps recording for ``linger_s`` so the save's result is on
    the last frame.
    """

    from openadapt_flow.record_sor import RecordingObservation, RecordSorError

    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    observation: Optional[RecordingObservation] = None
    reader: Optional[Callable[[], Any]] = None
    if record:
        try:
            # The record reads around run 1 (``record --verify``): run 2's
            # record check and compiled effects are proposed from them.
            observation = RecordingObservation.start(cfg.verify)
            reader = lambda: observation.observer.system_of_record  # noqa: E731
        except RecordSorError as exc:
            # The save is still read back by verify-run; only the
            # recording's record snapshots are missing.
            notes.append(f"no record snapshots in the recording: {exc}")
    guarded = GuardedSave(
        load_verify_config(cfg.verify), out / "guard", approval=cfg.approval
    )
    started = time.monotonic()
    pw, browser, tab = _connect(
        cfg, cdp_endpoint=cdp_endpoint, page_url=page_url, attach_wait_s=attach_wait_s
    )
    page: Optional[CdpGuardPage] = None
    recorder: Optional[_RecorderThread] = None
    ended = "stopped"
    try:
        scope = task_scope(cfg, task)
        page = CdpGuardPage(
            tab, screen_selector=cfg.screen_selector, all_resources=True
        )
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
            scope=scope,
        )
        if record:
            recorder = _RecorderThread(
                url=cfg.app_url,
                out_dir=out / "recording",
                cdp_endpoint=cdp_endpoint,
                browser_page_url=page_url,
                capture_network=True,
                system_of_record_reader=reader,
                secret_fields=secret_fields,
            )
            recorder.start()
            # Saves are held from here on; wait (pumping the guard) for the
            # recorder so run 1 is recorded from the first action.
            deadline = time.monotonic() + attach_wait_s
            while not recorder.ready.is_set() and time.monotonic() < deadline:
                tab.wait_for_timeout(POLL_MS)
                page.drain(guard)
        if on_attached is not None:
            on_attached()
        finished_at: Optional[float] = None
        while True:
            try:
                tab.wait_for_timeout(POLL_MS)
            except Exception:  # noqa: BLE001 - the tab or the browser is gone
                ended = "tab_closed"
                break
            page.drain(guard)
            now = time.monotonic()
            if guard.finished and finished_at is None:
                finished_at = now
            if finished_at is not None and now - finished_at >= linger_s:
                ended = "read_back"
                break
            if timeout_s is not None and now - started >= timeout_s:
                ended = "timeout"
                break
            if stop is not None and stop():
                ended = "stopped"
                break
        if ended != "tab_closed":
            # Hand over whatever the save left queued before letting go.
            page.drain(guard)
    except KeyboardInterrupt:
        ended = "stopped"
    except BaseException:
        if recorder is not None:
            recorder.stop(timeout_s=10.0)
        raise
    finally:
        if page is not None:
            page.release()
        try:
            browser.close()
        except Exception:  # noqa: BLE001 - only disconnects; the browser stays
            pass
        pw.stop()
    recording: Optional[Path] = None
    if recorder is not None:
        recording = recorder.stop()
        if recorder.error:
            notes.append(f"run 1 was not recorded: {recorder.error}")
    if recording is not None and observation is not None:
        observation.finish(recording, {k: str(v) for k, v in task.params.items()})
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
