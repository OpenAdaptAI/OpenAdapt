"""Coverage counts identity-bearing tokens only, in every layout.

Label and hint words appear identically beside every record, so they never
count toward coverage (numerator or denominator). A band showing an id, date
or other digit token needs one of them matched raw; a band with no
identity-bearing token abstains.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_template_identity,
)


def _statuses(recorded: str, observed: str) -> tuple[str, str]:
    plain = I.verify_target_identity(recorded, observed, params={}, param_examples={})
    tmpl = build_identity_template(recorded, param_examples={})
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, observed, params={}, param_examples={})
    return plain.status, hashed.status


LABEL = "Hint: enter values in multiple boxes."

# (layout, recorded, the true row re-read, a different record)
LAYOUTS = [
    (
        "label_padding",
        f"Smith John 15 47 {LABEL}",
        f"Smith John 15 47 {LABEL}",
        f"Smith John 16 50 {LABEL}",
    ),
    (
        # A list: the row's band picks up text bleeding from the rows above
        # and below; the record at the click is a sibling with its own number
        # and age.
        "list_with_siblings",
        "26 Smith Jane 52 25 Smith John 47 325 Satin Heights 27 Smith Junior 24",
        "26 Smith Jane 52 25 Smith John 47 325 Satin Heights 27 Smith Junior 24",
        "26 Smith Jane 52 28 Smith John 49 325 Satin Heights 27 Smith Junior 24",
    ),
    (
        "form_header",
        "Patient: Ashford, Jane DOB: 02/23/1975 MRN: RC79284 Sex: F",
        "Patient: Ashford, Jane DOB: 02/23/1975 MRN: RC79284 Sex: F",
        "Patient: Ashford, Jane DOB: 02/23/1975 MRN: RC79285 Sex: F",
    ),
    (
        "dialog",
        "Communication Item Patient John Smith User Admin Date / Time 11/03/2026",
        "Communication Item Patient John Smith User Admin Date / Time 11/03/2026",
        "Communication Item Patient John Smyth User Admin Date / Time 11/03/2026",
    ),
    (
        "band_without_id",
        "Ashford Jane Springfield Cardiology Last Name First Name",
        "Ashford Jane Springfield Cardiology Last Name First Name",
        "Ashford Joan Springfield Cardiology Last Name First Name",
    ),
]


@pytest.mark.parametrize("layout,recorded,same,other", LAYOUTS)
def test_the_true_record_verifies(layout, recorded, same, other) -> None:
    assert _statuses(recorded, same) == ("verified", "verified"), layout


@pytest.mark.parametrize("layout,recorded,same,other", LAYOUTS)
def test_another_record_refuses(layout, recorded, same, other) -> None:
    plain, hashed = _statuses(recorded, other)
    assert plain != "verified", layout
    assert hashed != "verified", layout


def test_label_words_are_not_identity_bearing() -> None:
    raw = f"Smith John 15 47 {LABEL} Last Name"
    bearing = [
        (r, I.identity_bearing(r, I.squash(r))) for r in raw.split() if I.squash(r)
    ]
    assert [r for r, b in bearing if b] == ["Smith", "John", "15", "47"]


def test_label_padding_does_not_lift_coverage() -> None:
    # Name matched, both digit tokens gone: coverage is over Smith John 15 47.
    match = I.band_match(f"Smith John 15 47 {LABEL}", f"Smith John {LABEL}")
    assert match.coverage == pytest.approx(9 / 13)


def test_a_name_only_match_is_not_enough_while_an_id_is_shown() -> None:
    # The id is read as a different one only through OCR noise the matcher
    # cannot fold; with no digit token matched raw, the band cannot verify.
    plain, hashed = _statuses(
        "Ashford Jane MRN RC79284 Springfield", "Ashford Jane MRN Springfield"
    )
    assert plain != "verified"
    assert hashed != "verified"
    assert I.band_match(
        "Ashford Jane MRN RC79284 Springfield", "Ashford Jane MRN Springfield"
    ).identity_evidence_short


def test_a_band_with_no_identity_bearing_token_abstains() -> None:
    recorded = "Last Name: First Name: Phone: Address: search all records"
    plain, hashed = _statuses(recorded, recorded)
    assert plain == "abstain"
    assert hashed == "abstain"


def test_old_templates_without_the_flag_count_every_token() -> None:
    tmpl = build_identity_template(f"Smith John 15 47 {LABEL}", param_examples={})
    assert tmpl is not None
    legacy = tmpl.model_copy(
        update={"tokens": [t.model_copy(update={"bearing": None}) for t in tmpl.tokens]}
    )
    check = verify_template_identity(
        legacy, f"Smith John 15 47 {LABEL}", params={}, param_examples={}
    )
    assert check.status == "verified"


@pytest.mark.parametrize(
    "recorded,observed",
    [
        # Review blocker 1: a real name that reads like a UI word ("Ali" folds
        # to "all") must keep counting.
        ("Ali Price Age 47", "Al Price Age 47"),
        ("Ali Price Age: 47", "Bo Price Age: 47"),
        ("Price, Ali Close", "Price, Al Close"),
        ("Ali Price Age 47", "Price Age 47"),
        # Review blocker 2: lowercase-entered names must keep counting.
        ("Smith, lee Age 47", "Smith, jo Age 47"),
        ("smith ann Age 47", "smith al Age 47"),
        ("Smith ann Age 47", "Smith al Age 47"),
    ],
)
def test_leaving_a_token_out_of_the_bearing_share_never_lets_a_band_pass(
    recorded, observed
) -> None:
    plain, hashed = _statuses(recorded, observed)
    assert plain != "verified"
    assert hashed != "verified"


def test_param_values_are_weighed_with_the_same_bearing_rule() -> None:
    # Review should-fix: the run's value tokens must not count as bearing on
    # the template path when the plaintext path does not count them.
    recorded = "Last: smith First: john"
    kwargs = {"params": {"last": "jones"}, "param_examples": {"last": "smith"}}
    plain = I.verify_target_identity(recorded, "Last: jones First: john", **kwargs)
    tmpl = build_identity_template(recorded, param_examples=kwargs["param_examples"])
    assert tmpl is not None
    hashed = verify_template_identity(tmpl, "Last: jones First: john", **kwargs)
    assert plain.status == hashed.status
