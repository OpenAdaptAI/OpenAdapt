"""Tests for showing and driving local recorders without OpenAdapt Desktop."""

import signal
from unittest.mock import MagicMock, patch

from openadapt_tray import capture_watch
from openadapt_tray.capture_watch import CaptureEvent
from openadapt_tray.config import TrayConfig
from openadapt_tray.state import TrayState

SESSION = "11111111-1111-4111-8111-111111111111"


def _make_app():
    from openadapt_tray.app import TrayApplication

    with (
        patch("openadapt_tray.app.get_platform_handler") as platform,
        patch("openadapt_tray.app.pystray") as pystray,
    ):
        platform.return_value = MagicMock()
        pystray.Icon.return_value = MagicMock()
        app = TrayApplication(config=TrayConfig())
    app.ipc = MagicMock()
    app.ipc.is_connected.return_value = False
    app.notifications = MagicMock()
    app.notifications.show.return_value = True
    return app


def _event(kind, outcome=None, error=None, session=SESSION):
    return CaptureEvent(
        kind=kind,
        session_id=session,
        name="login-demo",
        capture_dir="/c/login-demo/.capture",
        outcome=outcome,
        error_code=error,
    )


def _titles(app):
    return [call.args[0] for call in app.notifications.show.call_args_list]


class TestWatchedLifecycle:
    def test_ready_shows_recording_and_notifies(self):
        app = _make_app()
        app._on_capture_event(_event(capture_watch.STARTING))
        assert app.state.current.state == TrayState.RECORDING_STARTING
        assert _titles(app) == []

        app._on_capture_event(_event(capture_watch.READY))
        assert app.state.current.state == TrayState.RECORDING
        assert app.state.current.current_capture == "login-demo"
        assert app.icon.title == "OpenAdapt: recording"
        assert _titles(app) == ["Recording Started"]

    def test_saved_recording_notifies_stopped(self):
        app = _make_app()
        app._on_capture_event(_event(capture_watch.READY))
        app._on_capture_event(_event(capture_watch.STOPPING))
        app._on_capture_event(
            _event(capture_watch.ENDED, outcome=capture_watch.OUTCOME_COMPLETE)
        )
        assert app.state.current.state == TrayState.IDLE
        assert _titles(app) == ["Recording Started", "Recording Stopped"]
        body = app.notifications.show.call_args_list[-1].args[1]
        assert body == "Saved: login-demo"
        assert app._watched_session is None

    def test_failed_recording_stays_visible(self):
        app = _make_app()
        app._on_capture_event(_event(capture_watch.READY))
        app._on_capture_event(
            _event(
                capture_watch.ENDED,
                outcome=capture_watch.OUTCOME_FAILED,
                error="recorder_process_exited",
            )
        )
        assert app.state.current.state == TrayState.ERROR
        assert "recorder_process_exited" in app.state.current.error_message
        assert _titles(app)[-1] == "Error"

    def test_unknown_outcome_is_not_reported_as_saved(self):
        app = _make_app()
        app._on_capture_event(_event(capture_watch.READY))
        app._on_capture_event(
            _event(
                capture_watch.ENDED,
                outcome=capture_watch.OUTCOME_UNKNOWN,
                error="state_file_missing",
            )
        )
        assert app.state.current.state == TrayState.ERROR

    def test_ignored_while_desktop_is_connected(self):
        app = _make_app()
        app.ipc.is_connected.return_value = True
        app._on_capture_event(_event(capture_watch.READY))
        assert app.state.current.state == TrayState.IDLE

    def test_second_recorder_does_not_take_over(self):
        app = _make_app()
        app._on_capture_event(_event(capture_watch.READY))
        other = "22222222-2222-4222-8222-222222222222"
        app._on_capture_event(
            _event(
                capture_watch.ENDED,
                outcome=capture_watch.OUTCOME_FAILED,
                session=other,
            )
        )
        assert app.state.current.state == TrayState.RECORDING


