"""The page settle waits until a quiet answer is possible, then judges as before.

After a busy probe, the quiescence tracker reports how long until the latest
change is a full quiet window old. No probe can answer quiet sooner, so the
settle sleeps that long instead of probing every 20 ms. These tests pin that
the hint never makes a settle declare quiet earlier than the window allows,
that later activity still restarts the window, and that a page that never
goes quiet still times out unsettled.
"""

from __future__ import annotations

import time

import pytest

from openadapt_flow.vision import settle as settle_mod
from tests.test_page_settle import _backend, browser  # noqa: F401 - fixture


def test_busy_probe_reports_when_quiet_becomes_possible(browser) -> None:  # noqa: F811
    backend, page, ctx = _backend(browser, "<!doctype html><input id=i>")
    try:
        time.sleep(0.3)
        assert backend.page_quiescent(0.1) is True
        page.focus("#i")
        page.keyboard.type("x")
        assert backend.page_quiescent(0.1) is False
        hint = backend.page_quiet_retry_after()
        assert 0.0 < hint <= 0.1
        # Probing before the hint has passed still answers busy.
        assert backend.page_quiescent(0.1) is False
        time.sleep(hint + 0.05)
        assert backend.page_quiescent(0.1) is True
    finally:
        ctx.close()


def test_settle_after_input_is_never_shorter_than_the_window(browser) -> None:  # noqa: F811
    backend, page, ctx = _backend(browser, "<!doctype html><input id=i>")
    try:
        time.sleep(0.3)
        page.focus("#i")
        page.keyboard.type("x")
        typed_at = time.monotonic()
        result = settle_mod.wait_settled_result(backend, timeout_s=2.0)
        assert result.settled and result.mode == "page"
        assert time.monotonic() - typed_at >= 0.1
    finally:
        ctx.close()


def test_activity_during_the_wait_restarts_the_window(browser) -> None:  # noqa: F811
    # A change every 60 ms for 600 ms, then nothing: the settle can't answer
    # before the last change is a full window old.
    html = """<!doctype html><p id=p>0</p><script>
    let n = 0;
    const t = setInterval(() => {
      document.getElementById('p').textContent = String(++n);
      if (n >= 10) clearInterval(t);
    }, 60);
    </script>"""
    backend, page, ctx = _backend(browser, html)
    try:
        result = settle_mod.wait_settled_result(backend, timeout_s=3.0)
        assert result.settled
        assert page.evaluate("document.getElementById('p').textContent") == "10"
    finally:
        ctx.close()


def test_page_that_never_goes_quiet_still_times_out(browser) -> None:  # noqa: F811
    html = """<!doctype html><p id=p>0</p><script>
    let n = 0;
    setInterval(() => { document.getElementById('p').textContent = String(++n); }, 30);
    </script>"""
    backend, _page, ctx = _backend(browser, html)
    try:
        start = time.monotonic()
        result = settle_mod.wait_settled_result(backend, timeout_s=0.6)
        assert result.settled is False
        assert time.monotonic() - start < 1.5
    finally:
        ctx.close()


class _HintBackend:
    def __init__(self, hint) -> None:
        self._hint = hint

    def page_quiet_retry_after(self):
        return self._hint


@pytest.mark.parametrize(
    "hint, expected",
    [
        (0.07, 0.07),
        (0.0, settle_mod.PAGE_POLL_S),
        (None, settle_mod.PAGE_POLL_S),
        (True, settle_mod.PAGE_POLL_S),
        ("0.5", settle_mod.PAGE_POLL_S),
    ],
)
def test_poll_wait_uses_only_a_numeric_hint(hint, expected) -> None:
    wait = settle_mod._next_page_poll_s(_HintBackend(hint), time.monotonic() + 5.0)
    assert wait == pytest.approx(expected, abs=1e-3)


def test_poll_wait_never_passes_the_deadline() -> None:
    wait = settle_mod._next_page_poll_s(_HintBackend(0.09), time.monotonic() + 0.01)
    assert wait <= 0.011
