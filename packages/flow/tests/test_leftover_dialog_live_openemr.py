"""Live OpenEMR: a dialog one item leaves open does not reach the next item.

The shared runner (``benchmark/lifecycle/arms/openemr_ui.py``) keeps one
signed-in browser across items and gives each item a fresh New Patient tab.
OpenEMR opens its dialogs, such as the New Patient duplicate check, in the tab
shell rather than in the form tab. Before this fix, a duplicate-check popup
left open at the end of one item stayed over the next item's form and blocked
every click (incremental-value run 2, chunk A1, iv-038 onwards).

The tests open the duplicate-check popup on a made-up patient and never
confirm it. They start the next item the way the harness does and check that
the form can be used and that no save was sent.

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


@pytest.fixture
def ui():
    from lifecycle.arms import openemr_ui

    return openemr_ui


@pytest.fixture
def session(tmp_path: Path, ui):
    from incremental_value.run import env_for
    from lifecycle.apps import OpenEMRApp

    s = ui.OpenEMRSession(env_for(OpenEMRApp(), tmp_path), headless=True)
    s.start()
    s.sign_in()
    s.saves = []
    s.context.on(
        "request",
        lambda r: s.saves.append(r.url) if r.method == "POST" and ui.SAVE_URL_PART in r.url else None,
    )
    yield s
    s.close()


def _open_duplicate_check(s, ui) -> None:
    s.blank_form()
    f = s.form_frame()
    f.locator("#form_fname").fill("Leftover")
    f.locator("#form_lname").fill("Dialogtest")
    f.locator("#form_DOB").fill("1950-01-04")
    f.locator("#form_sex").select_option(label="Female")
    f.locator("#create").click()
    s.page.wait_for_function(ui._OPEN_DIALOGS, timeout=30_000)


def test_a_popup_left_open_is_gone_at_the_next_item(session, ui) -> None:
    _open_duplicate_check(session, ui)

    prep = session.blank_form()

    assert prep["cleared_dialogs"] >= 1
    assert prep["signed_in_again"] is False, session.log
    assert session.page.evaluate(ui._OPEN_DIALOGS) == 0
    session.form_frame().locator("#form_fname").click(timeout=5_000)
    assert session.saves == []


def test_a_popup_that_will_not_close_means_a_fresh_sign_in(session, ui, monkeypatch) -> None:
    _open_duplicate_check(session, ui)
    monkeypatch.setattr(ui, "_CLOSE_DIALOGS", "() => []")

    prep = session.blank_form()

    assert prep["signed_in_again"] is True
    assert "signed_out" in session.log[-1]["how"]
    assert session.page.evaluate(ui._OPEN_DIALOGS) == 0
    session.form_frame().locator("#form_fname").click(timeout=5_000)
    assert session.saves == []


def test_a_clean_item_start_does_not_reload(session, ui) -> None:
    session.blank_form()
    url = session.page.url

    prep = session.blank_form()

    assert prep["cleared_dialogs"] == 0
    assert session.page.url == url
    session.form_frame().locator("#form_fname").click(timeout=5_000)