class TestStartStopWithoutDesktop:
    def test_start_falls_back_to_openadapt_flow(self, tmp_path):
        app = _make_app()
        app.config.captures_directory = str(tmp_path)
        app.capture_watch = MagicMock()
        app.capture_watch.available.return_value = True
        app.local_recorder = MagicMock()
        app.local_recorder.available.return_value = True
        run = MagicMock(output_dir=tmp_path / "login-demo")
        run.wait.return_value = 0
        app.local_recorder.start.return_value = run

        with patch.object(app, "ensure_desktop_connection", return_value=False) as e:
            app._dispatch_start_recording("login-demo")

        e.assert_called_once_with(report_missing=False)
        app.local_recorder.start.assert_called_once_with("login-demo", tmp_path)
        assert "Desktop app not found" not in _titles(app)

    def test_without_flow_start_still_reports_the_missing_desktop(self):
        app = _make_app()
        app.local_recorder = MagicMock()
        app.local_recorder.available.return_value = False

        with patch.object(app, "ensure_desktop_connection", return_value=False) as e:
            app._dispatch_start_recording("x")

        e.assert_called_once_with(report_missing=True)
        assert app.state.current.state == TrayState.ERROR

    def test_stop_sends_ctrl_c_to_the_tray_owned_recorder(self):
        app = _make_app()
        app.local_recorder = MagicMock()
        app.local_recorder.is_running.return_value = True
        app._on_capture_event(_event(capture_watch.READY))

        with patch("openadapt_tray.app.threading.Thread") as thread:
            app.stop_recording()

        assert thread.call_args.kwargs["target"] == app._stop_local_run
        assert thread.call_args.kwargs["args"] == (app.local_recorder.current,)
        app.ipc.send_stop_recording.assert_not_called()
        assert app.state.current.state == TrayState.RECORDING_STOPPING

    def test_stop_uses_the_control_channel_for_another_recorder(self):
        app = _make_app()
        app.capture_watch = MagicMock()
        app.local_recorder = MagicMock()
        app.local_recorder.is_running.return_value = False
        app._on_capture_event(_event(capture_watch.READY))

        with patch("openadapt_tray.app.threading.Thread") as thread:
            app.stop_recording()
        target = thread.call_args.kwargs["target"]
        target(*thread.call_args.kwargs["args"])

        app.capture_watch.stop_session.assert_called_once_with(SESSION)
        app.ipc.send_stop_recording.assert_not_called()

    def test_failed_flow_exit_is_reported(self, tmp_path):
        app = _make_app()
        app.capture_watch = MagicMock()
        app.local_recorder = MagicMock()
        run = MagicMock(log_path=tmp_path / "x.record.log", capture_saved=False)
        run.wait.return_value = 2
        app.local_recorder.current = run
        app.state.transition(TrayState.RECORDING_STARTING, current_capture="x")

        app._reap_local_recording(run)

        assert app.state.current.state == TrayState.ERROR
        assert "exited with code 2" in app.state.current.error_message


