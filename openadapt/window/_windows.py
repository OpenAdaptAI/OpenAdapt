from loguru import logger
import pywinauto
from pywinauto import Desktop
import time
from pprint import pprint
import pickle


def get_active_window_state() -> dict:
    """
    Get the state of the active window.

    Returns:
        dict: A dictionary containing the state of the active window.
            The dictionary has the following keys:
                - "title": Title of the active window.
                - "left": Left position of the active window.
                - "top": Top position of the active window.
                - "width": Width of the active window.
                - "height": Height of the active window.
                - "meta": Meta information of the active window.
                - "data": None (to be filled with window data).
                - "window_id": ID of the active window.
    """
    # catch specific exceptions, when except happens do log.warning
    try:
        active_window = get_active_window()
    except RuntimeError as e:
        logger.warning(e)
        return {}
    logger.info(f"{active_window=}")
    meta = get_active_window_meta(active_window)
    logger.info(f"{meta=}")
    data = get_descendants_info(active_window)
    logger.info(f"{data=}")
    state = {
        "title": meta["texts"][0],
        "left": meta["rectangle"].left,
        "top": meta["rectangle"].top,
        "width": meta["rectangle"].width(),
        "height": meta["rectangle"].height(),
        "meta": meta,
        "data": data,
        "window_id": meta["control_id"],
    }
    try:
        pickle.dumps(state)
    except Exception as exc:
        logger.warning(f"{exc=}")
        state.pop("data")
    return state


def get_active_window_meta(active_window) -> dict:
    """
    Get the meta information of the active window.

    Args:
        active_window: The active window object.

    Returns:
        dict: A dictionary containing the meta information of the active window.
    """
    if not active_window:
        logger.warning(f"{active_window=}")
        return None
    return active_window.get_properties()


def get_active_element_state(x: int, y: int):
    """
    Get the state of the active element at the given coordinates.

    Args:
        x (int): The x-coordinate.
        y (int): The y-coordinate.

    Returns:
        dict: A dictionary containing the properties of the active element.
    """
    active_window = get_active_window()
    active_element = active_window.from_point(x, y)
    properties = active_element.get_properties()
    return properties


def get_active_window() -> Desktop:
    """
    Get the active window object.

    Returns:
        Desktop: The active window object.
    """
    app = pywinauto.application.Application(backend="uia").connect(active_only=True)
    logger.info(f"{app=}")
    window = app.active()
    logger.info(f"{window=}")
    return window


def get_descendants_info(window):
    """
    Get the properties of the descendants of the given window.

    Args:
        window: The window object.

    Returns:
        list: A list containing the properties of the descendants.
    """

    result = window.get_properties()
    result["children"] = []
    for child in window.descendants():
        if child in result["children"]:
            import ipdb

            ipdb.set_trace()
        result["children"].append(child.get_properties())
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
