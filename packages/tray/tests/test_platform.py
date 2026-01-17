"""Tests for platform detection and handlers."""

import sys
import pytest
from unittest.mock import patch, MagicMock

from openadapt_tray.platform import get_platform_handler
from openadapt_tray.platform.base import PlatformHandler


class TestPlatformDetection:
    """Tests for platform detection."""

    @patch("sys.platform", "darwin")
    def test_macos_detection(self):
        """Test that macOS platform is detected."""
        from openadapt_tray.platform.macos import MacOSHandler

        handler = get_platform_handler()
        assert isinstance(handler, MacOSHandler)

    @patch("sys.platform", "win32")
    def test_windows_detection(self):
        """Test that Windows platform is detected."""
        from openadapt_tray.platform.windows import WindowsHandler

        handler = get_platform_handler()
        assert isinstance(handler, WindowsHandler)

    @patch("sys.platform", "linux")
    def test_linux_detection(self):
        """Test that Linux platform is detected."""
        from openadapt_tray.platform.linux import LinuxHandler

        handler = get_platform_handler()
        assert isinstance(handler, LinuxHandler)


class TestPlatformHandlerBase:
    """Tests for PlatformHandler base class behavior."""

    def test_base_class_is_abstract(self):
        """Test that PlatformHandler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            PlatformHandler()

    def test_supports_native_dialogs_default(self):
        """Test default value for supports_native_dialogs."""

        class TestHandler(PlatformHandler):
            def setup(self):
                pass

            def prompt_input(self, title, message):
                return None

            def confirm_dialog(self, title, message):
                return False

            def open_settings_dialog(self, config):
                pass

            def open_training_dialog(self):
                pass

        handler = TestHandler()
        assert handler.supports_native_dialogs is True

    def test_supports_autostart_default(self):
        """Test default value for supports_autostart."""

        class TestHandler(PlatformHandler):
            def setup(self):
                pass

            def prompt_input(self, title, message):
                return None

            def confirm_dialog(self, title, message):
                return False

            def open_settings_dialog(self, config):
                pass

            def open_training_dialog(self):
                pass

        handler = TestHandler()
        assert handler.supports_autostart is False

    def test_setup_autostart_returns_false_by_default(self):
        """Test default setup_autostart returns False."""

        class TestHandler(PlatformHandler):
            def setup(self):
                pass

            def prompt_input(self, title, message):
                return None

            def confirm_dialog(self, title, message):
                return False

            def open_settings_dialog(self, config):
                pass

            def open_training_dialog(self):
                pass

        handler = TestHandler()
        assert handler.setup_autostart(True) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
class TestMacOSHandler:
    """Tests for macOS-specific handler."""

    def test_setup_hides_dock(self):
        """Test that setup attempts to hide from Dock."""
        from openadapt_tray.platform.macos import MacOSHandler

        handler = MacOSHandler()
        # Should not raise even if AppKit not available
        handler.setup()

    @patch("subprocess.run")
    def test_prompt_input_calls_osascript(self, mock_run):
        """Test that prompt_input uses osascript."""
        from openadapt_tray.platform.macos import MacOSHandler

        mock_run.return_value = MagicMock(returncode=0, stdout="test input\n")

        handler = MacOSHandler()
        result = handler.prompt_input("Title", "Message")

        assert result == "test input"
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"

    @patch("subprocess.run")
    def test_confirm_dialog_returns_true_on_ok(self, mock_run):
        """Test that confirm_dialog returns True when OK clicked."""
        from openadapt_tray.platform.macos import MacOSHandler

        mock_run.return_value = MagicMock(returncode=0, stdout="OK\n")

        handler = MacOSHandler()
        result = handler.confirm_dialog("Title", "Message")

        assert result is True

    @patch("subprocess.run")
    def test_confirm_dialog_returns_false_on_cancel(self, mock_run):
        """Test that confirm_dialog returns False when cancelled."""
        from openadapt_tray.platform.macos import MacOSHandler

        mock_run.return_value = MagicMock(returncode=1, stdout="")

        handler = MacOSHandler()
        result = handler.confirm_dialog("Title", "Message")

        assert result is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestWindowsHandler:
    """Tests for Windows-specific handler."""

    def test_setup_does_nothing(self):
        """Test that Windows setup completes without error."""
        from openadapt_tray.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.setup()  # Should not raise


@pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
class TestLinuxHandler:
    """Tests for Linux-specific handler."""

    def test_setup_does_nothing(self):
        """Test that Linux setup completes without error."""
        from openadapt_tray.platform.linux import LinuxHandler

        handler = LinuxHandler()
        handler.setup()  # Should not raise
