"""Declared value kinds: formatting alone never refutes, a different value
always does.

A clerk types a phone as ``(617) 555-0142`` or a birth date as
``03/07/1984`` into a US-format field. The record check compares the
expected value and the stored one in the kind's canonical form, so the same
value passes and a transposed digit or a swapped day and month still fails.
Kinds are declared on the contract, never inferred. Records are made up.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    EffectState,
    ValueExpr,
    kind_form,
    record_matches,
)


@pytest.mark.parametrize(
    "kind,value,order,form",
    [
        ("phone", "(617) 555-0142", None, "6175550142"),
        ("phone", "617.555.0142 x12", None, "6175550142x12"),
        ("phone", "617-555-0142 ext. 12", None, "6175550142x12"),
        ("date", "03/07/1984", "mdy", "1984-03-07"),
        ("date", "03/07/1984", "dmy", "1984-07-03"),
        ("date", "03/07/1984", None, None),  # ambiguous: no order declared
        ("date", "1984-03-07 00:00:00", None, "1984-03-07"),
        ("date", "02/31/1984", "mdy", None),
        ("email", " Ada.Quill@Example.TEST ", None, "ada.quill@example.test"),
        ("email", "not an address", None, None),
    ],
)
def test_canonical_forms(kind: str, value: str, order, form) -> None:
    assert kind_form(kind, value, order) == form


def _verdict(value: dict, stored: str, entered: str) -> str:
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS, match={"id": "1"}, field="f", value=value
    ).resolve({"x": entered})
    before = EffectState(substrate="t", reachable=True, records=[])
    return judge_records(
        effect, before, [{"id": "1", "f": stored}], substrate="t"
    ).verdict.value


def test_a_phone_is_compared_by_its_digits() -> None:
    phone = {"param": "x", "kind": "phone"}
    assert _verdict(phone, "(617) 555-0142", "617-555-0142") == "confirmed"
    assert _verdict(phone, "617-555-0124", "617-555-0142") == "refuted"
    # An extension is part of the number.
    assert _verdict(phone, "617-555-0142 x12", "617-555-0142") == "refuted"


def test_a_date_is_compared_as_a_calendar_date() -> None:
    dob = {"param": "x", "kind": "date", "date_order": "mdy"}
    assert _verdict(dob, "1984-03-07", "03/07/1984") == "confirmed"
    # A day and month swapped is a different date.
    assert _verdict(dob, "1984-07-03", "03/07/1984") == "refuted"


def test_an_email_ignores_case() -> None:
    email = {"param": "x", "kind": "email"}
    assert (
        _verdict(email, "ada.quill@example.test", "Ada.Quill@Example.TEST")
        == "confirmed"
    )
    assert (
        _verdict(email, "ada.quil@example.test", "ada.quill@example.test") == "refuted"
    )


def test_without_a_kind_the_comparison_stays_exact() -> None:
    assert _verdict({"param": "x"}, "(617) 555-0142", "617-555-0142") == "refuted"


def test_a_selector_with_a_kind_matches_in_canonical_form() -> None:
    want = ValueExpr(param="email", kind="email").resolved({"email": "ADA@X.TEST"})
    assert record_matches({"email": "ada@x.test"}, {"email": want})
    assert not record_matches({"email": "eda@x.test"}, {"email": want})


def test_date_order_needs_a_date_kind() -> None:
    with pytest.raises(ValueError):
        ValueExpr(param="x", date_order="mdy")


def test_an_unset_kind_keeps_the_contract_digest() -> None:
    assert ValueExpr(param="x").model_dump() == {"literal": None, "param": "x"}


def test_exact_new_set_matches_a_declared_kind() -> None:
    effect = Effect(
        kind=EffectKind.EXACT_NEW_SET,
        match={},
        identity_field="id",
        new_records=[{"phone": {"param": "x", "kind": "phone"}}],
    ).resolve({"x": "(617) 555-0142"})
    before = EffectState(substrate="t", reachable=True, records=[])

    def verdict(stored: str) -> str:
        return judge_records(
            effect, before, [{"id": "1", "phone": stored}], substrate="t"
        ).verdict.value

    assert verdict("617-555-0142") == "confirmed"
    assert verdict("617-555-0124") == "refuted"


def test_a_duplicate_key_with_a_kind_matches_in_canonical_form() -> None:
    email = {"param": "x", "kind": "email"}
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"email": email},
        idempotency_key=email,
        key_field="email",
    ).resolve({"x": "Ada.Quill@Example.TEST"})
    before = EffectState(substrate="t", reachable=True, records=[])
    verdict = judge_records(
        effect, before, [{"id": "1", "email": "Ada.Quill@Example.TEST"}], substrate="t"
    )
    assert verdict.verdict.value == "confirmed"


# -- round 2 of the review ---------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    ["617-555-0142 do not call", "Tel 617 555 0142", "617-555-0142; 617-555-0199"],
)
def test_a_phone_with_any_other_text_never_matches(stored: str) -> None:
    want = ValueExpr(param="p", kind="phone").resolved({"p": "617-555-0142"})
    assert not want.matches(stored)


@pytest.mark.parametrize(
    "value,form",
    [
        ("1984-03-07 00:00:00", "1984-03-07"),
        ("1984-03-07T23:30:00", "1984-03-07"),
        ("1984-03-07 junk", None),
        ("1984-03-07T23:30:00-05:00", None),
    ],
)
def test_only_a_time_of_day_may_follow_an_iso_date(value: str, form) -> None:
    assert kind_form("date", value) == form


def test_resolve_returns_the_entered_value() -> None:
    # Kinds apply only where values are compared, so an on-screen read-back
    # or an undo request sees what was typed.
    expr = ValueExpr(param="p", kind="phone")
    assert expr.resolve({"p": "(617) 555-0142"}) == "(617) 555-0142"
    assert expr.resolved({"p": "(617) 555-0142"}).matches("617-555-0142")


def test_the_screen_read_back_and_the_record_check_share_one_normalizer() -> None:
    from openadapt_flow.runtime.effects.readback_values import compare_value

    assert compare_value("(617) 555-0142", "617-555-0142", kind="phone") == "match"
    assert (
        compare_value("617-555-0142 call me", "617-555-0142", kind="phone")
        == "mismatch"
    )
    assert compare_value("ADA@X.TEST", "ada@x.test", kind="email") == "match"


def _round_trip(printed: str, stored: str, order) -> str:
    from openadapt_flow.intake.document import IntakeDocument
    from openadapt_flow.provenance import (
        ParamProvenance,
        ProvenanceFile,
        SourceRef,
        bound_fields,
        bound_maps,
        default_extractor,
        round_trip,
        sha256_bytes,
    )

    document = IntakeDocument("r.txt", "text", [f"Date of birth: {printed}"])
    sha = sha256_bytes(document.text.encode("utf-8"))
    prov = ProvenanceFile(
        params={
            "DOB": ParamProvenance(
                value=printed,
                source=SourceRef(
                    kind="document", sha256=sha, line=1, label="Date of birth"
                ),
                extractor="model:first-reader",
                value_kind="date",
                date_order=order,
            )
        }
    )
    effects = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"id": "1"},
            field="DOB",
            value={"param": "DOB", "kind": "date", "date_order": "mdy"},
        )
    ]
    return round_trip(
        prov,
        {"id": 1, "DOB": stored},
        bound_fields(effects),
        {sha: document},
        default_extractor(prov),
        maps=bound_maps(effects),
    ).status


def test_the_document_is_read_in_its_own_date_order() -> None:
    # The referral prints 03/07/1984 day-first (7 March): the app, set to
    # month-day, stored 1984-03-07 from a typed "03/07/1984". The round trip
    # reads the document day-first and refutes the misread.
    assert _round_trip("03/07/1984", "1984-03-07", "dmy") == "refuted"
    assert _round_trip("03/07/1984", "1984-07-03", "dmy") == "held"
    # With no order for the document, a date it prints two ways stays unchecked.
    assert _round_trip("03/07/1984", "1984-03-07", None) == "not_checked"
