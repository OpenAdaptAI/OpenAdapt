"""Compensating rollback after a stop (docs/ROLLBACK.md).

Two real stores:

- the MockMed record store (``fault_server``), read as one collection through
  the REST verifier, with a compensation that deletes through the real REST
  API actuator;
- a SQLite database read whole (every table, ``db_snapshot``), with a
  scripted app that writes a record, a side record and a log row, and a
  compensation that may or may not remove the side record.

The screen and keyboard are scripted (``tests.test_replayer.FakeBackend``);
every verdict reads the actual store. The unit tests at the end drive
``roll_back`` directly for uncertain writes, unapproved proposals, an
unreadable store and nothing landed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest
import requests

from openadapt_flow.after_stop import keep_prior_state
from openadapt_flow.ir import (
    ActionKind,
    ChangeLedgerEntry,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    UndoProposal,
    UndoStep,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.rollback import (
    CompensationRefused,
    ReplayerRunner,
    RollbackPlan,
    roll_back,
    rollback_lines,
)
from openadapt_flow.rollback_state import (
    DatabaseReader,
    StateUnreadable,
    VerifierReader,
    diff_states,
    records_snapshot,
)
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import classify_transaction_outcome
from openadapt_flow.undo_proposal import network_candidates
from tests.test_replayer import FakeBackend, FakeVision, Match

# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _vision() -> FakeVision:
    vision = FakeVision()
    hit = Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    vision.text_results = {"Saved": hit, "Deleted": hit, "Opened": hit}
    return vision


def _key_step(step_id: str, intent: str, key: str, *, effects=(), risk=None) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.KEY,
        key=key,
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
            )
        ],
        risk=risk or ("irreversible" if effects else "reversible"),
        effects=list(effects),
    )


# ---------------------------------------------------------------------------
# MockMed: one collection, an API compensation
# ---------------------------------------------------------------------------


def _records(url: str) -> list[dict]:
    return requests.get(f"{url}/api/db", timeout=5).json()["records"]


class MockMedBackend(FakeBackend):
    """Enter saves a Triage encounter; F2 is a step the server rejects."""

    def __init__(self, url: str):
        super().__init__(viewport=(300, 200))
        self.url = url

    def press(self, key):
        super().press(key)
        if key == "Enter":
            requests.post(
                f"{self.url}/api/encounter",
                json={"patient_id": "p1", "type": "Triage", "note": "n"},
                timeout=5,
            )
        elif key == "F2":
            requests.post(
                f"{self.url}/api/encounter?fault=optimistic",
                json={"patient_id": "p1", "type": "Vitals", "note": "n"},
                timeout=5,
            )


def _triage_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1", "type": "Triage"},
        expected_count=1,
        count_new_only=True,
        risk="irreversible",
        timeout_s=1.0,
    )


def _vitals_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1", "type": "Vitals"},
        expected_count=1,
        risk="irreversible",
        timeout_s=0.5,
    )


def _delete_triage_undo(*, proposed: bool = False) -> UndoStep:
    save = _key_step("save_triage", "save Triage", "Enter", effects=[_triage_effect()])
    lines = [
        {
            "method": "POST",
            "url": "http://127.0.0.1:1/api/encounter",
            "body": json.dumps({"patient_id": "p1", "type": "Triage"}),
            "status": 200,
            "response_text": json.dumps({"ok": True, "id": 1}),
        }
    ]
    [candidate], _ = network_candidates(save, save.effects[0], lines, {})
    undo = candidate.undo
    if proposed:
        undo = undo.model_copy(
            update={
                "proposal": UndoProposal(
                    candidate_source="network",
                    basis="test",
                    test_copy="t",
                    verified_at="t",
                    evidence_path="undo-evidence/x.json",
                    evidence_sha256="0" * 64,
                )
            }
        )
    return undo


def _mockmed_workflow(*, proposed: bool = False) -> Workflow:
    return Workflow(
        name="triage-then-vitals",
        steps=[
            _key_step("save_triage", "save Triage", "Enter", effects=[_triage_effect()]),
            _key_step("save_vitals", "save Vitals", "F2", effects=[_vitals_effect()]),
        ],
        undo_steps=[_delete_triage_undo(proposed=proposed)],
    )


@pytest.fixture
def mockmed():
    url, _db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _mockmed_replayer(url: str) -> Replayer:
    return Replayer(
        MockMedBackend(url),
        vision=_vision(),
        effect_verifier=RestRecordVerifier(url),
        api_actuator=ApiActuator(url),
        poll_interval_s=0.01,
    )


def _run_with_rollback(tmp_path: Path, url: str, workflow: Workflow) -> RunReport:
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    verifier = RestRecordVerifier(url)
    from openadapt_flow.after_stop import records_reader

    plan = RollbackPlan(VerifierReader(verifier), read_records=records_reader(verifier))
    before = plan.arm()
    run_dir = tmp_path / "run"
    report = _mockmed_replayer(url).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert not report.success
    return plan.finish(
        report,
        workflow,
        before=before,
        run_dir=run_dir,
        bundle_dir=bundle,
        runner=ReplayerRunner(lambda: _mockmed_replayer(url)),
    )


def test_a_stop_after_a_save_is_rolled_back_and_proven(tmp_path, mockmed):
    report = _run_with_rollback(tmp_path, mockmed, _mockmed_workflow())

    rollback = report.rollback
    assert rollback is not None
    assert rollback.status == "rolled_back", rollback.reason
    assert rollback.scope == "collection"
    assert rollback.after_stop_diff == {"records": {"added": 1, "removed": 0, "changed": 0}}
    assert rollback.remaining_diff == {}
    assert rollback.before_sha256 == rollback.final_sha256
    [step] = rollback.steps
    assert (step.step_id, step.decision) == ("save_triage", "compensated")
    # The rejected Vitals save is refuted and proven absent: not in the plan.
    assert report.transaction_outcome == "ROLLED_BACK"
    assert classify_transaction_outcome(report).value == "ROLLED_BACK"
    assert not report.success
    assert _records(mockmed) == []
    # The persisted report carries it, PHI-free.
    persisted = RunReport.model_validate_json((tmp_path / "run" / "report.json").read_text())
    assert persisted.rollback == rollback
    assert "p1" not in persisted.rollback.model_dump_json()
    assert "Rolled back" in "\n".join(rollback_lines(persisted))


def test_an_unapproved_proposal_is_never_used(tmp_path, mockmed):
    report = _run_with_rollback(tmp_path, mockmed, _mockmed_workflow(proposed=True))

    rollback = report.rollback
    assert rollback.status == "not_rolled_back"
    [step] = rollback.steps
    assert step.decision == "refused"
    assert "no compensation a person approved" in step.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    # Nothing was undone, and the person gets the exact difference.
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]
    diff_path = tmp_path / "run" / rollback.diff_path
    assert (diff_path.stat().st_mode & 0o777) == 0o600
    diff = json.loads(diff_path.read_text())
    [added] = diff["tables"]["records"]["added"]
    assert added["type"] == "Triage"
    assert "records: 1 added" in rollback.reason


# ---------------------------------------------------------------------------
# SQLite: the whole database, a side record, an allow-listed log
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT, dob TEXT);
CREATE TABLE history (id INTEGER PRIMARY KEY, patient_id INTEGER, smoker TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY, event TEXT);
CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);
"""


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO patients (name, dob) VALUES ('Existing Person', '1950-01-01')")
    conn.execute("INSERT INTO notes (body) VALUES ('keep me')")
    conn.commit()
    conn.close()


