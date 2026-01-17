"""macOS-specific functionality for OpenAdapt Tray."""

import subprocess
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import webbrowser

from openadapt_tray.platform.base import PlatformHandler

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig


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

    def prompt_input(self, title: str, message: str) -> Optional[str]:
        """Show native macOS input dialog.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if cancelled.
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
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"Error showing input dialog: {e}")
        return None

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show native macOS confirmation dialog.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if user clicked OK.
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
            )
            return result.returncode == 0 and "OK" in result.stdout
        except subprocess.TimeoutExpired:
            return False
        except Exception as e:
            print(f"Error showing confirm dialog: {e}")
            return False

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
        import sys

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
                    subprocess.run(
                        ["launchctl", "unload", str(plist_path)],
                        capture_output=True,
                    )
                    plist_path.unlink()
            return True
        except Exception as e:
            print(f"Error configuring auto-start: {e}")
            return False

    def _find_executable(self) -> Optional[str]:
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
        pass
