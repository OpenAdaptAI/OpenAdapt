"""Tests for the main TrayApplication."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray.config import ConfigLoadError, TrayConfig
from openadapt_tray.platform.base import DialogUnavailableError
from openadapt_tray.state import SyncState, TrayState


def _make_test_app():
    """Build an app without platform or tray side effects."""
    from openadapt_tray.app import TrayApplication

    with (
        patch("openadapt_tray.app.get_platform_handler") as platform,
        patch("openadapt_tray.app.pystray") as pystray,
    ):
        platform.return_value = MagicMock()
        pystray.Icon.return_value = MagicMock()
        return TrayApplication(config=TrayConfig())


class TestTrayApplicationInit:
    """Tests for TrayApplication initialization."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_initialization_with_defaults(self, mock_platform, mock_pystray):
        """Test that TrayApplication initializes with default config."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_icon = MagicMock()
        mock_pystray.Icon.return_value = mock_icon

        app = TrayApplication()

        assert app.config is not None
        assert app.state is not None
        assert app.icon == mock_icon

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_initialization_with_custom_config(self, mock_platform, mock_pystray):
        """Test initialization with custom config."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        config = TrayConfig(dashboard_port=9000)
        app = TrayApplication(config=config)

        assert app.config.dashboard_port == 9000


class TestRecordingControls:
    """Tests for recording start/stop functionality."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_start_recording_changes_state(self, mock_platform, mock_pystray):
        """Test that start_recording transitions to RECORDING_STARTING."""
        from openadapt_tray.app import TrayApplication

        mock_platform_instance = MagicMock()
        mock_platform_instance.prompt_input.return_value = "test_capture"
        mock_platform.return_value = mock_platform_instance
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()

        # Start recording with a name (skip prompt); the desktop dispatch runs
        # on a background thread, so stub it out.
        with patch.object(app, "_dispatch_start_recording"):
            app.start_recording("test_capture")

        assert app.state.current.state in (
            TrayState.RECORDING_STARTING,
            TrayState.RECORDING,
        )

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_cannot_start_recording_when_recording(self, mock_platform, mock_pystray):
        """Test that recording cannot start when already recording."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        app.state.transition(TrayState.RECORDING, current_capture="existing")

        with patch.object(app, "_dispatch_start_recording") as mock_dispatch:
            app.start_recording("new_capture")
            mock_dispatch.assert_not_called()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_stop_recording_signals_desktop(self, mock_platform, mock_pystray):
        """stop_recording transitions to STOPPING and signals the desktop."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        app.state.transition(TrayState.RECORDING, current_capture="test")

        with patch.object(app.ipc, "send_stop_recording") as mock_stop:
            mock_stop.return_value = True
            app.stop_recording()
            mock_stop.assert_called_once()

        # The tray waits for the desktop's RECORDING_STOPPED event.
        assert app.state.current.state == TrayState.RECORDING_STOPPING

    def test_failed_stop_command_enters_error(self):
        """A command that was not sent must not render as stopping."""
        app = _make_test_app()
        app.state.transition(TrayState.RECORDING, current_capture="test")

        with patch.object(app.ipc, "send_stop_recording", return_value=False):
            app.stop_recording()

        assert app.state.current.state == TrayState.ERROR
        assert "stop-recording" in (app.state.current.error_message or "")

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_cannot_stop_recording_when_idle(self, mock_platform, mock_pystray):
        """Test that stop_recording does nothing when idle."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        assert app.state.current.state == TrayState.IDLE

        app.stop_recording()

        assert app.state.current.state == TrayState.IDLE


