"""Tests for the MockMed demo app: server, screens, and drift modes.

Runs headless chromium against a localhost ephemeral-port server.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from openadapt_flow.mockmed.server import serve

NOTE = "Patient stable, follow up in two weeks after triage assessment"


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    url, stop = serve(port=0)
    yield url
    stop()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser: Browser) -> Iterator[Page]:
    page = browser.new_page(
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    yield page
    page.close()


def login(page: Page, url: str) -> None:
    """Sign in and land on the tasks screen."""
    page.goto(url)
    page.fill("#username", "nurse.demo")
    page.fill("#password", "mockmed-demo-pass")
    page.click("#signin")
    page.wait_for_selector("#tasks-table")


def open_first_patient(page: Page) -> None:
    page.locator(".open-btn").first.click()
    page.wait_for_selector("#new-encounter")


def goto_encounter(page: Page) -> None:
    page.click("#new-encounter")
    page.wait_for_selector("#save-encounter")


# -- server -------------------------------------------------------------------


def test_serve_returns_url_and_stop() -> None:
    url, stop = serve(port=0)
    body = urllib.request.urlopen(url, timeout=5).read().decode()
    assert "MockMed" in body
    assert "app.js" in body
    stop()
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(url, timeout=2)


def test_serve_uses_ephemeral_ports() -> None:
    url_a, stop_a = serve(port=0)
    url_b, stop_b = serve(port=0)
    try:
        assert url_a != url_b
    finally:
        stop_a()
        stop_b()


# -- screens ------------------------------------------------------------------


def test_login_screen_renders(page: Page, server_url: str) -> None:
    page.goto(server_url)
    assert page.locator("#username").is_visible()
    assert page.locator("#password").is_visible()
    assert page.locator("#signin").inner_text() == "Sign In"
    # Font floor: base font is >= 14px.
    size = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    assert size >= 14


def test_full_flow_all_screens_reachable(page: Page, server_url: str) -> None:
    login(page, server_url)

    # Tasks screen: table with fake patients and per-row Open buttons.
    assert "Referral Tasks" in page.locator("h1").inner_text()
    body_text = page.locator("#tasks-table").inner_text()
    assert "Jane Sample" in body_text
    assert "Alex Testcase" in body_text
    assert page.locator(".open-btn").count() == 3
    assert page.locator(".open-btn").first.inner_text() == "Open"

    # Patient screen: banner + New Encounter + encounters list area.
    open_first_patient(page)
    assert "Jane Sample" in page.locator("#patient-banner").inner_text()
    assert page.locator("#no-encounters").is_visible()

    # Encounter screen: segmented BUTTONS (no native <select>), note, save.
    goto_encounter(page)
    assert page.locator("select").count() == 0
    triage = page.locator("#type-triage")
    consult = page.locator("#type-consult")
    assert triage.evaluate("el => el.tagName") == "BUTTON"
    assert consult.evaluate("el => el.tagName") == "BUTTON"
    assert triage.inner_text() == "Triage"
    assert consult.inner_text() == "Consult"
    assert page.locator("#save-encounter").inner_text() == "Save Encounter"

    triage.click()
    assert "selected" in (triage.get_attribute("class") or "")

    page.fill("#note", NOTE)
    page.click("#save-encounter")

    # Back on the patient screen with the saved banner + encounter listed.
    page.wait_for_selector("#saved-banner")
    banner = page.locator("#saved-banner").inner_text()
    assert banner == "Encounter saved — " + NOTE[:40]
    assert page.locator("#encounter-list .enc-item").count() == 1
    assert "Triage" in page.locator("#encounter-list").inner_text()
    assert page.evaluate("location.hash") == "#patient/p1"


def test_no_css_transitions_or_animations(page: Page, server_url: str) -> None:
    page.goto(server_url)
    props = page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('#signin'));"
        " return [s.transitionDuration, s.animationName]; }"
    )
    assert props[0] in ("0s", "")
    assert props[1] in ("none", "")


# -- drift modes ----------------------------------------------------------------


def test_drift_theme_dark_palette(page: Page, server_url: str) -> None:
    page.goto(server_url)
    default_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.goto(server_url + "?drift=theme")
    themed_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert themed_bg != default_bg
    # Dark palette: all RGB channels low.
    channels = [
        int(c) for c in themed_bg.replace("rgb(", "").replace(")", "").split(",")
    ]
    assert all(c < 80 for c in channels[:3])
    assert page.evaluate("document.body.classList.contains('drift-theme')")


def _button_boxes(page: Page, url: str, query: str) -> tuple[dict, dict]:
    """Navigate to patient + encounter screens; return both button boxes."""
    login(page, url + query)
    open_first_patient(page)
    new_enc_box = page.locator("#new-encounter").bounding_box()
    goto_encounter(page)
    save_box = page.locator("#save-encounter").bounding_box()
    assert new_enc_box is not None and save_box is not None
    return new_enc_box, save_box


def test_drift_move_relocates_buttons(page: Page, server_url: str) -> None:
    new_default, save_default = _button_boxes(page, server_url, "")
    new_moved, save_moved = _button_boxes(page, server_url, "?drift=move")
    # Both buttons relocate to the opposite side of their container.
    assert abs(new_moved["x"] - new_default["x"]) > 200
    assert abs(save_moved["x"] - save_default["x"]) > 200
    # Vertical position unchanged (same container, same row).
    assert abs(new_moved["y"] - new_default["y"]) < 2
    assert abs(save_moved["y"] - save_default["y"]) < 2


def test_drift_rename_keeps_default_positions(page: Page, server_url: str) -> None:
    new_default, save_default = _button_boxes(page, server_url, "")

    login(page, server_url + "?drift=rename")
    assert page.locator(".open-btn").first.inner_text() == "View"
    open_first_patient(page)
    new_renamed = page.locator("#new-encounter").bounding_box()
    goto_encounter(page)
    save_btn = page.locator("#save-encounter")
    assert save_btn.inner_text() == "Submit Encounter"
    save_renamed = save_btn.bounding_box()

    assert new_renamed is not None and save_renamed is not None
    # Renamed buttons must stay in the SAME positions as default so the
    # geometry rung can resolve them.
    for renamed, default in ((new_renamed, new_default), (save_renamed, save_default)):
        assert abs(renamed["x"] - default["x"]) < 2
        assert abs(renamed["y"] - default["y"]) < 2
        assert abs(renamed["width"] - default["width"]) < 2
        assert abs(renamed["height"] - default["height"]) < 2


def test_drift_modal_blocks_saved_banner(page: Page, server_url: str) -> None:
    login(page, server_url + "?drift=modal")
    open_first_patient(page)
    goto_encounter(page)
    page.click("#type-triage")
    page.fill("#note", NOTE)
    page.click("#save-encounter")

    page.wait_for_selector("#survey-modal")
    assert "Survey" in page.locator("#survey-modal h2").inner_text()
    # The banner never appears and we never navigate back to the patient.
    assert page.locator("#saved-banner").count() == 0
    assert page.evaluate("location.hash") == "#encounter"
    # The overlay covers the full viewport (blocking).
    overlay = page.locator("#modal-overlay").bounding_box()
    assert overlay is not None
    assert overlay["width"] == 1280
    assert overlay["height"] == 800


def test_drift_survives_hash_navigation(page: Page, server_url: str) -> None:
    login(page, server_url + "?drift=rename")
    assert page.evaluate("location.search") == "?drift=rename"
    assert page.locator(".open-btn").first.inner_text() == "View"

    open_first_patient(page)
    assert page.evaluate("location.search") == "?drift=rename"

    goto_encounter(page)
    assert page.evaluate("location.search") == "?drift=rename"
    assert page.locator("#save-encounter").inner_text() == "Submit Encounter"


def test_drift_combined_modes(page: Page, server_url: str) -> None:
    login(page, server_url + "?drift=theme,rename")
    assert page.evaluate("document.body.classList.contains('drift-theme')")
    assert page.locator(".open-btn").first.inner_text() == "View"


def _sign_in(page: Page, url: str) -> None:
    """Fill credentials and click Sign In (no tasks-screen wait)."""
    page.goto(url)
    page.fill("#username", "nurse.demo")
    page.fill("#password", "mockmed-demo-pass")
    page.click("#signin")


def test_drift_notice_interstitial_until_dismissed(page: Page, server_url: str) -> None:
    _sign_in(page, server_url + "?drift=notice")

    # The tasks screen is replaced by the interstitial...
    page.wait_for_selector("#notice-continue")
    assert page.locator("#tasks-table").count() == 0
    assert "What's New" in page.locator("h1").inner_text()

    # ...until dismissed, after which tasks render normally.
    page.click("#notice-continue")
    page.wait_for_selector("#tasks-table")
    assert page.locator("#notice-continue").count() == 0


def test_drift_modal_once_blocks_first_save_only(page: Page, server_url: str) -> None:
    login(page, server_url + "?drift=modal-once")
    open_first_patient(page)
    goto_encounter(page)
    page.click("#type-triage")
    page.fill("#note", NOTE)

    # First save attempt: intercepted, nothing saved.
    page.click("#save-encounter")
    page.wait_for_selector("#survey-modal")
    assert page.locator("#saved-banner").count() == 0
    assert page.evaluate("location.hash") == "#encounter"

    # Dismiss and save again: the encounter saves normally.
    page.click("#survey-dismiss")
    page.click("#save-encounter")
    page.wait_for_selector("#saved-banner")
    assert "Encounter saved" in page.locator("#saved-banner").inner_text()
    assert "Triage" in page.locator("#encounter-list").inner_text()


def test_drift_reqfield_requires_acuity_before_save(
    page: Page, server_url: str
) -> None:
    login(page, server_url + "?drift=reqfield")
    open_first_patient(page)
    goto_encounter(page)
    page.click("#type-triage")
    page.fill("#note", NOTE)

    # Saving without an acuity shows an inline error; nothing saved.
    page.click("#save-encounter")
    assert (
        page.locator("#save-error").inner_text()
        == "Select an acuity level before saving."
    )
    assert page.locator("#saved-banner").count() == 0
    assert page.evaluate("location.hash") == "#encounter"

    # Selecting an acuity clears the error and lets the save through.
    page.click("#acuity-routine")
    assert page.locator("#save-error").inner_text() == ""
    page.click("#save-encounter")
    page.wait_for_selector("#saved-banner")
    assert "Triage" in page.locator("#encounter-list").inner_text()


def test_drift_reqfield_absent_by_default(page: Page, server_url: str) -> None:
    login(page, server_url)
    open_first_patient(page)
    goto_encounter(page)
    assert page.locator("#acuity-seg").count() == 0


def test_drift_typelabel_relabels_and_swaps_but_keeps_type_value(
    page: Page, server_url: str
) -> None:
    login(page, server_url + "?drift=typelabel")
    open_first_patient(page)
    goto_encounter(page)

    triage = page.locator("#type-triage")
    consult = page.locator("#type-consult")
    assert triage.inner_text() == "Triage Assessment"
    # Order swapped: Consult now sits where Triage used to be.
    t_box, c_box = triage.bounding_box(), consult.bounding_box()
    assert t_box is not None and c_box is not None
    assert c_box["x"] < t_box["x"]

    # Internal type value is unchanged: the saved row still says Triage.
    triage.click()
    page.fill("#note", NOTE)
    page.click("#save-encounter")
    page.wait_for_selector("#saved-banner")
    row = page.locator("#encounter-list .enc-item").inner_text()
    assert row.startswith("Triage —")
    assert "Assessment" not in row


def test_drift_sort_reorders_rows_without_changing_data(
    page: Page, server_url: str
) -> None:
    login(page, server_url + "?drift=sort")

    # Alphabetical by patient name: the recorded target (Jane Sample) is
    # no longer the first row, but every referral is still present.
    rows = page.locator("#tasks-table tbody tr")
    assert rows.count() == 3
    assert "Alex Testcase" in rows.nth(0).inner_text()
    assert "Jane Sample" in rows.nth(1).inner_text()
    assert "Sam Specimen" in rows.nth(2).inner_text()

    # Position-based "first row" automation now opens a different patient.
    page.locator(".open-btn").first.click()
    page.wait_for_selector("#patient-banner")
    assert "Alex Testcase" in page.locator("#patient-banner").inner_text()


def test_drift_sort_absent_by_default(page: Page, server_url: str) -> None:
    login(page, server_url)
    first = page.locator("#tasks-table tbody tr").first.inner_text()
    assert "Jane Sample" in first
