"""An ISO date typed into a native date input lands as that date.

Chromium splits ``<input type=date>`` into locale-ordered segments, so a
typed ``1984-03-07`` doesn't land: ``-`` doesn't move to the next segment.
The backend sets an ISO date with ``fill``, which takes the ISO value
whatever the locale. Other text, and text into other fields, is typed as
before. Runs headless against a local page.
"""

from __future__ import annotations

import pytest

_PAGE = """<meta name="openadapt-session-identity" content="{identity}">
<table><tr data-openadapt-identity="record-1"><td>Birth date</td><td>
<input id="d" type="date" aria-label="Birth date">
<input id="t" type="text" aria-label="Note">
<input id="ro" type="date" value="1999-01-01" readonly aria-label="Fixed">
<input id="v" type="date" value="1999-01-01" aria-label="Visit date">
</td></tr></table>
<iframe id="f" srcdoc="<input id=fd type=date>"></iframe>
<div id="host"></div>
<script>
window.changes = 0;
document.getElementById('d').addEventListener('change', () => window.changes++);
const root = document.getElementById('host').attachShadow({mode: 'open'});
root.innerHTML = '<input id="sd" type="date">';
</script>"""


@pytest.fixture
def page_backend():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 800, "height": 400})
        page.set_content(_PAGE.replace("{identity}", "a" * 64))
        page.frame_locator("#f").locator("#fd").wait_for()
        yield PlaywrightBackend(page), page
        browser.close()


def _center(locator) -> tuple[int, int]:
    box = locator.bounding_box()
    assert box is not None
    return (
        int(round(box["x"] + box["width"] / 2)),
        int(round(box["y"] + box["height"] / 2)),
    )


def test_typing_an_iso_date_key_by_key_does_not_land(page_backend):
    # The reason for the fill: plain key-by-key typing leaves the date empty.
    _backend, page = page_backend
    page.focus("#d")
    page.keyboard.type("1984-03-07")
    assert page.input_value("#d") != "1984-03-07"


def test_an_iso_date_lands_in_a_date_input(page_backend):
    backend, page = page_backend
    page.focus("#d")
    backend.type_text("1984-03-07")
    assert page.input_value("#d") == "1984-03-07"
    assert page.evaluate("window.changes") == 1


def test_other_text_and_other_fields_are_typed(page_backend):
    backend, page = page_backend
    page.focus("#t")
    backend.type_text("1984-03-07")
    assert page.input_value("#t") == "1984-03-07"
    page.focus("#d")
    backend.type_text("not a date")
    assert page.input_value("#d") == ""
    assert page.evaluate("window.changes") == 0


def test_an_iso_date_lands_through_the_guarded_lease(page_backend):
    backend, page = page_backend
    field = page.locator("#d")
    field.focus()
    backend.arm_guarded_keyboard(*_center(field))
    receipt = backend.type_text_guarded("2001-12-31", expected_frame_sha256="0" * 64)
    assert receipt.operation == "guarded_dom_type"
    assert field.input_value() == "2001-12-31"
    assert page.evaluate("window.changes") == 1


def test_guarded_non_date_text_is_typed_into_the_lease(page_backend):
    backend, page = page_backend
    field = page.locator("#t")
    field.focus()
    backend.arm_guarded_keyboard(*_center(field))
    backend.type_text_guarded("2001-12-31", expected_frame_sha256="0" * 64)
    assert field.input_value() == "2001-12-31"


class _Target:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate(self, *_args, **_kwargs):
        self.calls.append("evaluate")
        return True

    def fill(self, *_args, **_kwargs):
        self.calls.append("fill")


@pytest.mark.parametrize(
    "text", ["2001-02-30", "\u0661\u0669\u0668\u0664-03-07", "84-03-07", ""]
)
def test_a_text_that_is_not_a_real_ascii_date_never_touches_the_field(text):
    # fill on a date input clears it before rejecting a malformed value, so
    # such text must not reach fill (or even the date-input check).
    from openadapt_flow.backends.playwright_backend import _fill_iso_date

    target = _Target()
    assert _fill_iso_date(target, text) is False
    assert target.calls == []


def test_a_real_iso_date_is_checked_then_filled_once():
    from openadapt_flow.backends.playwright_backend import _fill_iso_date

    target = _Target()
    assert _fill_iso_date(target, "2024-02-29") is True
    assert target.calls == ["evaluate", "fill"]


def test_a_readonly_date_input_is_typed_as_before(page_backend):
    backend, page = page_backend
    page.focus("#ro")
    backend.type_text("2001-12-31")
    assert page.input_value("#ro") == "1999-01-01"


def test_a_date_in_a_frame_or_shadow_root_lands(page_backend):
    backend, page = page_backend
    inner = page.frame_locator("#f").locator("#fd")
    inner.focus()
    backend.type_text("2001-12-31")
    assert inner.input_value() == "2001-12-31"
    shadow = page.locator("#host #sd")
    shadow.focus()
    backend.type_text("1984-03-07")
    assert shadow.input_value() == "1984-03-07"
