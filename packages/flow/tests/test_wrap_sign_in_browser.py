"""PlaywrightBackend finds sign-in fields from the page's markup, in Chromium.

Covers the cases a fake backend can't: a hidden decoy password before the
real fields (an autofill trap), a password inside an open shadow root, and a
form with no password.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(300)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def _kinds(browser: Any, html: str, selectors: list[str]) -> list[Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.set_content(html)
        backend = PlaywrightBackend(page)
        out = []
        for selector in selectors:
            page.locator(selector).focus()
            found = backend.focused_field_credential()
            out.append(found["kind"] if found else None)
        return out
    finally:
        page.close()


def test_a_hidden_decoy_password_does_not_hide_the_username(browser: Any) -> None:
    html = """<form>
      <input id="trap" type="password" style="display:none">
      <input id="off" type="password" style="position:absolute;left:-9999px">
      <input id="ghost" type="password" style="opacity:0">
      <label>Username <input id="user" type="text"></label>
      <label>Password <input id="pass" type="password"></label>
      <label>Note <input id="note" type="text"></label>
    </form>"""
    assert _kinds(browser, html, ["#user", "#pass", "#note"]) == [
        "username",
        "password",
        None,
    ]


def test_autocomplete_names_the_fields(browser: Any) -> None:
    html = """<form>
      <input id="email" type="email" autocomplete="username">
      <input id="other" type="text">
      <input id="new" type="password" autocomplete="new-password">
    </form>"""
    assert _kinds(browser, html, ["#email", "#other", "#new"]) == [
        "username",
        "username",
        "new-password",
    ]


def test_a_password_in_a_shadow_root_is_found(browser: Any) -> None:
    html = """<div id="host"></div><script>
      const root = document.getElementById('host').attachShadow({mode: 'open'});
      root.innerHTML = '<input id="pw" type="password">';
    </script>"""
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.set_content(html)
        page.locator("#pw").focus()  # Playwright pierces open shadow roots
        found = PlaywrightBackend(page).focused_field_credential()
        assert found is not None and found["kind"] in ("password", "unknown")
    finally:
        page.close()


def test_a_form_without_a_password_has_no_credentials(browser: Any) -> None:
    html = """<form><input id="a" type="text"><input id="b" type="email"></form>"""
    assert _kinds(browser, html, ["#a", "#b"]) == [None, None]
