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


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review round 3, blocker 1: a copy of the recorded value the recorded
        # band did not show stands in for the record's own differing value.
        (f"Smith John 15 47 {LABEL}", f"Smith John 16 47 {LABEL} 15 patients found"),
        ("Smith John 03/04/1980 Active", "Smith John 03/05/1980 Active 03/04/1980"),
        ("Smith John 15 Active", "Smith John 16 15 Active"),
        # Review round 3: a 3-letter name replaced by a 2-letter one.
        ("Nguyen Eva 47 Recall Due", "Nguyen Bo 47 Recall Due"),
        ("Nguyen Eva 47 Patient Chart Balance", "Nguyen Bo 47 Patient Chart Balance"),
        ("Okafor Ann 05/14/1988 Active", "Okafor Bo 05/14/1988 Active"),
        ("Hall Ali 03/04/1980 Active", "Hall Bo 03/04/1980 Active"),
    ],
)
def test_review_round_3_wrong_records_refuse(recorded: str, observed: str) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "verified", (recorded, observed)
    assert hashed != "verified", (recorded, observed)


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review round 3, blocker 2: a word that folds to a digit value
        # ("Bo" ~ "80", "Li" ~ "11") must not take the exact copy's slot.
        ("Price Bo 80 03/04/1980", "Price Bo 80 03/04/1980"),
        ("Lee Li 11 03/04/1980 Active", "Lee Li 11 03/04/1980 Active"),
    ],
)
def test_a_word_folding_to_a_value_never_turns_a_reread_into_a_mismatch(
    recorded: str, observed: str
) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "mismatch", recorded
    assert hashed != "mismatch", recorded


def test_an_exact_digit_copy_is_taken_before_a_folded_word() -> None:
    match = I.band_match("Smith John 25 47 Active", "Smith John 2S 25 47 Active")
    assert match.contradicted_chars == 0


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review round 4, blocker 1: a recorded word folding to the live
        # record's differing id ("Bo" ~ "80", "Io" ~ "10") explained it.
        ("Price Bo 81 Active", "Price Bo 80 Active 81 found"),
        ("Smith Io 15 Active", "Smith Io 10 Active 15"),
        # Review round 4, blocker 2: a name replaced by a 1-letter token, or
        # a short name by a longer lowercase one.
        ("Okafor Ann 05/14/1988 Active", "Okafor J 05/14/1988 Active"),
        ("Price Al 02/16/1963 76 Active", "Price J. 02/16/1963 76 Active"),
        ("nguyen al 74 03/04/1980 pending", "nguyen joan 74 03/04/1980 pending"),
        ("Nguyen Eva 47 Recall Due", "Nguyen J 47 Recall Due"),
    ],
)
def test_review_round_4_wrong_records_refuse(recorded: str, observed: str) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "verified", (recorded, observed)
    assert hashed != "verified", (recorded, observed)


def _statuses_with_round_trip(recorded: str, observed: str) -> tuple[str, str, str]:
    from openadapt_flow.ir import IdentityTemplate

    plain, fresh = _statuses(recorded, observed)
    template = build_identity_template(recorded, param_examples={})
    assert template is not None
    loaded = IdentityTemplate.model_validate(template.model_dump(mode="json"))
    reread = verify_template_identity(loaded, observed, params={}, param_examples={})
    return plain, fresh, reread.status


def test_a_relocated_label_beside_the_id_abstains_without_span_evidence() -> None:
    # A false halt is accepted until capture provides record-span authority.
    recorded = f"{LABEL} Smith John 25 47"
    observed = f"Smith John 25 47 {LABEL}"
    assert _statuses_with_round_trip(recorded, observed) == (
        "abstain",
        "abstain",
        "abstain",
    )


@pytest.mark.parametrize("heading", ["ID:", "Guidance:"])
@pytest.mark.parametrize("prefix", ["rc.", "prefix."])
@pytest.mark.parametrize("reverse", [False, True])
def test_a_colon_heading_cannot_grant_identifier_prefix_relocation(
    heading: str, prefix: str, reverse: bool
) -> None:
    recorded = f"{heading} {prefix} 79284 Smith John 47"
    observed = f"79284 Smith John 47 {heading} {prefix}"
    if reverse:
        recorded, observed = observed, recorded
    assert _statuses_with_round_trip(recorded, observed) == (
        "abstain",
        "abstain",
        "abstain",
    )
