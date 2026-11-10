"""Real headless rendering, selected-page isolation, and refusal cleanup."""

from __future__ import annotations

import pytest

from openadapt_flow._browser_readiness import (
    BrowserReadinessError,
    require_rendering_ready,
)
from openadapt_flow._browser_setup import launch_chromium

playwright = pytest.importorskip("playwright.sync_api")


def _snapshot(page):
    return page.evaluate("""() => ({
        globals: Reflect.ownKeys(window).map(String).sort(),
        html: document.documentElement.outerHTML,
        url: location.href, focused: document.hasFocus()
    })""")


def test_headless_owned_and_selected_page_progress_preserves_user_state():
    with playwright.sync_playwright() as pw:
        browser = launch_chromium(pw.chromium, headless=True)
        try:
            assert browser.contexts == []  # blank startup probe is gone
            context = browser.new_context()
            page = context.new_page()
            page.set_content("<title>Synthetic page</title><button>Ready</button>")
            before = _snapshot(page)
            require_rendering_ready(browser, attached_page=page)
            assert _snapshot(page) == before
            assert context.pages == [page]
            assert not page.is_closed()
            assert browser.is_connected()
        finally:
            browser.close()


@pytest.mark.parametrize("mode", ["stalled", "nonadvancing"])
def test_selected_page_unproved_frames_refuse_and_preserve_user_state(mode):
    with playwright.sync_playwright() as pw:
        browser = launch_chromium(pw.chromium, headless=True)
        try:
            page = browser.new_page()
            # A controlled non-rendering scheduling boundary, with live timers.
            if mode == "stalled":
                page.evaluate("""() => {
                    window.requestAnimationFrame = () => 37;
                    window.cancelAnimationFrame = id => {
                        if (id !== 37) throw new Error('foreign callback');
                    };
                }""")
            else:
                page.evaluate("""() => {
                    window.requestAnimationFrame = callback =>
                        setTimeout(() => callback(7), 0);
                    window.cancelAnimationFrame = clearTimeout;
                }""")
            before = _snapshot(page)
            with pytest.raises(BrowserReadinessError, match="rendering_unavailable"):
                require_rendering_ready(browser, attached_page=page)
            assert _snapshot(page) == before
            assert not page.is_closed()
            assert browser.is_connected()
        finally:
            browser.close()
