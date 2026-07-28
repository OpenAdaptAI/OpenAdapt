"""Tests for platform detection and handlers."""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray.platform import get_platform_handler
from openadapt_tray.platform.base import DialogUnavailableError, PlatformHandler
from openadapt_tray.platform.linux import LinuxHandler
from openadapt_tray.platform.macos import MacOSHandler
from openadapt_tray.platform.windows import WindowsHandler


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


class TestLinuxDialogsSeparateFailureFromAnswer:
    """A dialog tool that could not run must not speak for the user.

    zenity/kdialog exit 1 when the user cancels and 255 when the tool itself
    fails (no DISPLAY, broken GTK). Treating every non-zero exit as the user's
    answer both invented an answer AND skipped the remaining fallbacks.
    """

    def _result(self, returncode, stdout=""):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    def test_prompt_input_exit_1_is_a_real_cancel(self):
        handler = LinuxHandler()
        with patch("subprocess.run", return_value=self._result(1)) as run:
            assert handler.prompt_input("t", "m") is None
        # Cancel is an answer: no point asking kdialog the same question.
        assert run.call_count == 1

    def test_prompt_input_tool_failure_tries_the_next_tool(self):
        handler = LinuxHandler()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == "zenity":
                return self._result(255)  # zenity could not show anything
            return self._result(0, "typed name")

        with patch("subprocess.run", side_effect=fake_run):
            assert handler.prompt_input("t", "m") == "typed name"
        assert calls == ["zenity", "kdialog"]

    def test_prompt_input_raises_when_nothing_can_ask(self):
        handler = LinuxHandler()
        with patch("subprocess.run", return_value=self._result(255)), patch.dict(
            sys.modules, {"tkinter": None}
        ), pytest.raises(DialogUnavailableError):
            handler.prompt_input("t", "m")

    def test_confirm_dialog_exit_1_is_a_real_no(self):
        handler = LinuxHandler()
        with patch("subprocess.run", return_value=self._result(1)) as run:
            assert handler.confirm_dialog("t", "m") is False
        assert run.call_count == 1

    def test_confirm_dialog_tool_failure_tries_the_next_tool(self):
        handler = LinuxHandler()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == "zenity":
                return self._result(255)
            return self._result(0)

        with patch("subprocess.run", side_effect=fake_run):
            assert handler.confirm_dialog("t", "m") is True
        assert calls == ["zenity", "kdialog"]

    def test_confirm_dialog_raises_rather_than_answering_no_for_the_user(self):
        handler = LinuxHandler()
        with patch("subprocess.run", return_value=self._result(255)), patch.dict(
            sys.modules, {"tkinter": None}
        ), pytest.raises(DialogUnavailableError):
            handler.confirm_dialog("t", "m")


class TestWindowsDialogsSeparateFailureFromAnswer:
    def test_confirm_dialog_raises_when_no_dialog_can_be_shown(self):
        handler = WindowsHandler()
        # Block BOTH mechanisms, on every OS: blocking ctypes matters on
        # Windows runners, where the real MessageBoxW would block forever.
        with patch.dict(
            sys.modules, {"ctypes": None, "tkinter": None}
        ), pytest.raises(DialogUnavailableError):
            handler.confirm_dialog("t", "m")

    def test_prompt_input_raises_when_no_dialog_can_be_shown(self):
        handler = WindowsHandler()
        with patch.dict(sys.modules, {"tkinter": None}), pytest.raises(
            DialogUnavailableError
        ):
            handler.prompt_input("t", "m")


class TestMacOSDialogsSeparateFailureFromAnswer:
    """osascript exits 1 for a user cancel AND for an execution error."""

    def _result(self, returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_cancel_is_an_answer(self):
        handler = MacOSHandler()
        with patch("subprocess.run", return_value=self._result(1, "", "")):
            assert handler.confirm_dialog("t", "m") is False
            assert handler.prompt_input("t", "m") is None

    def test_execution_error_raises_instead_of_answering_for_the_user(self):
        handler = MacOSHandler()
        failure = self._result(
            1, "", "execution error: Not authorized to send Apple events (-1743)"
        )
        with patch("subprocess.run", return_value=failure):
            with pytest.raises(DialogUnavailableError):
                handler.confirm_dialog("t", "m")
            with pytest.raises(DialogUnavailableError):
                handler.prompt_input("t", "m")

    def test_missing_osascript_raises(self):
        handler = MacOSHandler()
        with patch("subprocess.run", side_effect=FileNotFoundError("osascript")):
            with pytest.raises(DialogUnavailableError):
                handler.confirm_dialog("t", "m")
            with pytest.raises(DialogUnavailableError):
                handler.prompt_input("t", "m")

    def test_ok_is_still_ok(self):
        handler = MacOSHandler()
        with patch("subprocess.run", return_value=self._result(0, "OK", "")):
            assert handler.confirm_dialog("t", "m") is True
