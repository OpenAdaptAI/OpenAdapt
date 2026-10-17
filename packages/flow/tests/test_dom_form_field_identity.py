"""Page-structure (DOM) identity for form fields outside any record row.

A data-entry form lays fields out in label/control groups, not table rows, so
the row rule of ``PlaywrightBackend.structured_text_at`` abstains there and
identity used to fall through to the OCR band. The form-field rule mirrors
that band in the DOM: the group's label text plus the current values of the
group's OTHER controls, never the clicked field's own value and never a
select's option list. These tests pin what it reads, what it refuses to read,
and that the resulting identity still MISMATCHES (fails closed) when a field
the band depends on holds a different value than the run expects.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.identity import verify_structured_identity
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_structured_template,
)

_FORM_HTML = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<style>form > div { display: flex; align-items: center; height: 40px; gap: 6px; }</style>
<form>
 <div class="form-group row" id="g-name" style="height:40px">
  <label>Name:</label>
  <div><input id="fname" style="width:120px">
   <span><input id="lname" style="width:120px"></span></div>
 </div>
 <div class="form-group row" id="g-dob" style="height:40px">
  <label>DOB:</label><div><input id="dob" style="width:120px"></div>
  <label>Sex:</label><div><select id="sex">
   <option>Unassigned</option><option>Female</option><option>Male</option>
  </select></div>
 </div>
 <div class="form-group row" style="height:40px">
  <label>Home Phone:</label><div><input id="phone" style="width:120px"></div>
  <label>Secret:</label><input id="pw" type="password" value="hunter2">
  <input type="hidden" id="h" value="HIDDEN-TOKEN">
 </div>
 <div class="form-group row" style="height:40px">
  <label>Mobile Phone:</label><div><input id="mobile" style="width:120px"></div>
 </div>
 <div style="height:40px"><input id="go" type="submit" value="Create"></div>
 <div style="height:40px"><input id="lonely" style="width:120px"></div>
</form></body></html>"""


@pytest.fixture(scope="module")
def form_page():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(
            viewport={"width": 800, "height": 400}, device_scale_factor=1
        )
        page.set_content(_FORM_HTML, wait_until="load")
        backend = PlaywrightBackend(page)

        def at(selector: str):
            x, y = page.eval_on_selector(
                selector,
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x + r.width / 2, r.y + r.height / 2]; }",
            )
            return backend.structured_text_at(int(x), int(y))

        yield page, at
        browser.close()


def test_field_identity_is_group_labels_plus_other_values(form_page) -> None:
    page, at = form_page
    page.fill("#fname", "Elena")
    page.fill("#lname", "")
    # The clicked field's own value is excluded; its neighbour's is read.
    assert at("#lname") == "Name: Elena"
    page.fill("#lname", "Marsh")
    assert at("#fname") == "Name: Marsh"


def test_select_contributes_selected_option_only(form_page) -> None:
    page, at = form_page
    page.fill("#dob", "1979-06-14")
    page.select_option("#sex", "Female")
    assert at("#dob") == "DOB: Sex: Female"
    assert at("#sex") == "DOB: 1979-06-14 Sex:"


def test_password_and_hidden_values_never_read(form_page) -> None:
    page, at = form_page
    page.fill("#phone", "555-0142")
    identity = at("#phone")
    assert identity == "Home Phone: Secret:"
    assert "hunter2" not in identity and "HIDDEN-TOKEN" not in identity
    assert at("#pw") is None


def test_buttons_and_unlabeled_fields_abstain(form_page) -> None:
    _page, at = form_page
    assert at("#go") is None  # a button's own label stays on the OCR/heal path
    assert at("#lonely") is None  # no labeled group within reach


def test_different_field_group_mismatches(form_page) -> None:
    _page, at = form_page
    recorded = at("#phone")
    live = at("#mobile")
    check = verify_structured_identity(recorded, live)
    assert check is not None and check.status == "mismatch"


def test_template_binds_neighbour_value_to_run_param(form_page) -> None:
    page, at = form_page
    page.fill("#fname", "Elena")
    recorded = at("#lname")
    tmpl = build_identity_template(
        None, structured_identity=recorded, param_examples={"fname": "Elena"}
    )
    assert tmpl is not None and tmpl.structured_params == ["fname"]

    # Run: fname was typed as the run's value -> verified.
    page.fill("#fname", "Nadia")
    ok = verify_structured_template(
        tmpl, at("#lname"), params={"fname": "Nadia"}, param_examples={"fname": "Elena"}
    )
    assert ok is not None and ok.status == "verified"

    # The neighbouring field holds something other than the run's value
    # (wrong record / stale form) -> fail closed.
    page.fill("#fname", "Rosa")
    bad = verify_structured_template(
        tmpl, at("#lname"), params={"fname": "Nadia"}, param_examples={"fname": "Elena"}
    )
    assert bad is not None and bad.status == "mismatch"
