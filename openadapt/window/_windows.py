from loguru import logger
import pywinauto
from pywinauto import Desktop
import time
from pprint import pprint
import pickle


def get_active_window_state():
    active_window = get_active_window()
    meta = get_active_window_meta(active_window)
    data = get_window_data(active_window)
    state = {
        "title": meta["texts"][0],
        "left": meta["rectangle"].left,
        "top": meta["rectangle"].top,
        "width": meta["rectangle"].width(),
        "height": meta["rectangle"].height(),
        "meta": meta,
        "data": None,
        "window_id": meta["control_id"],
    }
    return state


def get_active_window_meta(active_window):
    if not active_window:
        logger.warning(f"{active_window=}")
        return None
    logger.info(f"{active_window.get_properties()}=")
    return active_window.get_properties()


def get_active_element_state(x, y):
    active_window = get_active_window()
    active_element = active_window.from_point(x, y)
    properties = active_element.get_properties()
    return properties


def get_active_window():
    app = pywinauto.application.Application(backend="uia").connect(active_only=True)
    window = app.active()
    logger.info(f"{window=}")
    return window


def get_window_data(active_window):
    state = get_descendants_info(active_window)
    logger.info(f"{state=}")
    # state = window.dump_window()
    return state


def get_descendants_info(window):
    result = []
    for child in window.descendants():
        info = child.get_properties()
        if info not in result:
            result.append(info)
        descendants = get_descendants_info(child)
        for descendant in descendants:
            if descendant not in result:
                result.append(descendant)
    return result


def main():
    import time

    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state)
    import ipdb

    ipdb.set_trace()


if __name__ == "__main__":
    main()
