"""Tests for the main TrayApplication."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from openadapt_tray.state import TrayState
from openadapt_tray.config import TrayConfig


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

        # Start recording with a name (skip prompt)
        with patch.object(app, "_run_capture"):
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

        with patch.object(app, "_run_capture") as mock_run:
            app.start_recording("new_capture")
            mock_run.assert_not_called()

    @patch("openadapt_tray.app.pystray")
    @patch("openadapt_tray.app.get_platform_handler")
    def test_stop_recording_changes_state(self, mock_platform, mock_pystray):
        """Test that stop_recording transitions to IDLE."""
        from openadapt_tray.app import TrayApplication

        mock_platform.return_value = MagicMock()
        mock_pystray.Icon.return_value = MagicMock()

        app = TrayApplication()
        app.state.transition(TrayState.RECORDING, current_capture="test")

        app.stop_recording()

        assert app.state.current.state == TrayState.IDLE

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
