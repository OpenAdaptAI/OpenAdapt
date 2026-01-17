"""Linux-specific functionality for OpenAdapt Tray."""

import subprocess
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import webbrowser
import os

from openadapt_tray.platform.base import PlatformHandler

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig


class LinuxHandler(PlatformHandler):
    """Linux-specific functionality."""

    def setup(self) -> None:
        """Linux-specific setup."""
        # No special setup needed on most Linux systems
        pass

    def prompt_input(self, title: str, message: str) -> Optional[str]:
        """Show input dialog using zenity or kdialog.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if cancelled.
        """
        # Try zenity first (GNOME)
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--entry",
                    f"--title={title}",
                    f"--text={message}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error with zenity: {e}")

        # Try kdialog (KDE)
        try:
            result = subprocess.run(
                [
                    "kdialog",
                    "--inputbox",
                    message,
                    "--title",
                    title,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error with kdialog: {e}")

        # Fallback to tkinter
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()
            result = simpledialog.askstring(title, message, parent=root)
            root.destroy()
            return result
        except Exception as e:
            print(f"Error with tkinter: {e}")
            return None

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show confirmation dialog using zenity or kdialog.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if user clicked OK.
        """
        # Try zenity first
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--question",
                    f"--title={title}",
                    f"--text={message}",
                ],
                capture_output=True,
                timeout=60,
            )
            return result.returncode == 0
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error with zenity: {e}")

        # Try kdialog
        try:
            result = subprocess.run(
                [
                    "kdialog",
                    "--yesno",
                    message,
                    "--title",
                    title,
                ],
                capture_output=True,
                timeout=60,
            )
            return result.returncode == 0
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error with kdialog: {e}")

        # Fallback to tkinter
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            result = messagebox.askokcancel(title, message)
            root.destroy()
            return result
        except Exception as e:
            print(f"Error with tkinter: {e}")
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
        """Configure XDG autostart for auto-start.

        Args:
            enabled: Whether to enable or disable auto-start.

        Returns:
            True if successful.
        """
        autostart_dir = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ) / "autostart"
        desktop_file = autostart_dir / "openadapt-tray.desktop"

        try:
            if enabled:
                exe_path = self._find_executable()
                if not exe_path:
                    print("Could not find openadapt-tray executable")
                    return False

                desktop_content = f"""[Desktop Entry]
Type=Application
Name=OpenAdapt Tray
Comment=System tray application for OpenAdapt
Exec={exe_path}
Icon=openadapt
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""
                autostart_dir.mkdir(parents=True, exist_ok=True)
                desktop_file.write_text(desktop_content)
            else:
                if desktop_file.exists():
                    desktop_file.unlink()

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
        return f"{sys.executable} -m openadapt_tray"

    @property
    def supports_autostart(self) -> bool:
        """Check if auto-start configuration is supported."""
        return True

    def cleanup(self) -> None:
        """Cleanup any platform-specific resources."""
        pass
