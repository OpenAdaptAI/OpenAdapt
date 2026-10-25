"""A read-back confirms a value only when it is the value, not part of one.

``250`` was confirmed inside ``$1,250.00`` because a value read within a
longer read was matched by substring. Values now compare exactly after
normalizing by kind (money, number, date, code, name or text), a value inside
a longer read must be whole tokens (a number a whole number), and a value
that appears twice is two records: ambiguous, never confirmed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.runtime.effects import Verdict
from openadapt_flow.runtime.effects.onscreen import (
    OnScreenReadbackVerifier,
    value_matches,
)
from openadapt_flow.runtime.effects.readback_values import (
    compare_value,
    infer_value_kind,
    token_occurrences,
)


@pytest.mark.parametrize("exact", [True, False])
@pytest.mark.parametrize("source", ["ocr", "structure"])
def test_250_is_not_confirmed_by_1250(exact: bool, source: str) -> None:
    for kind in ("text", "number", "currency"):
        assert (
            compare_value("$1,250.00", "250", kind=kind, exact=exact, source=source)
            == "mismatch"
        )  # type: ignore[arg-type]
    assert compare_value("Total due: $1,250.00", "250", exact=exact) == "mismatch"


@pytest.mark.parametrize(
    ("observed", "expected", "kind"),
    [
        ("1,250.00", "1250", "text"),
        ("1250", "1,250.00", "number"),
        ("$1,250.00", "1250", "currency"),
        ("USD 1,250.00", "$1,250", "currency"),
    ],
)
def test_the_same_number_matches_after_normalization(observed, expected, kind) -> None:
    assert compare_value(observed, expected, kind=kind, source="structure") == "match"


def test_a_value_inside_a_longer_read_must_be_whole_tokens() -> None:
    assert (
        compare_value("Total: $1,250.00", "$1,250.00", kind="currency", exact=False)
        == "match"
    )
    assert compare_value("MRN: AB-123", "AB-123", kind="code", exact=False) == "match"
    assert (
        compare_value("MRN: AB-1234", "AB-123", kind="code", exact=False) == "mismatch"
    )
    assert (
        compare_value("Patient: Ann Smithers", "Ann Smith", exact=False) == "mismatch"
    )
    assert compare_value("Patient: Ann Smith", "Ann Smith", exact=False) == "match"


@pytest.mark.parametrize(
    ("observed", "expected", "kind"),
    [
        ("Ada Quill | Ada Quill", "Ada Quill", "text"),  # two rows, one name
        ("$250.00  $250.00", "250", "currency"),  # the same amount twice
        ("AB-123 / AB-123", "AB-123", "code"),
    ],
)
def test_a_value_shown_twice_is_not_confirmed(observed, expected, kind) -> None:
    assert compare_value(observed, expected, kind=kind, exact=False) == "ambiguous"
    assert token_occurrences(observed, expected, kind) == 2


def test_plain_numbers_are_inferred_as_numbers() -> None:
    assert compare_value("250", "250") == "match"
    assert compare_value("2500", "250") == "mismatch"
    assert infer_value_kind("$1,250.00") == "currency"


def test_the_fuzzy_screen_match_takes_no_partial_numbers_or_duplicates() -> None:
    assert value_matches("$1,250.00", "250") is False
    assert value_matches("order 1250 placed", "250") is False
    assert value_matches("order 250 placed", "250") is True
    assert value_matches("ada quill ada quill", "ada quill") is False
    # Free text without digits keeps its OCR tolerance.
    assert value_matches("savednote for patient", "saved note for patient") is True


class _Line:
    def __init__(self, text: str) -> None:
        self.text, self.region, self.confidence = text, (0, 0, 10, 10), 0.95


class _Backend:
    def screenshot(self) -> bytes:
        return b"\\x89PNG\\r\\n\\x1a\\nfake"


def _read_back(text: str, expected: str, kind: str = "text") -> Verdict:
    verifier = OnScreenReadbackVerifier(
        _Backend(),
        region=(0, 0, 100, 40),
        vision=SimpleNamespace(ocr=lambda png, region=None: [_Line(text)]),
    )
    return verifier.read_back(expected, value_kind=kind).verdict


def test_the_screen_read_back_refutes_a_partial_number_and_holds_on_a_duplicate() -> (
    None
):
    assert _read_back("Balance $1,250.00", "250") is Verdict.REFUTED
    assert _read_back("Balance $250.00", "250") is Verdict.CONFIRMED
    assert _read_back("Ada Quill  Ada Quill", "Ada Quill") is not Verdict.CONFIRMED
    assert _read_back("Ada Quill", "Ada Quill") is Verdict.CONFIRMED
