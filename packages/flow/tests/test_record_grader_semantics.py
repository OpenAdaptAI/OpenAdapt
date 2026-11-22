"""One explicit value contract across record selectors, fields and answers."""

from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest

from openadapt_flow.rl_env.record_grader import _number, _value_ok, diff, grade
from openadapt_flow.rl_env.reward import RecordJudge
from openadapt_flow.rl_env.tasks import Task


def inserted(value, *, present=True):
    before = {"tables": {"records": {"key": ["id"], "rows": {}}}}
    row = {"id": 1}
    if present:
        row["value"] = value
    after = {"tables": {"records": {"key": ["id"], "rows": {"1": row}}}}
    return before, after


def result_for(expected, value, *, present=True):
    before, after = inserted(value, present=present)
    spec = {
        "expect": [{"table": "records", "op": "insert", "fields": {"value": expected}}]
    }
    return grade(spec, diff(before, after), claim="success")


def test_exact_numeric_matrix_matches_an_independent_rational_oracle():
    numbers = [
        "0",
        "-0",
        "+00.000",
        "1",
        "1.0",
        "01e0",
        "0.1",
        "1e-1",
        "0.1000001",
        "0.1000002",
        "9007199254740992",
        "9007199254740993",
        "120.0000001",
        "120.0000002",
        "-120.0000001",
        "-120.0000002",
        "100000000000000000000",
        "1e20",
        "9" * 400,
        "1." + "0" * 80 + "1",
        "1." + "0" * 80 + "2",
    ]
    for wanted in numbers:
        for observed in numbers:
            equal = Fraction(wanted) == Fraction(observed)
            contract = {"$number": wanted}
            assert (result_for(contract, observed).reward == 1) is equal, (
                wanted,
                observed,
            )
            assert _value_ok({"$in": [contract]}, observed, {}) is equal
            assert _value_ok({"$not": contract}, observed, {}) is not equal
            answer = grade(
                {"answer": {"value": contract}},
                [],
                claim="success",
                message=f"ANSWER: {observed}",
            )
            assert (answer.reward == 1) is equal, (wanted, observed)
            before = {
                "tables": {
                    "records": {
                        "key": ["id"],
                        "rows": {"1": {"id": 1, "code": observed, "value": 0}},
                    }
                }
            }
            after = copy.deepcopy(before)
            after["tables"]["records"]["rows"]["1"]["value"] = 1
            spec = {
                "expect": [
                    {
                        "table": "records",
                        "op": "update",
                        "where": {"code": contract},
                        "fields": {"value": 1},
                        "columns_within": ["value"],
                    }
                ]
            }
            state = {"record": before}
            judge = RecordJudge(lambda: copy.deepcopy(state["record"]), spec)
            state["record"] = after
            assert (judge.judge("success", True).reward == 1) is equal
            judge.close()


@pytest.mark.parametrize(
    ("expected", "observed", "accept"),
    [
        (120, "00120.0000", True),
        (1e20, "100000000000000000000", True),
        ("00123", "00123", True),
        ("00123", "123", False),
        ("RecordA", "recorda", False),
        ("A B", "A  B", False),
        ("ID ", "ID", False),
        ("123", 123, False),
        ("120.00", "120", False),
        (120, False, False),
        (0, False, False),
        (True, True, True),
        (True, 1, False),
        (None, None, True),
        (None, "", False),
        ({"$number": "120.0000001"}, "120.0000002", False),
        ({"$number": "9007199254740993"}, "9007199254740992", False),
    ],
)
def test_literal_text_numeric_and_boolean_contracts_do_not_blend(
    expected, observed, accept
):
    assert (result_for(expected, observed).reward == 1) is accept


@pytest.mark.parametrize(
    "contract",
    [
        None,
        {"$any": True},
        {"$not": None},
        {"$not": {"$number": "3"}},
        {"$ref": "selected.value"},
        {"$not": {"$ref": "selected.value"}},
    ],
)
def test_absent_fields_and_references_never_become_values_or_negated_success(contract):
    assert result_for(contract, None, present=False).reward == 0
    if contract is not None:
        assert not _value_ok(contract, None, {}) or contract == {"$any": True}
    assert _value_ok(None, None, {})
    assert _value_ok({"$ref": "selected.value"}, None, {"selected": {"value": None}})


@pytest.mark.parametrize(
    "contract",
    [
        {"$number": True},
        {"$number": 1},
        {"$number": "NaN"},
        {"$number": "Infinity"},
        {"$number": "1e4097"},
        {"$number": "1e999999999999999999999999"},
        {"$number": "9" * 4097},
        {"$number": ""},
        {"$any": 1},
        {"$any": False},
        {"$number": "1", "$any": True},
        {"$unknown": 1},
        {"$not": {"$number": "NaN"}},
        {"$not": {"$number": "1e999999"}},
        {"$not": {"$ref": "missing.value"}},
        {"$contains": ""},
        {"$in": [1, {"$number": "NaN"}]},
        float("nan"),
        float("inf"),
    ],
)
def test_malformed_or_unbounded_contracts_refuse_even_under_negation(contract):
    assert not _value_ok(contract, "1", {})
    assert not _value_ok({"$not": contract}, "1", {})


