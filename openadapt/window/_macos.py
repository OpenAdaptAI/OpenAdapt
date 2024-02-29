from pprint import pprint
from typing import Any, Literal, Union
import pickle
import plistlib
import re

from loguru import logger
import AppKit
import ApplicationServices
import Foundation
import oa_atomacos
import Quartz


def get_active_window_state(read_window_data: bool) -> dict | None:
    """Get the state of the active window.

    Returns:
        dict or None: A dictionary containing the state of the active window,
        or None if the state is not available.
    """
    # pywinctl performance on macOS is unusable, see:
    # https://github.com/Kalmat/PyWinCtl/issues/29
    meta = get_active_window_meta()
    if read_window_data:
        data = get_window_data(meta)
    else:
        data = {}
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
    active_windows_info = [
        win
        for win in windows
        if win["kCGWindowLayer"] == 0 and win["kCGWindowOwnerName"] != "Window Server"
    ]
    active_window_info = active_windows_info[0]
    return active_window_info


def get_active_window(window_meta: dict) -> ApplicationServices.AXUIElementRef | None:
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


def dump_state(
    element: Union[AppKit.NSArray, list, AppKit.NSDictionary, dict, Any],
    elements: set = None,
) -> Union[dict, list]:
    """Dump the state of the given element and its descendants.

    Args:
        element: The element to dump the state for.
        elements (set): Set to track elements to prevent circular traversal.

    Returns:
        dict or list: State of element and descendants as dict or list
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
                if attr_name is None:
                    continue
                # don't traverse back up
                # for WindowEvents:
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

                # for ActionEvents
                if attr_val is not None and (
                    attr_name == "AXRole" and "application" in attr_val.lower()
                ):
                    continue

                _state = dump_state(attr_val, elements)
                if _state:
                    state[attr_name] = _state
            return state
        else:
            return element


# https://github.com/autopkg/autopkg/commit/1aff762d8ea658b3fca8ac693f3bf13e8baf8778
def deepconvert_objc(object: Any) -> Any | list | dict | Literal[0]:
    """Convert all contents of an ObjC object to Python primitives.

    Args:
        object: The object to convert.

    Returns:
        object: The converted object with Python primitives.
    """
    value = object
    strings = (
        str,
        AppKit.NSString,
        ApplicationServices.AXTextMarkerRangeRef,
        ApplicationServices.AXUIElementRef,
        ApplicationServices.AXTextMarkerRef,
        Quartz.CGPathRef,
    )

    if isinstance(object, AppKit.NSNumber):
        value = int(object)
    elif isinstance(object, AppKit.NSArray) or isinstance(object, list):
        value = [deepconvert_objc(x) for x in object]
    elif isinstance(object, AppKit.NSDictionary) or isinstance(object, dict):
        value = {deepconvert_objc(k): deepconvert_objc(v) for k, v in object.items()}
    elif isinstance(object, strings):
        value = str(object)
    # handle core-foundation class AXValueRef
    elif isinstance(object, ApplicationServices.AXValueRef):
        # convert to dict - note: this object is not iterable
        # TODO: access directly, e.g. via
        # ApplicationServices.AXUIElementCopyAttributeValue
        rep = repr(object)
        x_value = re.search(r"x:([\d.]+)", rep)
        y_value = re.search(r"y:([\d.]+)", rep)
        w_value = re.search(r"w:([\d.]+)", rep)
        h_value = re.search(r"h:([\d.]+)", rep)
        type_value = re.search(r"type\s?=\s?(\w+)", rep)
        value = {
            "x": float(x_value.group(1)) if x_value else None,
            "y": float(y_value.group(1)) if y_value else None,
            "w": float(w_value.group(1)) if w_value else None,
            "h": float(h_value.group(1)) if h_value else None,
            "type": type_value.group(1) if type_value else None,
        }
    elif isinstance(object, Foundation.NSURL):
        value = str(object.absoluteString())
    elif isinstance(object, Foundation.__NSCFAttributedString):
        value = str(object.string())
    elif isinstance(object, Foundation.__NSCFData):
        value = {
            deepconvert_objc(k): deepconvert_objc(v)
            for k, v in plistlib.loads(object).items()
        }
    elif isinstance(object, plistlib.UID):
        value = object.data
    else:
        if object and not (isinstance(object, bool) or isinstance(object, int)):
            logger.warning(
                f"Unknown type: {type(object)} - "
                "Please report this on GitHub: "
                "github.com/MLDSAI/OpenAdapt/issues/new?"
                "assignees=&labels=bug&projects=&template=bug_form.yml&"
                "title=%5BBug%5D%3A+"
            )
            logger.warning(f"{object=}")
    if value:
        value = oa_atomacos._converter.Converter().convert_value(value)
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
    app = oa_atomacos._a11y.AXUIElement.from_pid(pid)
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
