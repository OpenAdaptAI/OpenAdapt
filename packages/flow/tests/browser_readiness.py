"""Protocol model for unit tests whose application browser is otherwise fake.

The production RAF predicate still runs through its ordinary acquisition and
cleanup calls. These deterministic handles model that protocol; they make no
claim about JavaScript execution or native rendering.
"""

from __future__ import annotations

from openadapt_flow import _browser_readiness as readiness


class ReadyBrowserMixin:
    _readiness_failure = ""

    def _readiness_mark(self, name: str) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def new_context(self, **options):
        assert options == {"service_workers": "block"}
        self._readiness_mark("readiness-context")
        return _Context(self)


class _Context:
    def __init__(self, browser):
        self.browser = browser

    def new_page(self):
        self.browser._readiness_mark("readiness-page")
        return _Page(self)

    def close(self):
        self.browser._readiness_mark("readiness-close")


class _Page:
    def __init__(self, context):
        self.context = context
        self.phase = ""
        self.state = None

    def wait_for_function(self, expression, *, polling, timeout, arg=None):
        assert polling == 10 and 0 < timeout <= readiness.READINESS_TIMEOUT_MS
        phase = {
            readiness._START: "start",
            readiness._PROGRESS: "progress",
            readiness._CANCEL: "cancel",
        }[expression]
        self.context.browser._readiness_mark(f"readiness-{phase}")
        if phase == "start":
            assert self.phase == "" and arg is None
        else:
            assert arg is self.state
            if phase == "progress":
                assert self.phase == "start"
            else:
                assert self.phase in ("start", "progress")
        if self.context.browser._readiness_failure == phase:
            raise RuntimeError("synthetic readiness failure")
        self.phase = phase
        handle = _Handle(self.context.browser)
        if phase == "start":
            self.state = handle
        return handle


class _Handle:
    def __init__(self, browser):
        self.browser = browser

    def dispose(self):
        self.browser._readiness_mark("readiness-dispose")
