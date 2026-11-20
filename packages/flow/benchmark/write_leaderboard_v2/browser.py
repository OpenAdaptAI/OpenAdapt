"""The screen every agent gets: the app's own web pages in a 1280x800 headless browser.

The browser talks only to the app's front door (``proxy.FrontDoor``), so a
save made by clicking meets the case's fault. The harness signs the agent's
user in by the app's own sign-in page, in a fresh browser context for every
run, and the run starts on the app's start page. No agent
sees or types a password. Navigation to any other origin is aborted in the
browser and logged (version 1's rule).

Only ``chrome-headless-shell`` is launched, from
``OPENADAPT_TEST_CHROMIUM_EXECUTABLE`` when set.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from benchmark.fhir_medplum.leaderboard.browser import VIEWPORT, Screen, off_app
from benchmark.write_leaderboard_v2.apps.base import App


def launch(pw: Any) -> Any:
    exe = os.environ.get("OPENADAPT_TEST_CHROMIUM_EXECUTABLE") or None
    if exe and "headless" not in exe:
        raise SystemExit("OPENADAPT_TEST_CHROMIUM_EXECUTABLE must be chrome-headless-shell")
    return pw.chromium.launch(headless=True, executable_path=exe)


class AppBrowser:
    def __init__(self, app: App) -> None:
        self.app = app
        self._pw: Any = None
        self._browser: Any = None
        self.state: Optional[dict[str, Any]] = None
        self.signed_in_at = 0.0
        self.home_url = ""

    def start(self) -> "AppBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = launch(self._pw)
        return self

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def fresh(self) -> None:
        """Kept for the runner's call order; every run signs in on its own (see ``open``)."""

    def is_signed_out(self, screen: Screen) -> bool:
        return self.app.signed_out(screen.url()) or bool(screen.page.locator(self.app.login_marker).count())

    def open(self) -> tuple[Any, Screen]:
        ctx = self._browser.new_context(viewport=VIEWPORT)
        allowed = (self.app.base,)
        blocked: list[dict[str, Any]] = []

        def stay(route: Any, request: Any) -> None:
            if request.is_navigation_request():
                blocked.append({"a": "blocked_navigation", "url": request.url})
                route.abort("blockedbyclient")
                return
            route.fallback()

        ctx.route(off_app(allowed), stay)
        page = ctx.new_page()
        # The harness signs in, in this run's own context: some apps (OpenEMR)
        # rotate the session on every page, so a saved session can't be reused.
        self.app.login(page)
        home = page.url if self.app.home_path is None else self.app.base + self.app.home_path
        if page.url != home:
            page.goto(home)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        return ctx, Screen(page, log=blocked, allowed=allowed)