class ClinicBackend(FakeBackend):
    """Enter creates a patient, its history row and a log row. Delete removes
    the newest patient; with ``thorough`` it removes its history row too.
    F2 is a later step that fails on screen and writes nothing."""

    def __init__(self, path: Path, *, thorough: bool = True):
        super().__init__(viewport=(300, 200))
        self.path = path
        self.thorough = thorough

    def _sql(self, *statements: tuple[str, tuple]) -> None:
        conn = sqlite3.connect(self.path)
        for sql, args in statements:
            conn.execute(sql, args)
        conn.commit()
        conn.close()

    def press(self, key):
        super().press(key)
        if key == "Enter":
            conn = sqlite3.connect(self.path)
            cur = conn.execute(
                "INSERT INTO patients (name, dob) VALUES ('Ada Synthetic', '1980-02-03')"
            )
            conn.execute(
                "INSERT INTO history (patient_id, smoker) VALUES (?, 'no')",
                (cur.lastrowid,),
            )
            conn.execute("INSERT INTO log (event) VALUES ('patient created')")
            conn.commit()
            conn.close()
        elif key == "Delete":
            conn = sqlite3.connect(self.path)
            row = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            if row and row[0] is not None:
                conn.execute("DELETE FROM patients WHERE id = ?", (row[0],))
                if self.thorough:
                    conn.execute("DELETE FROM history WHERE patient_id = ?", (row[0],))
                conn.execute("INSERT INTO log (event) VALUES ('patient deleted')")
            conn.commit()
            conn.close()


def _sql_verifier(path: Path) -> SqlRecordVerifier:
    uri = path.resolve().as_uri() + "?mode=ro"

    def connect():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    return SqlRecordVerifier(connect, "SELECT * FROM patients", poll_interval_s=0.01)


