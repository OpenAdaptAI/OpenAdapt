"""Unit tests for ``openadapt_flow.form_tx`` contracts, codecs, and anchor resolution."""

from __future__ import annotations

import pytest

from openadapt_flow.form_tx import (
    AmbiguousAnchorError,
    CodecValueError,
    DossierFact,
    FieldAnchorSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormSnapshot,
    InputDossier,
    ObservedControl,
    ObservedOption,
    ProvenanceKind,
    compute_field_signature,
    format_for_text_entry,
    match_anchor_in_snapshot,
    normalize_currency_cents,
    normalize_phone_digits,
    normalize_with_codec,
    parse_iso_date_strict,
    resolve_unique_option,
    values_equivalent,
)


def test_dossier_grounded_confidence_and_key_invariant():
    f_high = DossierFact(
        key="applicant.first_name",
        value="Alice",
        provenance=ProvenanceKind.INTAKE_DOCUMENT,
        source_id="doc_1",
        confidence=0.95,
    )
    f_low = DossierFact(
        key="applicant.middle_name",
        value="M",
        provenance=ProvenanceKind.INTAKE_DOCUMENT,
        source_id="doc_1",
        confidence=0.60,
    )
    dossier = InputDossier(
        dossier_id="d1",
        request_id="r1",
        facts={
            "applicant.first_name": f_high,
            "applicant.middle_name": f_low,
        },
        min_confidence=0.90,
    )
    assert dossier.grounded_fact("applicant.first_name") == f_high
    assert dossier.grounded_fact("applicant.middle_name") is None
    assert dossier.grounded_fact("missing.key") is None

    with pytest.raises(ValueError, match="Dossier fact key mismatch"):
        InputDossier(
            dossier_id="d2",
            request_id="r2",
            facts={"wrong.key": f_high},
        )


def test_secret_control_value_automatically_scrubbed():
    ctrl = ObservedControl(
        control_id="c1",
        dom_selector="#pwd",
        tag="input",
        input_type="password",
        current_value="super-secret-password",
        is_secret=True,
    )
    assert ctrl.current_value == "<secret>"


def test_date_codec_strict_disambiguation():
    assert parse_iso_date_strict("2026-11-19") == "2026-11-19"
    assert parse_iso_date_strict("19 Nov 2026") == "2026-11-19"
    assert parse_iso_date_strict("Nov 19, 2026") == "2026-11-19"
    assert parse_iso_date_strict("12/25/1984") == "1984-12-25"

    # Ambiguous 03/04/1984 fails closed under default YMD
    with pytest.raises(CodecValueError, match="Ambiguous numeric date"):
        parse_iso_date_strict("03/04/1984")

    assert parse_iso_date_strict("03/04/1984", date_order="MDY") == "1984-03-04"
    assert parse_iso_date_strict("03/04/1984", date_order="DMY") == "1984-04-03"

    with pytest.raises(CodecValueError, match="Invalid calendar date"):
        parse_iso_date_strict("2026-02-30")


def test_currency_and_phone_codecs():
    assert normalize_currency_cents("$485,000.00") == 48500000
    assert normalize_currency_cents("₹1,250.50") == 125050
    assert normalize_currency_cents(2500) == 250000
    assert values_equivalent(
        "2500", "$2,500.00", FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    )
    assert format_for_text_entry(
        "$485,000.00", FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    ) == "485000"

    assert normalize_phone_digits("+1 (614) 555-0148") == "6145550148"
    assert normalize_phone_digits("98765 43210") == "9876543210"
    assert values_equivalent(
        "614-555-0148", "(614) 555-0148", FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)
    )


def test_option_resolution_fails_closed_on_ambiguity_or_miss():
    opts = (
        ObservedOption(value="", label="-- Select --"),
        ObservedOption(value="Standard", label="Standard Finish"),
        ObservedOption(value="Custom", label="Custom Finish"),
    )
    spec = FieldCodecSpec(
        kind=FormCodecKind.ENUM_MAP,
        enum_aliases={"Custom": ("Custom", "Custom Finish")},
    )
    chosen = resolve_unique_option("Custom", opts, spec)
    assert chosen.value == "Custom"

    with pytest.raises(CodecValueError):
        resolve_unique_option("Platinum", opts, spec)


def test_anchor_matching_and_volatile_id_stripping():
    c1 = ObservedControl(
        control_id="c1",
        dom_selector="#dyn_91_first_name",
        tag="input",
        name_attr="first_name",
        id_attr="dyn_91_first_name",
        label_text="Applicant First Name *",
        section_text="Applicant Information",
    )
    c2 = ObservedControl(
        control_id="c2",
        dom_selector="#dyn_99_first_name",
        tag="input",
        name_attr="first_name",
        id_attr="dyn_99_first_name",
        label_text="Applicant First Name *",
        section_text="Applicant Information",
    )
    assert compute_field_signature(c1) == compute_field_signature(c2)

    snap_single = FormSnapshot(url_origin_and_path="http://portal.local/w", controls=(c1,))
    anchor = FieldAnchorSpec(label_patterns=(r"^Applicant First Name",))
    assert match_anchor_in_snapshot(anchor, snap_single) == c1

    snap_dup = FormSnapshot(url_origin_and_path="http://portal.local/w", controls=(c1, c2))
    with pytest.raises(AmbiguousAnchorError):
        match_anchor_in_snapshot(anchor, snap_dup)


def test_enum_codec_normalization():
    spec = FieldCodecSpec(
        kind=FormCodecKind.ENUM_MAP,
        enum_aliases={"2": ("2", "2 Stories"), "1": ("1", "1 Story")},
    )
    assert normalize_with_codec("2 Stories", spec) == "2"
    with pytest.raises(CodecValueError):
        normalize_with_codec("3 Stories", spec)
