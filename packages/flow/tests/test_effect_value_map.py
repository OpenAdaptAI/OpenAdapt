"""A record check that reads an entered label back as its stored code.

A form often shows one value and stores another: OpenEMR's state select shows
"Massachusetts" and stores "MA". A ``ValueExpr`` may name a parameter with a
``map`` from each value the run may enter to the value the system of record
stores, so the record check reads the entered value back. A value the map
doesn't name resolves to nothing and the check fails closed. A value without a
map serializes exactly as before, so existing contract hashes don't change.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

STATES = {"Massachusetts": "MA", "Maine": "ME"}


def _effect(value: ValueExpr) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"email": ValueExpr(param="email")},
        field="state",
        value=value,
    )


def test_an_entered_label_resolves_to_its_stored_code():
    expr = ValueExpr(param="state_label", map=STATES)
    assert expr.resolve({"state_label": "Massachusetts"}) == "MA"
    resolved = _effect(expr).resolve(
        {"state_label": "Massachusetts", "email": "a@example.invalid"}
    )
    assert resolved.value == "MA"


def test_a_value_the_map_does_not_name_fails_closed():
    expr = ValueExpr(param="state_label", map=STATES)
    assert expr.resolve({"state_label": "Vermont"}) is None
    assert expr.resolve({}) is None


def test_a_value_without_a_map_is_unchanged():
    plain = ValueExpr(param="state_label")
    assert "map" not in plain.model_dump()
    assert plain.model_dump() == {"literal": None, "param": "state_label"}
    assert (
        _effect(plain).contract_hash()
        == _effect(ValueExpr.model_validate({"param": "state_label"})).contract_hash()
    )
    assert plain.resolve({"state_label": "Massachusetts"}) == "Massachusetts"


def test_the_map_is_part_of_the_contract():
    mapped = ValueExpr(param="state_label", map=STATES)
    other = ValueExpr(param="state_label", map={"Massachusetts": "MX"})
    assert mapped != ValueExpr(param="state_label")
    assert mapped != other
    assert hash(mapped) != hash(other)
    assert _effect(mapped).contract_hash() != _effect(other).contract_hash()
    assert (
        _effect(mapped).contract_hash()
        != _effect(ValueExpr(param="state_label")).contract_hash()
    )


def test_a_map_needs_a_parameter_and_entries():
    with pytest.raises(ValidationError):
        ValueExpr(literal="MA", map=STATES)
    with pytest.raises(ValidationError):
        ValueExpr(param="state_label", map={})


def test_verify_yaml_declares_the_map_and_coverage_reads_the_input():
    from openadapt_flow.approval_mode.coverage import _effect_read_params
    from openadapt_flow.qualify import parse_verify_config

    config = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "app": {"name": "Demo", "version": "1", "url": "http://127.0.0.1:9301/"},
            "verifier": {
                "kind": "rest",
                "base_url": "http://127.0.0.1:9300",
                "records_path": "/api/patient",
                "records_key": "data",
            },
            "record": {
                "name": "patient",
                "match": ["email"],
                "fields": {
                    "email": "email",
                    "state": {"param": "state_label", "map": STATES},
                },
            },
        },
        env={},
    )
    expr = config.record.expr("state")
    assert expr == ValueExpr(param="state_label", map=STATES)
    assert "state_label" in _effect_read_params(_effect(expr))


def test_verify_yaml_can_require_exactly_the_new_record():
    from openadapt_flow.approval_mode.coverage import save_contract_problems
    from openadapt_flow.ir import ActionKind, Step
    from openadapt_flow.qualify import parse_verify_config
    from openadapt_flow.qualify.campaign import record_effects

    config = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "app": {"name": "Demo", "version": "1", "url": "http://127.0.0.1:9301/"},
            "verifier": {
                "kind": "rest",
                "base_url": "http://127.0.0.1:9300",
                "records_path": "/api/patient",
                "records_key": "data",
            },
            "record": {
                "name": "patient",
                "match": ["email"],
                "duplicate_key": "email",
                "exact_new_set": True,
                "identity_field": "uuid",
                "fields": {
                    "email": "email",
                    "state": {"param": "state_label", "map": STATES},
                },
            },
        },
        env={},
    )
    effects = record_effects(config)
    exact = [e for e in effects if e.kind is EffectKind.EXACT_NEW_SET]
    assert len(exact) == 1
    assert exact[0].identity_field == "uuid"
    assert exact[0].new_records == [
        {
            "email": ValueExpr(param="email"),
            "state": ValueExpr(param="state_label", map=STATES),
        }
    ]
    save = Step(id="save", intent="save", action=ActionKind.CLICK, effects=effects)
    assert "update_needs_field_delta_check" not in save_contract_problems(save)