class TestToggleRecording:
    """Tests for toggle recording functionality."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_toggle_starts_when_idle(self, mock_platform, mock_pystray):
        """Test that toggle starts recording when idle."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()

        with patch.object(app, "start_recording") as mock_start:
            app._toggle_recording()
            mock_start.assert_called_once()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_toggle_stops_when_recording(self, mock_platform, mock_pystray):
        """Test that toggle stops recording when recording."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        app.state.transition(TrayState.RECORDING, current_capture="test")

        with patch.object(app, "stop_recording") as mock_stop:
            app._toggle_recording()
            mock_stop.assert_called_once()


class TestStateNotifications:
    """Tests for state change notifications."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_notification_on_recording_start(self, mock_platform, mock_pystray):
        """Test that notification is shown when recording starts."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()

        with patch.object(app.notifications, "show") as mock_show:
            app.state.transition(TrayState.RECORDING, current_capture="test")

            mock_show.assert_called()
            call_args = mock_show.call_args
            assert "Recording Started" in call_args[0][0]

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_no_notification_when_disabled(self, mock_platform, mock_pystray):
        """Test that no notification is shown when disabled."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        config = TrayConfig(show_notifications=False)
        app = TrayApplication(config=config)

        with patch.object(app.notifications, "show") as mock_show:
            app.state.transition(TrayState.RECORDING, current_capture="test")
            mock_show.assert_not_called()

    def test_sync_change_does_not_claim_recording_stopped(self):
        """An orthogonal sync update is not a recording completion."""
        app = _make_test_app()

        with patch.object(app.notifications, "show") as mock_show:
            app.state.set_sync_state(SyncState.SYNCING)

        mock_show.assert_not_called()

    def test_failed_notification_is_retried(self):
        app = _make_test_app()
        with patch.object(app.notifications, "show", return_value=False) as show:
            app.state.transition(TrayState.RECORDING, current_capture="test")
            app._show_state_notification(app.state.current)

        assert show.call_count == 2
        assert app._last_notified_tray_state == TrayState.IDLE

    def test_delivered_notification_is_not_repeated(self):
        app = _make_test_app()
        with patch.object(app.notifications, "show", return_value=True) as show:
            app.state.transition(TrayState.RECORDING, current_capture="test")
            app._show_state_notification(app.state.current)

        show.assert_called_once()
        assert app._last_notified_tray_state == TrayState.RECORDING

    def test_unmessaged_transition_resets_notification_deduplication(self):
        app = _make_test_app()
        with patch.object(app.notifications, "show", return_value=False) as show:
            app.state.transition(TrayState.RECORDING, current_capture="test")
            app.state.transition(TrayState.RECORDING_STOPPING)
            app.state.transition(TrayState.IDLE)

        assert show.call_count == 2
        assert show.call_args.args[0] == "Recording Stopped"


class TestQuit:
    """Tests for application quit functionality."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_quit_stops_hotkeys(self, mock_platform, mock_pystray):
        """Test that quit stops hotkey listener."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()

        with patch.object(app.hotkeys, "stop") as mock_stop:
            app.quit()
            mock_stop.assert_called_once()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_quit_closes_ipc(self, mock_platform, mock_pystray):
        """Test that quit closes IPC connection."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()

        with patch.object(app.ipc, "close") as mock_close:
            app.quit()
            mock_close.assert_called_once()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_quit_stops_recording_if_active(self, mock_platform, mock_pystray):
        """Test that quit stops active recording."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        app.state.transition(TrayState.RECORDING, current_capture="test")

        with patch.object(app, "stop_recording") as mock_stop:
            app.quit()
            mock_stop.assert_called_once()


class TestUnreadableConfigIsReported:
    """Running on settings the user never chose must not look like a clean start."""

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_run_tells_the_user_the_settings_could_not_be_read(
        self, mock_platform, mock_pystray
    ):
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        error = ConfigLoadError(Path("/tmp/tray.json"), ValueError("bad json"))
        with patch.object(
            TrayConfig, "load_or_defaults", return_value=(TrayConfig(), error)
        ):
            app = TrayApplication()
        app.notifications = MagicMock()
        app.hotkeys = MagicMock()
        app.ipc = MagicMock()
        app.hosted = MagicMock()

        app.run()

        app.notifications.show.assert_called_once()
        title = app.notifications.show.call_args[0][0]
        assert "settings" in title.lower()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_a_clean_load_says_nothing(self, mock_platform, mock_pystray):
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        with patch.object(
            TrayConfig, "load_or_defaults", return_value=(TrayConfig(), None)
        ):
            app = TrayApplication()
        app.notifications = MagicMock()
        app.hotkeys = MagicMock()
        app.ipc = MagicMock()
        app.hosted = MagicMock()

        app.run()

        app.notifications.show.assert_not_called()


