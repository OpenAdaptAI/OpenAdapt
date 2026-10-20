"""Frame conditions: nothing outside the declared delta changed.

See ``docs/VERIFICATION_COVERAGE.md`` section 1. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects.effect import EffectState
from openadapt_flow.runtime.effects.frame import (
    FrameAttribution,
    FrameSpec,
    judge_frame,
)
from openadapt_flow.verify_run import (
    capture_baseline,
    parse_verify_config,
    receipt_problem,
    verify_run,
)

OTHER = {
    "id": 1,
    "lname": "Priorchart",
    "phone_home": "555-0188",
    "by": "clinic2",
    "v": 3,
}
TARGET = {"id": 2, "lname": "Newchart", "phone_home": "555-0131", "by": "bot", "v": 1}


def _effect(**match: str) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=match or {"lname": TARGET["lname"]},
        count_new_only=True,
    )


def _state(*records: dict) -> EffectState:
    return EffectState(
        substrate="t", reachable=True, records=[dict(r) for r in records]
    )


def _judge(before, after, effects=None, spec=None, **kw):
    return judge_frame(
        spec or FrameSpec(),
        effects or [_effect()],
        {"": (before, after)},
        **kw,
    )


# -- the judge -----------------------------------------------------------------


def test_clean_save_holds() -> None:
    verdict = _judge(_state(OTHER), [dict(OTHER), dict(TARGET)])
    assert verdict.status == "held"
    assert verdict.records_compared == 2


def test_edit_to_another_record_is_collateral() -> None:
    edited = {**OTHER, "phone_home": "555-0199"}
    verdict = _judge(_state(OTHER), [edited, dict(TARGET)])
    assert verdict.status == "refuted"
    assert verdict.collateral[0].fields == ["phone_home"]
    assert "phone_home" in verdict.reason


def test_an_undeclared_addition_is_collateral() -> None:
    stray = {"id": 9, "lname": "Junk", "phone_home": "", "by": "bot", "v": 1}
    verdict = _judge(_state(OTHER), [dict(OTHER), dict(TARGET), stray])
    assert verdict.status == "refuted"
    assert verdict.collateral[0].change == "added"


def test_a_removal_is_collateral() -> None:
    verdict = _judge(_state(OTHER), [dict(TARGET)])
    assert verdict.status == "refuted"
    assert verdict.collateral[0].change == "removed"


def test_no_baseline_is_not_checked() -> None:
    before = EffectState(substrate="t", reachable=False)
    assert _judge(before, [dict(TARGET)]).status == "not_checked"


def test_unreadable_after_is_not_checked() -> None:
    assert _judge(_state(OTHER), None).status == "not_checked"


def test_two_effects_on_one_read_do_not_flag_each_other() -> None:
    # An update to an existing record plus a new one. Per-effect collateral
    # rules flag the other effect's target; the frame declares both.
    updated = {**OTHER, "phone_home": "555-0100"}
    effects = [
        _effect(lname=TARGET["lname"]),
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"lname": OTHER["lname"], "phone_home": "555-0100"},
        ),
    ]
    spec = FrameSpec(ignore_fields=["by", "v"])
    verdict = _judge(_state(OTHER), [updated, dict(TARGET)], effects, spec)
    assert verdict.status == "held", verdict.reason


def test_a_target_may_change_only_in_its_declared_fields() -> None:
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": OTHER["lname"], "phone_home": "555-0100"},
    )
    changed = {**OTHER, "phone_home": "555-0100", "street": "1 Elsewhere"}
    spec = FrameSpec(ignore_fields=["by", "v"])
    verdict = _judge(_state({**OTHER, "street": "9 Road"}), [changed], [effect], spec)
    assert verdict.status == "refuted"
    assert verdict.collateral[0].on_target
    assert verdict.collateral[0].fields == ["street"]


def test_ignored_fields_do_not_count() -> None:
    touched = {**OTHER, "v": 4}
    spec = FrameSpec(ignore_fields=["v"])
    assert _judge(_state(OTHER), [touched, dict(TARGET)], spec=spec).status == "held"


def test_missing_identity_field_is_not_checked() -> None:
    spec = FrameSpec(identity_field="pid")
    assert _judge(_state(OTHER), [dict(OTHER)], spec=spec).status == "not_checked"


# -- attribution: record_field -------------------------------------------------


def _record_field(**kw) -> FrameSpec:
    return FrameSpec(
        attribution=FrameAttribution(
            kind="record_field",
            actor_field="by",
            version_field="v",
            server_set=["by", "v"],
            **{"self": ["bot"]},
            **kw,
        )
    )


def test_another_users_edit_with_a_new_version_is_concurrent() -> None:
    edited = {**OTHER, "phone_home": "555-0199", "by": "clinic3", "v": 4}
    verdict = _judge(_state(OTHER), [edited, dict(TARGET)], spec=_record_field())
    assert verdict.status == "held"
    assert len(verdict.concurrent) == 1


def test_the_bots_own_edit_is_collateral() -> None:
    edited = {**OTHER, "phone_home": "555-0199", "by": "bot", "v": 4}
    verdict = _judge(_state(OTHER), [edited, dict(TARGET)], spec=_record_field())
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "self"


def test_a_change_without_a_new_version_is_unattributed() -> None:
    # A direct database write: content moved, the version and modifier did not.
    edited = {**OTHER, "phone_home": "555-0199"}
    verdict = _judge(_state(OTHER), [edited, dict(TARGET)], spec=_record_field())
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "unattributed"


def test_self_ids_bind_to_run_params() -> None:
    spec = FrameSpec(
        attribution=FrameAttribution(
            kind="record_field",
            actor_field="by",
            server_set=["by"],
            **{"self": [{"param": "bot_user"}]},
        )
    )
    edited = {**OTHER, "phone_home": "555-0199", "by": "bot"}
    assert (
        _judge(_state(OTHER), [edited, dict(TARGET)], spec=spec).status == "not_checked"
    )
    verdict = _judge(
        _state(OTHER), [edited, dict(TARGET)], spec=spec, params={"bot_user": "bot"}
    )
    assert verdict.status == "refuted"


# -- attribution: audit_read ---------------------------------------------------


def _audit_spec() -> FrameSpec:
    return FrameSpec(
        identity_field="id",
        attribution=FrameAttribution(
            kind="audit_read",
            actor_field="user",
            record_field="record",
            read={"kind": "sql"},
            **{"self": ["bot"]},
        ),
    )


OLD_ENTRY = {"id": 100, "user": "clinic2", "record": 1}


def test_an_audited_edit_by_another_user_is_concurrent() -> None:
    edited = {**OTHER, "phone_home": "555-0199"}
    entries = [OLD_ENTRY, {"id": 101, "user": "clinic3", "record": 1}]
    verdict = _judge(
        _state(OTHER),
        [edited, dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[OLD_ENTRY],
        audit_after=entries,
    )
    assert verdict.status == "held"
    assert len(verdict.concurrent) == 1


def test_an_old_log_entry_explains_nothing() -> None:
    edited = {**OTHER, "phone_home": "555-0199"}
    verdict = _judge(
        _state(OTHER),
        [edited, dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[OLD_ENTRY],
        audit_after=[OLD_ENTRY],
    )
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "unattributed"


def test_the_bot_and_another_user_on_one_record_is_collateral() -> None:
    edited = {**OTHER, "phone_home": "555-0199"}
    entries = [
        {"id": 101, "user": "clinic3", "record": 1},
        {"id": 102, "user": "bot", "record": 1},
    ]
    verdict = _judge(
        _state(OTHER),
        [edited, dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[],
        audit_after=entries,
    )
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "self"


def test_a_bot_log_entry_on_an_undeclared_record_is_collateral() -> None:
    # The read shows nothing changed (an edit and its revert, or a field the
    # read doesn't return), but the log says the bot wrote record 1.
    entries = [
        {"id": 101, "user": "bot", "record": 1},
        {"id": 102, "user": "bot", "record": 2},
    ]
    verdict = _judge(
        _state(OTHER),
        [dict(OTHER), dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[],
        audit_after=entries,
    )
    assert verdict.status == "refuted"
    assert [c.audit_only for c in verdict.collateral] == [True]


def test_an_unreadable_log_attributes_nothing() -> None:
    edited = {**OTHER, "phone_home": "555-0199"}
    verdict = _judge(
        _state(OTHER),
        [edited, dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[],
        audit_after=None,
    )
    assert verdict.status == "refuted"
    assert verdict.attribution == "audit_read_unavailable"


def test_attribution_shape_is_validated() -> None:
    with pytest.raises(ValueError):
        FrameAttribution(kind="audit_read", actor_field="user", **{"self": ["bot"]})
    with pytest.raises(ValueError):
        FrameAttribution(kind="record_field", actor_field="by", **{"self": []})
    with pytest.raises(ValueError):
        FrameSpec(
            attribution=FrameAttribution(
                kind="audit_read",
                actor_field="user",
                record_field="record",
                read={"kind": "sql"},
                **{"self": ["bot"]},
            )
        )


# -- verify-run ----------------------------------------------------------------


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _yaml(base: str, **extra: object) -> dict:
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "rest", "base_url": base, "records_path": "/api/db"},
        "effects": [
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "note": {"param": "note"},
                },
                "expected_count": 1,
                "count_new_only": True,
                "timeout_s": 0.5,
            }
        ],
        **extra,
    }


PARAMS = {"patient_id": "p1", "note": "Follow-up in two weeks"}


def _save(base: str) -> None:
    requests.post(
        f"{base}/api/encounter",
        json={"patient_id": "p1", "type": "Triage", "note": PARAMS["note"]},
        timeout=5,
    )


def _verify(cfg, tmp_path: Path, baseline: Path | None) -> dict:
    return verify_run(
        cfg,
        params=PARAMS,
        run_record={"executor_kind": "agent", "executor_id": "test-agent"},
        authorization_ref="ticket-0000-frame",
        baseline_path=baseline,
        private_dir=tmp_path / "out",
    )


def test_verify_run_clean_save_states_every_part(sor, tmp_path: Path) -> None:
    base, db = sor
    db.add("p2", "Consult", "Allergy: penicillin")
    cfg = parse_verify_config(_yaml(base))
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, PARAMS, baseline)
    _save(base)
    result = _verify(cfg, tmp_path, baseline)
    receipt = result["receipt"]
    assert receipt["outcome"] == "verified"
    assert receipt["checked"] == {
        "delta": "held",
        "frame": "held",
        "source": "instruction",
    }
    assert result["evidence"]["frame"]["status"] == "held"
    assert result["evidence"]["frame"]["records_compared"] == 2
    assert receipt_problem(receipt) is None


def test_verify_run_without_a_baseline_is_not_verified(sor, tmp_path: Path) -> None:
    base, _db = sor
    cfg = parse_verify_config(_yaml(base))
    # Absolute count: the delta alone would confirm.
    cfg.effects[0] = cfg.effects[0].model_copy(update={"count_new_only": False})
    _save(base)
    receipt = _verify(cfg, tmp_path, None)["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["checked"]["frame"] == "not_checked"
    assert receipt["outcome"] == "reconciliation_required"


def test_verify_run_stray_addition_is_refuted(sor, tmp_path: Path) -> None:
    base, db = sor
    cfg = parse_verify_config(_yaml(base))
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, PARAMS, baseline)
    _save(base)
    db.add("p9", "Triage", "a stray note")
    result = _verify(cfg, tmp_path, baseline)
    assert result["receipt"]["checked"]["delta"] == "held"
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["effect_verdict"] == "REFUTED"
    assert result["receipt"]["outcome"] == "reconciliation_required"
    assert result["evidence"]["frame"]["collateral_kinds"] == ["added"]


def test_verify_run_attributes_through_an_audit_read(tmp_path: Path) -> None:
    """A SQL store with its own audit log: another user's edit is concurrent."""

    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE chart (id INTEGER PRIMARY KEY, lname TEXT, phone TEXT);"
        "CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, chart_id INTEGER);"
        "INSERT INTO chart VALUES (1, 'Priorchart', '555-0188');"
        "INSERT INTO log VALUES (1, 'clinic2', 1);"
    )
    con.commit()

    def doc(self_user: str) -> dict:
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT id, lname, phone FROM chart",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {"lname": {"param": "lname"}},
                    "count_new_only": True,
                    "timeout_s": 0.2,
                }
            ],
            "frame": {
                "identity_field": "id",
                "attribution": {
                    "kind": "audit_read",
                    "actor_field": "user",
                    "record_field": "chart_id",
                    "self": [self_user],
                    "read": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT id, user, chart_id FROM log",
                    },
                },
            },
        }

    params = {"lname": "Newchart"}

    def run(edit_user: str, name: str) -> dict:
        cfg = parse_verify_config(doc("bot"))
        baseline = tmp_path / f"{name}.json"
        capture_baseline(cfg, params, baseline)
        con.execute("DELETE FROM chart WHERE id > 1")
        con.execute("UPDATE chart SET phone = '555-0188' WHERE id = 1")
        con.execute("INSERT INTO chart (lname, phone) VALUES ('Newchart', '555-0131')")
        con.execute(
            "INSERT INTO log (user, chart_id) VALUES ('bot', last_insert_rowid())"
        )
        con.execute("UPDATE chart SET phone = '555-0199' WHERE id = 1")
        con.execute("INSERT INTO log (user, chart_id) VALUES (?, 1)", (edit_user,))
        con.commit()
        return verify_run(
            cfg,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": "test-agent"},
            authorization_ref="ticket-0000-audit",
            baseline_path=baseline,
            private_dir=tmp_path / name,
        )

    try:
        concurrent = run("clinic3", "concurrent")
        assert concurrent["receipt"]["outcome"] == "verified", concurrent["evidence"]
        assert concurrent["evidence"]["frame"]["concurrent_count"] == 1
        own = run("bot", "own")
        assert own["receipt"]["checked"]["frame"] == "refuted"
        assert own["receipt"]["outcome"] == "reconciliation_required"
    finally:
        con.close()


