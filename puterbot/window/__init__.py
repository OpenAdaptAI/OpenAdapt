import sys

from loguru import logger


if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform in ("win32", "linux"):
    # TOOD: implement linux
    from . import _windows as impl
else:
    raise Exception(f"Unsupposed {sys.platform=}")


def get_active_window_state():
    # TODO: save window identifier (a window's title can change, or
    # multiple windows can have the same title)
    try:
        return impl.get_active_window_state()
    except Exception as exc:
        logger.warning(f"{exc=}")
        return None
