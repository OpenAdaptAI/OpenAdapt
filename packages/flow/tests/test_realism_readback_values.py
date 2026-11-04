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


def test_a_value_inside_its_label_is_ambiguous_not_a_match() -> None:
    """Control: since #1744 a value inside other text never confirms."""

    assert compare_value("250", "250", exact=False) == "match"
    assert compare_value("Amount: 250", "250", exact=False) == "ambiguous"


@settings(max_examples=200, deadline=None)
@given(
    expected=st.integers(1, 99_999),
    prefix=st.integers(0, 99),
    suffix=st.text("0123456789", max_size=2),
)
def test_a_different_number_never_confirms(
    expected: int, prefix: int, suffix: str
) -> None:
    observed = int(f"{prefix or ''}{expected}{suffix}")
    assume(observed != expected)
    for shown in (f"{observed}", f"${observed:,}.00", f"Qty {observed}"):
        assert compare_value(shown, str(expected), exact=False) != "match", shown


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
