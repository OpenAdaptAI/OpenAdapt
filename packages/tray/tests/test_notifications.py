"""Tests for the notification fallback backends.

The fallback paths are the ones that report success back to the caller, so the
contract under test is: ``_show_*`` returns True only when the notification was
actually delivered.
"""

import subprocess
from unittest.mock import MagicMock, patch

from openadapt_tray.notifications import NotificationManager


def _manager() -> NotificationManager:
    """Build a manager without touching any real notification backend."""
    with patch.object(NotificationManager, "_detect_backend", return_value="windows"):
        return NotificationManager()


class TestShowWindows:
    """``_show_windows`` falls back pystray balloon -> PowerShell toast."""

    def test_tray_icon_balloon_is_preferred(self):
        manager = _manager()
        manager.set_tray_icon(MagicMock())
        with patch("subprocess.run") as run:
            assert manager._show_windows("t", "b", None, 5000) is True
        manager._tray_icon.notify.assert_called_once_with("b", "t")
        run.assert_not_called()

    def test_balloon_failure_falls_back_to_toast(self):
        manager = _manager()
        icon = MagicMock()
        icon.notify.side_effect = RuntimeError("no balloon")
        manager.set_tray_icon(icon)
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            assert manager._show_windows("t", "b", None, 5000) is True
        run.assert_called_once()

    def test_failed_toast_reports_failure(self):
        """A non-zero PowerShell exit must NOT be reported as delivered.

        This used to ``return True`` unconditionally, so a machine where the
        WinRT toast API is unavailable reported every notification as shown.
        """
        manager = _manager()
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"boom")
            assert manager._show_windows("t", "b", None, 5000) is False

    def test_toast_exception_reports_failure(self):
        manager = _manager()
        with patch("subprocess.run", side_effect=FileNotFoundError("powershell")):
            assert manager._show_windows("t", "b", None, 5000) is False


class TestShowLinux:
    def test_returncode_drives_result(self):
        manager = _manager()
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            assert manager._show_linux("t", "b", None) is True
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"")
            assert manager._show_linux("t", "b", None) is False
