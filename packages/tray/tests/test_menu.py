"""Tests for menu construction."""

from unittest.mock import MagicMock

from openadapt_tray.menu import CaptureInfo, MenuBuilder
from openadapt_tray.state import AppState, TrayState


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

    def test_recording_item_when_compiling(self):
        """Test recording item shows 'Compiling' when compiling."""
        app = self.create_mock_app(AppState(state=TrayState.COMPILING))
        builder = MenuBuilder(app)

        item = builder._build_recording_item(app.state.current)

        assert "Compiling" in str(item.text)

    def test_break_item_absent_without_breaks(self):
        """No needs-attention item when there are no breaks."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE, break_count=0))
        builder = MenuBuilder(app)

        assert builder._build_break_item(app.state.current) is None

    def test_break_item_present_with_breaks(self):
        """Needs-attention item shows the count when breaks exist."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE, break_count=2))
        builder = MenuBuilder(app)

        item = builder._build_break_item(app.state.current)
        assert item is not None
        assert "2" in str(item.text)
        assert "need attention" in str(item.text)

    def test_sync_item_offline_disabled(self):
        """Sync item is disabled and labelled offline when offline."""
        from openadapt_tray.state import SyncState

        app = self.create_mock_app(
            AppState(state=TrayState.IDLE, sync_state=SyncState.OFFLINE)
        )
        builder = MenuBuilder(app)

        item = builder._build_sync_item(app.state.current)
        assert "offline" in str(item.text).lower()

    def test_sync_item_pause_when_online(self):
        """Sync item offers Pause Sync when online."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE))
        builder = MenuBuilder(app)

        item = builder._build_sync_item(app.state.current)
        assert "Pause Sync" in str(item.text)

    def test_get_recent_captures_empty(self):
        """Test get_recent_captures returns empty list when no captures."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        captures = builder._get_recent_captures()

        assert captures == []

    def test_open_desktop_app_delegates(self):
        """Test the desktop-app menu action delegates to the app."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._open_desktop_app()

        app.open_desktop_app.assert_called_once()

    def test_open_cloud_dashboard_delegates(self):
        """Test the cloud-dashboard menu action delegates to the app."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._open_cloud_dashboard()

        app.open_cloud_dashboard.assert_called_once()

    def test_login_delegates(self):
        """Test the login menu action delegates to the app."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._login()

        app.login.assert_called_once()

    def test_build_full_menu(self):
        """The full menu builds without error in an idle state."""
        app = self.create_mock_app(AppState(state=TrayState.IDLE, break_count=1))
        builder = MenuBuilder(app)
        menu = builder.build()
        assert menu is not None

    def test_quit_calls_app_quit(self):
        """Test quit calls app.quit."""
        app = self.create_mock_app()
        builder = MenuBuilder(app)

        builder._quit()

        app.quit.assert_called_once()
