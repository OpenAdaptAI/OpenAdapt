"""Abstract base class for platform-specific functionality."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_tray.config import TrayConfig


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
            User input string, or None if cancelled.
        """

    @abstractmethod
    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show confirmation dialog and return result.

        Args:
            title: Dialog title.
            message: Confirmation message.

        Returns:
            True if user confirmed, False otherwise.
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
