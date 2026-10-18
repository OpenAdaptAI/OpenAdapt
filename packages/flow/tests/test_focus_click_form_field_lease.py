"""Guarded focusing click on a new-record form (no record row exists).

A click that only focuses a field binds an editable target. Without a record
row it is refused unless the backend opts in (``keyboard_admits_form_fields``),
the same opt-in guarded keyboard delivery uses. With the opt-in, a uniquely
identified field is admitted, and so is a field without an id when it sits in
the page's only form. A password field, a duplicated id, and an id-less field
on a page with two forms stay refused.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.playwright_backend import PlaywrightBackend

_ONE_FORM = """<!doctype html><html><body style="margin:0">
<form>
 <div><input id="claim_no" style="width:160px;height:30px"></div>
 <div><input class="noid" style="width:160px;height:30px"></div>
 <div><input id="pw" type="password" style="width:160px;height:30px"></div>
 <div><input class="dup" id="dup" style="width:160px;height:30px"></div>
 <div><input class="dup2" id="dup" style="width:160px;height:30px"></div>
</form></body></html>"""

_TWO_FORMS = """<!doctype html><html><body style="margin:0">
<form><input class="noid" style="width:160px;height:30px"></form>
<form><input class="other" style="width:160px;height:30px"></form>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser in env
            pytest.skip(f"chromium unavailable: {exc}")
        yield browser
        browser.close()


def _arm(browser, html: str, selector: str, *, admit: bool) -> None:
    page = browser.new_page(viewport={"width": 640, "height": 400})
    try:
        page.set_content(html, wait_until="load")
        backend = PlaywrightBackend(page)
        backend.keyboard_admits_form_fields = admit
        box = page.locator(selector).bounding_box()
        backend.arm_guarded_editable_coordinate(
            int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        )
        backend.cancel_guarded_coordinate()
    finally:
        page.close()


def test_default_refuses_an_unrowed_field(browser) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(browser, _ONE_FORM, "#claim_no", admit=False)


def test_opt_in_admits_a_unique_field(browser) -> None:
    _arm(browser, _ONE_FORM, "#claim_no", admit=True)


def test_opt_in_admits_an_id_less_field_on_the_only_form(browser) -> None:
    _arm(browser, _ONE_FORM, "input.noid", admit=True)


def test_opt_in_refuses_an_id_less_field_when_two_forms_exist(browser) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(browser, _TWO_FORMS, "input.noid", admit=True)


def test_opt_in_never_admits_a_password(browser) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(browser, _ONE_FORM, "#pw", admit=True)


def test_opt_in_never_admits_a_duplicate_id(browser) -> None:
    with pytest.raises(StructuralResolutionRefused):
        _arm(browser, _ONE_FORM, "input.dup", admit=True)
