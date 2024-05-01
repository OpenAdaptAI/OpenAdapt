"""Package for interacting with active window and elements across platforms.

Module: __init__.py
"""

from typing import Any
import sys

from loguru import logger

from openadapt.config import config

if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform in ("win32", "linux"):
    # TODO: implement Linux
    from . import _windows as impl
else:
    raise Exception(f"Unsupported platform: {sys.platform}")


def get_active_window_data() -> dict[str, Any] | None:
    """Get data of the active window.

    Returns:
        dict or None: A dictionary containing information about the active window,
        or None if the state is not available.
    """
    state = get_active_window_state(config.RECORD_WINDOW_DATA)
    if not state:
        return None
    title = state["title"]
    left = state["left"]
    top = state["top"]
    width = state["width"]
    height = state["height"]
    window_id = state["window_id"]
    window_data = {
        "title": title,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "window_id": window_id,
        "state": state,
    }
    return window_data


def get_active_window_state(read_window_data: bool) -> dict | None:
    """Get the state of the active window.

    Returns:
        dict or None: A dictionary containing the state of the active window,
          or None if the state is not available.
    """
    # TODO: save window identifier (a window's title can change, or
    try:
        return impl.get_active_window_state(read_window_data)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None


def get_active_element_state(x: int, y: int) -> dict | None:
    """Get the state of the active element at the specified coordinates.

    Args:
        x (int): The x-coordinate of the element.
        y (int): The y-coordinate of the element.

    Returns:
        dict or None: A dictionary containing the state of the active element,
        or None if the state is not available.
    """
    try:
        return impl.get_active_element_state(x, y)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None
