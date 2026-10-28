"""A contract value can say how the system of record stores what was entered.

OpenEMR's New Patient form saves street and city in capitals. With
``stored: upper`` the check expects exactly the capitals, so a correct save
isn't refuted and a wrong value still is. Records are made up.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import EffectState, ValueExpr


def test_the_stored_form_is_applied_after_the_map() -> None:
    upper = ValueExpr(param="street", stored="upper")
    assert upper.resolve({"street": "41 Larch Way"}) == "41 LARCH WAY"
    coded = ValueExpr(param="state", map={"Massachusetts": "ma"}, stored="upper")
    assert coded.resolve({"state": "Massachusetts"}) == "MA"
    assert coded.resolve({"state": "Maine"}) is None  # fails closed


def test_a_literal_has_no_stored_form() -> None:
    with pytest.raises(ValueError):
        ValueExpr(literal="x", stored="upper")


def test_an_unset_stored_form_keeps_the_contract_digest() -> None:
    assert ValueExpr(param="street").model_dump() == {
        "literal": None,
        "param": "street",
    }


def test_field_equals_expects_the_stored_capitals() -> None:
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": "1"},
        field="street",
        value={"param": "street", "stored": "upper"},
    ).resolve({"street": "41 Larch Way"})
    before = EffectState(substrate="t", reachable=True, records=[])

    def verdict(stored: str) -> str:
        return judge_records(
            effect, before, [{"id": "1", "street": stored}], substrate="t"
        ).verdict.value

    assert verdict("41 LARCH WAY") == "confirmed"
    assert verdict("41 Larch Way") != "confirmed"  # exact: the stored form only
    assert verdict("14 LARCH WAY") != "confirmed"


def test_the_stored_form_uses_full_unicode_case() -> None:
    # OpenEMR's "U" fields upper-case in the browser (toUpperCase): measured
    # on the pinned fixture, "12 Calle Peña" is stored "12 CALLE PEÑA" and
    # "Straße" is stored "STRASSE".
    upper = ValueExpr(param="street", stored="upper")
    assert upper.resolve({"street": "12 Calle Peña"}) == "12 CALLE PEÑA"
    assert upper.resolve({"street": "Straße"}) == "STRASSE"
    lower = ValueExpr(param="email", stored="lower")
    assert lower.resolve({"email": "ÉLodie@Example.TEST"}) == "élodie@example.test"


def test_a_binding_comes_from_one_effect() -> None:
    from openadapt_flow.provenance import bindings, bound_maps

    city = {"param": "city", "stored": "upper"}
    one = [
        Effect(
            kind=EffectKind.FIELD_EQUALS, match={"id": "1"}, field="city", value=city
        )
    ]
    assert bindings(one)["city"][0] == "city"
    assert bound_maps(one)["city"].stored == "upper"
    # The same parameter written to two fields two ways: not compared.
    two = one + [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"id": "1"},
            field="town",
            value={"param": "city"},
        )
    ]
    assert "city" not in bindings(two)