class TestTrayOwnedRecording:
    def _app_with_child(self, tmp_path):
        from openadapt_tray.local_record import LocalRecorder

        app = _make_app()
        process = MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 1
        app.local_recorder = LocalRecorder(
            platform="darwin",
            which=lambda _: "/bin/openadapt-flow",
            popen=MagicMock(return_value=process),
        )
        app.local_recorder.start("login-demo", tmp_path)
        return app, process

    def _own(self, tmp_path, kind, **kwargs):
        return CaptureEvent(
            kind=kind,
            session_id=SESSION,
            name="login-demo",
            capture_dir=str(tmp_path / "login-demo" / ".capture"),
            **kwargs,
        )

    def test_other_recorders_are_ignored_while_the_tray_records(self, tmp_path):
        app, _ = self._app_with_child(tmp_path)
        other = CaptureEvent(
            kind=capture_watch.STOPPING,
            session_id="22222222-2222-4222-8222-222222222222",
            name="failed-finalization",
            capture_dir="/elsewhere/failed-finalization",
        )
        app._on_capture_event(other)
        assert app.state.current.state == TrayState.IDLE

        app._on_capture_event(self._own(tmp_path, capture_watch.READY))
        assert app.state.current.state == TrayState.RECORDING

    def test_saved_capture_then_failed_conversion_says_both(self, tmp_path):
        app, process = self._app_with_child(tmp_path)
        app._on_capture_event(self._own(tmp_path, capture_watch.READY))
        process.poll.return_value = 1  # the child has exited
        ended = self._own(
            tmp_path,
            capture_watch.ENDED,
            outcome=capture_watch.OUTCOME_COMPLETE,
        )
        app.capture_watch = MagicMock()
        app.capture_watch.poll_and_dispatch.side_effect = lambda: app._on_capture_event(
            ended
        )

        app._reap_local_recording(app.local_recorder.current)

        assert app.state.current.state == TrayState.ERROR
        assert app.state.current.error_message.startswith(
            "The capture was saved, but openadapt-flow could not convert it"
        )
        assert _titles(app) == ["Recording Started", "Recording Stopped", "Error"]


def test_idle_after_a_failed_start_is_not_announced_as_saved():
    app = _make_app()
    app.state.transition(TrayState.RECORDING_STARTING, current_capture="x")
    app.state.transition(TrayState.IDLE)
    assert "Recording Stopped" not in _titles(app)


class TestReviewRegressions:
    """Cases from the independent review of #1481."""

    _helper = TestTrayOwnedRecording()

    def test_stale_watched_session_does_not_hide_a_new_recording(self, tmp_path):
        app = _make_app()
        app.capture_watch = MagicMock()
        app.capture_watch.stop_session.side_effect = TimeoutError("finalizing")
        app._on_capture_event(_event(capture_watch.READY))
        app._stop_watched_session(SESSION)
        assert app.state.current.state == TrayState.ERROR

        process = MagicMock()
        process.poll.return_value = None
        from openadapt_tray.local_record import LocalRecorder

        app.local_recorder = LocalRecorder(
            platform="darwin",
            which=lambda _: "/bin/openadapt-flow",
            popen=MagicMock(return_value=process),
        )
        with patch("openadapt_tray.app.threading.Thread"):
            app.config.captures_directory = str(tmp_path)
            app._start_local_recording("login-demo")
        new = CaptureEvent(
            kind=capture_watch.READY,
            session_id="33333333-3333-4333-8333-333333333333",
            name="login-demo",
            capture_dir=str(tmp_path / "login-demo" / ".capture"),
        )
        app._on_capture_event(new)
        assert app.state.current.state == TrayState.RECORDING
        assert app.state.current.can_stop_recording()

    def test_quit_while_starting_stops_the_child(self, tmp_path):
        app, process = _app_with_fake_child(tmp_path, FakeProcess({signal.SIGINT}))
        app.state.transition(TrayState.RECORDING_STARTING, current_capture="x")
        app.quit()
        assert process.signals == [signal.SIGINT]
        assert process.poll() is not None  # quit waited for it to exit

    def test_own_recording_still_shows_after_desktop_connects(self, tmp_path):
        app, _ = self._helper._app_with_child(tmp_path)
        app.ipc.is_connected.return_value = True
        app._on_capture_event(self._helper._own(tmp_path, capture_watch.READY))
        assert app.state.current.state == TrayState.RECORDING
        app._on_capture_event(self._helper._own(tmp_path, capture_watch.STOPPING))
        app._on_capture_event(
            self._helper._own(
                tmp_path, capture_watch.ENDED, outcome=capture_watch.OUTCOME_COMPLETE
            )
        )
        assert app.state.current.state == TrayState.IDLE

    def test_clean_exit_clears_a_stuck_stopping_state(self, tmp_path):
        app, process = self._helper._app_with_child(tmp_path)
        app.capture_watch = MagicMock()
        app.state.transition(TrayState.RECORDING_STOPPING, current_capture="x")
        process.wait.return_value = 0
        app._reap_local_recording(app.local_recorder.current)
        assert app.state.current.state == TrayState.IDLE

    def test_stale_reaper_does_not_touch_a_newer_recording(self, tmp_path):
        app, process = self._helper._app_with_child(tmp_path)
        old = app.local_recorder.current
        process.poll.return_value = 1
        app.local_recorder.start("second", tmp_path)
        app.state.transition(TrayState.RECORDING_STARTING, current_capture="second")
        app.capture_watch = MagicMock()

        app._reap_local_recording(old)

        assert app.state.current.state == TrayState.RECORDING_STARTING
        assert app.local_recorder.current.log is not None  # new log still open


