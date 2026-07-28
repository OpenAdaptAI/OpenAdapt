"""Abstract base class for platform-specific functionality."""

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
    def open_settings_dialog(self, config: "TrayConfig") -> None:
        """Open settings dialog.

        Args:
            config: Current configuration.
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
