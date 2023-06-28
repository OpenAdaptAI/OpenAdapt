import sys

from loguru import logger

SHOW_DATA = True
if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform in ("win32", "linux"):
    # TOOD: implement linux
    from . import _windows as impl
else:
    raise Exception(f"Unsupposed {sys.platform=}")


def get_active_window_data():
    state = get_active_window_state(show_data=SHOW_DATA)
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


def get_active_window_state(show_data):
    # TODO: save window identifier (a window's title can change, or
    # multiple windows can have the same title)
    try:
        return impl.get_active_window_state(show_data)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None


def get_active_element_state(x, y):
    try:
        return impl.get_active_element_state(x, y)
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None
