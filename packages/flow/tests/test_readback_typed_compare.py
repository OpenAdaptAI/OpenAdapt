"""A read-back confirms a value only when the field is the value, not part of one.

An external review found that a region read with ``exact=False`` confirmed
``Ann Smith`` from ``Ann Smithers`` and ``250`` from ``$1,250.00``, by
substring. A confirming comparison is now of the whole field, normalized only
by the field's kind (numbers by value, dates parsed, case and whitespace for
text). Finding the value inside a longer read locates it; it never confirms.
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
    normalize_value,
    token_occurrences,
)

KINDS = ("text", "code", "number", "currency")


# -- the reviewer's examples ---------------------------------------------------


@pytest.mark.parametrize("exact", [True, False])
@pytest.mark.parametrize("source", ["ocr", "structure"])
@pytest.mark.parametrize("kind", KINDS)
def test_250_is_never_confirmed_by_1250(exact: bool, source: str, kind: str) -> None:
    for observed in ("$1,250.00", "1,250.00", "1250", "Total due: $1,250.00"):
        assert (
            compare_value(observed, "250", kind=kind, exact=exact, source=source)  # type: ignore[arg-type]
            != "match"
        )


@pytest.mark.parametrize("exact", [True, False])
@pytest.mark.parametrize("source", ["ocr", "structure"])
@pytest.mark.parametrize("kind", ("text", "code"))
def test_ann_smith_is_never_confirmed_by_ann_smithers(
    exact: bool, source: str, kind: str
) -> None:
    for observed in ("Ann Smithers", "Patient: Ann Smithers", "ANN SMITHERS"):
        assert (
            compare_value(observed, "Ann Smith", kind=kind, exact=exact, source=source)  # type: ignore[arg-type]
            == "mismatch"
        )


# -- adversarial: prefixes, substrings, look-alikes ----------------------------


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("Ann Smith Jr", "Ann Smith"),  # the value plus a suffix
        ("Dr Ann Smith", "Ann Smith"),  # a prefix
        ("Ann Smithson", "Ann Smith"),
        ("Joann Smith", "Ann Smith"),
        ("Smith, Ann", "Ann Smith"),  # the same words, another value
        ("Ann", "Ann Smith"),  # a prefix of the value
        ("2500", "250"),
        ("25", "250"),
        ("250.5", "250"),
        ("-250", "250"),
        ("12-345", "123-45"),  # the same digits, split differently
        ("AB-1234", "AB-123"),
        ("A250", "250"),
        ("250-1", "250"),
        ("MRN 12345", "12345"),
        ("0012345", "12345"),  # a leading zero is an identifier
        ("12345", "0012345"),
        ("007", "7"),
    ],
)
@pytest.mark.parametrize("exact", [True, False])
def test_near_values_never_confirm(observed: str, expected: str, exact: bool) -> None:
    for kind in ("text", "code"):
        for source in ("ocr", "structure"):
            assert (
                compare_value(observed, expected, kind=kind, exact=exact, source=source)
                != "match"
            ), (observed, expected, kind, source)


@pytest.mark.parametrize(
    ("observed", "expected", "kind"),
    [
        ("Name: Ann Smith", "Ann Smith", "text"),
        ("Ann Smith Jr", "Ann Smith", "text"),
        ("MRN: AB-123", "AB-123", "code"),
        ("Total: $1,250.00", "$1,250.00", "currency"),
        ("MRN 12345", "12345", "text"),
        ("Ada Quill | Ada Quill", "Ada Quill", "text"),  # two rows
    ],
)
def test_a_value_inside_a_longer_read_is_ambiguous_never_a_match(
    observed: str, expected: str, kind: str
) -> None:
    assert compare_value(observed, expected, kind=kind, exact=False) == "ambiguous"  # type: ignore[arg-type]
    assert compare_value(observed, expected, kind=kind, exact=True) != "match"  # type: ignore[arg-type]


# -- the same value after documented normalization -----------------------------


@pytest.mark.parametrize(
    ("observed", "expected", "kind"),
    [
        ("1250", "1,250.00", "number"),
        ("$1,250.00", "1250", "currency"),
        ("USD 1,250.00", "$1,250", "currency"),
        ("12,50", "12.50", "currency"),  # a decimal comma
        ("(250.00)", "-250", "currency"),
        ("ann  SMITH", "Ann Smith", "text"),  # text: case and whitespace
        ("ac-1042", "AC1042", "code"),  # code: case and separators
        ("2026-01-02", "Jan 2, 2026", "date"),
        ("007", "007", "text"),
        ("007", "007", "number"),
    ],
)
@pytest.mark.parametrize("exact", [True, False])
def test_the_same_value_matches_after_normalization(
    observed: str, expected: str, kind: str, exact: bool
) -> None:
    assert (
        compare_value(observed, expected, kind=kind, source="structure", exact=exact)  # type: ignore[arg-type]
        == "match"
    )


@pytest.mark.parametrize("malformed", ["1,2,5", "12 50", "(250", "$$250", "$250 USD"])
def test_malformed_numbers_do_not_parse(malformed: str) -> None:
    assert normalize_value(malformed, "number") is None
    assert normalize_value(malformed, "currency") is None
    assert (
        compare_value(malformed, "125", kind="number", source="structure") == "mismatch"
    )


def test_a_currency_symbol_is_not_a_plain_number() -> None:
    # A declared number must read as a plain number; a currency field takes
    # the symbol.
    assert compare_value("$250.00", "250", kind="number", source="structure") == (
        "mismatch"
    )
    assert compare_value("$250.00", "250", kind="currency", source="structure") == (
        "match"
    )


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("250.0", "250.00"),  # two ICD-9 codes, not one number
        ("250", "250.00"),
        ("1.1", "1.10"),
        ("12,345", "12345"),
        ("+250", "250"),
        ("1,250.00", "1250"),
    ],
)
def test_a_text_field_holding_digits_is_compared_as_text(
    observed: str, expected: str
) -> None:
    for source in ("ocr", "structure"):
        assert compare_value(observed, expected, source=source) != "match"
    assert value_matches(observed, expected) is False
    # Declared a number, the same values compare by value.
    assert compare_value(observed, expected, kind="number", source="structure") == (
        "match"
    )


@pytest.mark.parametrize(
    ("observed", "expected"),
    [("€250", "$250"), ("250 EUR", "250 USD"), ("£250", "USD 250"), ("¥250", "$250")],
)
def test_amounts_in_different_currencies_differ(observed: str, expected: str) -> None:
    assert compare_value(observed, expected, kind="currency") == "mismatch"
    assert compare_value("USD 250", "$250.00", kind="currency") == "match"


def test_non_ascii_digits_are_not_numbers() -> None:
    assert compare_value("２５０", "250", kind="number", source="structure") != "match"
    assert compare_value("١٢", "12", kind="currency", source="structure") != "match"


@pytest.mark.parametrize(
    ("observed", "expected", "kind"),
    [
        ("AnnSmith", "Ann Smith", "text"),
        ("2026-11-0209:30", "2026-11-02 09:30", "text"),
        ("1 2-345", "12-345", "code"),
    ],
)
def test_an_ocr_read_that_only_gained_or_lost_spaces_is_ambiguous(
    observed: str, expected: str, kind: str
) -> None:
    assert compare_value(observed, expected, kind=kind, source="ocr") == "ambiguous"  # type: ignore[arg-type]
    assert (
        compare_value(observed, expected, kind=kind, source="structure") == "mismatch"  # type: ignore[arg-type]
    )


def test_ocr_look_alikes_stay_ambiguous_and_a_structure_read_refutes_them() -> None:
    assert compare_value("Ann Smlth", "Ann Smith", source="ocr") == "ambiguous"
    assert compare_value("Ann Smlth", "Ann Smith", source="structure") == "mismatch"
    assert compare_value("AB-I23", "AB-123", kind="code", source="ocr") == "ambiguous"


def test_token_occurrences_takes_whole_tokens() -> None:
    assert token_occurrences("changes unsaved", "saved") == 0
    assert token_occurrences("record saved.", "saved") == 1
    assert token_occurrences("Ann Smithers", "Ann Smith") == 0
    assert token_occurrences("$1,250.00", "250") == 0
    assert token_occurrences("Ada Quill Ada Quill", "Ada Quill") == 2


def test_kinds_are_inferred_conservatively() -> None:
    assert compare_value("250", "250") == "match"
    assert infer_value_kind("$1,250.00") == "currency"
    assert infer_value_kind("Ann Smith") == "text"


# -- through the on-screen verifier --------------------------------------------


def test_value_matches_is_whole_field() -> None:
    assert value_matches("$1,250.00", "250") is False
    assert value_matches("order 250 placed", "250") is False
    assert value_matches("ann smithers", "ann smith") is False
    assert value_matches("patient: ann smith", "ann smith") is False
    assert value_matches("ann smith", "Ann Smith") is True


class _Line:
    def __init__(self, text: str) -> None:
        self.text, self.region, self.confidence = text, (0, 0, 10, 10), 0.95


class _Backend:
    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake"


def _read_back(text: str, expected: str, kind: str = "text") -> Verdict:
    verifier = OnScreenReadbackVerifier(
        _Backend(),
        region=(0, 0, 100, 40),
        vision=SimpleNamespace(ocr=lambda png, region=None: [_Line(text)]),
    )
    return verifier.read_back(expected, value_kind=kind).verdict


@pytest.mark.parametrize(
    ("shown", "expected", "kind", "verdict"),
    [
        ("Ann Smithers", "Ann Smith", "text", Verdict.REFUTED),
        ("Patient: Ann Smithers", "Ann Smith", "text", Verdict.REFUTED),
        ("$1,250.00", "250", "text", Verdict.REFUTED),
        ("AnnSmith", "Ann Smith", "text", Verdict.INDETERMINATE),
        ("$1,250.00", "250", "currency", Verdict.REFUTED),
        ("Balance $1,250.00", "250", "currency", Verdict.REFUTED),
        ("Ann Smith Jr", "Ann Smith", "text", Verdict.INDETERMINATE),
        ("Patient: Ann Smith", "Ann Smith", "text", Verdict.INDETERMINATE),
        ("Ada Quill  Ada Quill", "Ada Quill", "text", Verdict.INDETERMINATE),
        ("Ann Smith", "Ann Smith", "text", Verdict.CONFIRMED),
        ("$250.00", "250", "currency", Verdict.CONFIRMED),
        ("250", "250", "text", Verdict.CONFIRMED),
    ],
)
def test_the_ocr_read_back_confirms_only_the_whole_field(
    shown: str, expected: str, kind: str, verdict: Verdict
) -> None:
    assert _read_back(shown, expected, kind) is verdict


@pytest.mark.parametrize(
    ("shown", "verdict"),
    [
        ("Ann Smithers", Verdict.REFUTED),
        ("Ann Smith Jr", Verdict.INDETERMINATE),
        ("Ada Quill Ann Smith", Verdict.INDETERMINATE),
        ("ann smith", Verdict.CONFIRMED),
    ],
)
def test_the_structure_read_confirms_only_the_whole_field(
    shown: str, verdict: Verdict
) -> None:
    class _Structure:
        def screenshot(self) -> bytes:
            return b"x"

        def displayed_text_at(self, x: int, y: int) -> str:
            return shown

    verifier = OnScreenReadbackVerifier(_Structure(), region=(0, 0, 10, 10))
    assert verifier._read_structure("Ann Smith", (0, 0, 10, 10)).verdict is verdict
