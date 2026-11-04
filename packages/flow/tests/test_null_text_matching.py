"""A null never equals the text "None" in the rollback and qualification
read-backs, and the reward worker's SQL channel compares numbers by value."""

from __future__ import annotations

from openadapt_flow.qualify.campaign import _same_field
from openadapt_flow.rollback import _same_text
from openadapt_flow.runtime.effects._common import TYPED_SUBSTRATES, judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    Verdict,
)


def test_rollback_null_equals_only_null() -> None:
    assert _same_text(None, None)
    assert not _same_text(None, "None")
    assert not _same_text("None", None)
    assert not _same_text("", None)
    assert _same_text(3, "3")
    assert not _same_text("2.0", "2")


def test_qualification_null_needs_a_present_null_field() -> None:
    assert _same_field({"note": None}, "note", None)
    assert not _same_field({}, "note", None)
    assert not _same_field({"note": "None"}, "note", None)
    assert not _same_field({"note": None}, "note", "None")
    assert _same_field({"id": 7}, "id", "7")


def test_reward_db_channel_is_typed() -> None:
    assert "db" in TYPED_SUBSTRATES
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="qty",
        value="2.0",
        match={"id": "1"},
    )
    after = [{"id": 1, "qty": 2}]
    for substrate in ("sql", "db"):
        before = EffectState(substrate=substrate, reachable=True, records=after)
        verdict = judge_records(effect, before, after, substrate=substrate)
        assert verdict.verdict is Verdict.CONFIRMED, substrate