class TestStartRecordingWhenNoDialogCanBeShown:
    """ "The prompt never appeared" used to be indistinguishable from "cancelled"."""

    def _app(self, mock_platform, mock_pystray):
        from openadapt_tray.app import TrayApplication

        mock_pystray.Icon.return_value = MagicMock()
        app = TrayApplication(config=TrayConfig(use_native_dialogs=True))
        app.notifications = MagicMock()
        return app

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_unavailable_dialog_still_starts_the_recording(
        self, mock_platform, mock_pystray
    ):
        platform = MagicMock()
        platform.prompt_input.side_effect = DialogUnavailableError("no display")
        mock_platform.return_value = platform

        app = self._app(mock_platform, mock_pystray)
        with patch.object(app, "_dispatch_start_recording"):
            app.start_recording()

        assert app.state.current.state == TrayState.RECORDING_STARTING
        # And the user is told why no naming prompt appeared.
        app.notifications.show.assert_called_once()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_a_real_cancel_still_cancels(self, mock_platform, mock_pystray):
        platform = MagicMock()
        platform.prompt_input.return_value = None  # the user pressed Cancel
        mock_platform.return_value = platform

        app = self._app(mock_platform, mock_pystray)
        with patch.object(app, "_dispatch_start_recording"):
            app.start_recording()

        assert app.state.current.state == TrayState.IDLE
        app.notifications.show.assert_not_called()


class TestNeedsAttentionClickReportsDeadEnds:
    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_click_that_opened_nothing_tells_the_user(
        self, mock_platform, mock_pystray
    ):
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()
        app = TrayApplication(config=TrayConfig())
        app.notifications = MagicMock()

        with patch("openadapt_tray.app.route_break_click", return_value=False):
            assert app.open_needs_attention() is False
        app.notifications.show.assert_called_once()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_click_that_opened_something_stays_quiet(self, mock_platform, mock_pystray):
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()
        app = TrayApplication(config=TrayConfig())
        app.notifications = MagicMock()

        with patch("openadapt_tray.app.route_break_click", return_value=True):
            assert app.open_needs_attention() is True
        app.notifications.show.assert_not_called()


class TestOpenActionsReportDeadEnds:
    def test_connected_desktop_command_failure_is_reported(self):
        app = _make_test_app()
        app.notifications = MagicMock()
        app.ipc = MagicMock()
        app.ipc.is_connected.return_value = True
        app.ipc.send_open_workflow_library.return_value = False

        app.open_desktop_app()

        app.notifications.show.assert_called_once()

    def test_desktop_launch_uses_native_application_helper(self):
        app = _make_test_app()

        with patch("openadapt_tray.app.launch_native_desktop") as launch:
            assert app._launch_desktop_app() is True

        launch.assert_called_once_with()

    def test_missing_native_desktop_is_reported(self):
        from openadapt_tray.desktop import DesktopLaunchError

        app = _make_test_app()
        app.notifications = MagicMock()

        with patch(
            "openadapt_tray.app.launch_native_desktop",
            side_effect=DesktopLaunchError("not installed"),
        ):
            assert app._launch_desktop_app() is False

        app.notifications.show.assert_called_once()

    def test_browser_failure_is_not_reported_as_opened(self):
        app = _make_test_app()
        app.notifications = MagicMock()

        with patch("openadapt_tray.app.webbrowser.open", return_value=False):
            assert app.open_cloud_dashboard() is False

        app.notifications.show.assert_called_once()

    def test_browser_exception_is_reported(self):
        app = _make_test_app()
        app.notifications = MagicMock()

        with patch(
            "openadapt_tray.app.webbrowser.open", side_effect=OSError("no browser")
        ):
            assert app.open_cloud_dashboard() is False

        app.notifications.show.assert_called_once()


