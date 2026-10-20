"""Platform-specific functionality for OpenAdapt Tray."""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_tray.platform.base import PlatformHandler


def get_platform_handler() -> "PlatformHandler":
    """Get the appropriate platform handler for the current OS.

    Returns:
        Platform-specific handler instance.
    """
    if sys.platform == "darwin":
        from openadapt_tray.platform.macos import MacOSHandler

        return MacOSHandler()
    elif sys.platform == "win32":
        from openadapt_tray.platform.windows import WindowsHandler

        return WindowsHandler()
    else:
        from openadapt_tray.platform.linux import LinuxHandler

        return LinuxHandler()


__all__ = ["get_platform_handler"]
