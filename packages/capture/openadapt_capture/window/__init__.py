"""Package for interacting with active window and elements across platforms.

Copied from legacy OpenAdapt window/__init__.py. Only import paths changed.
"""

import sys
from typing import Any

from loguru import logger

from openadapt_capture.config import config


class WindowCaptureUnavailable(RuntimeError):
    """No window-capture backend could be loaded for this process.

    Raised rather than letting every window query return an empty result
    forever. An unavailable backend and a momentarily unreadable window
    otherwise look identical, and a recording proceeds with a silently empty
    window timeline.
    """


impl = None
impl_unavailable_reason: str | None = None
try:
    if sys.platform == "darwin":
        from . import _macos as impl
    elif sys.platform == "win32":
        from . import _windows as impl
    elif sys.platform.startswith("linux"):
        from . import _linux as impl
    else:
        impl_unavailable_reason = (
            f"Unsupported platform for window capture: {sys.platform}"
        )
        logger.warning(impl_unavailable_reason)
except ImportError as exc:
    impl_unavailable_reason = f"Window capture backend failed to import: {exc}"
    logger.error(impl_unavailable_reason)


def require_impl() -> Any:
    """Return the window-capture backend, or refuse.

    Returns:
        The platform backend module.

    Raises:
        WindowCaptureUnavailable: If no backend loaded. Call this before
            starting a window-reading loop so an absent backend surfaces as a
            recording-startup failure instead of an empty window timeline.
    """
    if impl is None:
        raise WindowCaptureUnavailable(
            impl_unavailable_reason or "No window-capture backend is loaded"
        )
    return impl


def get_active_window_data(
    include_window_data: bool = config.RECORD_WINDOW_DATA,
) -> dict[str, Any] | None:
    """Get data of the active window.

    Args:
        include_window_data (bool): whether to include a11y data.

    Returns:
        dict or None: A dictionary containing information about the active window,
            or None if the state is not available. `None` is the documented
            unavailable answer; an empty dict is never returned, because a
            caller cannot tell it from a window with no fields.
    """
    state = get_active_window_state(include_window_data)
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
    if impl is None:
        return None
    # TODO: save window identifier (a window's title can change, or
    # multiple windows can have the same title)
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
    if impl is None:
        return None
    try:
        return impl.get_active_element_state(x, y)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None
