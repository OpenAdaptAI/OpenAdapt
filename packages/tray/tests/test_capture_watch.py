"""Tests for the local Capture recorder watcher."""

import json
from dataclasses import dataclass
from pathlib import Path

from openadapt_tray import capture_watch
from openadapt_tray.capture_watch import (
    ENDED,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    READY,
    STARTING,
    STOPPING,
    CaptureWatcher,
    capture_name,
    read_terminal_outcome,
)

SESSION = "11111111-1111-4111-8111-111111111111"


@dataclass
class FakeStatus:
    capture_dir: str
    phase: str
    ready: bool = False
    complete: bool = False
    integrity_verified: bool = False
    error_code: str | None = None


class FakeControl:
    """Stands in for ``openadapt_capture.control``."""

    def __init__(self):
        self.statuses: dict[str, FakeStatus] = {}
        self.stopped: list[str] = []
        self.discover_error: Exception | None = None

    def discover_recorders(self):
        if self.discover_error is not None:
            raise self.discover_error
        return list(self.statuses)

    def status_recording(self, session_id, timeout=5.0):
        return self.statuses[session_id]

    def stop_recording(self, session_id, timeout=60.0):
        self.stopped.append(session_id)


def _write_state(capture_dir: Path, **fields):
    payload = {
        "schema_version": "openadapt.capture-terminal.v1",
        "session_id": SESSION,
        **fields,
    }
    capture_dir.mkdir(parents=True, exist_ok=True)
    (capture_dir / "capture-state.json").write_text(json.dumps(payload))


def _kinds(events):
    return [event.kind for event in events]


class TestCaptureName:
    def test_flow_capture_subfolder_uses_the_recording_name(self):
        assert capture_name("/x/captures/login-demo/.capture") == "login-demo"

    def test_plain_capture_dir_uses_its_name(self):
        assert capture_name("/x/captures/my_capture") == "my_capture"


class TestLifecycle:
    def test_ready_only_when_the_recorder_says_ready(self, tmp_path):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        cap = str(tmp_path / "demo" / ".capture")

        control.statuses[SESSION] = FakeStatus(cap, "starting")
        events = watcher.poll_once()
        assert _kinds(events) == [STARTING]
        assert events[0].name == "demo"

        # phase "recording" without ready is still starting: no event.
        control.statuses[SESSION] = FakeStatus(cap, "recording", ready=False)
        assert watcher.poll_once() == []

        control.statuses[SESSION] = FakeStatus(cap, "recording", ready=True)
        assert _kinds(watcher.poll_once()) == [READY]

        # Unchanged status emits nothing.
        assert watcher.poll_once() == []

        control.statuses[SESSION] = FakeStatus(cap, "finalizing")
        assert _kinds(watcher.poll_once()) == [STOPPING]

    def test_disappeared_recorder_reports_its_saved_outcome(self, tmp_path):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        cap = tmp_path / "demo" / ".capture"
        control.statuses[SESSION] = FakeStatus(str(cap), "recording", ready=True)
        watcher.poll_once()

        _write_state(cap, phase="complete", complete=True, integrity_verified=True)
        del control.statuses[SESSION]
        events = watcher.poll_once()
        assert _kinds(events) == [ENDED]
        assert events[0].outcome == OUTCOME_COMPLETE
        assert watcher.active_session_ids() == []
        # Reported once.
        assert watcher.poll_once() == []

    def test_crashed_recorder_reports_failure(self, tmp_path):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        cap = tmp_path / "demo" / ".capture"
        control.statuses[SESSION] = FakeStatus(str(cap), "recording", ready=True)
        watcher.poll_once()

        _write_state(
            cap,
            phase="crashed",
            complete=False,
            error_code="recorder_process_exited",
        )
        del control.statuses[SESSION]
        (event,) = watcher.poll_once()
        assert event.outcome == OUTCOME_FAILED
        assert event.error_code == "recorder_process_exited"

    def test_terminal_phase_in_status_ends_once(self, tmp_path):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        cap = str(tmp_path / "c")
        control.statuses[SESSION] = FakeStatus(
            cap, "failed", error_code="video_writer_not_ready"
        )
        (event,) = watcher.poll_once()
        assert event.kind == ENDED
        assert event.outcome == OUTCOME_FAILED
        assert event.error_code == "video_writer_not_ready"
        assert watcher.poll_once() == []
        # When the descriptor then goes away, nothing new is reported.
        del control.statuses[SESSION]
        assert watcher.poll_once() == []

    def test_missing_state_file_is_unknown_not_success(self, tmp_path):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        control.statuses[SESSION] = FakeStatus(str(tmp_path / "gone"), "starting")
        watcher.poll_once()
        del control.statuses[SESSION]
        (event,) = watcher.poll_once()
        assert event.outcome == OUTCOME_UNKNOWN

    def test_discovery_failure_reports_nothing(self, tmp_path):
        control = FakeControl()
        control.discover_error = PermissionError("unsafe runtime dir")
        watcher = CaptureWatcher(lambda e: None, control=control)
        assert watcher.poll_once() == []


class TestTerminalOutcome:
    def test_other_session_is_a_mismatch(self, tmp_path):
        _write_state(tmp_path, phase="complete", complete=True, integrity_verified=True)
        outcome, error = read_terminal_outcome(str(tmp_path), "another-session")
        assert outcome == OUTCOME_UNKNOWN
        assert error == "state_file_mismatch"

    def test_complete_without_integrity_is_not_success(self, tmp_path):
        _write_state(
            tmp_path, phase="complete", complete=True, integrity_verified=False
        )
        outcome, _ = read_terminal_outcome(str(tmp_path), SESSION)
        assert outcome == OUTCOME_FAILED

    def test_corrupt_file_is_unknown(self, tmp_path):
        (tmp_path / "capture-state.json").write_text("{not json")
        outcome, error = read_terminal_outcome(str(tmp_path), SESSION)
        assert outcome == OUTCOME_UNKNOWN
        assert error == "state_file_unreadable"


class TestAvailability:
    def test_unavailable_without_openadapt_capture(self, monkeypatch):
        monkeypatch.setattr(capture_watch, "_load_control", lambda: None)
        watcher = CaptureWatcher(lambda e: None)
        assert not watcher.available()
        assert watcher.poll_once() == []
        assert watcher.start() is False

    def test_stop_session_uses_the_control_channel(self):
        control = FakeControl()
        watcher = CaptureWatcher(lambda e: None, control=control)
        watcher.stop_session(SESSION)
        assert control.stopped == [SESSION]
