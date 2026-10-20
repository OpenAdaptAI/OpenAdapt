"""The outside agent: a scripted Playwright "agent" that OpenAdapt doesn't control.

It stands in for a developer's own browser agent (a Playwright script,
Playwright MCP, browser-use, a computer-use agent with a Chromium). It opens
MockMed, opens a patient, starts an encounter, picks Triage, types the note
and clicks Save, with ``think_s`` between actions where a model-driven agent
would be thinking. It calls no model.

The only change a developer makes for the guard is the DevTools port on the
browser their agent already launches, marked ``# OpenAdapt guard`` below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["AgentRun", "ScriptedAgent"]


@dataclass
class AgentRun:
    actions: int = 0
    seconds: float = 0.0
    #: What the page said after Save: ``saved`` (the app's banner), ``error``
    #: (the app's error text), ``blocked`` (the guard's notice) or ``nothing``.
    screen_after: str = "nothing"
    notice: Optional[str] = None
    log: list[str] = field(default_factory=list)


class ScriptedAgent:
    """One browser, one tab, driven by a fixed script."""

    def __init__(self, *, port: int, headless: bool = True, think_s: float = 1.0):
        from playwright.sync_api import sync_playwright

        self.think_s = think_s
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=headless,
            args=[f"--remote-debugging-port={port}"],  # OpenAdapt guard
        )
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 800})

    def open(self, url: str) -> None:
        self.page.goto(url)
        self.page.wait_for_selector("#tasks-table")

    def _act(self, run: AgentRun, what: str, fn: Any) -> None:
        time.sleep(self.think_s)
        fn()
        run.actions += 1
        run.log.append(what)

    def add_triage_note(self, *, patient: str, note: str) -> AgentRun:
        """Open ``patient``, write a Triage encounter with ``note``, Save."""

        page = self.page
        run = AgentRun()
        started = time.monotonic()
        self._act(run, f"open {patient}", lambda: page.click(f"#open-{patient}"))
        self._act(run, "new encounter", lambda: page.click("#new-encounter"))
        self._act(run, "triage", lambda: page.click("#type-triage"))
        # Click the field, then type, as a computer-use agent does. (A bare
        # ``fill`` records no focus step; see GUARD_AN_AGENT.md, Limits.)
        self._act(run, "click note", lambda: page.click("#note"))
        self._act(run, "type note", lambda: page.fill("#note", note))
        self._act(run, "save", lambda: page.click("#save-encounter"))
        # Look at the result, as an agent would before reporting back.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            notice = page.query_selector("#openadapt-guard-notice")
            if notice is not None:
                run.notice = notice.inner_text()
                if "blocked" in run.notice:
                    run.screen_after = "blocked"
                    break
            if page.query_selector("#saved-banner"):
                run.screen_after = "saved"
                break
            error = page.query_selector("#save-error")
            if error is not None and error.inner_text().strip():
                run.screen_after = "error"
                break
            page.wait_for_timeout(100)
        notice = page.query_selector("#openadapt-guard-notice")
        if notice is not None:
            run.notice = notice.inner_text()
        run.seconds = round(time.monotonic() - started, 3)
        return run

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()