class FakeProcess:
    """A child that exits on the given signals and ignores the rest."""

    def __init__(self, exits_on):
        self.exits_on = set(exits_on)
        self.signals = []
        self.returncode = None

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig in self.exits_on:
            self.returncode = 1

    def terminate(self):
        self.send_signal("terminate")

    def kill(self):
        self.send_signal("kill")

    def wait(self):
        return self.returncode


def _app_with_fake_child(tmp_path, process):
    from openadapt_tray.local_record import LocalRecorder

    app = _make_app()
    app.capture_watch = MagicMock()
    app.LOCAL_STOP_TIMEOUT_S = 0.2
    app.LOCAL_QUIT_TIMEOUT_S = 0.2
    app.LOCAL_TERMINATE_TIMEOUT_S = 0.2
    app.local_recorder = LocalRecorder(
        platform="darwin",
        which=lambda _: "/bin/openadapt-flow",
        popen=MagicMock(return_value=process),
    )
    app.local_recorder.start("login-demo", tmp_path)
    return app, process


class TestBoundedStop:
    """Coordinator review: stop and quit must not leave a recorder running."""

    def test_quit_while_recording_waits_for_the_recorder(self, tmp_path):
        app, process = _app_with_fake_child(tmp_path, FakeProcess({signal.SIGINT}))
        app._on_capture_event(TestTrayOwnedRecording()._own(tmp_path, "ready"))
        assert app.state.current.state == TrayState.RECORDING
        app.quit()
        assert process.signals == [signal.SIGINT]
        assert process.poll() is not None

    def test_recorder_ignoring_ctrl_c_is_terminated_and_reported(self, tmp_path):
        app, process = _app_with_fake_child(tmp_path, FakeProcess({"terminate"}))
        app._on_capture_event(TestTrayOwnedRecording()._own(tmp_path, "ready"))

        outcome = app._stop_local_run(app.local_recorder.current)

        assert outcome == "terminated"
        assert process.signals == [signal.SIGINT, "terminate"]
        assert app.state.current.state == TrayState.ERROR
        assert "ignored Ctrl-C" in app.state.current.error_message
        assert _titles(app)[-1] == "Error"

    def test_recorder_ignoring_everything_but_sigkill_is_killed(self, tmp_path):
        app, process = _app_with_fake_child(tmp_path, FakeProcess({"kill"}))
        app.quit()
        assert process.signals == [signal.SIGINT, "terminate", "kill"]
        assert "killed" in app.state.current.error_message

    def test_reported_stop_is_enough_while_flow_converts(self, tmp_path):
        """Once the recorder says it stopped, conversion may take its time."""
        app, process = _app_with_fake_child(tmp_path, FakeProcess(set()))
        own = TestTrayOwnedRecording()._own
        app._on_capture_event(own(tmp_path, "ready"))
        app._on_capture_event(own(tmp_path, "stopping"))

        outcome = app._stop_local_run(app.local_recorder.current)

        assert outcome == "recorder_stopped"
        assert process.signals == [signal.SIGINT]  # no escalation
        assert app.state.current.state == TrayState.RECORDING_STOPPING
