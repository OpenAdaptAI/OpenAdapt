"""Planted faults for full-snapshot insert and update qualification read-back."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from openadapt_flow.qualify.campaign import RecordReadback
from openadapt_flow.qualify.record_oracle import judge_records
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr


def effect(kind, **kwargs):
    return Effect(kind=kind, probe="complete record read", **kwargs).resolve({})


def update_effects():
    return [
        effect(
            EffectKind.FIELD_EQUALS,
            match={"id": ValueExpr(literal="1")},
            field="value",
            value=ValueExpr(literal="new"),
        )
    ]


BEFORE = [
    {"id": 1, "value": "old", "untouched": "keep"},
    {"id": 2, "value": "sibling", "untouched": "keep"},
]


def judge(before, after, effects=None, oracle="exact_record"):
    return judge_records(
        before, after, effects or update_effects(), oracle, success=True
    )


def test_declared_update_and_unchanged_fault_case_are_distinguished():
    expected = deepcopy(BEFORE)
    expected[0]["value"] = "new"
    assert judge(BEFORE, expected)["passed"]
    fault = judge(BEFORE, BEFORE, oracle="no_mutation")
    assert fault["passed"] and fault["effect_verdict"] == "REFUTED"
    assert not judge(BEFORE, BEFORE)["passed"]


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_value",
        "wrong_record",
        "collateral_field",
        "missing_record",
        "added_record",
        "duplicate_record",
        "omitted_field",
    ],
)
def test_any_undeclared_change_refuses_update(fault):
    after = deepcopy(BEFORE)
    after[0]["value"] = "new"
    if fault == "wrong_value":
        after[0]["value"] = "wrong"
    if fault == "wrong_record":
        after[0]["value"], after[1]["value"] = "old", "new"
    if fault == "collateral_field":
        after[0]["untouched"] = "changed"
    if fault == "missing_record":
        after.pop()
    if fault == "added_record":
        after.append({"id": 3, "value": "extra"})
    if fault == "duplicate_record":
        after.append(dict(after[0]))
    if fault == "omitted_field":
        del after[0]["untouched"]
    assert not judge(BEFORE, after)["passed"]
    assert not judge(BEFORE, after, oracle="no_mutation")["passed"]


def test_read_failure_is_indeterminate_even_when_baseline_empty():
    save = SimpleNamespace(effects=update_effects())
    current = [[]]
    readback = RecordReadback(
        SimpleNamespace(_fetch_records=lambda: current[0]), save, {}
    )
    readback.snapshot()
    current[0] = None
    result = readback.judge("no_mutation", SimpleNamespace(success=False))
    assert not result["passed"] and result["effect_verdict"] == "INDETERMINATE"


def test_insert_requires_exact_addition_and_preserves_multiplicity():
    effects = [
        effect(
            EffectKind.RECORD_WRITTEN,
            match={"id": ValueExpr(literal="3")},
            expected_count=1,
        )
    ]
    new = {"id": 3, "value": "new"}
    assert judge(BEFORE, [*BEFORE, new], effects)["passed"]
    for after in [
        BEFORE,
        [*BEFORE, new, new],
        [*BEFORE, new, {"id": 4}],
        [BEFORE[0], new],
    ]:
        assert not judge(BEFORE, after, effects)["passed"]
    duplicated_before = [*BEFORE, dict(BEFORE[0])]
    assert not judge(duplicated_before, [*BEFORE, new], effects)["passed"]


def test_insert_exact_new_set_is_checked_independently_of_key_match():
    effects = [
        effect(
            EffectKind.RECORD_WRITTEN,
            match={"id": ValueExpr(literal="3")},
            expected_count=1,
        ),
        effect(
            EffectKind.EXACT_NEW_SET,
            expected_count=1,
            new_records=[
                {"id": ValueExpr(literal="3"), "value": ValueExpr(literal="new")}
            ],
        ),
    ]
    assert judge(BEFORE, [*BEFORE, {"id": 3, "value": "new"}], effects)["passed"]
    assert not judge(BEFORE, [*BEFORE, {"id": 3, "value": "wrong"}], effects)["passed"]


def test_update_supports_numeric_and_null_values_without_allowing_collateral_change():
    for value, expected in [
        (ValueExpr(literal="4", number=True), 4),
        (ValueExpr(null=True), None),
    ]:
        effects = [
            effect(
                EffectKind.FIELD_EQUALS,
                match={"id": ValueExpr(literal="1")},
                field="value",
                value=value,
            )
        ]
        after = deepcopy(BEFORE)
        after[0]["value"] = expected
        assert judge(BEFORE, after, effects)["passed"]
        after[1]["value"] = expected
        assert not judge(BEFORE, after, effects)["passed"]


def test_record_readback_update_state_machine_preserves_the_declared_lens():
    """The public wrapper and model agree across successive typed updates."""
    from hypothesis import given, settings
    from hypothesis import strategies as st

    values = st.one_of(
        st.text(max_size=16), st.none(), st.integers(min_value=-10, max_value=10)
    )

    @settings(max_examples=25, deadline=None)
    @given(st.lists(values, min_size=1, max_size=8))
    def transitions(sequence):
        rows = deepcopy(BEFORE)
        for value in sequence:
            expression = (
                ValueExpr(null=True)
                if value is None
                else ValueExpr(literal=str(value), number=True)
                if isinstance(value, int)
                else ValueExpr(literal=value)
            )
            effects = [
                effect(
                    EffectKind.FIELD_EQUALS,
                    match={"id": ValueExpr(literal="1")},
                    field="value",
                    value=expression,
                )
            ]
            expected = deepcopy(rows)
            expected[0]["value"] = value
            current = [rows]
            readback = RecordReadback(
                SimpleNamespace(_fetch_records=lambda: current[0]),
                SimpleNamespace(effects=effects),
                {},
            )
            readback.snapshot()
            current[0] = list(reversed(expected))
            assert readback.judge("exact_record", SimpleNamespace(success=True))[
                "passed"
            ]
            for planted in [
                expected + [dict(expected[1])],
                [{**expected[0], "untouched": "unauthorized"}, expected[1]],
                expected[:1],
            ]:
                current[0] = planted
                assert not readback.judge(
                    "exact_record", SimpleNamespace(success=True)
                )["passed"]
                assert not readback.judge(
                    "no_mutation", SimpleNamespace(success=False)
                )["passed"]
            current[0] = rows
            assert readback.judge("no_mutation", SimpleNamespace(success=False))[
                "passed"
            ]
            rows = expected

    transitions()
