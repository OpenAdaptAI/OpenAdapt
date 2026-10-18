"""Behavior tests for the public capture CLI recording path."""

from __future__ import annotations

import pytest

from openadapt_capture.cli import record, status, stop
from openadapt_capture.control import CaptureControlUnavailable, RecorderStatus
from openadapt_capture.desktop_capture import DesktopCaptureError
from openadapt_capture.input_observer import (
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
)
from openadapt_capture.video import FFmpegUnavailableError


class _RecorderThatNeverBecomesReady:
    """Small recorder double for startup-failure behavior."""

    def __init__(self, *args, **kwargs):
        self.event_count = 0
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False

    def wait_for_ready(self):
        return False


def test_record_refuses_when_recorder_never_becomes_ready(monkeypatch, tmp_path, capsys):
    import openadapt_capture.recorder as recorder_module

    recorder = None

    def recorder_factory(*args, **kwargs):
        nonlocal recorder
        recorder = _RecorderThatNeverBecomesReady(*args, **kwargs)
        return recorder

    monkeypatch.setattr(recorder_module, "Recorder", recorder_factory)

    with pytest.raises(SystemExit) as raised:
        record(str(tmp_path / "capture"), video=False)

    assert raised.value.code == 1
    assert recorder is not None and recorder.exited
    output = capsys.readouterr().out
    assert "did not become ready" in output
    assert "Recorded 0 events" not in output
    assert "Saved to:" not in output


class _RecorderThatFailsStartup(_RecorderThatNeverBecomesReady):
    """Recorder double whose startup surfaces a worker setup error."""

    error: BaseException

    def wait_for_ready(self):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        FFmpegUnavailableError(
            "Video capture needs an FFmpeg executable, and none is installed."
        ),
        InputObserverPermissionError(
            "macOS denied global input observation. In System Settings > "
            "Privacy & Security > Input Monitoring, allow the app that runs "
            "the recorder."
        ),
        InputObserverUnavailableError(
            "X RECORD requires an X11 desktop and DISPLAY is not set"
        ),
        DesktopCaptureError(
            "MSS did not report a combined desktop and a physical monitor"
        ),
    ],
)
def test_record_prints_the_setup_fix_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys, error
):
    import openadapt_capture.recorder as recorder_module

    recorder = None

    def recorder_factory(*args, **kwargs):
        nonlocal recorder
        recorder = _RecorderThatFailsStartup(*args, **kwargs)
        recorder.error = error
        return recorder

    monkeypatch.setattr(recorder_module, "Recorder", recorder_factory)

    with pytest.raises(SystemExit) as raised:
        record(str(tmp_path / "capture"), video=False)

    assert raised.value.code == 1
    assert raised.value.__cause__ is error
    assert recorder is not None and recorder.exited
    output = capsys.readouterr().out
    assert f"Recording did not start: {error}" in output
    assert "No successful capture was saved." in output
    assert "Saved to:" not in output
    assert ("Unlock the screen" in output) is isinstance(error, DesktopCaptureError)


class _RecorderThatFailsAfterStart(_RecorderThatNeverBecomesReady):
    """Recorder double that starts, then fails while recording."""

    control_session_id = "7fce2c55-5391-47d4-96bf-b3c90feaa69f"

    def wait_for_ready(self):
        return True

    @property
    def is_recording(self):
        raise InputObserverError(
            "the input consumer returned without completing its native receipt "
            "reservation"
        )


def test_record_does_not_call_a_failure_after_start_a_setup_error(
    monkeypatch, tmp_path, capsys
):
    import openadapt_capture.recorder as recorder_module

    monkeypatch.setattr(recorder_module, "Recorder", _RecorderThatFailsAfterStart)

    with pytest.raises(SystemExit) as raised:
        record(str(tmp_path / "capture"), video=False)

    assert raised.value.code == 1
    assert isinstance(raised.value.__cause__, InputObserverError)
    output = capsys.readouterr().out
    assert "Capture session:" in output
    assert "Recording failed: the input consumer returned" in output
    assert "did not start" not in output
    assert "Saved to:" not in output