def test_receipt_check_refuses_verified_with_an_unchecked_part(
    sor, tmp_path: Path
) -> None:
    base, _db = sor
    cfg = parse_verify_config(_yaml(base))
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, PARAMS, baseline)
    _save(base)
    receipt = _verify(cfg, tmp_path, baseline)["receipt"]
    forged = {**receipt, "checked": {**receipt["checked"], "frame": "not_checked"}}
    assert receipt_problem(forged) == "verified_without_every_part"
    assert receipt_problem({**receipt, "checked": {"delta": "held"}}) == "checked"


def test_qualify_accepts_a_frame_section() -> None:
    from openadapt_flow.qualify.config import VERIFY_RUN_ONLY_SECTIONS

    assert "frame" in VERIFY_RUN_ONLY_SECTIONS


def test_countersign_states_every_part() -> None:
    from openadapt_flow.countersign.render import checked_line

    line = checked_line({"delta": "held", "frame": "refuted", "source": "instruction"})
    assert "frame (nothing else in the slice changed): refuted" in line
    assert "the inputs were the authorized instruction" in line


def test_a_content_twin_is_a_view_of_the_same_read(tmp_path: Path) -> None:
    """scaffold-verifier reads each table twice: by id, and by content
    (``records_drop_fields: [id]``). The saved row is declared in both."""

    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE enc (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT);"
        "INSERT INTO enc VALUES (1, 'p2', 'an older note');"
    )
    con.commit()
    read = {"kind": "sql", "sqlite_database": str(db), "sql_query": "SELECT * FROM enc"}
    match = {"patient_id": {"param": "patient_id"}, "note": {"param": "note"}}
    cfg = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "surfaces",
                "surfaces": {
                    "enc": read,
                    "enc.content": {**read, "records_drop_fields": ["id"]},
                },
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": match,
                    "count_new_only": True,
                    "probe": "surface=enc|the saved row",
                    "timeout_s": 0.2,
                },
                {
                    "kind": "record_written",
                    "match": {"_none": "-"},
                    "expected_count": 0,
                    "count_new_only": True,
                    "probe": "surface=enc.content|no row changes",
                    "timeout_s": 0.2,
                },
            ],
        }
    )
    params = {"patient_id": "p1", "note": "Follow-up"}
    baseline = tmp_path / "b.json"
    capture_baseline(cfg, params, baseline)
    con.execute("INSERT INTO enc (patient_id, note) VALUES ('p1', 'Follow-up')")
    con.commit()
    result = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "script"},
        authorization_ref="ticket-0000-twin",
        baseline_path=baseline,
        private_dir=tmp_path / "out",
    )
    assert result["receipt"]["outcome"] == "verified", result["evidence"]["frame"]
    con.execute("UPDATE enc SET note = 'edited' WHERE id = 1")
    con.commit()
    baseline2 = tmp_path / "b2.json"
    con.execute("DELETE FROM enc WHERE id > 1")
    con.execute("UPDATE enc SET note = 'an older note' WHERE id = 1")
    con.commit()
    capture_baseline(cfg, params, baseline2)
    con.execute("INSERT INTO enc (patient_id, note) VALUES ('p1', 'Follow-up')")
    con.execute("UPDATE enc SET note = 'edited' WHERE id = 1")
    con.commit()
    edited = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "script"},
        authorization_ref="ticket-0000-twin",
        baseline_path=baseline2,
        private_dir=tmp_path / "out2",
    )
    assert edited["receipt"]["checked"]["frame"] == "refuted"
    con.close()


