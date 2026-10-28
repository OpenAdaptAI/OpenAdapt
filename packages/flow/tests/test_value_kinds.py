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
