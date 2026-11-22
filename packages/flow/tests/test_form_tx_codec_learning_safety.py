"""Refusal regressions for representation loss and unsupported learned policy."""

from datetime import datetime
from decimal import Decimal

import pytest

from openadapt_flow.form_tx.codecs import (
    CodecValueError,
    format_for_text_entry,
    normalize_boolean_yes_no,
    normalize_currency_cents,
    normalize_phone_digits,
    parse_iso_date_strict,
    values_equivalent,
)
from openadapt_flow.form_tx.learning import RuleAccumulator, admit_candidate_into_pack
from openadapt_flow.form_tx.types import (
    FieldAnchorSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    HumanDecisionAction,
    HumanResolutionRecord,
)


@pytest.mark.parametrize("left", ["$100.00", "€100.00", "£100.00", "₹100.00"])
@pytest.mark.parametrize("right", ["$100.00", "€100.00", "£100.00", "₹100.00"])
def test_currency_comparison_retains_explicit_unit(left, right):
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    assert values_equivalent(left, right, codec) is (left[0] == right[0])


@pytest.mark.parametrize("symbol", ["$", "€", "£", "₹"])
def test_bare_amount_cannot_establish_an_explicit_currency(symbol):
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    assert not values_equivalent(100, f"{symbol}100.00", codec)
    assert not values_equivalent(f"{symbol}100.00", 100, codec)


@pytest.mark.parametrize("symbol", ["$", "€", "£", "₹"])
def test_declared_unit_authorizes_bare_form_representation_only_for_that_unit(symbol):
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol=symbol)
    assert values_equivalent(100, f"{symbol}100.00", codec)
    assert values_equivalent(f"{symbol}100.00", "100", codec)
    for other in {"$", "€", "£", "₹"} - {symbol}:
        assert not values_equivalent(100, f"{other}100.00", codec)
        assert not values_equivalent(f"{other}100.00", f"{other}100.00", codec)


@pytest.mark.parametrize(
    "value",
    [
        "$€100",
        "1,00",
        "10,00.00",
        "1 000",
        "100.001",
        "NaN",
        "Infinity",
        float("inf"),
        Decimal("NaN"),
    ],
)
def test_money_refuses_unparsed_or_nonfinite_representation(value):
    with pytest.raises(CodecValueError):
        normalize_currency_cents(value)


def test_money_entry_preserves_large_exact_minor_units():
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
    amount = "$123456789012345678901234567890.99"
    assert normalize_currency_cents(amount) == 12345678901234567890123456789099
    assert format_for_text_entry(amount, codec) == "123456789012345678901234567890.99"
    assert format_for_text_entry("$-1.01", codec) == "-1.01"


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01Tgarbage",
        "2026-01-01T23:00:00Z",
        "2026-01-01 extra",
        datetime(2026, 1, 1),
    ],
)
def test_calendar_date_does_not_discard_timestamp_or_suffix(value):
    with pytest.raises(CodecValueError):
        parse_iso_date_strict(value)


@pytest.mark.parametrize(
    "value,order", [("13/02/2026", "MDY"), ("02/13/2026", "DMY"), ("03/04/2026", "YMD")]
)
def test_date_order_is_a_contract_not_an_inference(value, order):
    with pytest.raises(CodecValueError):
        parse_iso_date_strict(value, order)


@pytest.mark.parametrize(
    "value",
    [
        "Do not call 6175550100",
        "6175550100 ext 2",
        "6175550100 disabled",
        "+1+6175550100",
        ")(6175550100(",
        True,
    ],
)
def test_phone_rejects_text_qualifiers_and_malformed_syntax(value):
    with pytest.raises(CodecValueError):
        normalize_phone_digits(value)


@pytest.mark.parametrize(
    "value", ["No, needs review", "not yes", "pending", "unknown", "yes unless denied"]
)
def test_boolean_does_not_erase_negation_or_uncertainty(value):
    with pytest.raises(CodecValueError):
        normalize_boolean_yes_no(value)


def _pack():
    return FormPack(
        pack_id="generic", domain_schema="record.v1", ui_profile_id="generic"
    )


def _record(nonce, *, value="Yes", key="known_fact"):
    return HumanResolutionRecord(
        halt_id="halt-same-field",
        halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
        action=HumanDecisionAction.PROVIDE_VALUE,
        actor_id="operator",
        actor_role="operator",
        field_signature="details|question|text|q",
        dossier_key=key,
        resolved_value=value,
        remember_as_candidate_rule=True,
        timestamp_utc="2026-11-20T00:00:00Z",
        idempotency_key=nonce,
    )


def _add(accumulator, pack, run_id, nonce, **kwargs):
    accumulator.record_run_resolutions(
        pack.pack_id,
        (_record(nonce, **kwargs),),
        run_id=run_id,
        pack_digest=pack.digest(),
    )


def _supported():
    pack = _pack()
    accumulator = RuleAccumulator()
    _add(accumulator, pack, "run-one", "nonce-one")
    _add(accumulator, pack, "run-two", "nonce-two")
    return accumulator, pack, accumulator.induce_candidates(pack.pack_id)[0]