# -- the safety review's conditions (#1443) ------------------------------------


def test_record_field_attribution_must_declare_its_fields_server_set() -> None:
    """(a) Only fields the app sets on the server, declared per app, count."""

    with pytest.raises(ValueError, match="server-set"):
        FrameAttribution(
            kind="record_field",
            actor_field="by",
            version_field="v",
            **{"self": ["bot"]},
        )
    with pytest.raises(ValueError, match="server-set"):
        FrameAttribution(
            kind="record_field",
            actor_field="by",
            version_field="v",
            server_set=["by"],
            **{"self": ["bot"]},
        )


def test_attribution_from_a_field_the_run_writes_is_refused() -> None:
    """(a) A field a step typed into is no evidence of who changed a record."""

    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": TARGET["lname"], "by": "bot"},
        count_new_only=True,
    )
    edited = {**OTHER, "phone_home": "555-0199", "by": "clinic3", "v": 4}
    verdict = _judge(_state(OTHER), [edited, dict(TARGET)], [effect], _record_field())
    assert verdict.attribution == "attribution_refused"
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "unattributed"
    # The same field named by a run parameter is refused too.
    verdict = _judge(
        _state(OTHER), [edited, dict(TARGET)], spec=_record_field(), params={"by": "x"}
    )
    assert verdict.attribution == "attribution_refused"


