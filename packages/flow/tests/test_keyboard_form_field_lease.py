"""Guarded keyboard lease on a new-record form (no record row exists).

By default a consequential keyboard action needs a focused element tied to a
record row. A backend may opt in (``keyboard_admits_form_fields``) to admit a
focused, uniquely identified data-entry control instead. These tests pin that
the default still refuses, that the opt-in admits only such controls, and
that it never admits a password field or a duplicated id.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.playwright_backend import PlaywrightBackend

_HTML = """<!doctype html><html><body style="margin:0">
<form>
 <div><label>First:</label><input id="form_fname" style="width:160px;height:30px"></div>
 <div><label>Secret:</label><input id="form_pw" type="password" style="width:160px;height:30px"></div>
 <div><input class="dup" id="dup" style="width:160px;height:30px"></div>
 <div><input class="dup2" id="dup" style="width:160px;height:30px"></div>
</form></body></html>"""


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser in env
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 640, "height": 400})
        page.set_content(_HTML, wait_until="load")
        yield page
        browser.close()


def _arm(page, selector: str, *, admit: bool) -> None:
    backend = PlaywrightBackend(page)
    backend.keyboard_admits_form_fields = admit
    page.focus(selector)
    box = page.locator(selector).bounding_box()
    backend.arm_guarded_keyboard(
        int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    )
    backend.cancel_guarded_keyboard()


def test_default_refuses_unrowed_field(page) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(page, "#form_fname", admit=False)


def test_opt_in_admits_unique_form_field(page) -> None:
    _arm(page, "#form_fname", admit=True)


def test_opt_in_never_admits_password(page) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(page, "#form_pw", admit=True)


def test_opt_in_never_admits_duplicate_id(page) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(page, "input.dup", admit=True)
