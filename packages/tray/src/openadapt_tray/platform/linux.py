"""Linux-specific functionality for OpenAdapt Tray."""

import os
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from openadapt_tray.platform.base import DialogUnavailableError, PlatformHandler

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig

# zenity and kdialog both use exit 1 for "the user said no / cancelled". Any
# other non-zero exit (zenity uses 255) is the TOOL failing -- no display, a
# broken GTK, a timeout -- which must not be read as a user's answer.
DIALOG_DECLINED_EXIT = 1


class LinuxHandler(PlatformHandler):
    """Linux-specific functionality."""

    def setup(self) -> None:
        """Linux-specific setup."""
        # No special setup needed on most Linux systems

    def prompt_input(self, title: str, message: str) -> str | None:
        """Show input dialog using zenity or kdialog.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if the user cancelled.

        Raises:
            DialogUnavailableError: zenity, kdialog and tkinter all failed to
                show a dialog.
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
                check=False,  # returncode is inspected directly below
            )
            if result.returncode == 0:
                return result.stdout.strip()
            if result.returncode == DIALOG_DECLINED_EXIT:
                return None  # The user cancelled. That is an answer.
            # zenity ran but could not show anything (no display, exit 255).
            # This used to return None, i.e. it was reported as the user
            # cancelling, and it also skipped the kdialog/tkinter fallbacks.
            print(f"zenity could not show a dialog (exit {result.returncode})")
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
                check=False,  # returncode is inspected directly below
            )
            if result.returncode == 0:
                return result.stdout.strip()
            if result.returncode == DIALOG_DECLINED_EXIT:
                return None  # The user cancelled.
            print(f"kdialog could not show a dialog (exit {result.returncode})")
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
            raise DialogUnavailableError(
                f"no input dialog available (zenity, kdialog, tkinter): {e}"
            ) from e

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show confirmation dialog using zenity or kdialog.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if the user clicked OK, False if the user declined.

        Raises:
            DialogUnavailableError: zenity, kdialog and tkinter all failed to
                show a dialog, so the user was never asked.
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
                check=False,  # returncode is inspected directly below
            )
            if result.returncode in (0, DIALOG_DECLINED_EXIT):
                return result.returncode == 0
            # zenity ran but showed nothing. Reporting that as "the user said
            # no" is a guess dressed up as an answer.
            print(f"zenity could not show a dialog (exit {result.returncode})")
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
                check=False,  # returncode is inspected directly below
            )
            if result.returncode in (0, DIALOG_DECLINED_EXIT):
                return result.returncode == 0
            print(f"kdialog could not show a dialog (exit {result.returncode})")
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
            return bool(result)
        except Exception as e:
            raise DialogUnavailableError(
                f"no confirmation dialog available (zenity, kdialog, tkinter): {e}"
            ) from e

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

    def _find_executable(self) -> str | None:
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