def test_the_runs_own_executor_is_never_another_actor() -> None:
    """(b) The named user must differ from the run's own identity."""

    edited = {**OTHER, "phone_home": "555-0199", "by": "agent-7", "v": 4}
    verdict = _judge(
        _state(OTHER),
        [edited, dict(TARGET)],
        spec=_record_field(),
        run_actors=["agent-7"],
    )
    assert verdict.status == "refuted"
    assert verdict.collateral[0].attribution == "self"


def test_another_users_change_to_the_saved_record_blocks() -> None:
    """(c) The saved value may not stand: never excused, record_field."""

    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": OTHER["lname"], "phone_home": "555-0100"},
    )
    # The bot set phone_home; another user then wrote the same record.
    saved = {**OTHER, "phone_home": "555-0100", "by": "clinic3", "v": 4}
    verdict = _judge(_state(OTHER), [saved], [effect], _record_field())
    assert verdict.status == "refuted"
    assert verdict.collateral[0].other_on_target
    assert "may not stand" in verdict.reason
    # A record the bot itself wrote last is the delta's business.
    own = {**OTHER, "phone_home": "555-0100", "by": "bot", "v": 4}
    assert _judge(_state(OTHER), [own], [effect], _record_field()).status == "held"


def test_an_audited_edit_of_the_saved_record_by_another_user_blocks() -> None:
    """(c) Same rule through the audit log: an entry on the target by another
    user blocks, while one on an undeclared record is a concurrent edit."""

    entries = [
        {"id": 101, "user": "bot", "record": 2},
        {"id": 102, "user": "clinic3", "record": 2},
    ]
    verdict = _judge(
        _state(OTHER),
        [dict(OTHER), dict(TARGET)],
        spec=_audit_spec(),
        audit_before=[],
        audit_after=entries,
    )
    assert verdict.status == "refuted"
    assert verdict.collateral[0].other_on_target
    assert verdict.evidence(_audit_spec())["target_changed_by_other"] == 1