def _record_log_for(monkeypatch, tmp_path, error):
    """Run recorder.record() until the FFmpeg check raises, and return its log."""
    from loguru import logger

    import openadapt_capture.recorder as recorder_module
    from openadapt_capture.config import config

    def unavailable(**kwargs):
        raise error

    monkeypatch.setattr(config, "RECORD_BROWSER_EVENTS", False)
    monkeypatch.setattr(config, "RECORD_VIDEO", True)
    monkeypatch.setattr(recorder_module.video, "require_video_encoder", unavailable)
    messages: list[str] = []
    sink = logger.add(messages.append, level="ERROR", format="{message}")
    try:
        with pytest.raises(type(error)):
            recorder_module.record("setup check", capture_dir=str(tmp_path))
    finally:
        logger.remove(sink)
    return "".join(messages)


def test_recorder_logs_a_setup_error_without_a_traceback(monkeypatch, tmp_path):
    log = _record_log_for(
        monkeypatch,
        tmp_path,
        FFmpegUnavailableError("Video capture needs an FFmpeg executable."),
    )

    assert "Error in record: Video capture needs an FFmpeg executable." in log
    assert "Traceback" not in log


def test_recorder_still_logs_a_traceback_for_an_unexpected_error(monkeypatch, tmp_path):
    log = _record_log_for(monkeypatch, tmp_path, RuntimeError("encoder probe crashed"))

    assert "Error in record: encoder probe crashed" in log
    assert "Traceback" in log


def test_record_names_the_configured_stop_sequences(monkeypatch, tmp_path, capsys):
    import openadapt_capture.recorder as recorder_module
    from openadapt_capture.config import settings

    monkeypatch.setattr(recorder_module, "Recorder", _RecorderThatNeverBecomesReady)
    monkeypatch.setattr(
        settings,
        "STOP_SEQUENCES",
        [list("oa.stop"), ["ctrl", "ctrl", "ctrl"], ["ctrl", "alt"], []],
    )

    with pytest.raises(SystemExit):
        record(str(tmp_path / "capture"), video=False)

    output = capsys.readouterr().out
    assert (
        "To stop, press Ctrl+C here. In any app, you can also type oa.stop, "
        "press Ctrl 3 times, or press ctrl alt."
    ) in output

    monkeypatch.setattr(settings, "STOP_SEQUENCES", [list("oa.stop")])
    with pytest.raises(SystemExit):
        record(str(tmp_path / "capture"), video=False)
    assert (
        "To stop, press Ctrl+C here. In any app, you can also type oa.stop.\n"
    ) in capsys.readouterr().out

    monkeypatch.setattr(settings, "STOP_SEQUENCES", [])
    with pytest.raises(SystemExit):
        record(str(tmp_path / "capture"), video=False)
    assert "To stop, press Ctrl+C here.\n" in capsys.readouterr().out


def _status_result() -> RecorderStatus:
    return RecorderStatus(
        session_id="7fce2c55-5391-47d4-96bf-b3c90feaa69f",
        pid=42,
        process_started_at=100.0,
        capture_dir="/capture",
        phase="complete",
        ready=True,
        complete=True,
        integrity_verified=True,
        event_counts={"action": 1},
    )


def test_status_cli_calls_the_public_control_api(monkeypatch, capsys):
    import openadapt_capture.control as control_module

    monkeypatch.setattr(control_module, "status_recording", lambda *args, **kwargs: _status_result())
    status("7fce2c55-5391-47d4-96bf-b3c90feaa69f")
    output = capsys.readouterr().out
    assert '"phase": "complete"' in output
    assert '"integrity_verified": true' in output


def test_stop_cli_returns_non_success_when_no_recorder_exists(monkeypatch, capsys):
    import openadapt_capture.control as control_module

    def unavailable(*args, **kwargs):
        raise CaptureControlUnavailable("No live Capture recorder was found.")

    monkeypatch.setattr(control_module, "stop_recording", unavailable)
    with pytest.raises(SystemExit) as raised:
        stop()
    assert raised.value.code == 1
    assert "No live Capture recorder" in capsys.readouterr().out
