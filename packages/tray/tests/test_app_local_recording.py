"""Tests for showing and driving local recorders without OpenAdapt Desktop."""

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
        app.local_recorder.start.return_value = tmp_path / "login-demo"
        app.local_recorder.wait.return_value = 0

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

        app.stop_recording()

        app.local_recorder.stop.assert_called_once_with()
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
        app.local_recorder = MagicMock()
        app.local_recorder.wait.return_value = 2
        app.local_recorder.log_path = tmp_path / "x.record.log"
        app.state.transition(TrayState.RECORDING_STARTING, current_capture="x")

        app._reap_local_recording()

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

        app._reap_local_recording()

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
