"""Watch local Capture recorders that OpenAdapt Desktop did not start.

``openadapt-flow record`` and ``capture record`` both run an
openadapt-capture ``Recorder``. Each recorder publishes an authenticated
loopback control endpoint and, when it ends, a ``capture-state.json`` file in
its capture directory. This module polls that public client contract
(``openadapt_capture.control.discover_recorders`` and ``status_recording``)
so the tray can show when a recording is really ready and when it ends.

The recorder's ``ready`` flag is the only signal used for "Recording". The
"perform your workflow now" text that ``openadapt-flow record`` prints comes
before the recorder has started, so it is not a readiness signal.

openadapt-capture is optional. When it is not importable the watcher reports
``available() == False`` and never starts.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Event kinds, in lifecycle order.
STARTING = "starting"
READY = "ready"
STOPPING = "stopping"
ENDED = "ended"

# Outcomes carried by an ENDED event.
OUTCOME_COMPLETE = "complete"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"

_TERMINAL_PHASES = frozenset({"complete", "failed", "crashed"})
_STOPPING_PHASES = frozenset({"stopping", "finalizing"})
_TERMINAL_STATE_FILENAME = "capture-state.json"
_TERMINAL_STATE_SCHEMA = "openadapt.capture-terminal.v1"
_MAX_STATE_BYTES = 64 * 1024


@dataclass(frozen=True)
class CaptureEvent:
    """One lifecycle change of one local recorder session."""

    kind: str
    session_id: str
    name: str
    capture_dir: str
    outcome: str | None = None
    error_code: str | None = None


@dataclass
class _Tracked:
    capture_dir: str
    stage: str | None = None


def capture_name(capture_dir: str) -> str:
    """Return a person-readable name for a capture directory.

    ``openadapt-flow record --out X`` records into ``X/.capture``; the useful
    name there is ``X``.
    """
    path = Path(capture_dir)
    if path.name.startswith(".") and path.parent.name:
        return path.parent.name
    return path.name or str(path)


def _stage(status: Any) -> str:
    """Map a ``RecorderStatus`` onto a watcher stage."""
    phase = str(getattr(status, "phase", ""))
    if phase in _TERMINAL_PHASES:
        return ENDED
    if phase in _STOPPING_PHASES:
        return STOPPING
    if phase == "recording" and getattr(status, "ready", False) is True:
        return READY
    return STARTING


def _outcome_from_status(status: Any) -> tuple[str, str | None]:
    if (
        getattr(status, "phase", None) == "complete"
        and getattr(status, "complete", False) is True
        and getattr(status, "integrity_verified", False) is True
    ):
        return OUTCOME_COMPLETE, None
    return OUTCOME_FAILED, getattr(status, "error_code", None) or str(
        getattr(status, "phase", "failed")
    )


def read_terminal_outcome(
    capture_dir: str,
    session_id: str,
    verify_seal: Callable[[str], Any] | None = None,
) -> tuple[str, str | None]:
    """Read the outcome a recorder left in ``capture-state.json``.

    Success needs all of: an owner-only file for this exact session, marked
    complete and integrity verified, and a capture seal that ``verify_seal``
    accepts. The recorder writes "complete" just before it seals, so the file
    alone is not proof. A missing or unreadable file, or no way to check the
    seal, is ``OUTCOME_UNKNOWN``, never success.
    """
    path = Path(capture_dir) / _TERMINAL_STATE_FILENAME
    try:
        details = path.lstat()
        if path.is_symlink():
            return OUTCOME_UNKNOWN, "state_file_unreadable"
        if os.name == "posix" and (
            details.st_mode & 0o077 or details.st_uid != os.getuid()
        ):
            return OUTCOME_UNKNOWN, "state_file_not_owner_only"
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            return OUTCOME_UNKNOWN, "state_file_unreadable"
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return OUTCOME_UNKNOWN, "state_file_missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return OUTCOME_UNKNOWN, "state_file_unreadable"
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _TERMINAL_STATE_SCHEMA
        or payload.get("session_id") != session_id
    ):
        return OUTCOME_UNKNOWN, "state_file_mismatch"
    if (
        payload.get("phase") == "complete"
        and payload.get("complete") is True
        and payload.get("integrity_verified") is True
    ):
        if verify_seal is None:
            return OUTCOME_UNKNOWN, "seal_not_checked"
        try:
            verify_seal(capture_dir)
        except Exception:
            return OUTCOME_FAILED, "capture_seal_invalid"
        return OUTCOME_COMPLETE, None
    if payload.get("phase") in _TERMINAL_PHASES:
        error = payload.get("error_code") or payload.get("phase")
        return OUTCOME_FAILED, str(error)
    # The process is gone but never wrote a terminal phase.
    return OUTCOME_FAILED, str(payload.get("error_code") or "recorder_exited")


def _load_control() -> Any | None:
    try:
        from openadapt_capture import control
    except Exception:
        return None
    return control


def _load_seal_verifier() -> Callable[[str], Any] | None:
    try:
        from openadapt_capture.terminal import verify_capture_artifacts
    except Exception:
        return None
    return verify_capture_artifacts


class CaptureWatcher:
    """Poll local recorders and report lifecycle changes.

    ``poll_once`` is synchronous and returns the events it found, so it can be
    tested without threads. ``start`` runs it on a daemon thread and passes
    each event to ``on_event``.
    """

    def __init__(
        self,
        on_event: Callable[[CaptureEvent], None],
        *,
        interval_s: float = 0.5,
        control: Any | None = None,
        status_timeout_s: float = 1.0,
        verify_seal: Callable[[str], Any] | None = None,
    ):
        """Create a watcher.

        Args:
            on_event: Called once per lifecycle change, from the watcher thread.
            interval_s: Seconds between polls.
            control: The ``openadapt_capture.control`` module (injectable for
                tests). Loaded lazily when omitted.
            status_timeout_s: Timeout for one status request.
            verify_seal: Checks a capture's seal; raises when it is invalid.
                Defaults to ``openadapt_capture.terminal.verify_capture_artifacts``.
        """
        self._on_event = on_event
        self._interval_s = interval_s
        self._control = control if control is not None else _load_control()
        self._status_timeout_s = status_timeout_s
        self._verify_seal = (
            verify_seal if verify_seal is not None else _load_seal_verifier()
        )
        self._sessions: dict[str, _Tracked] = {}
        # Held while polling AND while dispatching, so the watcher thread and
        # a caller of poll_and_dispatch apply events in the order produced.
        self._poll_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def available(self) -> bool:
        """Return True when openadapt-capture's control client is importable."""
        return self._control is not None

    def active_session_ids(self) -> list[str]:
        """Return sessions that are live and not yet ended."""
        return [sid for sid, t in self._sessions.items() if t.stage != ENDED]

    def poll_once(self) -> list[CaptureEvent]:
        """Poll every live recorder once and return lifecycle changes.

        Safe to call from more than one thread; each change is returned once.
        """
        if self._control is None:
            return []
        with self._poll_lock:
            return self._poll_locked()

    def poll_and_dispatch(self) -> None:
        """Poll once and pass each change to ``on_event`` on this thread.

        Dispatch happens under the poll lock: two threads never interleave
        their events, so a late "stopping" cannot follow an "ended".
        """
        if self._control is None:
            return
        with self._poll_lock:
            for event in self._poll_locked():
                try:
                    self._on_event(event)
                except Exception as e:
                    print(f"Error handling capture event: {e}")

    def _poll_locked(self) -> list[CaptureEvent]:
        try:
            live = list(self._control.discover_recorders())
        except Exception as e:
            self._log_once(f"Could not list local recorders: {e}")
            return []
        self._last_error = None

        events: list[CaptureEvent] = []
        for session_id in live:
            try:
                status = self._control.status_recording(
                    session_id, timeout=self._status_timeout_s
                )
            except Exception as e:
                # The recorder may be exiting. If it is gone, the next poll
                # reads its state file.
                self._log_once(f"Could not read recorder {session_id}: {e}")
                continue
            tracked = self._sessions.get(session_id)
            if tracked is None:
                tracked = _Tracked(capture_dir=str(status.capture_dir))
                self._sessions[session_id] = tracked
            if tracked.stage == ENDED:
                continue
            stage = _stage(status)
            if stage == tracked.stage:
                continue
            tracked.stage = stage
            if stage == ENDED:
                outcome, error = _outcome_from_status(status)
                events.append(self._event(ENDED, session_id, tracked, outcome, error))
            else:
                events.append(self._event(stage, session_id, tracked))

        live_ids = set(live)
        for session_id in list(self._sessions):
            if session_id in live_ids:
                continue
            tracked = self._sessions.pop(session_id)
            if tracked.stage == ENDED:
                continue
            outcome, error = read_terminal_outcome(
                tracked.capture_dir, session_id, self._verify_seal
            )
            events.append(self._event(ENDED, session_id, tracked, outcome, error))
        return events

    def stop_session(self, session_id: str, timeout_s: float = 60.0) -> None:
        """Ask one recorder to stop through its authenticated control channel.

        Raises:
            Exception: Whatever ``openadapt_capture.control.stop_recording``
                raises when the stop does not end in a verified capture.
        """
        if self._control is None:
            raise RuntimeError("openadapt-capture is not installed")
        self._control.stop_recording(session_id, timeout=timeout_s)

    def start(self) -> bool:
        """Start polling on a daemon thread. Returns False when unavailable."""
        if self._control is None or self._thread is not None:
            return self._thread is not None
        self._thread = threading.Thread(
            target=self._run, name="openadapt-capture-watch", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_and_dispatch()
            self._stop.wait(self._interval_s)

    def _event(
        self,
        kind: str,
        session_id: str,
        tracked: _Tracked,
        outcome: str | None = None,
        error: str | None = None,
    ) -> CaptureEvent:
        return CaptureEvent(
            kind=kind,
            session_id=session_id,
            name=capture_name(tracked.capture_dir),
            capture_dir=tracked.capture_dir,
            outcome=outcome,
            error_code=error,
        )

    def _log_once(self, message: str) -> None:
        if message != self._last_error:
            print(message)
            self._last_error = message
