"""Windows-specific functionality for OpenAdapt Tray."""

import webbrowser
from typing import TYPE_CHECKING

from openadapt_tray.platform.base import DialogUnavailableError, PlatformHandler

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig


class WindowsHandler(PlatformHandler):
    """Windows-specific functionality."""

    def setup(self) -> None:
        """Windows-specific setup."""
        # No special setup needed on Windows

    def prompt_input(self, title: str, message: str) -> str | None:
        """Show Windows input dialog using tkinter.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if the user cancelled.

        Raises:
            DialogUnavailableError: tkinter could not show a dialog, so the
                user was never prompted. This used to return ``None``, which
                the caller reads as "the user cancelled".
        """
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()  # Hide the root window
            root.attributes("-topmost", True)  # Bring to front

            result = simpledialog.askstring(title, message, parent=root)
            root.destroy()
            return result
        except Exception as e:
            raise DialogUnavailableError(f"no input dialog available: {e}") from e

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show Windows confirmation dialog.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if the user clicked OK, False if the user declined.

        Raises:
            DialogUnavailableError: Neither MessageBoxW nor tkinter could show
                a dialog. This used to return ``False``, which the caller reads
                as "the user said no" -- so a destructive action would appear
                to be declined by a user who was never asked.
        """
        try:
            import ctypes

            MB_OKCANCEL = 0x01
            MB_ICONQUESTION = 0x20
            IDOK = 1

            result = ctypes.windll.user32.MessageBoxW(
                0, message, title, MB_OKCANCEL | MB_ICONQUESTION
            )
            # MessageBoxW returns 0 when it could not create the box at all.
            if result == 0:
                raise OSError("MessageBoxW returned 0 (dialog not created)")
            return result == IDOK
        except Exception as e:
            print(f"Error showing confirm dialog: {e}")
            # Fallback to tkinter
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                result = messagebox.askokcancel(title, message)
                root.destroy()
                return bool(result)
            except Exception as fallback_error:
                raise DialogUnavailableError(
                    "no confirmation dialog available "
                    f"(MessageBoxW: {e}; tkinter: {fallback_error})"
                ) from fallback_error

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
        """Configure Windows Registry for auto-start.

        Args:
            enabled: Whether to enable or disable auto-start.

        Returns:
            True if successful.
        """
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            app_name = "OpenAdapt"

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS
            )

            try:
                if enabled:
                    exe_path = self._find_executable()
                    if exe_path:
                        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
                    else:
                        return False
                else:
                    try:
                        winreg.DeleteValue(key, app_name)
                    except FileNotFoundError:
                        pass  # Already not set
            finally:
                winreg.CloseKey(key)

            return True
        except Exception as e:
            print(f"Error configuring auto-start: {e}")
            return False

    def _find_executable(self) -> str | None:
        """Find the openadapt-tray executable path."""
        import os
        import shutil
        import sys

        # Check for installed script in Scripts folder
        exe = shutil.which("openadapt-tray")
        if exe:
            return exe

        # Check for .exe in Scripts folder. `os.path.exists` swallows its own
        # OSErrors and returns False, so the try/except/pass that used to wrap
        # this was unreachable, and it also hid a failed `import os`.
        python_dir = sys.prefix
        exe_path = f"{python_dir}\\Scripts\\openadapt-tray.exe"
        if os.path.exists(exe_path):
            return exe_path

        # Fallback to Python module invocation
        return f'"{sys.executable}" -m openadapt_tray'

    @property
    def supports_autostart(self) -> bool:
        """Check if auto-start configuration is supported."""
        return True

    def cleanup(self) -> None:
        """Cleanup any platform-specific resources."""
