"""A differing identifier is a contradiction, never a tolerated gap.

Found on Open Dental's Select Patient dialog (Windows lab, 2026-11-03): a row
band padded with a static label let a same-name sibling with another PatNum
and age verify. "15 47" vs "16 50" left a 4-char uncovered run, within
UNCOVERED_RUN_CAP, and 2-char tokens were below every contradiction rule.
Every case here must refuse on the plaintext and the template path.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_template_identity,
)

LABEL = "Hint: enter values in multiple boxes."


def _statuses(recorded: str, observed: str) -> tuple[str, str]:
    plain = I.verify_target_identity(recorded, observed, params={}, param_examples={})
    tmpl = build_identity_template(recorded, param_examples={})
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, observed, params={}, param_examples={})
    return plain.status, hashed.status


WRONG_RECORDS = [
    # The pair found on Open Dental: same name, another PatNum and age.
    (f"Smith John 15 47 {LABEL}", f"Smith John 16 50 {LABEL}"),
    # Label padding before the row, and on both sides.
    (f"{LABEL} Smith John 15 47", f"{LABEL} Smith John 16 50"),
    (
        f"Last Name First Name {LABEL} Smith John 15 47 Phone Address City",
        f"Last Name First Name {LABEL} Smith John 16 50 Phone Address City",
    ),
    # The label glued by OCR on the live side.
    (f"Smith John 15 47 {LABEL}", "Smith John 16 50 Hintentervaluesinmultipleboxes."),
    # Only the PatNum differs, or only the age.
    (f"Smith John 15 47 {LABEL}", f"Smith John 16 47 {LABEL}"),
    (f"Smith John 15 47 {LABEL}", f"Smith John 15 50 {LABEL}"),
    # Another date of birth, MRN, or account under a shared address.
    (
        "Ashford Jane 02/23/1975 125 Satin Heights Springfield Active",
        "Ashford Jane 02/24/1975 125 Satin Heights Springfield Active",
    ),
    (
        "Ashford Jane MRN 40081 125 Satin Heights Springfield Active",
        "Ashford Jane MRN 40082 125 Satin Heights Springfield Active",
    ),
    (
        f"Ashford Jane Acct 7 {LABEL}",
        f"Ashford Jane Acct 8 {LABEL}",
    ),
    # A single-digit counter or age.
    (f"Smith John 9 {LABEL}", f"Smith John 8 {LABEL}"),
    # OCR confusions on identifiers (1/l, 0/O): the live id is another one,
    # or an OCR-ambiguous read of one.
    (f"Smith John 15 47 {LABEL}", f"Smith John l6 47 {LABEL}"),
    (f"Smith John 105 47 {LABEL}", f"Smith John l05 47 {LABEL}"),
    (
        "Ashford Jane MRN 40081 125 Satin Heights",
        "Ashford Jane MRN 4OO8l 125 Satin Heights",
    ),
    # The recorded id dropped and another digit token in its place.
    (f"Smith John 15 {LABEL}", f"Smith John {LABEL} 16"),
    # Review blocker 1: a second copy of the recorded value elsewhere in the
    # band must not stand in for the sibling's differing id.
    ("Smith John 15 47 15 patients found", "Smith John 16 47 15 patients found"),
    ("Smith John 15 47 Page 1 of 15", "Smith John 16 47 Page 1 of 15"),
    ("Smith John 3 47 Select 1 of 3 boxes", "Smith John 4 47 Select 1 of 3 boxes"),
    (f"Smith John 15 15 {LABEL}", f"Smith John 16 15 {LABEL}"),
    # Review should-fix: two values read glued are not one id.
    ("Smith John 15 47 Active", "Smith John 1547 Active"),
    ("Smith John 1547 Active", "Smith John 15 47 Active"),
]


@pytest.mark.parametrize("recorded,observed", WRONG_RECORDS)
def test_a_differing_identifier_refuses(recorded: str, observed: str) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "verified", (recorded, observed)
    assert hashed != "verified", (recorded, observed)


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # OCR dropped the identifier: not a contradiction, but not verified.
        (f"Smith John 15 47 {LABEL}", f"Smith John {LABEL}"),
        (f"Smith John 15 {LABEL}", f"Smith John {LABEL}"),
    ],
)
def test_an_absent_identifier_never_verifies(recorded: str, observed: str) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "verified"
    assert hashed != "verified"
    assert I.band_match(recorded, observed).identity_evidence_short


def test_the_digit_contradiction_is_charged_not_the_run_cap() -> None:
    match = I.band_match(f"Smith John 15 47 {LABEL}", f"Smith John 16 50 {LABEL}")
    assert match.max_uncovered_run <= I.UNCOVERED_RUN_CAP  # the old hole
    assert match.contradicted_chars == 4


@pytest.mark.parametrize(
    "recorded,observed",
    [
        (f"Smith John 25 47 {LABEL}", f"Smith John 25 47 {LABEL}"),
        (f"{LABEL} Smith John 25 47", f"Smith John 25 47 {LABEL}"),
        (
            "Ashford Jane 02/23/1975 RC79284 Active",
            "Ashford Jane 02/23/1975 RC79284 Active",
        ),
    ],
)
def test_the_true_row_still_verifies(recorded: str, observed: str) -> None:
    assert _statuses(recorded, observed) == ("verified", "verified")


def test_param_mode_keeps_a_short_id_inside_the_example_value() -> None:
    # Review blocker 2: "15" sits inside the date example "20150101"; the
    # substring test dropped it from the band before matching.
    recorded = "Smith John 15 47 2015-0l-01 Hint"
    observed = "Smith John 16 47 2016-01-01 Hint"
    kwargs = {"params": {"dob": "2016-01-01"}, "param_examples": {"dob": "2015-01-01"}}
    plain = I.verify_target_identity(recorded, observed, **kwargs)
    tmpl = build_identity_template(recorded, param_examples=kwargs["param_examples"])
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, observed, **kwargs)
    assert plain.status != "verified"
    assert hashed.status != "verified"


def test_a_repeated_value_still_verifies_the_true_row() -> None:
    band = f"Smith John 25 25 {LABEL} 25 patients found"
    assert _statuses(band, band) == ("verified", "verified")


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review blocker 3: a PatNum merely similar to the example account
        # number was dropped from the band by the similarity fallback.
        ("Price Mark 547 l5470", "Price Mark 548 99999"),
        ("Price Mark 1547 l5470", "Price Mark 1548 99999"),
    ],
)
def test_param_mode_keeps_a_digit_token_similar_to_the_example(recorded, observed):
    kwargs = {"params": {"acct": "99999"}, "param_examples": {"acct": "15470"}}
    plain = I.verify_target_identity(recorded, observed, **kwargs)
    tmpl = build_identity_template(recorded, param_examples=kwargs["param_examples"])
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, observed, **kwargs)
    assert plain.status != "verified"
    assert hashed.status != "verified"
