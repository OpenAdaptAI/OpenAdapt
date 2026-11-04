"""Tests for the local Capture recorder watcher."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

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
    check_owner_only,
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
    path = capture_dir / "capture-state.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)  # the recorder writes it owner-only


def _seal_ok(capture_dir):
    return None


def _owner_ok(path):
    """Stand-in owner check: the files tests write have no Windows DACL."""


def _seal_broken(capture_dir):
    raise RuntimeError("capture terminal bytes are not canonical")


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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
        control.statuses[SESSION] = FakeStatus(str(tmp_path / "gone"), "starting")
        watcher.poll_once()
        del control.statuses[SESSION]
        (event,) = watcher.poll_once()
        assert event.outcome == OUTCOME_UNKNOWN

    def test_discovery_failure_reports_nothing(self, tmp_path):
        control = FakeControl()
        control.discover_error = PermissionError("unsafe runtime dir")
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
        assert watcher.poll_once() == []


class TestTerminalOutcome:
    def test_other_session_is_a_mismatch(self, tmp_path):
        _write_state(tmp_path, phase="complete", complete=True, integrity_verified=True)
        outcome, error = read_terminal_outcome(
            str(tmp_path), "another-session", _seal_ok, _owner_ok
        )
        assert outcome == OUTCOME_UNKNOWN
        assert error == "state_file_mismatch"

    def test_complete_without_integrity_is_not_success(self, tmp_path):
        _write_state(
            tmp_path, phase="complete", complete=True, integrity_verified=False
        )
        outcome, _ = read_terminal_outcome(str(tmp_path), SESSION, _seal_ok, _owner_ok)
        assert outcome == OUTCOME_FAILED

    def test_corrupt_file_is_unknown(self, tmp_path):
        (tmp_path / "capture-state.json").write_text("{not json")
        (tmp_path / "capture-state.json").chmod(0o600)
        outcome, error = read_terminal_outcome(
            str(tmp_path), SESSION, _seal_ok, _owner_ok
        )
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
        watcher = CaptureWatcher(lambda e: None, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
        watcher.stop_session(SESSION)
        assert control.stopped == [SESSION]


class TestSuccessNeedsProof:
    def _complete(self, tmp_path):
        _write_state(tmp_path, phase="complete", complete=True, integrity_verified=True)

    def test_verified_seal_is_success(self, tmp_path):
        self._complete(tmp_path)
        assert read_terminal_outcome(str(tmp_path), SESSION, _seal_ok, _owner_ok) == (
            OUTCOME_COMPLETE,
            None,
        )

    def test_broken_seal_is_failure(self, tmp_path):
        # The recorder writes "complete" just before sealing; a crash in
        # between leaves a success-shaped file with no valid seal.
        self._complete(tmp_path)
        outcome, error = read_terminal_outcome(
            str(tmp_path), SESSION, _seal_broken, _owner_ok
        )
        assert outcome == OUTCOME_FAILED
        assert error == "capture_seal_invalid"

    def test_no_seal_check_is_unknown(self, tmp_path):
        self._complete(tmp_path)
        outcome, error = read_terminal_outcome(str(tmp_path), SESSION, None, _owner_ok)
        assert outcome == OUTCOME_UNKNOWN
        assert error == "seal_not_checked"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_group_readable_file_is_not_trusted(self, tmp_path):
        self._complete(tmp_path)
        (tmp_path / "capture-state.json").chmod(0o644)
        outcome, error = read_terminal_outcome(str(tmp_path), SESSION, _seal_ok)
        assert outcome == OUTCOME_UNKNOWN
        assert error == "state_file_not_owner_only"


def test_dispatch_is_ordered_across_threads(tmp_path):
    """A caller of poll_and_dispatch waits for the watcher's dispatch."""
    import threading

    control = FakeControl()
    seen = []
    entered = threading.Event()
    release = threading.Event()

    def on_event(event):
        seen.append(event.kind)
        if event.kind == STOPPING:
            entered.set()
            release.wait(2)

    watcher = CaptureWatcher(on_event, control=control, verify_seal=_seal_ok, check_owner=_owner_ok)
    cap = tmp_path / "demo" / ".capture"
    control.statuses[SESSION] = FakeStatus(str(cap), "finalizing")
    first = threading.Thread(target=watcher.poll_and_dispatch)
    first.start()
    assert entered.wait(2)

    _write_state(cap, phase="complete", complete=True, integrity_verified=True)
    del control.statuses[SESSION]
    second = threading.Thread(target=watcher.poll_and_dispatch)
    second.start()
    second.join(0.2)
    assert second.is_alive()  # blocked behind the first dispatch
    release.set()
    first.join(2)
    second.join(2)
    assert seen == [STOPPING, ENDED]


def test_real_seal_is_accepted_and_a_tampered_one_is_not(tmp_path):
    """Use openadapt-capture's own sealing, when the capture extra is installed."""
    terminal = pytest.importorskip("openadapt_capture.terminal")

    cap = tmp_path / "demo"
    _write_state(cap, phase="complete", complete=True, integrity_verified=True)
    (cap / "recording.db").write_bytes(b"recorded bytes")
    terminal.seal_capture(
        cap,
        session_id=SESSION,
        process_started_at=1.0,
        capture_started_at=2.0,
        capture_ended_at=3.0,
        event_counts={"action": 0, "screen": 0, "window": 0, "browser": 0, "video": 0},
        last_source_ordinal=None,
    )
    verify = terminal.verify_capture_artifacts
    assert read_terminal_outcome(str(cap), SESSION, verify, _owner_ok) == (OUTCOME_COMPLETE, None)

    (cap / "recording.db").write_bytes(b"changed after sealing")
    outcome, error = read_terminal_outcome(str(cap), SESSION, verify, _owner_ok)
    assert outcome == OUTCOME_FAILED
    assert error == "capture_seal_invalid"


class TestWindowsOwnerCheck:
    """The win32 branch, exercised on any host with a stand-in control module."""

    def _path(self, tmp_path):
        path = tmp_path / "capture-state.json"
        path.write_text("{}")
        return path

    def test_missing_acl_check_fails_closed(self, tmp_path):
        with pytest.raises(PermissionError):
            check_owner_only(self._path(tmp_path), platform="win32", control=object())

    def test_acl_check_failure_fails_closed(self, tmp_path):
        class Control:
            @staticmethod
            def _set_and_verify_windows_owner_acl(path, *, _apply=True):
                raise PermissionError("a second ACE grants Everyone read")

        with pytest.raises(PermissionError):
            check_owner_only(self._path(tmp_path), platform="win32", control=Control)

    def test_acl_check_is_verify_only(self, tmp_path):
        calls = []

        class Control:
            @staticmethod
            def _set_and_verify_windows_owner_acl(path, *, _apply=True):
                calls.append(_apply)

        check_owner_only(self._path(tmp_path), platform="win32", control=Control)
        assert calls == [False]  # the tray never changes the file's ACL

    def test_unverifiable_owner_is_unknown_not_saved(self, tmp_path):
        _write_state(tmp_path, phase="complete", complete=True, integrity_verified=True)

        def windows_without_capture(path):
            check_owner_only(path, platform="win32", control=object())

        outcome, error = read_terminal_outcome(
            str(tmp_path), SESSION, _seal_ok, windows_without_capture
        )
        assert outcome == OUTCOME_UNKNOWN
        assert error == "state_file_not_owner_only"
