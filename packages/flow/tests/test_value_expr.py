"""Unit contract for :class:`ValueExpr` and the parameterized :class:`Effect`.

Pins the P0-3 primitives: a literal expression is transparently
string-compatible (so every v1 reader is unaffected), a param expression
resolves against run params, and the coercion validators accept the old
plain-string JSON form byte-for-byte.
"""

from __future__ import annotations

import json

from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

# -- ValueExpr: transparent string-compatibility for a literal ---------------


def test_literal_value_expr_is_string_compatible():
    v = ValueExpr(literal="phil")
    assert v == "phil"
    assert v == ValueExpr(literal="phil")
    assert v != "susan"
    assert str(v) == "phil"
    assert repr(v) == "'phil'"
    # Equal to its string AND hashes identically -> collides with it in sets.
    assert hash(v) == hash("phil")
    assert v in {"phil"}
    assert {"patient": v} == {"patient": "phil"}


def test_param_value_expr_resolves_against_params():
    v = ValueExpr(param="patient_id")
    assert v.resolve({"patient_id": "susan"}) == "susan"
    # Unsupplied -> None (fail-safe: an unresolved selector matches nothing).
    assert v.resolve({}) is None
    assert str(v) == "{patient_id}"
    # A param expr never equals a bare string (it is not a literal).
    assert v != "patient_id"


def test_resolved_returns_pure_literal():
    assert ValueExpr(param="p").resolved({"p": "x"}) == ValueExpr(literal="x")
    assert ValueExpr(literal="x").resolved({"p": "y"}) == ValueExpr(literal="x")


# -- Effect coercion: v1 plain-string form loads unchanged -------------------


def test_effect_coerces_plain_strings_from_v1_json():
    e = Effect.model_validate(
        {
            "kind": "field_equals",
            "match": {"patient_id": "p1", "type": "Triage"},
            "field": "note",
            "value": "Phil",
            "idempotency_key": "k-1",
        }
    )
    assert isinstance(e.value, ValueExpr)
    assert e.match == {"patient_id": "p1", "type": "Triage"}
    assert e.value == "Phil"
    assert e.idempotency_key == "k-1"


def test_validate_assignment_coerces_raw_string():
    # The compiler assigns a raw string (effect_mining); validate_assignment
    # must coerce it to a ValueExpr so the field type stays consistent.
    e = Effect(kind=EffectKind.RECORD_WRITTEN, match={"patient_id": "p1"})
    e.idempotency_key = "mined-key"
    assert isinstance(e.idempotency_key, ValueExpr)
    assert e.idempotency_key == "mined-key"


def test_effect_json_roundtrips():
    e = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": ValueExpr(param="pid")},
        field="note",
        value=ValueExpr(literal="static"),
    )
    e2 = Effect.model_validate_json(e.model_dump_json())
    assert e2.match["patient_id"].param == "pid"
    assert e2.value == "static"


# -- Effect.resolve + contract_hash ------------------------------------------


def test_effect_resolve_binds_all_value_exprs():
    e = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": ValueExpr(param="pid")},
        field="note",
        value=ValueExpr(param="note"),
        idempotency_key=ValueExpr(param="pid"),
    )
    r = e.resolve({"pid": "susan", "note": "hello"})
    assert r.match == {"patient_id": "susan"}
    assert r.value == "hello"
    assert r.idempotency_key == "susan"
    # A pure-literal effect resolves to itself.
    lit = Effect(kind=EffectKind.RECORD_WRITTEN, match={"patient_id": "p1"})
    assert lit.resolve({"anything": "x"}).match == {"patient_id": "p1"}


def test_contract_hash_is_stable_and_value_sensitive():
    e = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": ValueExpr(param="pid")},
        field="note",
        value=ValueExpr(param="note"),
    )
    h_susan = e.resolve({"pid": "susan", "note": "n1"}).contract_hash()
    h_susan2 = e.resolve({"pid": "susan", "note": "n1"}).contract_hash()
    h_phil = e.resolve({"pid": "phil", "note": "n1"}).contract_hash()
    assert h_susan.startswith("sha256:")
    assert h_susan == h_susan2  # deterministic for the same resolved contract
    assert h_susan != h_phil  # sensitive to the resolved value
    # One-way digest: it does not embed the underlying value.
    assert "susan" not in h_susan
    assert json.loads(json.dumps({"h": h_susan}))  # plain-serializable