def test_numeric_resource_bounds_do_not_expand_exponents_or_use_float_rounding():
    assert _number("1e4096") == "1e4096"
    assert _number("1e-4096") == "1e-4096"
    assert _number("1e4097") is None
    assert _number("1e-4097") is None
    assert _number("1e9999999999999999999999999") is None
    assert _number("9" * 4097) is None
    assert _number("1" + "0" * 3999) == "1e3999"
    assert _number(True) is None
    assert _number("1.0000000000000000001") != _number("1.0000000000000000002")


def test_union_domains_and_reference_data_cannot_create_matcher_authority():
    assert _value_ok({"$in": [1, "literal"]}, "literal", {})
    assert _value_ok({"$in": [1, "literal"]}, 1, {})
    assert not _value_ok({"$not": 1}, "not a number", {})
    bound = {"chosen": {"value": {"$any": True}}}
    assert _value_ok({"$ref": "chosen.value"}, {"$any": True}, bound)
    assert not _value_ok({"$ref": "chosen.value"}, "arbitrary", bound)


@pytest.mark.parametrize(
    ("expected", "message", "accept"),
    [
        (9, "ANSWER: $9.00", True),
        (9, "ANSWER: not 9", False),
        (9, "ANSWER: 9 or one hundred", False),
        (9, "ANSWER: 9 units", False),
        (1200, "ANSWER: 1,200", False),
        (1200, "ANSWER: 1200", True),
        ("00123", "ANSWER: 00123", True),
        ("00123", "ANSWER: 123", False),
        ("RecordA", "ANSWER: recorda", False),
        ("RecordA", "ANSWER: RecordA", True),
        ("A B", "ANSWER: A  B", False),
        ("00123", "ANSWER: 000123", False),
        ("00123", "ANSWER: 00123.0", False),
        ("00123", "ANSWER: -00123", False),
        ("00123", "ANSWER: ID00123", False),
        ({"$number": "9007199254740993"}, "ANSWER: 9007199254740992", False),
        ({"$number": "120.0000001"}, "ANSWER: 120.0000002", False),
    ],
)
def test_answer_contracts_are_values_only_and_preserve_literal_identifiers(
    expected, message, accept
):
    result = grade(
        {"answer": {"value": expected}}, [], claim="success", message=message
    )
    assert (result.reward == 1) is accept


def test_mixed_answer_values_and_numeric_token_boundaries_are_explicit():
    spec = {"answer": {"all_of": ["Acme", 120]}}
    assert (
        grade(
            spec, [], claim="success", message="Context outside.\nANSWER: Acme; $120.00"
        ).reward
        == 1
    )
    assert (
        grade(spec, [], claim="success", message="ANSWER: Acme owes $120.00").reward
        == 0
    )
    arithmetic = {"answer": {"all_of": [9, -10]}}
    assert grade(arithmetic, [], claim="success", message="ANSWER: 9; -10").reward == 1
    assert grade(arithmetic, [], claim="success", message="ANSWER: 9-10").reward == 0


def test_task_json_roundtrip_preserves_exact_decimal_tags_and_literal_ids():
    task = Task.model_validate(
        {
            "id": "synthetic",
            "instruction": "Check synthetic data.",
            "grade": {
                "expect": [
                    {
                        "table": "records",
                        "fields": {
                            "amount": {"$number": "120.0000000000000000001"},
                            "code": "00123",
                        },
                    }
                ]
            },
        }
    )
    restored = Task.model_validate(json.loads(task.model_dump_json()))
    fields = restored.grade["expect"][0]["fields"]
    assert fields == task.grade["expect"][0]["fields"]
    assert _value_ok(fields["amount"], "120.0000000000000000001", {})
    assert not _value_ok(fields["amount"], "120.0000000000000000002", {})
    assert not _value_ok(fields["code"], "123", {})


@pytest.mark.parametrize(
    "invalid",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"nested": float("nan")},
        [float("inf")],
    ],
)
def test_non_json_numeric_values_are_not_admitted_by_any_or_negation(invalid):
    assert not _value_ok({"$any": True}, invalid, {})
    assert not _value_ok({"$not": None}, invalid, {})
    assert not _value_ok({"$not": invalid}, "ordinary", {})
    assert not _value_ok(
        {"$not": {"$ref": "selected.value"}},
        "ordinary",
        {"selected": {"value": invalid}},
    )
