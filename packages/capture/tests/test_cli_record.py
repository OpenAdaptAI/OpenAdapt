"""Behavior tests for the public capture CLI recording path."""

from __future__ import annotations

import pytest

from openadapt_capture.cli import record


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
