"""Dates are converted to ISO at intake, never guessed.

A referral writes a date of birth however its sender writes dates. Intake
converts it to the canonical ISO date the program types, and records the
conversion as provenance, when the order can be told: the source's declared
format, a part over 12, equal parts, a spelled-out month, or ISO already. A
numeric date that could be read either way, with no declared format, and a
date that doesn't exist both go to a person. The run's own ISO check stays as
the last guard (tests/test_program_ir_phase1.py).
"""

from __future__ import annotations

import pytest

from openadapt_flow.intake.document import IntakeDocument
from openadapt_flow.intake.extract import extract_fields, normalize_date
from openadapt_flow.ir import ParamKind, ParamSpec
from openadapt_flow.provenance import from_intake, sha256_bytes

SPECS = {"DOB": ParamSpec(name="DOB", type=ParamKind.DATE)}


def _doc(dob: str) -> IntakeDocument:
    return IntakeDocument("referral.txt", "text", ["Last name: Okafor", f"DOB: {dob}"])


@pytest.mark.parametrize(
    "raw, iso",
    [
        ("12/25/1970", "1970-12-25"),  # 25 can only be the day
        ("25/12/1970", "1970-12-25"),
        ("07/07/1970", "1970-07-07"),  # reads the same either way
        ("12-25-1970", "1970-12-25"),
        ("1970-12-25", "1970-12-25"),
        ("25 Dec 1970", "1970-12-25"),
        ("December 25, 1970", "1970-12-25"),
        ("1970/12/25", "1970-12-25"),
    ],
)
def test_a_date_whose_order_can_be_told_converts(raw: str, iso: str) -> None:
    value, error, _conversion = normalize_date(raw)
    assert (value, error) == (iso, None)


def test_an_ambiguous_date_halts_without_a_declared_format() -> None:
    value, error, conversion = normalize_date("03/04/1970")
    assert value is None and conversion is None
    assert error is not None and "ambiguous" in error


@pytest.mark.parametrize(
    "declared, iso", [("MM/DD/YYYY", "1970-03-04"), ("DD/MM/YYYY", "1970-04-03")]
)
def test_an_ambiguous_date_converts_with_a_declared_format(declared, iso) -> None:
    value, error, conversion = normalize_date("03/04/1970", declared)
    assert (value, error) == (iso, None)
    assert conversion == f"{declared} to ISO (declared)"


@pytest.mark.parametrize(
    "raw", ["1970-02-30", "02/30/1970", "13/13/1970", "1970-2-3", "next Tuesday"]
)
def test_an_invalid_date_halts(raw: str) -> None:
    value, error, _ = normalize_date(raw)
    assert value is None and error


def test_a_declared_format_still_refuses_a_date_that_does_not_exist() -> None:
    assert normalize_date("02/30/1970", "MM/DD/YYYY")[0] is None


def test_intake_records_the_conversion_as_provenance() -> None:
    doc = _doc("12/25/1970")
    result = extract_fields(doc, SPECS)
    field = result.fields["DOB"]
    assert field.value == "1970-12-25" and field.error is None
    assert field.raw == "12/25/1970"
    assert field.conversion == "MM/DD/YYYY to ISO (25 can only be the day)"
    prov = from_intake(result, sha256_bytes(doc.text.encode("utf-8")))
    entry = prov.params["DOB"]
    assert (entry.value, entry.raw_value) == ("1970-12-25", "12/25/1970")
    assert entry.conversion == field.conversion
    assert result.to_params() == {"DOB": "1970-12-25"}


def test_intake_sends_an_ambiguous_date_to_a_person() -> None:
    result = extract_fields(_doc("03/04/1970"), SPECS)
    assert result.needs_confirmation() == ["DOB"]
    assert "ambiguous" in (result.fields["DOB"].error or "")
    # With the source's format declared, it converts and needs nobody.
    declared = extract_fields(
        _doc("03/04/1970"), SPECS, date_formats={"DOB": "DD/MM/YYYY"}
    )
    assert declared.needs_confirmation() == []
    assert declared.to_params() == {"DOB": "1970-04-03"}


def test_an_iso_date_records_no_conversion() -> None:
    field = extract_fields(_doc("1970-12-25"), SPECS).fields["DOB"]
    assert (
        field.value == "1970-12-25" and field.raw is None and field.conversion is None
    )
