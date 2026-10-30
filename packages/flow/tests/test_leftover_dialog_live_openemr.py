"""Live OpenEMR: a dialog one item leaves open does not reach the next item.

The shared runner (``benchmark/lifecycle/arms/openemr_ui.py``) keeps one
signed-in browser across items and gives each item a fresh New Patient tab.
OpenEMR opens its dialogs, such as the New Patient duplicate check, in the tab
shell rather than in the form tab. Before this fix, a duplicate-check popup
left open at the end of one item stayed over the next item's form and blocked
every click (incremental-value run 2, chunk A1, iv-038 onwards).

The test opens the duplicate-check popup on a made-up patient and never
confirms it, so nothing is saved. It then starts the next item the way the
harness does and checks that the form can be used.

The module is skipped unless ``OPENEMR_LEFTOVER_DIALOG_LIVE=1``. It needs the
local fixture up and bootstrapped (``benchmark/openemr_local``; it does not
start it), with ``LIFECYCLE_OPENEMR_PROJECT``, ``LIFECYCLE_UI_PORT``,
``LIFECYCLE_API_PORT`` and ``LIFECYCLE_OPENEMR_STATE`` pointing at it, and
Playwright's Chromium installed. ``benchmark`` must be on ``PYTHONPATH``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEMR_LEFTOVER_DIALOG_LIVE") != "1",
    reason="live OpenEMR: set OPENEMR_LEFTOVER_DIALOG_LIVE=1 with the local fixture up",
)

OPEN_DIALOGS = (
    "() => [...document.querySelectorAll('.modal')]"
    ".filter(e => !e.classList.contains('product-registration-modal'))"
    ".filter(e => e.offsetWidth || e.offsetHeight || e.getClientRects().length).length"
)


@pytest.fixture
def session(tmp_path: Path):
    from incremental_value.run import env_for
    from lifecycle.apps import OpenEMRApp
    from lifecycle.arms.openemr_ui import OpenEMRSession

    s = OpenEMRSession(env_for(OpenEMRApp(), tmp_path), headless=True)
    s.start()
    s.sign_in()
    yield s
    s.close()


def _open_duplicate_check(s) -> None:
    s.blank_form()
    f = s.form_frame()
    f.locator("#form_fname").fill("Leftover")
    f.locator("#form_lname").fill("Dialogtest")
    f.locator("#form_DOB").fill("1950-01-04")
    f.locator("#form_sex").select_option(label="Female")
    f.locator("#create").click()
    s.page.wait_for_function(OPEN_DIALOGS, timeout=30_000)


def test_a_popup_left_open_is_gone_at_the_next_item(session) -> None:
    _open_duplicate_check(session)
    assert session.page.evaluate(OPEN_DIALOGS) >= 1

    prep = session.blank_form()

    assert prep["cleared_dialogs"] >= 1
    assert prep["signed_in_again"] is False, session.log
    assert session.page.evaluate(OPEN_DIALOGS) == 0
    session.form_frame().locator("#form_fname").click(timeout=5_000)


def test_a_clean_item_start_does_not_reload(session) -> None:
    session.blank_form()
    url = session.page.url

    prep = session.blank_form()

    assert prep["cleared_dialogs"] == 0
    assert session.page.url == url
    session.form_frame().locator("#form_fname").click(timeout=5_000)
