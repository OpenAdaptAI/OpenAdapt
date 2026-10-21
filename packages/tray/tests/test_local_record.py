"""Tests for the tray-owned ``openadapt-flow record`` launcher."""

import signal
from unittest.mock import MagicMock

import pytest

from openadapt_tray.local_record import (
    LocalRecorder,
    LocalRecordError,
    unique_output_dir,
)


def _recorder(platform="darwin", which="/usr/local/bin/openadapt-flow"):
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)
    recorder = LocalRecorder(platform=platform, which=lambda _: which, popen=popen)
    return recorder, popen, process


class TestAvailability:
    def test_macos_with_flow_on_path(self):
        recorder, _, _ = _recorder()
        assert recorder.available()

    def test_not_on_windows_or_linux(self):
        for platform in ("win32", "linux"):
            recorder, _, _ = _recorder(platform=platform)
            assert not recorder.available()

    def test_not_without_flow(self):
        recorder, _, _ = _recorder(which=None)
        assert not recorder.available()
        with pytest.raises(LocalRecordError):
            recorder.start("x", MagicMock())


class TestStartStop:
    def test_runs_the_documented_command(self, tmp_path):
        recorder, popen, _ = _recorder()
        out = recorder.start("login-demo", tmp_path)
        assert out == tmp_path / "login-demo"
        command = popen.call_args.args[0]
        assert command == [
            "/usr/local/bin/openadapt-flow",
            "record",
            "--backend",
            "macos",
            "--out",
            str(tmp_path / "login-demo"),
            "--task",
            "login-demo",
        ]
        assert popen.call_args.kwargs["start_new_session"] is True
        assert recorder.log_path == tmp_path / "login-demo.record.log"
        assert recorder.is_running()

    def test_refuses_a_second_recording(self, tmp_path):
        recorder, _, _ = _recorder()
        recorder.start("a", tmp_path)
        with pytest.raises(LocalRecordError):
            recorder.start("b", tmp_path)

    def test_stop_sends_ctrl_c(self, tmp_path):
        recorder, _, process = _recorder()
        recorder.start("a", tmp_path)
        recorder.stop()
        process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_stop_without_a_recording_raises(self):
        recorder, _, _ = _recorder()
        with pytest.raises(LocalRecordError):
            recorder.stop()

    def test_popen_failure_is_reported(self, tmp_path):
        recorder, popen, _ = _recorder()
        popen.side_effect = OSError("exec format error")
        with pytest.raises(LocalRecordError):
            recorder.start("a", tmp_path)
        assert not recorder.is_running()


def test_unique_output_dir_never_reuses_a_folder(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo-2").mkdir()
    assert unique_output_dir(tmp_path, "demo") == tmp_path / "demo-3"


def test_owns_only_its_own_output_dir(tmp_path):
    recorder, _, _ = _recorder()
    assert not recorder.owns(str(tmp_path / "demo" / ".capture"))
    recorder.start("demo", tmp_path)
    assert recorder.owns(str(tmp_path / "demo" / ".capture"))
    assert not recorder.owns(str(tmp_path / "other" / ".capture"))
