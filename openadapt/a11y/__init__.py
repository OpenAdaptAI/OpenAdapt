"""This module provides platform-specific implementations for window and element
    interactions using accessibility APIs. It abstracts the platform differences
    and provides a unified interface for retrieving the active window, finding
    display elements, and getting element values.
"""

import sys

from loguru import logger

if sys.platform == "darwin":
    from . import _macos as impl

    role = "AXStaticText"
elif sys.platform in ("win32", "linux"):
    from . import _windows as impl

    role = "Text"
else:
    raise Exception(f"Unsupported platform: {sys.platform}")


def get_active_window():
    """Get the active window object.

    Returns:
        The active window object.
    """
    try:
        return impl.get_active_window()
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None


def get_element_value(active_window, role=role):
    """Find the display of active_window.

    Args:
        active_window: The parent window to search within.

    Returns:
        The found active_window.
    """
    try:
        return impl.get_element_value(active_window, role)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None