def test_same_run_different_decision_nonce_does_not_create_support():
    pack = _pack()
    accumulator = RuleAccumulator()
    _add(accumulator, pack, "one-run", "nonce-one")
    _add(accumulator, pack, "one-run", "nonce-two")
    assert accumulator.induce_candidates(pack.pack_id) == ()


def test_legacy_unscoped_records_cannot_establish_support():
    pack = _pack()
    accumulator = RuleAccumulator()
    accumulator.record_run_resolutions(pack.pack_id, (_record("one"),))
    accumulator.record_run_resolutions(pack.pack_id, (_record("two"),))
    assert accumulator.induce_candidates(pack.pack_id) == ()


def test_distinct_runs_only_propose_a_rule():
    accumulator, pack, candidate = _supported()
    assert candidate.supporting_run_count == 2
    assert not candidate.admitted
    assert pack.bindings == ()
    assert accumulator.admitted_candidates(pack.pack_id) == ()


@pytest.mark.parametrize("reuse_nonce", [False, True])
def test_contradiction_in_one_run_blocks_promotion(reuse_nonce):
    accumulator, pack, _ = _supported()
    _add(
        accumulator,
        pack,
        "run-one",
        "nonce-one" if reuse_nonce else "new-nonce",
        key="different_fact",
    )
    assert accumulator.induce_candidates(pack.pack_id) == ()


def test_evidence_cannot_mix_pack_versions():
    accumulator, pack, _ = _supported()
    with pytest.raises(ValueError, match="different pack versions"):
        accumulator.record_run_resolutions(
            pack.pack_id,
            (_record("three"),),
            run_id="run-three",
            pack_digest="different",
        )


def test_stale_or_forged_candidate_cannot_be_admitted():
    accumulator, pack, candidate = _supported()
    for forged in [
        candidate.model_copy(update={"supporting_run_count": 99}),
        candidate.model_copy(update={"proposed_dossier_key": "other"}),
    ]:
        with pytest.raises(ValueError, match="current accumulated evidence"):
            accumulator.admit_candidate_into_pack(
                pack,
                forged,
                FieldAnchorSpec(label_patterns=("Question",)),
                approval_id="review-1",
                qualification_check=lambda p: True,
            )
    changed = pack.model_copy(update={"domain_schema": "record.v2"})
    with pytest.raises(ValueError, match="current pack digest"):
        accumulator.admit_candidate_into_pack(
            changed,
            candidate,
            FieldAnchorSpec(label_patterns=("Question",)),
            approval_id="review-1",
            qualification_check=lambda p: True,
        )


def test_rule_promotion_requires_explicit_approval_and_pack_qualification():
    accumulator, pack, candidate = _supported()
    anchor = FieldAnchorSpec(label_patterns=("Question",))
    with pytest.raises(ValueError, match="approval_id"):
        accumulator.admit_candidate_into_pack(pack, candidate, anchor)
    with pytest.raises(ValueError, match="qualification"):
        accumulator.admit_candidate_into_pack(
            pack, candidate, anchor, approval_id="review-1"
        )
    for result in [False, None, 1]:
        with pytest.raises(ValueError, match="did not pass qualification"):
            accumulator.admit_candidate_into_pack(
                pack,
                candidate,
                anchor,
                approval_id="review-1",
                qualification_check=lambda p: result,
            )
    assert accumulator.admitted_candidates(pack.pack_id) == ()
    assert pack.bindings == ()
    checked = []

    def qualify(updated):
        checked.append(updated.digest())
        return (
            len(updated.bindings) == 1
            and updated.bindings[0].dossier_key == "known_fact"
        )

    updated = accumulator.admit_candidate_into_pack(
        pack, candidate, anchor, approval_id="review-1", qualification_check=qualify
    )
    assert checked == [updated.digest()]
    assert pack.bindings == ()
    assert accumulator.admitted_candidates(pack.pack_id)[0].admitted


def test_standalone_admission_does_not_automatically_promote_a_proposal():
    _, pack, candidate = _supported()
    with pytest.raises(ValueError, match="approval_id"):
        admit_candidate_into_pack(
            pack, candidate, FieldAnchorSpec(label_patterns=("Question",))
        )


@pytest.mark.parametrize("value", ["€100", "£100", "₹100"])
def test_text_entry_refuses_a_unit_conflicting_with_the_field(value):
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
    with pytest.raises(CodecValueError, match="unit contradicts"):
        format_for_text_entry(value, codec)


def test_text_entry_never_strips_an_undeclared_currency_unit():
    codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    with pytest.raises(CodecValueError, match="declared field unit"):
        format_for_text_entry("$100", codec)


@pytest.mark.parametrize(
    "kind,value",
    [
        (FormCodecKind.PHONE_DIGITS, "Do not call 6175550100"),
        (FormCodecKind.INTEGER, "not 12"),
    ],
)
def test_text_entry_checks_representation_before_writing(kind, value):
    with pytest.raises(CodecValueError):
        format_for_text_entry(value, FieldCodecSpec(kind=kind))
