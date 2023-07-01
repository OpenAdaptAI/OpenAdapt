from pprint import pprint
from typing import Any
import pickle

from loguru import logger
import atomacos
import AppKit
import ApplicationServices
import Quartz


def get_active_window_state() -> dict | None:
    """Get the state of the active window.

    Returns:
        dict or None: A dictionary containing the state of the active window,
        or None if the state is not available.
    """
    # pywinctl performance on macOS is unusable, see:
    # https://github.com/Kalmat/PyWinCtl/issues/29
    meta = get_active_window_meta()
    data = get_window_data(meta)
    title_parts = [
        meta["kCGWindowOwnerName"],
        meta["kCGWindowName"],
    ]
    title_parts = [part for part in title_parts if part]
    title = " ".join(title_parts)
    window_id = meta["kCGWindowNumber"]
    bounds = meta["kCGWindowBounds"]
    left = bounds["X"]
    top = bounds["Y"]
    width = bounds["Width"]
    height = bounds["Height"]
    rval = {
        "title": title,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "window_id": window_id,
        "meta": meta,
        "data": data,
    }
    rval = deepconvert_objc(rval)
    try:
        pickle.dumps(rval, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        logger.warning(f"{exc=}")
        rval.pop("data")
    return rval


def get_active_window_meta() -> dict:
    """Get the metadata of the active window.

    Returns:
        dict: A dictionary containing the metadata of the active window.
    """
    windows = Quartz.CGWindowListCopyWindowInfo(
        (
            Quartz.kCGWindowListExcludeDesktopElements
            | Quartz.kCGWindowListOptionOnScreenOnly
        ),
        Quartz.kCGNullWindowID,
    )
    active_windows_info = [win for win in windows if win["kCGWindowLayer"] == 0]
    active_window_info = active_windows_info[0]
    return active_window_info


def get_active_window(window_meta: dict) -> Any:
    """Get the active window from the given metadata.

    Args:
        window_meta (dict): The metadata of the window.

    Returns:
        AXUIElement or None: The active window as an AXUIElement object,
        or None if the active window cannot be retrieved.
    """
    pid = window_meta["kCGWindowOwnerPID"]
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    error_code, window = ApplicationServices.AXUIElementCopyAttributeValue(
        app_ref, "AXFocusedWindow", None
    )
    if error_code:
        logger.error("Error getting focused window")
        return None
    return window


def get_window_data(window_meta: dict) -> dict:
    """Get the data of the window.

    Args:
        window_meta (dict): The metadata of the window.

    Returns:
        dict: A dictionary containing the data of the window.
    """
    window = get_active_window(window_meta)
    state = dump_state(window)
    return state


def dump_state(element: Any, elements: set = None) -> dict | list:
    """Dump the state of the given element and its descendants.

    Args:
        element: The element to dump the state for.
        elements (set): Set to track elements to prevent circular traversal.

    Returns:
        dict or list: The state of the element and its descendants as a dictionary or list.
    """
    elements = elements or set()
    if element in elements:
        return
    elements.add(element)

    if isinstance(element, AppKit.NSArray) or isinstance(element, list):
        state = []
        for child in element:
            _state = dump_state(child, elements)
            if _state:
                state.append(_state)
        return state
    elif isinstance(element, AppKit.NSDictionary) or isinstance(element, dict):
        state = {}
        for k, v in element.items():
            _state = dump_state(v, elements)
            if _state:
                state[k] = _state
        return state
    else:
        error_code, attr_names = ApplicationServices.AXUIElementCopyAttributeNames(
            element, None
        )
        if attr_names:
            state = {}
            for attr_name in attr_names:
                # Don't traverse back up for WindowEvents:
                if "parent" in attr_name.lower():
                    continue
                # For ActionEvents:
                if attr_name in ("AXTopLevelUIElement", "AXWindow"):
                    continue

                (
                    error_code,
                    attr_val,
                ) = ApplicationServices.AXUIElementCopyAttributeValue(
                    element,
                    attr_name,
                    None,
                )

                # For ActionEvents:
                if attr_name == "AXRole" and "application" in attr_val.lower():
                    continue

                _state = dump_state(attr_val, elements)
                if _state:
                    state[attr_name] = _state
            return state
        else:
            return element


# https://github.com/autopkg/autopkg/commit/1aff762d8ea658b3fca8ac693f3bf13e8baf8778
def deepconvert_objc(object: Any) -> Any:
    """Convert all contents of an ObjC object to Python primitives.

    Args:
        object: The object to convert.

    Returns:
        object: The converted object with Python primitives.
    """
    value = object
    if isinstance(object, AppKit.NSNumber):
        value = int(object)
    elif isinstance(object, AppKit.NSArray) or isinstance(object, list):
        value = [deepconvert_objc(x) for x in object]
    elif isinstance(object, AppKit.NSDictionary) or isinstance(object, dict):
        value = dict(object)
        for k, v in value.items():
            value[k] = deepconvert_objc(v)
    value = atomacos._converter.Converter().convert_value(value)
    return value


def get_active_element_state(x: int, y: int) -> dict:
    """Get the state of the active element at the specified coordinates.

    Args:
        x (int): The x-coordinate of the element.
        y (int): The y-coordinate of the element.

    Returns:
        dict: A dictionary containing the state of the active element.
    """
    window_meta = get_active_window_meta()
    pid = window_meta["kCGWindowOwnerPID"]
    app = atomacos._a11y.AXUIElement.from_pid(pid)
    el = app.get_element_at_position(x, y)
    state = dump_state(el.ref)
    state = deepconvert_objc(state)
    try:
        pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        logger.warning(f"{exc=}")
        state = {}
    return state


def main() -> None:
    """Main function for testing the functionality.

    This function sleeps for 1 second, gets the state of the active window,
    pretty-prints the state, and pickles the state. It also sets up the ipdb
    debugger for further debugging.

    Returns:
        None
    """
    import time

    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    import ipdb

    ipdb.set_trace()  # noqa: E702


if __name__ == "__main__":
    main()
