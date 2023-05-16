"""
https://github.com/daveenguyen/atomacos/blob/master/atomacos/_macos.py
"""

from pprint import pprint

from loguru import logger
import atomacos
import AppKit
import ApplicationServices
import Quartz


def get_active_window_state():
    active_window_meta = get_active_window_meta()
    active_window_data = get_window_data(active_window_meta)
    rval = {
        "meta": active_window_meta,
        "data": active_window_data,
    }
    rval = deepconvert_objc(rval)
    return rval


def get_active_window_meta():
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListExcludeDesktopElements | Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    active_windows_info = [win for win in windows if win['kCGWindowLayer'] == 0]
    active_window_info = active_windows_info[0]
    return active_window_info


def get_window_data(window_meta):
    pid = window_meta["kCGWindowOwnerPID"]
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    error_code, window = ApplicationServices.AXUIElementCopyAttributeValue(
        app_ref, 'AXFocusedWindow', None
    )
    if error_code:
        logger.error("Error getting focused window")
        return None
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
        error_code, attr_names = ApplicationServices.AXUIElementCopyAttributeNames(element, None)
        if attr_names:
            state = {}
            for attr_name in attr_names:
                # don't traverse back up
                if "parent" in attr_name.lower():
                    continue
                error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(
                    element, attr_name, None,
                )
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


def main():
    import pickle
    import time
    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state)
    import ipdb; ipdb.set_trace()


if __name__ == "__main__":
    main()
