"""macOS-specific functionality for OpenAdapt Tray."""

import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from openadapt_tray.platform.base import DialogUnavailableError, PlatformHandler

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig

# osascript exits 1 both when the user cancels a dialog AND when the script
# itself fails (no Apple-events permission, no window server). The two are only
# distinguishable by stderr, which carries this marker on a real failure.
OSASCRIPT_ERROR_MARKER = "execution error"


def _raise_if_osascript_failed(
    result: subprocess.CompletedProcess, kind: str
) -> None:
    """Raise when a non-zero osascript exit was a failure, not a user decision.

    Args:
        result: The finished ``osascript`` process.
        kind: Dialog kind, used in the error message.

    Raises:
        DialogUnavailableError: stderr carries an AppleScript execution error,
            which means no dialog was shown -- as opposed to the user clicking
            Cancel, which exits non-zero with a quiet stderr.
    """
    stderr = result.stderr or ""
    if OSASCRIPT_ERROR_MARKER in stderr.lower():
        raise DialogUnavailableError(
            f"osascript could not show the {kind} dialog: {stderr.strip()}"
        )


class MacOSHandler(PlatformHandler):
    """macOS-specific functionality."""

    def setup(self) -> None:
        """Hide from Dock, show only in menu bar."""
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except ImportError:
            # AppKit not available, continue without Dock hiding
            pass

    def prompt_input(self, title: str, message: str) -> str | None:
        """Show native macOS input dialog.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if the user cancelled (or did not answer
            within the timeout).

        Raises:
            DialogUnavailableError: osascript is missing or reported an
                execution error, so no dialog ever reached the screen.
        """
        # Escape special characters for AppleScript
        title_escaped = title.replace('"', '\\"').replace("\\", "\\\\")
        message_escaped = message.replace('"', '\\"').replace("\\", "\\\\")

        script = f'''
        tell application "System Events"
            display dialog "{message_escaped}" default answer "" with title "{title_escaped}"
            return text returned of result
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=60,  # 1 minute timeout for user input
                check=False,  # returncode is inspected directly below
            )
        except subprocess.TimeoutExpired:
            # The dialog WAS shown; the user just did not answer it.
            return None
        except Exception as e:
            raise DialogUnavailableError(f"could not run osascript: {e}") from e

        if result.returncode == 0:
            return result.stdout.strip()
        _raise_if_osascript_failed(result, "input")
        return None  # The user cancelled.

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show native macOS confirmation dialog.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if the user clicked OK, False if the user declined (or did not
            answer within the timeout).

        Raises:
            DialogUnavailableError: osascript is missing or reported an
                execution error, so the user was never asked.
        """
        # Escape special characters for AppleScript
        title_escaped = title.replace('"', '\\"').replace("\\", "\\\\")
        message_escaped = message.replace('"', '\\"').replace("\\", "\\\\")

        script = f'''
        tell application "System Events"
            display dialog "{message_escaped}" with title "{title_escaped}" buttons {{"Cancel", "OK"}} default button "OK"
            return button returned of result
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,  # returncode is inspected directly below
            )
        except subprocess.TimeoutExpired:
            # The dialog WAS shown; the user just did not answer it.
            return False
        except Exception as e:
            raise DialogUnavailableError(f"could not run osascript: {e}") from e

        if result.returncode == 0:
            return "OK" in result.stdout
        _raise_if_osascript_failed(result, "confirmation")
        return False  # The user clicked Cancel.

    def open_settings_dialog(self, config: "TrayConfig") -> None:
        """Open settings in default browser.

        Args:
            config: Current configuration.
        """
        webbrowser.open(f"http://localhost:{config.dashboard_port}/settings")

    def open_training_dialog(self) -> None:
        """Open training dialog in browser."""
        webbrowser.open("http://localhost:8080/training/new")

    def setup_autostart(self, enabled: bool) -> bool:
        """Configure Launch Agent for auto-start.

        Args:
            enabled: Whether to enable or disable auto-start.

        Returns:
            True if successful.
        """
        plist_path = Path.home() / "Library/LaunchAgents/ai.openadapt.tray.plist"

        try:
            if enabled:
                # Find the openadapt-tray executable
                exe_path = self._find_executable()
                if not exe_path:
                    print("Could not find openadapt-tray executable")
                    return False

                plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.openadapt.tray</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>'''
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                plist_path.write_text(plist_content)
                subprocess.run(["launchctl", "load", str(plist_path)], check=True)
            else:
                if plist_path.exists():
                    # check=False on purpose: unloading an agent that was
                    # never loaded exits non-zero, and we still want to delete
                    # the plist below.
                    subprocess.run(
                        ["launchctl", "unload", str(plist_path)],
                        capture_output=True,
                        check=False,
                    )
                    plist_path.unlink()
            return True
        except Exception as e:
            print(f"Error configuring auto-start: {e}")
            return False

    def _find_executable(self) -> str | None:
        """Find the openadapt-tray executable path."""
        import shutil
        import sys

        # Check if running from installed script
        exe = shutil.which("openadapt-tray")
        if exe:
            return exe

        # Fallback to Python module invocation
        python_path = sys.executable
        return f"{python_path} -m openadapt_tray"

    @property
    def supports_autostart(self) -> bool:
        """Check if auto-start configuration is supported."""
        return True

    def cleanup(self) -> None:
        """Cleanup any platform-specific resources."""
