"""Sign-in and reset hooks that verify.yaml names (``app.session``, ``app.reset``).

They are the engineer's per-app setup code: open a signed-in browser at the
task's start screen, and reset the test copy. The workflow and installation
come from the environment the implementer sets:

- ``OE_WORKFLOW``: pool id;
- ``OE_INSTALLATION``: ``first`` or ``second``.
"""

from __future__ import annotations

import atexit
import os
from typing import Any, Callable

_browser: Any = None


def current() -> tuple[Any, str]:
    from benchmark.onboarding_effort.registry import get

    return get(os.environ["OE_WORKFLOW"]), os.environ.get("OE_INSTALLATION", "first")


def shared_browser() -> Any:
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=os.environ.get("OE_HEADED") != "1")
        _browser = (pw, browser)

        def _stop() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        atexit.register(_stop)
    return _browser[1]


def open_page(wf: Any, installation: str) -> tuple[Any, Callable[[], None]]:
    width, height = wf.installation_viewport(installation)
    context = shared_browser().new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=wf.device_scale(installation),
        ignore_https_errors=True,
    )
    page = context.new_page()
    return page, context.close


def open_session(config: Any = None) -> tuple[Any, Callable[[], None]]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    wf, installation = current()
    page, close = open_page(wf, installation)
    try:
        wf.start(page, installation)
    except Exception:
        close()
        raise
    return PlaywrightBackend(page), close


def reset() -> None:
    wf, installation = current()
    wf.reset(installation)
