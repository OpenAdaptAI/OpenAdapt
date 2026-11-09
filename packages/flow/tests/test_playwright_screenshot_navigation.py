"""A capture that overlaps a navigation is retried, other failures are not.

Chromium answers ``Page.captureScreenshot`` with "Unable to capture
screenshot" (or stalls to a timeout) when a navigation commits mid-capture,
for example right after a form POST that redirects. The recorder's settle
loop captures right after the click, so this flaked the Claude Code demo
build in CI. The screenshot guard must still run before every attempt.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright")

from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402


@pytest.fixture(scope="module")
def backend_and_close():
    backend, close = PlaywrightBackend.launch("data:text/html,<p>hello</p>")
    try:
        yield backend
    finally:
        close()


class _FlakyPage:
    def __init__(self, page: Any, errors: list[Exception]) -> None:
        self._page = page
        self._errors = errors
        self.calls = 0

    def screenshot(self, **kwargs: Any) -> bytes:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._page.screenshot(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)


def _swap(backend: PlaywrightBackend, flaky: _FlakyPage) -> Any:
    real = backend.page
    backend.page = flaky  # type: ignore[assignment]
    return real


def test_capture_during_navigation_is_retried(backend_and_close) -> None:
    backend = backend_and_close
    flaky = _FlakyPage(
        backend.page,
        [
            Exception(
                "Page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot"
            )
        ],
    )
    guard_calls: list[int] = []
    real = _swap(backend, flaky)
    previous_guard = backend._screenshot_guard
    backend._screenshot_guard = lambda: guard_calls.append(1)
    try:
        png = backend.screenshot()
    finally:
        backend.page = real
        backend._screenshot_guard = previous_guard
    assert png.startswith(b"\x89PNG")
    assert flaky.calls == 2
    assert len(guard_calls) == 2  # the guard ran before each attempt


def test_unrelated_capture_error_propagates(backend_and_close) -> None:
    backend = backend_and_close
    flaky = _FlakyPage(backend.page, [ValueError("something else")])
    real = _swap(backend, flaky)
    try:
        with pytest.raises(ValueError):
            backend.screenshot()
    finally:
        backend.page = real
    assert flaky.calls == 1
