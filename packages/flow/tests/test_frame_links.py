"""Frame links: audit entries keyed by a record field, and side records.

OpenEMR's audit log names a patient (``pid``) while its ``pnotes`` and
``lists`` rows are told apart by their own ``id``, and its New Patient path
adds one ``history_data`` row beside each new chart. Records are made up.
"""

from __future__ import annotations

from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects.effect import EffectState
from openadapt_flow.runtime.effects.frame import (
    FrameAttribution,
    FrameSideRecord,
    FrameSpec,
    judge_frame,
)


def _state(*records: dict) -> EffectState:
    return EffectState(
        substrate="t", reachable=True, records=[dict(r) for r in records]
    )


ATTRIBUTION = FrameAttribution(
    kind="audit_read",
    actor_field="user",
    record_field="patient_id",
    record_key="pid",
    **{"self": ["bot"], "read": {"kind": "sql", "sql_query": "SELECT 1"}},
)
NOTE = {"id": 10, "pid": 1, "body": "follow-up"}


def _notes_change(spec: FrameSpec, author: str) -> str:
    verdict = judge_frame(
        spec,
        [Effect(kind=EffectKind.RECORD_WRITTEN, match={"lname": "Newchart"})],
        {"": (_state(NOTE), [{**NOTE, "body": "changed"}])},
        audit_before=[],
        audit_after=[{"id": 1, "user": author, "patient_id": "1"}],
    )
    return verdict.status


def test_an_audit_entry_names_a_row_through_its_record_key() -> None:
    spec = FrameSpec(identity_field="id", attribution=ATTRIBUTION)
    # Another user's edit to patient 1's note: concurrent, not collateral.
    assert _notes_change(spec, "clinic2") == "held"
    # The bot's own: collateral.
    assert _notes_change(spec, "bot") == "refuted"


def test_without_a_record_key_the_entry_can_t_name_the_row() -> None:
    plain = ATTRIBUTION.model_copy(update={"record_key": None})
    spec = FrameSpec(identity_field="id", attribution=plain)
    assert _notes_change(spec, "clinic2") == "refuted"  # unattributed


PATIENT = Effect(
    kind=EffectKind.RECORD_WRITTEN,
    match={"lname": "Newchart"},
    count_new_only=True,
    probe="surface:patient",
)
SIDE = FrameSideRecord(
    surface="history", link_field="pid", of_surface="patient", of_field="pid"
)


def _with_history(*history_after: dict) -> str:
    verdict = judge_frame(
        FrameSpec(identity_field="id", side_records=[SIDE]),
        [PATIENT],
        {
            "patient": (
                _state({"id": 1, "pid": 1, "lname": "Old"}),
                [
                    {"id": 1, "pid": 1, "lname": "Old"},
                    {"id": 2, "pid": 2, "lname": "Newchart"},
                ],
            ),
            "history": (
                _state({"id": 5, "pid": 1}),
                [{"id": 5, "pid": 1}, *history_after],
            ),
        },
        routes={"0": ["patient"]},
    )
    return verdict.status


def test_a_side_record_of_the_new_chart_is_part_of_the_save() -> None:
    assert _with_history({"id": 6, "pid": 2}) == "held"


def test_a_missing_or_doubled_side_record_refutes() -> None:
    assert _with_history() == "refuted"
    assert _with_history({"id": 6, "pid": 2}, {"id": 7, "pid": 2}) == "refuted"


def test_a_row_for_another_chart_is_still_collateral() -> None:
    assert _with_history({"id": 6, "pid": 2}, {"id": 7, "pid": 1}) == "refuted"


# Adversarial review of #1570: each of these ended ``held``.

_TARGET = [Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "10"}, field="body")]
_BOT_ON_1 = {"id": 100, "user": "bot", "patient_id": "1"}


def _judge(before: list[dict], after: list[dict], audit: list[dict]) -> str:
    spec = FrameSpec(identity_field="id", attribution=ATTRIBUTION)
    return judge_frame(
        spec,
        _TARGET,
        {"": (_state(*before), after)},
        audit_before=[],
        audit_after=audit,
    ).status


def test_a_row_moved_to_another_record_key_is_never_excused() -> None:
    moved = {"id": 11, "pid": 1, "body": "allergy"}
    status = _judge(
        [NOTE | {"body": "old"}, moved],
        [NOTE, {**moved, "pid": 2}],
        [_BOT_ON_1, {"id": 101, "user": "nurse", "patient_id": "2"}],
    )
    assert status == "refuted"


def test_a_bot_entry_is_not_excused_by_a_row_id_that_equals_it() -> None:
    other = {"id": 5, "pid": 3, "body": "x"}
    status = _judge(
        [NOTE | {"body": "old"}, other],
        [NOTE, {**other, "body": "y"}],
        [
            _BOT_ON_1,
            {"id": 101, "user": "nurse", "patient_id": "3"},
            {"id": 102, "user": "bot", "patient_id": "5"},
        ],
    )
    assert status == "refuted"


def test_a_declared_record_without_its_record_key_is_not_checked() -> None:
    status = _judge(
        [{"id": 10, "body": "old"}],
        [{"id": 10, "body": "new"}],
        [{"id": 102, "user": "bot"}],
    )
    assert status == "not_checked"


def test_a_side_record_for_an_existing_link_is_collateral() -> None:
    side = FrameSideRecord(
        surface="history", link_field="pid", of_surface="patient", of_field="pid"
    )
    old = {"id": 1, "pid": 1, "lname": "Old"}
    verdict = judge_frame(
        FrameSpec(identity_field="id", side_records=[side]),
        [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"lname": "Newchart"},
                count_new_only=True,
                probe="surface:patient",
            )
        ],
        {
            "patient": (_state(old), [old, {"id": 2, "pid": 1, "lname": "Newchart"}]),
            "history": (
                _state({"id": 5, "pid": 1}),
                [{"id": 5, "pid": 1}, {"id": 6, "pid": 1}],
            ),
        },
        routes={"0": ["patient"]},
    )
    assert verdict.status == "refuted"