def _clinic_workflow() -> Workflow:
    created = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"name": ValueExpr(param="name")},
        expected_count=1,
        count_new_only=True,
        risk="irreversible",
        timeout_s=1.0,
    )
    delete = Step(
        id="delete_patient",
        intent="delete the patient the run created",
        action=ActionKind.KEY,
        key="Delete",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Deleted", timeout_s=0.2
            )
        ],
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"id": ValueExpr(param="undo_record_id")},
                expected_count=0,
                risk="irreversible",
                timeout_s=1.0,
            )
        ],
    )
    failing = Step(
        id="sign_chart",
        intent="sign the chart",
        action=ActionKind.KEY,
        key="F2",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Signed", timeout_s=0.1
            )
        ],
    )
    return Workflow(
        name="create-patient-then-sign",
        params={"name": "Ada Synthetic"},
        steps=[
            _key_step("create_patient", "create the patient", "Enter", effects=[created]),
            failing,
        ],
        undo_steps=[
            UndoStep(
                id="undo-create",
                undoes="create_patient",
                intent="delete the patient the run created",
                source="hand_written",
                steps=[delete],
                locate={"match": {"name": {"param": "name"}}},
            )
        ],
    )


def _run_clinic(tmp_path: Path, *, thorough: bool) -> RunReport:
    db = tmp_path / "clinic.db"
    _make_db(db)
    workflow = _clinic_workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    from openadapt_flow.after_stop import records_reader

    reader = DatabaseReader(
        dialect="sqlite", read_config={"sqlite_database": str(db)}, allow_tables=["log"]
    )
    plan = RollbackPlan(reader, read_records=records_reader(_sql_verifier(db)))
    before = plan.arm()

    def replayer() -> Replayer:
        return Replayer(
            ClinicBackend(db, thorough=thorough),
            vision=_vision(),
            effect_verifier=_sql_verifier(db),
            poll_interval_s=0.01,
        )

    run_dir = tmp_path / "run"
    report = replayer().run(
        workflow, bundle_dir=bundle, run_dir=run_dir, params={"name": "Ada Synthetic"}
    )
    assert not report.success
    return plan.finish(
        report,
        workflow,
        before=before,
        run_dir=run_dir,
        bundle_dir=bundle,
        runner=ReplayerRunner(replayer, fresh_session=lambda: None),
    )


def test_whole_database_rollback_ignores_only_the_allow_listed_log(tmp_path):
    report = _run_clinic(tmp_path, thorough=True)
    rollback = report.rollback
    assert rollback.status == "rolled_back", rollback.reason
    assert rollback.scope == "database"
    assert rollback.allow_tables == ["log"]
    assert set(rollback.after_stop_diff) == {"patients", "history"}
    assert report.transaction_outcome == "ROLLED_BACK"


def test_a_compensation_that_leaves_a_side_record_is_named_precisely(tmp_path):
    report = _run_clinic(tmp_path, thorough=False)
    rollback = report.rollback
    # The compensation's own check passed: the patient row is gone. The
    # whole-database read still sees the history row it left.
    [step] = rollback.steps
    assert step.decision == "compensated"
    assert rollback.status == "not_rolled_back"
    assert rollback.remaining_diff == {"history": {"added": 1, "removed": 0, "changed": 0}}
    assert "history: 1 added" in rollback.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    diff = json.loads((tmp_path / "run" / rollback.diff_path).read_text())
    [row] = diff["tables"]["history"]["added"]
    assert row["smoker"] == "no"


# ---------------------------------------------------------------------------
# Units: uncertain writes, refusals, unreadable stores
# ---------------------------------------------------------------------------


class ListReader:
    scope = "collection"

    def __init__(self, *reads: Optional[list[dict[str, Any]]]):
        self.reads = list(reads)

    def read(self):
        records = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if records is None:
            raise StateUnreadable("the system of record could not be read")
        return records_snapshot(records)


class RecordingRunner:
    def __init__(self, on_run=None):
        self.runs: list[str] = []
        self.on_run = on_run

    def run(self, workflow, *, bundle_dir, run_dir, params):
        self.runs.append(workflow.name)
        if self.on_run is not None:
            return self.on_run(workflow, params)
        return None


def _entry(step_id="save_triage", *, attempt="delivered", status="confirmed"):
    return ChangeLedgerEntry(
        position=1,
        step_id=step_id,
        intent="save",
        attempt_state=attempt,
        status=status,
        may_need_undo=True,
    )


def _stopped(entries) -> RunReport:
    return RunReport(
        workflow_name="w",
        success=False,
        results=[],
        started_at="t",
        change_ledger=list(entries),
        params={},
    )


