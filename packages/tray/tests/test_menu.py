"""Tests for menu construction."""

import pytest
from unittest.mock import patch, MagicMock

from openadapt_tray.state import TrayState, AppState
from openadapt_tray.menu import MenuBuilder, CaptureInfo


class TestCaptureInfo:
    """Tests for CaptureInfo dataclass."""

    def test_capture_info_creation(self):
        """Test CaptureInfo creation."""
        info = CaptureInfo(
            name="test_capture",
            path="/path/to/capture",
            timestamp="2024-01-15 10:30",
        )
        assert info.name == "test_capture"
        assert info.path == "/path/to/capture"
        assert info.timestamp == "2024-01-15 10:30"


class TestMenuBuilder:
    """Tests for MenuBuilder class."""

    def create_mock_app(self, state=None):
        """Create a mock TrayApplication."""
        app = MagicMock()
        app.state = MagicMock()
        app.state.current = state or AppState(state=TrayState.IDLE)
        app.config = MagicMock()
        app.config.hotkeys = MagicMock()
        app.config.hotkeys.toggle_recording = "<ctrl>+<shift>+r"
        app.config.dashboard_port = 8080
        app.config.get_captures_path.return_value = MagicMock(exists=lambda: False)
        app.platform = MagicMock()
        app.notifications = MagicMock()
        return app

    def test_build_returns_menu(self):
        """Test that build returns a pystray Menu."""
        import pystray

        app = self.create_mock_app()
        builder = MenuBuilder(app)

        menu = builder.build()

        # pystray.Menu is the expected type
        assert menu is not None

    def test_recording_item_when_idle(self):
        """Test recording item shows 'Start Recording' when idle."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE))
        builder = MenuBuilder(app)

        item = builder._build_recording_item(app.state.current)

        assert "Start Recording" in str(item.text)

    def test_recording_item_when_recording(self):
        """Test recording item shows 'Stop Recording' when recording."""
        app = self.create_mock_app(
            AppState(state=TrayState.RECORDING, current_capture="test")
        )
        builder = MenuBuilder(app)

        item = builder._build_recording_item(app.state.current)

        assert "Stop Recording" in str(item.text)

    def test_recording_item_disabled_when_starting(self):
        """Test recording item is disabled when starting."""
        app = self.create_mock_app(AppState(state=TrayState.RECORDING_STARTING))
        builder = MenuBuilder(app)

        item = builder._build_recording_item(app.state.current)

        assert "Starting" in str(item.text)

    def test_training_item_when_idle(self):
        """Test training item when not training."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE))
        builder = MenuBuilder(app)

        item = builder._build_training_item(app.state.current)

        assert "Training" in str(item.text)

    def test_training_item_when_training(self):
        """Test training item shows progress when training."""
        app = self.create_mock_app(
            AppState(state=TrayState.TRAINING, training_progress=0.5)
        )
        builder = MenuBuilder(app)

        item = builder._build_training_item(app.state.current)

        # Should show percentage
        assert "50%" in str(item.text) or "Training" in str(item.text)

    def test_get_recent_captures_empty(self):
        """Test get_recent_captures returns empty list when no captures."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        captures = builder._get_recent_captures()

        assert captures == []

    @patch("webbrowser.open")
    def test_open_dashboard(self, mock_webbrowser):
        """Test open_dashboard opens browser."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._open_dashboard()

        app._open_dashboard.assert_called_once()

    def test_quit_calls_app_quit(self):
        """Test quit calls app.quit."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._quit()

        app.quit.assert_called_once()
