"""Size the window of a separately started Chromium the way a person would.

Tests that stand in for a person's signed-in browser start Chromium
themselves and attach to it over DevTools. The size the page sees must be set
on the real window, not emulated:

* ``Emulation.setDeviceMetricsOverride`` belongs to the DevTools session that
  set it. Chromium 156 (Playwright 1.64) drops it when that session detaches,
  so a size emulated by a setup helper is gone before the run or recording
  under test attaches. Chromium 153 kept the emulated size, and tests came to
  depend on that.
* ``--window-size`` sets the outer window. The CSS viewport inside it depends
  on the browser's own toolbar height, which differs by version: 1280x800
  gives 1280x713 in Chromium 125, 1280x657 in 153 and 1280x600 in 156.

This is a test helper (leading underscore: pytest does not collect it).
"""

from __future__ import annotations

from typing import Any


def resize_tab_viewport(page: Any, width: int, height: int) -> None:
    """Resize ``page``'s browser window so its CSS viewport is width x height.

    ``Browser.setContentsSize`` sizes the window by its contents, so the result
    does not depend on the toolbar height. Older Chromium (125, for example)
    lacks it; there the window bounds are set from the measured toolbar size.
    Either way the new size outlives the DevTools connection, and the call
    returns only once the page reports the requested size.
    """

    session = page.context.new_cdp_session(page)
    try:
        window = session.send("Browser.getWindowForTarget")
        try:
            session.send(
                "Browser.setContentsSize",
                {"windowId": window["windowId"], "width": width, "height": height},
            )
        except Exception as exc:
            if "setContentsSize" not in str(exc):
                raise
            live = page.evaluate("() => [window.innerWidth, window.innerHeight]")
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {
                        "width": window["bounds"]["width"] + width - live[0],
                        "height": window["bounds"]["height"] + height - live[1],
                    },
                },
            )
    finally:
        session.detach()
    page.wait_for_function(
        "([w, h]) => window.innerWidth === w && window.innerHeight === h",
        arg=[width, height],
        timeout=10_000,
    )
