"""Abstract base class for platform-specific functionality."""

import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig


class DialogUnavailableError(RuntimeError):
    """No dialog could be shown to the user at all.

    Distinct from a dialog the user answered. "The user declined" and "we never
    managed to ask" used to be the same ``False``, and "the user cancelled" and
    "the prompt never appeared" used to be the same ``None`` -- so a click on
    Delete or Start Recording could do nothing at all, silently, forever.
    """


def run_opener(command: list[str]) -> bool:
    """Run a command that hands a file to another app, and report success.

    Args:
        command: The opener and its arguments, such as ``["open", "-t", path]``.

    Returns:
        True if the opener ran and exited 0. False if it was missing, timed
        out, or exited non-zero.
    """
    try:
        result = subprocess.run(command, check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Could not run {command[0]}: {e}")
        return False
    if result.returncode != 0:
        print(f"{command[0]} exited {result.returncode}: {result.stderr}")
        return False
    return True


class PlatformHandler(ABC):
    """Abstract base class for platform-specific functionality."""

    @abstractmethod
    def setup(self) -> None:
        """Platform-specific setup.

        Called once when the tray application starts.
        """

    @abstractmethod
    def prompt_input(self, title: str, message: str) -> str | None:
        """Show input dialog and return user input.

        Args:
            title: Dialog title.
            message: Prompt message.

        Returns:
            User input string, or None if the user cancelled. ``None`` means
            the user was asked and declined to answer -- never that we failed
            to ask.

        Raises:
            DialogUnavailableError: No dialog mechanism could be shown.
        """

    @abstractmethod
    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show confirmation dialog and return result.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if the user confirmed, False if the user declined. Both
            answers mean the user was actually asked.

        Raises:
            DialogUnavailableError: No dialog mechanism could be shown.
        """

    @abstractmethod
    def open_settings_dialog(self, config: "TrayConfig") -> bool:
        """Open ``tray.json`` in a text editor.

        The file is written with the current settings first if it does not
        exist. Changes take effect the next time the tray starts.

        Args:
            config: Current configuration.

        Returns:
            True if an editor accepted the file.
        """

    @abstractmethod
    def open_training_dialog(self) -> None:
        """Open training configuration dialog."""

    def setup_autostart(self, enabled: bool) -> bool:
        """Configure auto-start on login.

        Args:
            enabled: Whether to enable or disable auto-start.

        Returns:
            True if successful.
        """
        return False

    def cleanup(self) -> None:
        """Cleanup any platform-specific resources.

        Called when the tray application is shutting down.
        """

    @property
    def supports_native_dialogs(self) -> bool:
        """Check if native dialogs are supported."""
        return True

    @property
    def supports_autostart(self) -> bool:
        """Check if auto-start configuration is supported."""
        return False
