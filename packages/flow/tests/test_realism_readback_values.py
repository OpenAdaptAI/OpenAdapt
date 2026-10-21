"""Read-back value comparison against neighbouring values (REALISM_AUDIT.md).

The on-screen read-back compares the expected value with OCR text from the
field's region using ``exact=False``. The tests next to it check that a
value inside a label ("Amount: 250") still confirms. They never check a
neighbouring value that contains the expected one as a substring: "$1,250.00"
when 250 was expected, "Qty 15" when 5 was. Those must not confirm a write.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from openadapt_flow.runtime.effects.onscreen import value_matches
from openadapt_flow.runtime.effects.readback_values import compare_value

_AUDIT = "REALISM_AUDIT identity-3: non-exact read-back matches a substring"


def test_a_value_inside_its_label_still_confirms() -> None:
    """Control: the case the substring match exists for."""

    assert compare_value("Amount: 250", "250", exact=False) == "match"


@pytest.mark.xfail(strict=True, reason=_AUDIT)
@settings(max_examples=200, deadline=None)
@given(expected=st.integers(1, 99_999), observed=st.integers(1, 9_999_999))
def test_a_different_number_never_confirms(expected: int, observed: int) -> None:
    assume(expected != observed and str(expected) in str(observed))
    for shown in (f"{observed}", f"${observed:,}.00", f"Qty {observed}"):
        assert compare_value(shown, str(expected), exact=False) != "match", shown


@pytest.mark.xfail(strict=True, reason=_AUDIT)
@pytest.mark.parametrize(
    ("shown", "expected", "kind"),
    [
        ("$1,250.00", "250", "text"),
        ("mrn 123456", "12345", "code"),
        ("Qty 15", "5", "text"),
        ("100.00-", "100.00", "currency"),
        ("$100.00 CR", "100.00", "currency"),
    ],
)
def test_neighbouring_values_do_not_confirm(shown: str, expected: str, kind) -> None:
    assert compare_value(shown, expected, kind=kind, exact=False) != "match"


@pytest.mark.xfail(
    strict=True, reason="REALISM_AUDIT identity-4: fuzzy text match ignores the end"
)
@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        (
            "follow-up scheduled for 2026-10-29",
            "Follow-up scheduled for 2026-10-21",
        ),
        ("patient: john smithson", "John Smith"),
    ],
)
def test_fuzzy_text_does_not_confirm_a_different_value(
    shown: str, expected: str
) -> None:
    assert not value_matches(shown, expected)