def _single_save_workflow() -> Workflow:
    return Workflow(
        name="w",
        steps=[_key_step("save_triage", "save Triage", "Enter", effects=[_triage_effect()])],
        undo_steps=[_delete_triage_undo()],
    )


def _uncertain(tmp_path, after_stop, *, kept=()):
    workflow = _single_save_workflow()
    keep_prior_state(
        workflow,
        workflow.steps[0],
        [dict(r) for r in kept],
        {},
        tmp_path,
    )
    runner = RecordingRunner()
    record = roll_back(
        _stopped([_entry(attempt="delivery_uncertain", status="unknown")]),
        workflow,
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader(after_stop, after_stop),
        before=records_snapshot([]),
        runner=runner,
        read_records=lambda: after_stop,
    )
    return record, runner


def test_an_uncertain_write_that_did_not_land_is_not_compensated(tmp_path):
    record, runner = _uncertain(tmp_path, [])
    assert record.status == "nothing_landed"
    assert runner.runs == []


def test_an_uncertain_write_is_compensated_only_when_a_fresh_read_shows_it(tmp_path):
    landed = [{"id": 7, "patient_id": "p1", "type": "Triage"}]
    # Two new matching records: the fresh read can't say which is the run's.
    two = landed + [{"id": 8, "patient_id": "p1", "type": "Triage"}]
    record, runner = _uncertain(tmp_path, two)
    [step] = record.steps
    assert step.fresh_read == "unknown"
    assert step.decision == "refused"
    assert "not compensated on a guess" in step.reason
    assert runner.runs == []
    assert record.status == "not_rolled_back"


def test_an_uncertain_write_present_on_a_fresh_read_is_compensated(tmp_path):
    landed = [{"id": 7, "patient_id": "p1", "type": "Triage"}]
    record, runner = _uncertain(tmp_path, landed)
    [step] = record.steps
    assert step.fresh_read == "present"
    assert runner.runs  # it ran; the fake runner left no report, so not confirmed
    assert step.decision == "failed"
    assert record.status == "not_rolled_back"


def test_an_unreadable_store_before_the_run_leaves_the_rollback_unarmed(tmp_path):
    runner = RecordingRunner()
    record = roll_back(
        _stopped([_entry()]),
        _single_save_workflow(),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader([]),
        before=None,
        runner=runner,
    )
    assert record.status == "unarmed"
    assert runner.runs == []
    report = _stopped([_entry()])
    report.rollback = record
    # An unarmed rollback proves nothing: the outcome is what it would be
    # without it.
    without = classify_transaction_outcome(_stopped([_entry()]))
    assert classify_transaction_outcome(report) == without


def test_nothing_landed_is_proof_of_absence(tmp_path):
    record = roll_back(
        _stopped([_entry(attempt="delivery_uncertain", status="unknown")]),
        _single_save_workflow(),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader([{"id": 1, "x": "y"}]),
        before=records_snapshot([{"id": 1, "x": "y"}]),
        runner=RecordingRunner(),
    )
    assert record.status == "nothing_landed"
    report = _stopped([_entry(attempt="delivery_uncertain", status="unknown")])
    report.execution_outcome = "HALTED"
    report.rollback = record
    assert classify_transaction_outcome(report).value == "HALTED_BEFORE_EFFECT"


def test_an_unreadable_store_after_the_stop_runs_nothing(tmp_path):
    runner = RecordingRunner()
    record = roll_back(
        _stopped([_entry()]),
        _single_save_workflow(),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader(None),
        before=records_snapshot([]),
        runner=runner,
    )
    assert record.status == "not_rolled_back"
    assert "could not be read" in record.reason
    assert runner.runs == []


def test_a_gui_compensation_needs_a_fresh_session():
    runner = ReplayerRunner(lambda: None, fresh_session=None)
    workflow = Workflow(name="gui", steps=[_key_step("d", "delete", "Delete")])
    with pytest.raises(CompensationRefused, match="fresh session"):
        runner.run(workflow, bundle_dir=Path("."), run_dir=Path("."), params={})


def test_diff_names_columns_never_values():
    before = records_snapshot([{"id": 1, "name": "A", "dob": "1"}])
    after = records_snapshot([{"id": 1, "name": "B", "dob": "1"}])
    diff = diff_states(before, after)
    assert diff.summary() == {
        "records": {"added": 0, "removed": 0, "changed": 1, "changed_columns": ["name"]}
    }
    assert "B" not in json.dumps(diff.summary())
    assert diff_states(before, after, ignore_columns={"*": ["name"]}).empty
