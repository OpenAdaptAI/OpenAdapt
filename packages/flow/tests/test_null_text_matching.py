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


def _restore_presence(tmp_path, value, params, fresh_note):
    """fresh_presence for a field edit of ``note`` whose old value was "old"."""
    from openadapt_flow.after_stop import keep_prior_state
    from openadapt_flow.ir import (
        ActionKind,
        ChangeLedgerEntry,
        Step,
        UndoRestore,
        UndoStep,
        Workflow,
    )
    from openadapt_flow.rollback import fresh_presence

    edit = Step(
        id="edit_note",
        intent="edit the note",
        action=ActionKind.KEY,
        key="Enter",
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"id": "r1"},
                field="note",
                value=value,
                risk="irreversible",
            )
        ],
    )
    undo = UndoStep(
        id="restore_note",
        undoes="edit_note",
        intent="put the old note back",
        source="hand_written",
        kind="restore",
        restore=UndoRestore(match={"id": "r1"}, fields={"note": "old_note"}),
        steps=[
            Step(
                id="put_back",
                intent="type the old note",
                action=ActionKind.KEY,
                key="Enter",
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"id": "r1"},
                        field="note",
                        value={"param": "old_note"},
                    )
                ],
            )
        ],
    )
    workflow = Workflow(name="w", steps=[edit], undo_steps=[undo])
    kept = keep_prior_state(
        workflow, edit, [{"id": "r1", "note": "old"}], params, tmp_path
    )
    assert kept is not None
    entry = ChangeLedgerEntry(
        position=1,
        step_id="edit_note",
        intent="edit the note",
        attempt_state="delivery_uncertain",
        status="unknown",
        undo_id="restore_note",
    )
    return fresh_presence(
        entry,
        undo,
        workflow,
        run_dir=tmp_path,
        params=params,
        read_records=lambda: [{"id": "r1", "note": fresh_note}],
    )


def test_a_value_its_map_does_not_name_is_unknown_not_a_null(tmp_path) -> None:
    mapped = {"param": "new", "map": {"known": "mapped"}}
    # The supplied value is outside the map: it resolves to nothing, which
    # says nothing about whether the write landed.
    assert _restore_presence(tmp_path / "a", mapped, {"new": "unmapped"}, None)[0] == (
        "unknown"
    )
    # Controls: a parameter passed as None is an explicit null ...
    assert _restore_presence(tmp_path / "b", {"param": "new"}, {"new": None}, None)[
        0
    ] == ("present")
    # ... an unset one has no value ...
    assert _restore_presence(tmp_path / "c", {"param": "new"}, {}, None)[0] == (
        "unknown"
    )
    # ... and a mapped value reads back as its code.
    assert _restore_presence(tmp_path / "d", mapped, {"new": "known"}, "mapped")[0] == (
        "present"
    )
