import collections
import sys
from loguru import logger
import pywinauto
from pywinauto import Desktop
import time
from pprint import pprint
import pickle


def get_active_window_state(show_data) -> dict:
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
    meta = get_active_window_meta(active_window)
    rectangle_dict = dictify_rect(meta["rectangle"])
    if show_data:
        data = get_element_properties(active_window)
    else:
        logger.info("SHOW_DATA = FALSE")
        data = None
    state = {
        "title": meta["texts"][0],
        "left": meta["rectangle"].left,
        "top": meta["rectangle"].top,
        "width": meta["rectangle"].width(),
        "height": meta["rectangle"].height(),
        "meta": {**meta, "rectangle": rectangle_dict},
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
        dict: A dictionary containing the meta information of the
              active window.
    """
    if not active_window:
        logger.warning(f"{active_window=}")
        return None
    result = active_window.get_properties()
    return result


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
    properties["rectangle"] = dictify_rect(properties["rectangle"])
    return properties


def get_active_window(depth=10, max_width=10, filename=None) -> Desktop:
    """
    Get the active window object.

    Returns:
        Desktop: The active window object.
    """
    app = pywinauto.application.Application(backend="uia").connect(active_only=True)
    window = app.top_window()
    return window


def get_element_properties(element):
    """
    Recursively retrieves the properties of each element and its children.

    Args:
        element: An instance of a custom element class
                 that has the `.get_properties()` and `.children()` methods.

    Returns:
        A nested dictionary containing the properties of each element and its children.
        The dictionary includes a "children" key for each element,
        which holds the properties of its children.

    Example:
        element = Element()
        properties = get_element_properties(element)
        print(properties)
        # Output: {'prop1': 'value1', 'prop2': 'value2',
                  'children': [{'prop1': 'child_value1', 'prop2': 'child_value2',
                  'children': []}]}
    """

    properties = element.get_properties()
    children = element.children()

    if children:
        properties["children"] = [get_element_properties(child) for child in children]

    # Dictify the "rectangle" key
    properties["rectangle"] = dictify_rect(properties["rectangle"])

    return properties


def dictify_rect(rect):
    rect_dict = {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
    }
    return rect_dict


def main():
    """
    Test function for retrieving and inspecting the state of the active window.

    This function is primarily used for testing and debugging purposes.
    """
    import time

    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state)
    import ipdb

    ipdb.set_trace()


if __name__ == "__main__":
    main()
