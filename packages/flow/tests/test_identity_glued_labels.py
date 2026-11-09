"""OCR glues a whole static label into one token, dropping its punctuation.

Found on Open Dental's Select Patient dialog (Windows lab, 2026-11-03): the
identity band of John Smith's row runs across the dialog and takes in the
search panel's label "Hint: enter values in multiple boxes." Recorded OCR
split it into six words; replay OCR read the same pixels as
"Hintentervaluesinmultipleboxes." The rejoin window was four words, and the
colon after "Hint" was gone, so six of thirty tokens went unmatched and the
true row was refused (coverage 0.76).
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_template_identity,
)

# The recorded band (token order as the compiler stored it) and the live band.
RECORDED = (
    "50 Smith Jane 125 Satin Heights 16 Hint: enter values in multiple boxes. "
    "125Satin Heights Smith John 47 15 Smith Last Name Appts Junior 125 Satin "
    "Heights Smith 24 17"
)
OBSERVED = (
    "16 Smith Jane 50 125 Satin Heights Hintentervaluesinmultipleboxes. 15 John "
    "47 125 Satin Heights Smith Last Name Smith Appts Junior 24 17 Smith "
    "125Satin Heights"
)


def _both(recorded: str, observed: str) -> tuple[str, str]:
    plain = I.verify_target_identity(recorded, observed, params={}, param_examples={})
    tmpl = build_identity_template(recorded, param_examples={})
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, observed, params={}, param_examples={})
    return plain.status, hashed.status


def test_glued_static_label_reads_like_the_split_label() -> None:
    # The verdict is whatever the cleanly split label gets: here an abstain,
    # from the separate rule on raw-matched ids with a 1 or 0 (PatNum 15)
    # when no name plus date of birth backs them. Coverage must be whole.
    split = OBSERVED.replace(
        "Hintentervaluesinmultipleboxes.", "Hint: enter values in multiple boxes."
    )
    assert _both(RECORDED, OBSERVED) == _both(RECORDED, split)
    assert I.band_match(RECORDED, OBSERVED).coverage == 1.0
    tmpl = build_identity_template(RECORDED, param_examples={})
    check = verify_template_identity(tmpl, OBSERVED, params={}, param_examples={})
    assert check.coverage == 1.0


@pytest.mark.parametrize(
    "wrong",
    [
        # Jane's row (PatNum 16, age 50) under the same glued label.
        OBSERVED.replace("15 John 47", "16 Jane 50"),
        # Junior's row (PatNum 17, age 24).
        OBSERVED.replace("15 John 47", "17 Junior 24"),
    ],
)
def test_glued_label_does_not_verify_another_row(wrong: str) -> None:
    plain, hashed = _both(RECORDED, wrong)
    assert plain != "verified"
    assert hashed != "verified"


def test_glued_forms_drop_only_punctuation_between_fragments() -> None:
    assert I.glued_forms(["hint:", "enter", "boxes."]) == [
        "hint:enterboxes.",
        "hintenterboxes.",
    ]
    # Punctuation inside a fragment is a different value, not glue.
    assert I.glued_forms(["1.25", "kg"]) == ["1.25kg"]


def test_interior_decimal_point_still_separates_amounts() -> None:
    recorded = "Balance 1.25 kg Ashford Jane 02/23/1975 RC79284"
    observed = "Balance 12.5kg Ashford Jane 02/23/1975 RC79284"
    plain, hashed = _both(recorded, observed)
    assert plain != "verified"
    assert hashed != "verified"


def test_rejoin_window_reaches_eight_tokens() -> None:
    recorded = "Ashford Jane RC79284 one two three four five six seven eight"
    observed = "Ashford Jane RC79284 onetwothreefourfivesixseveneight"
    assert _both(recorded, observed) == ("verified", "verified")


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review blocker 1: a point or comma OCR split off a number is part of
        # its value; dropping it would read a different id or amount.
        ("1. 23456 Smith John 01/02/1980", "123456 Smith John 01/02/1980"),
        ("Smith John MRN 12. 345 01/02/1980", "Smith John MRN 12345 01/02/1980"),
        (
            "Balance 12. 50 Ashford Jane 02/23/1975 RC79284",
            "Balance 1250 Ashford Jane 02/23/1975 RC79284",
        ),
        (
            "Smith John Acct 3, 1250 DOB 01/02/1980",
            "Smith John Acct 31250 DOB 01/02/1980",
        ),
    ],
)
def test_punctuation_beside_a_digit_is_never_dropped(recorded, observed) -> None:
    plain, hashed = _both(recorded, observed)
    assert plain != "verified"
    assert hashed != "verified"


def test_glued_forms_keep_marks_beside_digits() -> None:
    assert I.glued_forms(["1.", "23456"]) == ["1.23456"]
    assert I.glued_forms(["name:", "15"]) == ["name:15"]
    assert I.glued_forms(["hint:", "enter"]) == ["hint:enter", "hintenter"]


# Review blocker 2 (Codex, PR 1742): a fragment that is only punctuation sits
# between two other fragments, so checking only immediate neighbours let a run
# of marks between two id fragments lose one ("RC79..284" read as
# "RC79.284"). Every mark, on both the plaintext and the template path.
@pytest.mark.parametrize("mark", [".", ",", ":", ";"])
def test_punctuation_run_between_id_fragments_is_kept(mark: str) -> None:
    recorded = f"Ashford Jane 02/23/1975 RC79 {mark} {mark} 284 Active"
    changed = f"Ashford Jane 02/23/1975 RC79{mark}284 Active"
    unchanged = f"Ashford Jane 02/23/1975 RC79{mark}{mark}284 Active"
    plain, hashed = _both(recorded, changed)
    assert plain != "verified"
    assert hashed != "verified"
    assert _both(recorded, unchanged) == ("verified", "verified")


@pytest.mark.parametrize("mark", [".", ",", ":", ";"])
def test_glued_forms_keep_punctuation_runs_beside_digits(mark: str) -> None:
    assert I.glued_forms(["RC79", mark, mark, "284"]) == [f"RC79{mark}{mark}284"]
    assert I.glued_forms(["hint", mark, mark, "enter"]) == [
        f"hint{mark}{mark}enter",
        "hintenter",
    ]
