"""Exception detectors on synthetic pages: a code prompt, a mid-task sign-in,
a modal, a stall and a duplicate, plus the weak signal for each (it must
still ask) and ordinary pages (they must not)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.exceptions import detectors as d
from openadapt_flow.exceptions.detectors import Observation, detect, elements_from_html

FORM = """<html><body><h1>New entry</h1><form>
<label for="title">Title</label><input id="title" name="title" type="text">
<label for="when">Date</label><input id="when" name="when" type="date">
<button type="submit">Save</button></form></body></html>"""

OTP_MARKED = """<html><body><h1>Check your phone</h1><form>
<label for="otc">Code</label>
<input id="otc" name="otc" autocomplete="one-time-code" inputmode="numeric">
<button>Verify</button></form></body></html>"""

OTP_WEAK = """<html><body><p>We sent you a message.</p><form>
<label for="v">Verification</label><input id="v" name="v" maxlength="6">
<button>Next</button></form></body></html>"""

OTP_TEXT_ONLY = "<html><body><p>Enter the code we sent to your phone.</p><form><input name='x'></form></body></html>"

SIGN_IN = """<html><body><h1>Sign in</h1><form>
<label for="u">Username</label><input id="u" name="u">
<label for="p">Password</label><input id="p" name="p" type="password">
<button>Sign in</button></form></body></html>"""

MODAL = """<html><body>""" + FORM + """
<div role="dialog" aria-modal="true"><p>Your changes may be lost.</p><button>OK</button></div>
</body></html>"""

HIDDEN_MODAL = """<html><body>""" + FORM + """
<div role="dialog" aria-modal="true" hidden><p>Later</p></div></body></html>"""


def _obs(html: str, **fields) -> Observation:
    elements, text = elements_from_html(html)
    return Observation(screen_text=text, elements=elements, **fields)


# -- second factor ------------------------------------------------------------------


@pytest.mark.parametrize("page", [OTP_MARKED, OTP_WEAK, OTP_TEXT_ONLY])
def test_a_code_prompt_is_a_second_factor(page):
    assert detect(_obs(page)) == "second_factor"


def test_screen_text_alone_still_asks():
    assert d.second_factor((), "Two-factor authentication: enter the code") == "second_factor"


def test_an_ordinary_form_is_not_a_second_factor():
    assert d.second_factor(*elements_from_html(FORM)) is None
    assert detect(_obs(FORM)) is None


def test_a_hidden_code_field_is_not_on_screen():
    page = "<html><body><form><input autocomplete='one-time-code' type='hidden'></form></body></html>"
    assert d.second_factor(*elements_from_html(page)) is None


# -- session expired ----------------------------------------------------------------


def test_a_sign_in_page_before_any_save_screen_is_the_tasks_start():
    assert detect(_obs(SIGN_IN, save_eligible_seen=False)) is None


def test_a_sign_in_page_after_a_save_screen_is_a_session_expiry():
    assert detect(_obs(SIGN_IN, save_eligible_seen=True)) == "session_expired"


def test_words_alone_after_a_save_screen_still_ask():
    page = "<html><body><p>Your session has expired.</p></body></html>"
    assert detect(_obs(page, save_eligible_seen=True)) == "session_expired"


def test_accessibility_secure_field_counts_as_a_password():
    nodes = [{"role": "AXSecureTextField", "name": "Password"}]
    elements = d.elements_from_accessibility(nodes)
    assert d.session_expired(elements, save_eligible_seen=True) == "session_expired"


# -- dialogs ------------------------------------------------------------------------


def test_a_modal_over_the_form_is_an_unexpected_dialog():
    assert detect(_obs(MODAL)) == "unexpected_dialog"


def test_a_hidden_modal_is_not_in_the_way():
    assert detect(_obs(HIDDEN_MODAL)) is None


def test_a_dialog_the_step_expects_is_not_an_exception():
    assert detect(_obs(MODAL, expected_dialog=True)) is None


def test_a_non_modal_dialog_away_from_the_target_is_not_in_the_way():
    dialog = {"role": "dialog", "bbox": [900, 0, 200, 100], "z": 5}
    target = {"tag": "button", "bbox": [10, 500, 80, 30], "z": 0}
    assert d.unexpected_dialog([dialog], target=target) is None


def test_a_non_modal_dialog_covering_the_target_is_in_the_way():
    dialog = {"role": "dialog", "bbox": [0, 400, 400, 300], "z": 5}
    target = {"tag": "button", "bbox": [10, 500, 80, 30], "z": 0}
    assert d.unexpected_dialog([dialog], target=target) == "unexpected_dialog"


def test_a_dialog_with_no_geometry_asks():
    assert d.unexpected_dialog([{"role": "alertdialog"}], target={"bbox": [1, 1, 1, 1]}) == (
        "unexpected_dialog"
    )


# -- stalls -------------------------------------------------------------------------


def test_no_action_and_no_change_for_too_long_is_a_stall():
    assert detect(Observation(now=1000.0, last_action_at=700.0, last_change_at=800.0, stall_s=120)) == (
        "timeout_or_stall"
    )


def test_a_recent_change_is_not_a_stall():
    assert d.timeout_or_stall(now=1000.0, last_action_at=700.0, last_change_at=950.0, stall_s=120) is None


def test_a_clock_with_no_last_event_asks():
    assert d.timeout_or_stall(now=5.0, last_action_at=None, last_change_at=None) == "timeout_or_stall"


def test_no_clock_means_no_stall_check():
    assert detect(Observation()) is None


# -- duplicates ---------------------------------------------------------------------

ROWS = [{"last_name": "Okafor", "dob": "1950-01-01"}, {"last_name": "Vance", "dob": "1972-03-04"}]


def test_a_row_with_the_same_key_is_a_possible_duplicate():
    observation = Observation(
        duplicate_key=["last_name", "dob"],
        proposed={"last_name": " vance ", "dob": "1972-03-04", "note": "x"},
        existing=ROWS,
    )
    assert detect(observation) == "possible_duplicate"


def test_no_matching_row_is_not_a_duplicate():
    assert d.possible_duplicate("last_name", {"last_name": "Nobody"}, ROWS) is None


@pytest.mark.parametrize(
    "key,proposed,rows",
    [
        (["last_name", "dob"], {"last_name": "Vance"}, ROWS),  # no key value
        ("last_name", {"last_name": "Vance"}, None),  # the read failed
        ("last_name", {"last_name": "Nobody"}, [{"dob": "1950-01-01"}]),  # row lacks key
        ([], {"last_name": "Vance"}, ROWS),  # a key with no fields
        ({"weird": 1}, {"last_name": "Vance"}, ROWS),  # a key shape we can't check
    ],
)
def test_a_duplicate_check_that_cant_rule_it_out_asks(key, proposed, rows):
    assert d.possible_duplicate(key, proposed, rows) == "possible_duplicate"


def test_no_duplicate_key_configured_means_no_duplicate_check():
    assert d.possible_duplicate(None, {"a": "b"}, ROWS) is None


def test_the_key_comes_from_verify_yaml_record_duplicate_key():
    config = {"record": {"duplicate_key": ["last_name", "dob"]}, "verifier": {}}
    assert d.duplicate_key_from_config(config) == ["last_name", "dob"]
    assert d.duplicate_key_from_config({"verifier": {}}) is None


# -- general mechanisms only --------------------------------------------------------


def test_the_detectors_name_no_app():
    source = Path(d.__file__).read_text(encoding="utf-8").casefold()
    for name in ("openemr", "mockmed", "epic", "cerner", "salesforce", "okta", "google", "microsoft"):
        assert name not in source


def test_every_detector_returns_a_closed_kind_or_none():
    from openadapt_types.exception_card import EXCEPTION_KINDS

    pages = [FORM, OTP_MARKED, OTP_WEAK, SIGN_IN, MODAL]
    for page in pages:
        for kind in d.detect_all(_obs(page, save_eligible_seen=True)):
            assert kind in EXCEPTION_KINDS
