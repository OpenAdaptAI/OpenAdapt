"""The Desktop launcher shares Flow's readiness gate before navigation."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from openadapt_desktop.authoring_runner import AuthoringError, launch_empty_playwright_chromium


class BrowserReadinessError(RuntimeError):
    pass


@pytest.mark.parametrize("ready", [False, True])
def test_authoring_checks_the_owned_browser_before_application_calls(monkeypatch, ready):
    calls = []
    page = SimpleNamespace(goto=lambda url: calls.append(("goto", url)))
    context = SimpleNamespace(cookies=lambda: [], new_page=lambda: page)
    browser = SimpleNamespace(new_context=lambda: context)
    driver = SimpleNamespace(chromium=object(), stop=lambda: calls.append("stop"))
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: driver)),
    )

    def launch(chromium, **options):
        assert chromium is driver.chromium
        assert options == {"headless": False}
        calls.append("readiness")
        if not ready:
            raise BrowserReadinessError("browser_rendering_unavailable")
        return browser

    # Desktop's unit lane need not install Flow; this checks the delegation
    # boundary. Flow separately exercises the real helper and live browser.
    monkeypatch.setitem(
        sys.modules,
        "openadapt_flow._browser_readiness",
        SimpleNamespace(BrowserReadinessError=BrowserReadinessError, launch_rendering_ready=launch),
    )
    if ready:
        assert launch_empty_playwright_chromium("https://synthetic.invalid") is page
        assert calls == ["readiness", ("goto", "https://synthetic.invalid")]
    else:
        with pytest.raises(AuthoringError, match="^browser_rendering_unavailable$"):
            launch_empty_playwright_chromium("https://synthetic.invalid")
        assert calls == ["readiness", "stop"]
