from loguru import logger
import pygetwindow as pgw


def get_active_window_state():
    """ """
    window = pgw.getActiveWindow()
    if not window:
        logger.warning(f"{window=}")
        return None
    title = window.title
    geometry = window.box
    left, top, width, height = geometry
    state = {
        "title": title,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        # TODO: get window state; see:
        # https://github.com/MLDSAI/OpenAdapt/issues/75#issuecomment-1536762953
        "meta": None,
        "data": None,
        "window_id": None,
    }
    return state


def get_element_at_position(x, y):
    """

    :param x: param y:
    :param y: 

    """
    # TODO
    return None
