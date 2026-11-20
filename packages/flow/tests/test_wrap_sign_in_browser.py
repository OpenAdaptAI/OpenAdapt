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
        "unknown",
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


@pytest.mark.parametrize(
    "trap",
    [
        '<input type="password" style="display:none">',
        '<div style="opacity:0"><input type="password"></div>',
        '<input type="password" style="position:absolute;left:10000px">',
        '<input type="password" style="transform:translateX(9999px)">',
        '<input type="password" style="position:absolute;clip:rect(0,0,0,0)">',
        '<input type="password" style="width:1px;height:1px;clip-path:inset(50%)">',
        '<div style="height:0;overflow:hidden"><input type="password"></div>',
    ],
)
def test_password_candidate_rendering_cannot_expose_the_recorded_username(
    browser: Any,
    tmp_path: Any,
    trap: str,
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    page = browser.new_page()
    try:
        page.set_content(f'<form>{trap}<input id="user"><input type="password"></form>')
        page.locator("#user").focus()
        recorder = Recorder(PlaywrightBackend(page), tmp_path / "recording")
        recorder.withhold_credentials({"user": "private-candidate-user"})
        recorder.type_text("private-candidate-user")
        recorder.finish()
        events = (tmp_path / "recording/events.jsonl").read_text()
        assert "private-candidate-user" not in events
        assert '"secret": true' in events
    finally:
        page.close()


@pytest.mark.parametrize("separate", [False, True])
def test_username_pairing_enters_open_shadow_roots(
    browser: Any, separate: bool
) -> None:
    html = '<div id="a"></div><div id="b"></div><script>'
    html += "const a = document.querySelector('#a').attachShadow({mode:'open'});"
    if separate:
        html += "a.innerHTML = '<input id=\"u\">';"
        html += "document.querySelector('#b').attachShadow({mode:'open'}).innerHTML = '<input id=\"p\" type=\"password\">';"
    else:
        html += 'a.innerHTML = \'<input id="u"><input id="p" type="password">\';'
    html += "</script>"
    assert _kinds(browser, html, ["#u", "#p"]) == ["username", "password"]


@pytest.mark.parametrize("tag", ["div", "span", "secret-widget"])
@pytest.mark.parametrize("delegates_focus", [False, True])
def test_closed_shadow_control_refuses_declared_input_before_recording(
    browser: Any,
    tmp_path: Any,
    tag: str,
    delegates_focus: bool,
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import CredentialFieldError, Recorder

    page = browser.new_page()
    try:
        page.set_content(
            f'<{tag} id="host"></{tag}>'
            + "<script>const root = document.querySelector('#host').attachShadow("
            + "{mode:'closed', delegatesFocus:"
            + str(delegates_focus).lower()
            + '});root.innerHTML = \'<input id="pw" type="password">\';'
            + "window.focusSecret = () => root.querySelector('#pw').focus();</script>"
        )
        page.evaluate("window.focusSecret()")
        backend = PlaywrightBackend(page)
        assert backend.focused_field_credential()["kind"] == "unknown"
        recorder = Recorder(backend, tmp_path / "recording")
        recorder.withhold_credentials({"password": "closed-shadow-secret"})
        with pytest.raises(CredentialFieldError):
            recorder.type_text("closed-shadow-secret")
        assert page.evaluate("document.activeElement.tagName") == tag.upper()
        recorder.finish()
        assert all(
            b"closed-shadow-secret" not in p.read_bytes()
            for p in (tmp_path / "recording").rglob("*")
            if p.is_file()
        )
    finally:
        page.close()


def test_ambiguous_password_pairing_refuses_before_any_declared_value_is_typed(
    browser: Any,
    tmp_path: Any,
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import CredentialFieldError, Recorder

    page = browser.new_page()
    try:
        page.set_content(
            '<form><input id="other"><input type="password" hidden><input id="user"><input type="password"></form>'
        )
        page.locator("#user").focus()
        backend = PlaywrightBackend(page)
        assert backend.focused_field_credential()["kind"] == "unknown"
        recorder = Recorder(backend, tmp_path / "recording")
        recorder.withhold_credentials({"username": "ambiguous-user-sentinel"})
        with pytest.raises(CredentialFieldError):
            recorder.type_text("ambiguous-user-sentinel")
        recorder.finish()
        assert page.locator("#user").input_value() == ""
        assert all(
            b"ambiguous-user-sentinel" not in p.read_bytes()
            for p in (tmp_path / "recording").rglob("*")
            if p.is_file()
        )
    finally:
        page.close()


def test_noneditable_focus_is_not_proof_of_an_ordinary_text_field(browser: Any) -> None:
    html = '<button id="button">Continue</button><textarea id="note"></textarea>'
    assert _kinds(browser, html, ["#button", "#note"]) == ["unknown", None]
