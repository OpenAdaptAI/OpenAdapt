"""Tests for the tray-owned ``openadapt-flow record`` launcher."""

import os
import signal
import subprocess
from unittest.mock import MagicMock

import pytest

from openadapt_tray.local_record import (
    LocalRecorder,
    LocalRecordError,
    interrupt_signal,
    spawn_options,
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
        run = recorder.start("login-demo", tmp_path)
        assert run.output_dir == tmp_path / "login-demo"
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
        assert run.log_path == tmp_path / "login-demo.record.log"
        if os.name == "posix":  # Windows: the folder's ACL applies instead
            assert run.log_path.stat().st_mode & 0o777 == 0o600
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
        # Quitting afterwards must not send a second Ctrl-C mid-finalization.
        assert recorder.interrupt_if_running() is False
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


def test_interrupt_if_running_stops_a_starting_recording(tmp_path):
    recorder, _, process = _recorder()
    assert recorder.interrupt_if_running() is False
    recorder.start("a", tmp_path)
    assert recorder.interrupt_if_running() is True
    process.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("/etc/x", "etc-x"), ("../../up", "up"), ("a/b", "a-b"), ("ok name", "ok name")],
)
def test_names_cannot_leave_the_captures_folder(tmp_path, name, expected):
    recorder, _, _ = _recorder()
    run = recorder.start(name, tmp_path)
    assert run.output_dir == tmp_path / expected
    assert run.output_dir.parent == tmp_path


@pytest.mark.parametrize("name", ["..", "/", "  . "])
def test_unusable_names_are_refused(tmp_path, name):
    recorder, popen, _ = _recorder()
    with pytest.raises(LocalRecordError):
        recorder.start(name, tmp_path)
    popen.assert_not_called()


class TestStopWithin:
    def _run(self, tmp_path, exits_on):
        from tests.test_app_local_recording import FakeProcess

        process = FakeProcess(exits_on)
        recorder = LocalRecorder(
            platform="darwin",
            which=lambda _: "/bin/openadapt-flow",
            popen=MagicMock(return_value=process),
        )
        return recorder.start("a", tmp_path), process

    def _stop(self, run):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        return run.stop_within(30.0, 10.0, clock=lambda: now[0], sleep=sleep), now[0]

    def test_ctrl_c_obeyed(self, tmp_path):
        run, process = self._run(tmp_path, {signal.SIGINT})
        assert self._stop(run) == ("exited", 0.0)
        assert process.signals == [signal.SIGINT]

    def test_ctrl_c_ignored_escalates_after_the_deadline(self, tmp_path):
        run, process = self._run(tmp_path, {"terminate"})
        outcome, elapsed = self._stop(run)
        assert outcome == "terminated"
        assert 30.0 <= elapsed < 31.0
        assert process.signals == [signal.SIGINT, "terminate"]

    def test_nothing_obeyed_is_reported(self, tmp_path):
        run, process = self._run(tmp_path, set())
        outcome, _ = self._stop(run)
        assert outcome == "still_running"
        assert process.signals == [signal.SIGINT, "terminate", "kill"]

    def test_not_running_does_nothing(self, tmp_path):
        run, process = self._run(tmp_path, {signal.SIGINT})
        process.returncode = 0
        assert self._stop(run)[0] == "not_running"
        assert process.signals == []


class TestPortableProcessControl:
    def test_posix_detaches_with_a_new_session(self):
        assert spawn_options("darwin") == {"start_new_session": True}
        assert interrupt_signal("darwin") == signal.SIGINT

    def test_windows_uses_a_new_process_group_and_ctrl_break(self):
        options = spawn_options("win32")
        assert options == {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        }
        assert "start_new_session" not in options
        expected = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
        assert interrupt_signal("win32") == expected

    def test_escalation_uses_popen_methods_not_posix_signals(self, tmp_path):
        process = MagicMock()
        process.poll.return_value = None
        recorder = LocalRecorder(
            platform="darwin",
            which=lambda _: "/bin/openadapt-flow",
            popen=MagicMock(return_value=process),
        )
        run = recorder.start("a", tmp_path)
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        assert run.stop_within(1.0, 1.0, clock=lambda: now[0], sleep=sleep) == (
            "still_running"
        )
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
