"""Tests for menu construction."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray.menu import CaptureInfo, MenuBuilder
from openadapt_tray.platform.base import DialogUnavailableError
from openadapt_tray.state import (
    AppState,
    CredentialState,
    CredentialStatus,
    TrayState,
)


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

    @pytest.mark.parametrize(
        ("credential", "label"),
        [
            (CredentialStatus.signed_out(), "sign in"),
            (CredentialStatus.unknown(), "status unavailable"),
            (
                CredentialStatus(
                    state=CredentialState.EXPIRING,
                    expires_at="2026-08-05T12:00:00Z",
                    expires_in_days=8,
                    warning_days=14,
                ),
                "expires in 8 days",
            ),
        ],
    )
    def test_account_item_exposes_actionable_status(self, credential, label):
        app = self.create_mock_app(AppState(credential=credential))
        item = MenuBuilder(app)._build_account_item(app.state.current)

        assert label in str(item.text).lower()

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


class TestCapturesSubmenuDistinguishesEmptyFromUnreadable:
    """"Nothing there" and "could not look" must not render the same.

    ``_get_recent_captures`` used to swallow every exception and return ``[]``,
    so an unreadable captures directory (permissions, a dead mount) produced
    the same reassuring "No captures" entry as a directory that really was
    empty.
    """

    def _app_with_captures_dir(self, captures_dir):
        app = MagicMock()
        app.config.get_captures_path.return_value = captures_dir
        return app

    def test_empty_directory_says_no_captures(self, tmp_path):
        builder = MenuBuilder(self._app_with_captures_dir(tmp_path))
        item = builder._build_captures_submenu()
        labels = [str(i.text) for i in item.submenu.items]
        assert "No captures" in labels

    def test_unreadable_directory_says_so(self):
        captures_dir = MagicMock()
        captures_dir.exists.return_value = True
        captures_dir.iterdir.side_effect = PermissionError("permission denied")
        builder = MenuBuilder(self._app_with_captures_dir(captures_dir))

        item = builder._build_captures_submenu()

        labels = [str(i.text) for i in item.submenu.items]
        assert "Could not read captures" in labels
        assert "No captures" not in labels

    def test_get_recent_captures_raises_rather_than_returning_empty(self):
        captures_dir = MagicMock()
        captures_dir.exists.return_value = True
        captures_dir.iterdir.side_effect = PermissionError("permission denied")
        builder = MenuBuilder(self._app_with_captures_dir(captures_dir))

        with pytest.raises(OSError):
            builder._get_recent_captures()


class TestViewCaptureChecksExitStatus:
    """A CLI that ran and failed used to be indistinguishable from one that worked."""

    def test_nonzero_exit_falls_back_to_file_browser(self):
        builder = MenuBuilder(MagicMock())
        with patch("subprocess.run") as run, patch.object(
            builder, "_open_in_file_browser"
        ) as fallback:
            run.return_value = subprocess.CompletedProcess([], 2, b"", b"boom")
            builder._view_capture("/tmp/capture")
        fallback.assert_called_once_with("/tmp/capture")

    def test_zero_exit_does_not_fall_back(self):
        builder = MenuBuilder(MagicMock())
        with patch("subprocess.run") as run, patch.object(
            builder, "_open_in_file_browser"
        ) as fallback:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            builder._view_capture("/tmp/capture")
        fallback.assert_not_called()

    def test_missing_cli_falls_back_once(self):
        builder = MenuBuilder(MagicMock())
        with patch("subprocess.run", side_effect=FileNotFoundError), patch.object(
            builder, "_open_in_file_browser"
        ) as fallback:
            builder._view_capture("/tmp/capture")
        fallback.assert_called_once_with("/tmp/capture")


class TestDeleteCaptureWhenNoDialogCanBeShown:
    """"We never asked" must not be silently read as "the user said no"."""

    def test_unavailable_dialog_deletes_nothing_and_says_so(self):
        app = MagicMock()
        app.platform.confirm_dialog.side_effect = DialogUnavailableError("no display")
        builder = MenuBuilder(app)

        with patch("shutil.rmtree") as rmtree:
            builder._delete_capture("/tmp/capture", "cap")

        rmtree.assert_not_called()
        app.notifications.show.assert_called_once()
        title = app.notifications.show.call_args[0][0]
        assert "confirm" in title.lower()

    def test_declining_deletes_nothing_and_stays_quiet(self):
        app = MagicMock()
        app.platform.confirm_dialog.return_value = False
        builder = MenuBuilder(app)

        with patch("shutil.rmtree") as rmtree:
            builder._delete_capture("/tmp/capture", "cap")

        rmtree.assert_not_called()
        app.notifications.show.assert_not_called()
