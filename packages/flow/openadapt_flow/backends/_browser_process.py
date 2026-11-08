"""Browser process lifetime, kept free of the image stack.

This module imports neither OpenCV nor the OCR engine, so a caller can start
the browser while those load on another thread.
"""

from __future__ import annotations

import contextlib
import sys

from openadapt_flow.backends._owned import close_owned
from typing import Any, Iterator

#: Browsers kept open by :func:`shared_browser`, keyed by ``headless``.
_SHARED_BROWSER: dict[bool, tuple[Any, Any]] = {}


def _start_browser(headless: bool) -> tuple[Any, Any]:
    """Start Playwright and the selected Chromium (see _browser_setup)."""
    from openadapt_flow._browser_setup import launch_chromium, require_browser_support

    require_browser_support()
    from playwright.sync_api import sync_playwright

    manager = sync_playwright()
    try:
        pw = manager.start()
    except BaseException as error:
        close_owned(
            (lambda: manager.__exit__(type(error), error, error.__traceback__),),
            primary=error,
        )
        raise
    try:
        browser = launch_chromium(pw.chromium, headless=headless)
    except BaseException as error:
        close_owned((pw.stop,), primary=error)
        raise
    return pw, browser


@contextlib.contextmanager
def shared_browser(headless: bool = True) -> Iterator[None]:
    """Reuse one browser process for every ``PlaywrightBackend.launch`` inside.

    Each launch still gets a fresh browser context (no cookies, storage, or
    cache from an earlier run), exactly as a separate browser would; only the
    process start and shutdown are paid once. Single-threaded use only, like
    Playwright's sync API.
    """
    if headless in _SHARED_BROWSER:
        yield
        return
    pw, browser = _start_browser(headless)
    _SHARED_BROWSER[headless] = (pw, browser)
    try:
        yield
    finally:
        _SHARED_BROWSER.pop(headless, None)
        close_owned((browser.close, pw.stop), primary=sys.exc_info()[1])
