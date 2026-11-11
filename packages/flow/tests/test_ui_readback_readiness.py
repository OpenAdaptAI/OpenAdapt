"""Actual separate-session entry with model browser handles, never a live browser."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import _browser_readiness as readiness
from openadapt_flow.runtime.effects.ui_readback import (
    PlaywrightReadbackDriver,
    ReadbackSessionError,
    UiReadbackSpec,
    resolve_spec,
)

SIGN_IN_URL = "https://reader.synthetic.invalid/sign-in"


class Cancelled(BaseException):
    pass


class Handle:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class Submitter:
    """The sign-in button of a native form that POSTs to the declared URL."""

    def element_handle(self) -> "Submitter":
        return self

    def evaluate(self, *args: Any) -> dict[str, str]:
        return {"method": "post", "target": "", "action": SIGN_IN_URL}

    def owner_frame(self) -> object:
        return self


class Page:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.handles: list[Handle] = []
        self.phases: list[str] = []

    def wait_for_function(self, expression: str, **options: Any) -> Handle:
        phase = {
            readiness._START: "schedule",
            readiness._PROGRESS: "progress",
            readiness._CANCEL: "cleanup",
        }[expression]
        self.phases.append(phase)
        self.context.browser.events.append(phase)
        assert options["polling"] == 10
        assert 0 < options["timeout"] <= readiness.READINESS_TIMEOUT_MS
        if self.context.browser.failure == phase:
            raise RuntimeError("private protocol detail must not escape")
        if self.context.browser.failure == "cancel" and phase == "progress":
            raise Cancelled()
        handle = Handle()
        self.handles.append(handle)
        return handle

    def goto(self, *args: Any, **kwargs: Any) -> None:
        self.context.browser.events.append("goto")

    def evaluate(self, *args: Any) -> bool:
        return True

    def fill(self, *args: Any) -> None:
        self.context.browser.events.append("fill")

    def locator(self, *args: Any) -> Submitter:
        return Submitter()

    def click(self, *args: Any) -> None:
        self.context.browser.events.append("click")

    def wait_for_selector(self, *args: Any, **kwargs: Any) -> None:
        self.context.browser.events.append("signed_in")


class Context:
    def __init__(self, browser: Browser, options: dict[str, Any]) -> None:
        self.browser = browser
        self.options = options
        self.closed = False
        self.page = Page(self)

    def new_page(self) -> Page:
        return self.page

    def cookies(self) -> list[Any]:
        return []

    def set_default_timeout(self, timeout: int) -> None:
        assert timeout == 15000

    def route(self, *args: Any) -> None:
        pass

    def add_init_script(self, script: str) -> None:
        self.browser.events.append("read_only_guard")

    def set_offline(self, value: bool) -> None:
        assert value is True

    def close(self) -> None:
        self.closed = True


class Browser:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.closed = False
        self.contexts: list[Context] = []
        self.events: list[str] = []

    def is_connected(self) -> bool:
        return True

    def new_context(self, **options: Any) -> Context:
        context = Context(self, options)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


def spec(browser: str) -> UiReadbackSpec:
    return UiReadbackSpec.model_validate(
        {
            "browser": browser,
            "headless": True,
            "record_url": "https://reader.synthetic.invalid/record/r1",
            "sign_in": {
                "url": SIGN_IN_URL,
                "post_url": SIGN_IN_URL,
                "username_selector": "#user",
                "password_selector": "#password",
                "submit_selector": "#submit",
                "signed_in_selector": "#signed-in",
                "username_env": "SYNTHETIC_READER_USER",
                "password_env": "SYNTHETIC_READER_PASSWORD",
            },
            "ready_selector": "#record",
            "row_selector": ".row",
            "fields": {"value": {"selector": ".value"}},
            "identity": {"reader": {"selector": "#identity"}, "value": "r1"},
        }
    )


@pytest.mark.parametrize("browser_name", ["chromium", "firefox", "webkit"])
@pytest.mark.parametrize("failure", ["", "schedule", "progress", "cleanup", "cancel"])
def test_dynamic_readback_launch_requires_progress_before_navigation_and_input(
    monkeypatch: pytest.MonkeyPatch, browser_name: str, failure: str
) -> None:
    browser = Browser(failure)
    launches, stopped = [], []

    class Launcher:
        def launch(self, **options: Any) -> Browser:
            launches.append((browser_name, options))
            browser.events.append("launch")
            return browser

    selected = Launcher()
    driver = SimpleNamespace(
        **{browser_name: selected}, stop=lambda: stopped.append(True)
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: driver)),
    )
    session = PlaywrightReadbackDriver(executable_path="/synthetic/browser").open(
        resolve_spec(spec(browser_name)), "synthetic-reader", "synthetic-password"
    )
    if failure:
        error = Cancelled if failure == "cancel" else ReadbackSessionError
        with pytest.raises(error) as caught:
            with session:
                pytest.fail("an unready browser reached application input")
        assert "private protocol detail" not in str(caught.value)
        assert not session.facts.signed_in
        assert not {"goto", "fill", "click", "signed_in"}.intersection(browser.events)
    else:
        with session:
            assert session.facts.own_browser and session.facts.fresh_context
            assert session.facts.signed_in
            assert browser.events == [
                "launch",
                "schedule",
                "progress",
                "cleanup",
                "goto",
                "fill",
                "fill",
                "click",
                "signed_in",
                "read_only_guard",
            ]
        assert browser.contexts[1].options == {
            "service_workers": "block",
            "accept_downloads": False,
        }
    assert launches == [
        (browser_name, {"headless": True, "executable_path": "/synthetic/browser"})
    ]
    assert len(browser.contexts) == (1 if failure else 2)
    assert browser.contexts[0].options == {"service_workers": "block"}
    assert all(context.closed for context in browser.contexts)
    assert all(
        handle.disposed
        for context in browser.contexts
        for handle in context.page.handles
    )
    assert browser.closed and stopped == [True]
    assert session._username == session._password == ""
