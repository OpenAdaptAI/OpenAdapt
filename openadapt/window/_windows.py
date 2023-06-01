from loguru import logger
import pywinauto
from pywinauto import Desktop
import time
from pprint import pprint
global active_window
def get_active_window_state():
    active_window = get_active_window()
    meta = get_active_window_meta(active_window)
    data = get_window_data(active_window)
    state = {
        "title": meta['texts'][0],
        "left": meta['rectangle'].left,
        "top": meta['rectangle'].top,
        "width": meta['rectangle'].width(),
        "height": meta['rectangle'].height(),
        # TODO: get window state; see:
        # https://github.com/MLDSAI/OpenAdapt/issues/75#issuecomment-1536762953
        "meta": meta,
        "data": None,
        "window_id": meta['control_id'],
    }
    return state

def get_active_window_meta(active_window) :
    if not active_window:
        logger.warning(f"{active_window=}")
        return None
    logger.info(f"{active_window.get_properties()}=")
    return active_window.get_properties()

def get_active_element_state(x, y):
    return active_window.from_points(x,y)

def get_active_window():
    app = pywinauto.application.Application(backend="uia").connect(active_only=True)
    window = app.active()
    logger.info(f"{window=}")
    return window


def get_window_data(active_window):
    state = get_descendants_info(active_window)
    logger.info(f"{state=}")
    #state = window.dump_window()
    return state

def get_descendants_info(window):
    result = []
    for child in window.descendants():
        properties = child.get_properties()
        info = {
            'friendly classname': properties.get('friendly classname'),
            'texts': properties.get('texts'),
            'rectangle': properties.get('rectangle')
        }
        descendants = get_descendants_info(child)  # Recursively get descendants of the current child
        if descendants:
            info['descendants'] = descendants
        result.append(info)
    return result

def main():
    import pickle
    import time
    time.sleep(1)

    state = get_active_window_state()
    pprint(state)
    pickle.dumps(state)


if __name__ == "__main__":
    main()