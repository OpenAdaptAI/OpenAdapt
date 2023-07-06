from pprint import pprint
import pickle

from loguru import logger
import atomacos
import AppKit
import ApplicationServices
import Quartz
import Foundation
import re
import plistlib


def get_active_window_state():
    # pywinctl performance on mac is unusable, see:
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


def get_active_window_meta():
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


def get_active_window(window_meta):
    pid = window_meta["kCGWindowOwnerPID"]
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    error_code, window = ApplicationServices.AXUIElementCopyAttributeValue(
        app_ref, "AXFocusedWindow", None
    )
    if error_code:
        logger.error("Error getting focused window")
        return None
    return window


def get_window_data(window_meta):
    window = get_active_window(window_meta)
    state = dump_state(window)
    return state


def dump_state(element, elements=None):
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
                # for ActionEvents:
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
def deepconvert_objc(object):
    """Convert all contents of an ObjC object to Python primitives."""
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
        if not (isinstance(object, bool) or isinstance(object, int)):
            logger.warning(
                f"Unknown type: {type(object)} - "
                f"Please report this on GitHub: "
                f"https://github.com/MLDSAI/OpenAdapt/issues/new?assignees=&labels=bug&projects=&template=bug_form.yml&title=%5BBug%5D%3A+"
            )
            logger.warning(f"{object=}")

    value = atomacos._converter.Converter().convert_value(value)
    return value


def get_active_element_state(x, y):
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


def main():
    import time
    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    import ipdb; ipdb.set_trace()


if __name__ == "__main__":
    main()
