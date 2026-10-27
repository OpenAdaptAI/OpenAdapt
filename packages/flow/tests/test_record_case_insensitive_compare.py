"""Tests for case-insensitive and typed field comparisons in record contracts (Fixes #1524)."""

import importlib.util
from pathlib import Path

import yaml

from openadapt_flow.qualify.campaign import record_effects
from openadapt_flow.qualify.config import VerifyConfig
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
    Verdict,
    record_matches,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.verify_run import parse_verify_config


def test_value_expr_compare_field():
    expr = ValueExpr(param="street", compare="text_ci")
    assert expr.param == "street"
    assert expr.compare == "text_ci"

    # resolve preserves param lookup
    params = {"street": "41 Larch Way"}
    assert expr.resolve(params) == "41 Larch Way"

    # resolved copy keeps compare
    bound = expr.resolved(params)
    assert bound.literal == "41 Larch Way"
    assert bound.compare == "text_ci"

    # serialization omits compare when None, keeps when set
    plain = ValueExpr(param="street")
    assert "compare" not in plain.model_dump(mode="json")
    assert expr.model_dump(mode="json")["compare"] == "text_ci"


def test_value_expr_equality_and_hash():
    e1 = ValueExpr(param="street", compare="text_ci")
    e2 = ValueExpr(param="street", compare="text_ci")
    e3 = ValueExpr(param="street")
    assert e1 == e2
    assert e1 != e3
    assert hash(e1) == hash(e2)
    assert hash(e1) != hash(e3)


def test_effect_inherits_compare_from_value_expr():
    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="street",
        value=ValueExpr(param="street", compare="text_ci"),
    )
    assert eff.compare == "text_ci"

    bound = eff.resolve({"street": "41 Larch Way"})
    assert bound.compare == "text_ci"
    assert bound.value.literal == "41 Larch Way"
    assert bound.value.compare == "text_ci"


def test_record_effects_defaults_address_fields():
    cfg = VerifyConfig.model_validate(
        {
            "schema_version": "openadapt.verify/v1",
            "app": {"name": "test", "version": "1.0", "url": "http://127.0.0.1:8000"},
            "verifier": {"kind": "rest", "base_url": "http://127.0.0.1:8000"},
            "record": {
                "name": "patient",
                "match": ["email"],
                "fields": {
                    "email": "email",
                    "fname": "fname",
                    "street": "street",
                    "city": "city",
                },
            },
        }
    )
    effects = record_effects(cfg)
    by_field = {e.field: e for e in effects if e.kind is EffectKind.FIELD_EQUALS}

    assert by_field["street"].compare == "text_ci"
    assert by_field["city"].compare == "text_ci"
    assert by_field["fname"].compare is None


def test_judge_field_equals_case_insensitive():
    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"email": ValueExpr(literal="w.priorchart@example.invalid")},
        field="street",
        value=ValueExpr(literal="41 Larch Way", compare="text_ci"),
    )
    before = EffectState(substrate="rest", reachable=True, records=[])

    # OpenEMR saves uppercase; referral requested mixed-case
    current = [
        {
            "email": "w.priorchart@example.invalid",
            "street": "41 LARCH WAY",
            "city": "CAMBRIDGE",
        }
    ]
    verdict = judge_records(eff, before, current, substrate="rest")
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.observed_value == "41 LARCH WAY"
    assert verdict.expected_value == "41 Larch Way"

    # Real mismatch must still be refuted
    wrong_street = [
        {
            "email": "w.priorchart@example.invalid",
            "street": "42 LARCH WAY",
            "city": "CAMBRIDGE",
        }
    ]
    refuted = judge_records(eff, before, wrong_street, substrate="rest")
    assert refuted.verdict is Verdict.REFUTED


def test_record_matches_with_compare():
    record = {"email": "test@example.com", "city": "CAMBRIDGE"}
    selector_ci = {
        "email": "test@example.com",
        "city": ValueExpr(literal="Cambridge", compare="text_ci"),
    }
    selector_exact = {
        "email": "test@example.com",
        "city": "Cambridge",
    }
    assert record_matches(record, selector_ci) is True
    assert record_matches(record, selector_exact) is False


def _load_claims_openemr_verify_yaml() -> str:
    path = (
        Path(__file__).resolve().parents[1] / "benchmark/scoreboard/claims_openemr.py"
    )
    spec = importlib.util.spec_from_file_location("claims_openemr", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VERIFY_YAML


def test_openemr_verify_yaml_contract_confirms_mixed_case():
    verify_yaml = _load_claims_openemr_verify_yaml()
    formatted = verify_yaml.format(
        api="http://127.0.0.1:9300", token_env="TEST_TOKEN_ENV"
    )
    raw = yaml.safe_load(formatted)
    cfg = parse_verify_config(raw)

    params = {
        "email": "jane.doe@example.com",
        "lname": "Doe",
        "title": "Ms.",
        "fname": "Jane",
        "DOB": "1990-01-01",
        "sex": "Female",
        "street": "41 Larch Way",
        "city": "Cambridge",
        "postal_code": "02138",
        "phone_home": "555-0199",
        "country_code": "USA",
    }

    bound_effects = [e.resolve(params) for e in cfg.effects]
    by_field = {e.field: e for e in bound_effects if e.kind is EffectKind.FIELD_EQUALS}

    assert by_field["street"].compare == "text_ci"
    assert by_field["city"].compare == "text_ci"

    # System of record holding uppercase values saved by OpenEMR
    saved_record = {
        "email": "jane.doe@example.com",
        "lname": "Doe",
        "title": "Ms.",
        "fname": "Jane",
        "DOB": "1990-01-01",
        "sex": "Female",
        "street": "41 LARCH WAY",
        "city": "CAMBRIDGE",
        "state": "MA",
        "postal_code": "02138",
        "phone_home": "555-0199",
        "country_code": "USA",
    }
    before = EffectState(substrate="rest", reachable=True, records=[])

    verdicts = [
        judge_records(e, before, [saved_record], substrate="rest")
        for e in bound_effects
    ]
    for v in verdicts:
        assert v.verdict is Verdict.CONFIRMED, f"Effect failed: {v.reason}"