class TestFailedIPCResultsStayFailed:
    """A False command result means that the tray sent no command."""

    def test_failed_pause_does_not_report_synced(self):
        app = _make_test_app()
        app.notifications = MagicMock()
        app.state.set_sync_state(SyncState.SYNCING)
        app.ipc = MagicMock()
        app.ipc.is_connected.return_value = True
        app.ipc.send_pause_sync.return_value = False

        assert app.pause_sync() is False
        assert app.state.current.sync_state == SyncState.SYNCING
        app.notifications.show.assert_called_once()

    def test_successful_pause_updates_sync_state(self):
        app = _make_test_app()
        app.state.set_sync_state(SyncState.SYNCING)
        app.ipc = MagicMock()
        app.ipc.is_connected.return_value = True
        app.ipc.send_pause_sync.return_value = True

        assert app.pause_sync() is True
        assert app.state.current.sync_state == SyncState.SYNCED

    def test_failed_resume_is_reported(self):
        app = _make_test_app()
        app.notifications = MagicMock()
        app.ipc = MagicMock()
        app.ipc.is_connected.return_value = True
        app.ipc.send_resume_sync.return_value = False

        assert app.resume_sync() is False
        app.notifications.show.assert_called_once()


class TestUnreadableIPCBreakCounts:
    """An unreadable pushed count must not clear the last known badge."""

    def test_missing_count_keeps_last_known_value(self):
        app = _make_test_app()
        app.state.set_break_count(4)
        message = MagicMock(data={})

        app._on_ipc_break_count(message)

        assert app.state.current.break_count == 4

    def test_invalid_status_count_keeps_last_known_value(self):
        app = _make_test_app()
        app.state.set_break_count(4)
        message = MagicMock(data={"break_count": "unknown"})

        app._on_ipc_status_update(message)

        assert app.state.current.break_count == 4


class TestCanonicalDesktopState:
    """The tray consumes Desktop protocol v1 without compatibility fields."""

    def test_status_uses_recording_boolean_and_capture_id(self):
        app = _make_test_app()

        app._on_ipc_status_update(
            MagicMock(
                data={
                    "recording": True,
                    "paused": False,
                    "capture_id": "capture-1",
                }
            )
        )

        assert app.state.current.state == TrayState.RECORDING
        assert app.state.current.current_capture == "capture-1"

    def test_status_false_clears_stale_recording_state(self):
        app = _make_test_app()
        app.state.transition(TrayState.RECORDING, current_capture="old")

        app._on_ipc_status_update(
            MagicMock(data={"recording": False, "capture_id": None})
        )

        assert app.state.current.state == TrayState.IDLE
        assert app.state.current.current_capture is None

    def test_recording_event_uses_capture_id(self):
        app = _make_test_app()

        app._on_ipc_recording_started(MagicMock(data={"capture_id": "capture-2"}))

        assert app.state.current.current_capture == "capture-2"

    def test_compiled_is_a_successful_terminal_state(self):
        app = _make_test_app()
        app.state.transition(TrayState.COMPILING, current_capture="capture-3")

        app._on_ipc_compile_progress(
            MagicMock(data={"state": "compiled", "capture_id": "capture-3"})
        )

        assert app.state.current.state == TrayState.IDLE
        assert app.state.current.error_message is None

    @pytest.mark.parametrize("terminal", ["failed", "review_failed"])
    def test_compile_failure_terminal_states_stay_visible(self, terminal):
        app = _make_test_app()

        app._on_ipc_compile_progress(
            MagicMock(
                data={
                    "state": terminal,
                    "capture_id": "capture-4",
                    "error": "retained failure",
                }
            )
        )

        assert app.state.current.state == TrayState.ERROR
        assert app.state.current.current_capture == "capture-4"
        assert app.state.current.error_message == "retained failure"
