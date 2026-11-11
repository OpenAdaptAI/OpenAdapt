"""Drive a workbench through its screens with OpenEnv web actions.

The same action set the OpenEnv environment accepts (``click``, ``fill``,
``type``, ``press``, ``select``, ``goto``, ``wait``, ``done``), run in a
headless Chromium. Nothing here reads the store: a solution that works here
works through the UI alone.
"""

from __future__ import annotations

import time
from typing import Any, Optional


class ActionError(RuntimeError):
    """An action could not be performed on the page."""


class Browser:
    def __init__(self, *, timeout_ms: int = 8000) -> None:
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_readiness import launch_rendering_ready

        self._pw = sync_playwright().start()
        try:
            self._browser = launch_rendering_ready(self._pw.chromium, headless=True)
        except BaseException:
            self._pw.stop()
            raise
        self.timeout_ms = timeout_ms
        self._ctx: Any = None
        self.page: Any = None
        #: App name -> base URL, for ``switch_app`` in a multi-app task.
        self.apps: dict[str, str] = {}

    def open(self, url: str) -> None:
        if self._ctx is not None:
            self._ctx.close()
        self._ctx = self._browser.new_context()
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.base = url.split("/", 3)[:3]
        self.page.goto(url, wait_until="load")

    def run(self, actions: list[dict[str, Any]]) -> Optional[str]:
        """Run actions up to ``done``; the claim, or None if none was made."""

        for a in actions:
            claim = self.act(a)
            if a["kind"] == "done":
                return claim
        return None

    def act(self, a: dict[str, Any]) -> Optional[str]:
        page = self.page
        kind = a["kind"]
        try:
            if kind == "click":
                page.click(a["selector"])
                page.wait_for_load_state("load")
            elif kind == "fill":
                page.fill(a["selector"], a.get("text", ""))
            elif kind == "type":
                page.keyboard.type(a.get("text", ""))
            elif kind == "press":
                page.keyboard.press(a.get("text", "Enter"))
            elif kind == "select":
                page.select_option(a["selector"], a.get("value", ""))
            elif kind == "goto":
                page.goto("/".join(self.base) + a["path"], wait_until="load")
            elif kind == "wait":
                time.sleep(float(a.get("seconds", 1)))
            elif kind == "switch_app":
                url = self.apps[a["app"]]
                self.base = url.split("/", 3)[:3]
                page.goto(url + "/login", wait_until="load")
            elif kind == "done":
                return str(a.get("claim", "success"))
            else:
                raise ActionError(f"unknown action {kind!r}")
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported with the action
            raise ActionError(
                f"{a}: {type(exc).__name__}: {str(exc).splitlines()[0]}"
            ) from exc
        return None

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
