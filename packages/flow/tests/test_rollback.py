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
    rollback_lines,
)
from openadapt_flow.rollback import (
    roll_back as _roll_back,
)
from openadapt_flow.rollback_state import (
    Attributor,
    DatabaseReader,
    StateUnreadable,
    VerifierReader,
    changes,
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


def roll_back(*args: Any, **kwargs: Any) -> Any:
    """The unit tests' store is one only the run writes (a test copy); the
    tests of attribution and its absence pass ``exclusive`` themselves."""
    kwargs.setdefault("exclusive", True)
    return _roll_back(*args, **kwargs)


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
            _key_step(
                "save_triage", "save Triage", "Enter", effects=[_triage_effect()]
            ),
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


def _run_with_rollback(
    tmp_path: Path, url: str, workflow: Workflow, *, allow_delete: bool = True
) -> RunReport:
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    verifier = RestRecordVerifier(url)
    from openadapt_flow.after_stop import records_reader

    plan = RollbackPlan(
        VerifierReader(verifier),
        read_records=records_reader(verifier),
        allow_delete=allow_delete,
        exclusive=True,  # MockMed in-process: only this run writes
    )
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
    assert rollback.after_stop_diff == {
        "records": {"added": 1, "removed": 0, "changed": 0}
    }
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
    persisted = RunReport.model_validate_json(
        (tmp_path / "run" / "report.json").read_text()
    )
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
    assert added["row"]["type"] == "Triage"
    assert "records: 1 added" in rollback.reason


# ---------------------------------------------------------------------------
# SQLite: the whole database, a side record, an allow-listed log
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT, dob TEXT,
    status TEXT DEFAULT 'active', modified_by TEXT);
CREATE TABLE history (id INTEGER PRIMARY KEY, patient_id INTEGER, smoker TEXT,
    modified_by TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY, event TEXT);
CREATE TABLE notes (id INTEGER PRIMARY KEY, patient_id INTEGER, body TEXT,
    modified_by TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY, pid INTEGER, relation TEXT,
    modified_by TEXT);
CREATE TABLE allergies (id INTEGER PRIMARY KEY, patient_id INTEGER, substance TEXT);
"""


def _make_db(path: Path, *, autoinc: bool = False) -> None:
    conn = sqlite3.connect(path)
    schema = SCHEMA
    if autoinc:
        # Database-numbered ids: the counter can't be wound back.
        schema = schema.replace(
            "CREATE TABLE patients (id INTEGER PRIMARY KEY,",
            "CREATE TABLE patients (id INTEGER PRIMARY KEY AUTOINCREMENT,",
        )
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO patients (name, dob) VALUES ('Existing Person', '1950-01-01')"
    )
    conn.execute("INSERT INTO notes (patient_id, body) VALUES (1, 'keep me')")
    conn.commit()
    conn.close()


class ClinicBackend(FakeBackend):
    """Enter creates a patient (and, with ``history``, its history row) and a
    log row, as the bot. Delete undoes it: ``delete`` removes the patient
    (and its history row when ``thorough``); ``void`` marks it void. F2 is a
    later step that fails on screen; with ``colleague`` another user writes
    at that moment: ``note`` adds an unrelated note, ``edit`` changes the
    patient the run just created."""

    def __init__(
        self,
        path: Path,
        *,
        thorough: bool = True,
        mode: str = "delete",
        history: bool = True,
        colleague: Optional[str] = None,
        audit: Optional[list[dict[str, str]]] = None,
        during_undo: Optional[str] = None,
        hidden_log: bool = False,
    ):
        super().__init__(viewport=(300, 200))
        self.hidden_log = hidden_log
        self.during_undo = during_undo
        self.audit = audit if audit is not None else []
        self.path = path
        self.thorough = thorough
        self.mode = mode
        self.history = history
        self.colleague = colleague

    def _log(self, actor: str, event: str, table: str, record: Any) -> None:
        # The app's audit log, keyed by patient as OpenEMR's is: who did what,
        # in which table, for which patient.
        self.audit.append(
            {"actor": actor, "event": event, "tbl": table, "record": str(record)}
        )

    def press(self, key):
        super().press(key)
        conn = sqlite3.connect(self.path)
        if key == "Enter":
            cur = conn.execute(
                "INSERT INTO patients (name, dob, modified_by) "
                "VALUES ('Ada Synthetic', '1980-02-03', 'bot')"
            )
            self._log("bot", "insert", "patients", cur.lastrowid)
            if self.history:
                hist = conn.execute(
                    "INSERT INTO history (patient_id, smoker, modified_by) "
                    "VALUES (?, 'no', 'bot')",
                    (cur.lastrowid,),
                )
                assert hist.lastrowid
                self._log("bot", "insert", "history", cur.lastrowid)
            conn.execute("INSERT INTO log (event) VALUES ('patient created')")
            if self.hidden_log:
                # The save also writes a row the read keeps append-only (the
                # allow-listed log), and the app logs that write.
                self._log("bot", "insert", "log", cur.lastrowid)
        elif key == "F2" and self.colleague == "note":
            # About another patient (Existing Person, id 1).
            conn.execute(
                "INSERT INTO notes (patient_id, body, modified_by) "
                "VALUES (1, 'call back', 'nurse')"
            )
            self._log("nurse", "insert", "notes", 1)
        elif key == "F2" and self.colleague == "view":
            # The nurse only looks at the run's patient: not a write.
            (pid,) = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            self._log("nurse", "view", "patients", pid)
        elif key == "F2" and self.colleague == "note_on_run_patient":
            # The nurse adds a note to the patient the run just created.
            (pid,) = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            conn.execute(
                "INSERT INTO notes (patient_id, body, modified_by) "
                "VALUES (?, 'allergy: penicillin', 'nurse')",
                (pid,),
            )
            self._log("nurse", "insert", "notes", pid)
        elif key == "F2" and self.colleague == "contact":
            # The run's patient gets a contact row (keyed by ``pid``, not
            # ``patient_id``: OpenEMR mixes both), and the nurse completes it.
            (pid,) = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            conn.execute(
                "INSERT INTO contacts (pid, relation, modified_by) "
                "VALUES (?, '', 'bot')",
                (pid,),
            )
            conn.execute(
                "UPDATE contacts SET relation = 'mother', modified_by = 'nurse'"
            )
            self._log("nurse", "update", "contacts", pid)
        elif key == "F2" and self.colleague == "allergy":
            # A table with no last-writer column.
            (pid,) = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            conn.execute(
                "INSERT INTO allergies (patient_id, substance) VALUES (?, 'penicillin')",
                (pid,),
            )
            self._log("nurse", "insert", "allergies", pid)
        elif key == "F2" and self.colleague == "register":
            # The nurse registers her own patient.
            other = conn.execute(
                "INSERT INTO patients (name, dob, modified_by) "
                "VALUES ('Bo Synthetic', '1990-01-01', 'nurse')"
            )
            self._log("nurse", "insert", "patients", other.lastrowid)
        elif key == "F2" and self.colleague == "edit_note":
            # The nurse edits a note that was there before the run.
            conn.execute(
                "UPDATE notes SET body = 'keep me (read)', modified_by = 'nurse' "
                "WHERE id = 1"
            )
            self._log("nurse", "update", "notes", 1)
        elif key == "F2" and self.colleague == "edit":
            conn.execute(
                "UPDATE patients SET dob = '1980-02-04', modified_by = 'nurse' "
                "WHERE name = 'Ada Synthetic'"
            )
            (pid,) = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            self._log("nurse", "update", "patients", pid)
        elif key == "F2" and self.colleague == "edit_history":
            # The nurse edits the side row the run created.
            hid, pid = conn.execute(
                "SELECT id, patient_id FROM history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.execute(
                "UPDATE history SET smoker = 'yes', modified_by = 'nurse' WHERE id = ?",
                (hid,),
            )
            self._log("nurse", "update", "history", pid)
        elif key == "Delete":
            row = conn.execute(
                "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
            ).fetchone()
            if row and row[0] is not None and self.during_undo == "edit_patient":
                # The nurse edits the patient while the compensation runs.
                conn.execute(
                    "UPDATE patients SET dob = '1980-02-05', modified_by = 'nurse' "
                    "WHERE id = ?",
                    (row[0],),
                )
                self._log("nurse", "update", "patients", row[0])
            if self.during_undo == "edit_other":
                # The nurse keeps working on her own patient.
                conn.execute(
                    "UPDATE patients SET dob = '1990-01-02' WHERE name = 'Bo Synthetic'"
                )
                (bo,) = conn.execute(
                    "SELECT id FROM patients WHERE name = 'Bo Synthetic'"
                ).fetchone()
                self._log("nurse", "update", "patients", bo)
            if self.during_undo == "extra_patient":
                # Another patient written and removed again: the counter moves
                # past more rows than the run's own.
                conn.execute("INSERT INTO patients (name) VALUES ('Temp Synthetic')")
                conn.execute("DELETE FROM patients WHERE name = 'Temp Synthetic'")
            if self.during_undo == "audit_down":
                # The audit log goes unreadable while the compensation runs.
                self.audit.append({"__down__": "1"})
            if self.during_undo == "wipe_seeded":
                # The app's delete also takes the note that was there before.
                conn.execute("DELETE FROM notes WHERE id = 1")
                self._log("bot", "delete", "notes", 1)
            if self.during_undo == "wipe_notes":
                # The app's delete takes a colleague's unrelated note with it.
                conn.execute("DELETE FROM notes WHERE modified_by = 'nurse'")
            if row and row[0] is not None:
                if self.mode == "void":
                    conn.execute(
                        "UPDATE patients SET status = 'void', modified_by = 'bot' "
                        "WHERE id = ?",
                        (row[0],),
                    )
                else:
                    conn.execute("DELETE FROM patients WHERE id = ?", (row[0],))
                    if self.thorough:
                        conn.execute(
                            "DELETE FROM history WHERE patient_id = ?", (row[0],)
                        )
                conn.execute("INSERT INTO log (event) VALUES ('patient undone')")
        conn.commit()
        conn.close()


def _sql_verifier(path: Path) -> SqlRecordVerifier:
    uri = path.resolve().as_uri() + "?mode=ro"

    def connect():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    return SqlRecordVerifier(connect, "SELECT * FROM patients", poll_interval_s=0.01)


def _clinic_workflow(*, kind: str = "delete", retains=()) -> Workflow:
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
                kind=EffectKind.FIELD_EQUALS,
                match={"id": ValueExpr(param="undo_record_id")},
                field="status",
                value="void",
                risk="irreversible",
                timeout_s=1.0,
            )
            if kind == "void"
            else Effect(
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
            _key_step(
                "create_patient", "create the patient", "Enter", effects=[created]
            ),
            failing,
        ],
        undo_steps=[
            UndoStep(
                id="undo-create",
                undoes="create_patient",
                intent="delete the patient the run created",
                source="hand_written",
                kind=kind,
                retains=list(retains),
                steps=[delete],
                locate={"match": {"name": {"param": "name"}}},
            )
        ],
    )


def _run_clinic(
    tmp_path: Path,
    *,
    thorough: bool = True,
    mode: str = "delete",
    kind: str = "delete",
    retains=(),
    history: bool = True,
    colleague: Optional[str] = None,
    attribution: Any = False,
    allow_delete: bool = True,
    profile=None,
    allow_tables=("log",),
    exclusive: Optional[bool] = None,
    during_undo: Optional[str] = None,
    audit_down: Optional[str] = None,
    on_read: Optional[dict[int, Any]] = None,
    autoinc: bool = False,
    hidden_log: bool = False,
) -> RunReport:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "clinic.db"
    _make_db(db, autoinc=autoinc)
    workflow = _clinic_workflow(kind=kind, retains=retains)
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    from openadapt_flow.after_stop import records_reader
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    reader = DatabaseReader(
        dialect="sqlite",
        read_config={"sqlite_database": str(db)},
        allow_tables=list(allow_tables),
        profile=profile,
    )
    if on_read:
        # Something happens right before the n-th read of the store.
        plain_read, seen = reader.read, [0]

        def read(before=None):
            seen[0] += 1
            if seen[0] in on_read:
                on_read[seen[0]](db)
            return plain_read(before=before)

        reader.read = read  # type: ignore[method-assign]
    audit: list[dict[str, str]] = []
    # ``audit_down``: the audit log can't be read at "arm" or "after" it.
    audit_up = {"before": audit_down != "arm", "after": audit_down != "after"}
    spec = None
    if attribution == "audit":
        spec = FrameAttribution(
            kind="audit_read",
            actor_field="actor",
            self=["bot"],
            record_field="record",
            record_key="patient_id",
            record_keys={"patients": "id"},
            event_field="event",
            write_events=["insert", "update", "delete"],
            insert_events=["insert"],
            delete_events=["delete"],
            table_field="tbl",
            read={"kind": "in_memory"},  # the plan reads ``audit`` below
        )
    plan = RollbackPlan(
        reader,
        read_records=records_reader(_sql_verifier(db)),
        attribution=spec,
        audit_read=lambda: (
            [dict(e) for e in audit]
            if audit_up["before" if not audit_up.get("armed") else "after"]
            and not any("__down__" in e for e in audit)
            else None
        ),
        allow_delete=allow_delete,
        # Without attribution the clinic stands for a test copy only the run
        # writes; with it, other users may write.
        exclusive=(not attribution) if exclusive is None else exclusive,
    )
    params = {"name": "Ada Synthetic"}
    before = plan.arm(params)
    audit_up["armed"] = True

    def replayer() -> Replayer:
        return Replayer(
            ClinicBackend(
                db,
                thorough=thorough,
                mode=mode,
                history=history,
                colleague=colleague,
                audit=audit,
                during_undo=during_undo,
                hidden_log=hidden_log,
            ),
            vision=_vision(),
            effect_verifier=_sql_verifier(db),
            poll_interval_s=0.01,
        )

    run_dir = tmp_path / "run"
    report = replayer().run(workflow, bundle_dir=bundle, run_dir=run_dir, params=params)
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
    assert rollback.remaining_diff == {
        "history": {"added": 1, "removed": 0, "changed": 0}
    }
    assert "history: 1 added" in rollback.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    diff = json.loads((tmp_path / "run" / rollback.diff_path).read_text())
    [row] = diff["tables"]["history"]["added"]
    assert row["row"]["smoker"] == "no"


# ---------------------------------------------------------------------------
# Units: uncertain writes, refusals, unreadable stores
# ---------------------------------------------------------------------------


class ListReader:
    scope = "collection"

    def __init__(self, *reads: Optional[list[dict[str, Any]]]):
        self.reads = list(reads)

    def read(self, before=None):
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
        steps=[
            _key_step("save_triage", "save Triage", "Enter", effects=[_triage_effect()])
        ],
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
        allow_delete=True,
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


class DatabaseListReader(ListReader):
    scope = "database"


def test_nothing_landed_is_proof_of_absence(tmp_path):
    rows = [{"id": 1, "x": "y"}]

    def settle(reader):
        workflow = _single_save_workflow()
        keep_prior_state(
            workflow, workflow.steps[0], [dict(r) for r in rows], {}, tmp_path
        )
        return roll_back(
            _stopped([_entry(attempt="delivery_uncertain", status="unknown")]),
            workflow,
            run_dir=tmp_path,
            bundle_dir=tmp_path,
            reader=reader,
            before=records_snapshot(rows),
            runner=RecordingRunner(),
            read_records=lambda: rows,
        )

    record = settle(DatabaseListReader(rows))
    assert record.status == "nothing_landed", record.reason
    report = _stopped([_entry(attempt="delivery_uncertain", status="unknown")])
    report.execution_outcome = "HALTED"
    report.rollback = record
    assert classify_transaction_outcome(report).value == "HALTED_BEFORE_EFFECT"
    # A read of one collection isn't proof the run wrote nowhere else: it
    # never overrides the run's own uncertainty.
    from openadapt_flow.transaction import _store_proven_unchanged

    assert _store_proven_unchanged(report)
    report.rollback = settle(ListReader(rows))
    assert report.rollback.status == "nothing_landed"
    assert not _store_proven_unchanged(report)


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


# ---------------------------------------------------------------------------
# Safeguards: no hard delete unless allowed, void, other actors, conflicts
# ---------------------------------------------------------------------------


def test_a_hard_delete_is_refused_unless_the_deployment_allows_it(tmp_path, mockmed):
    report = _run_with_rollback(
        tmp_path, mockmed, _mockmed_workflow(), allow_delete=False
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back"
    [step] = rollback.steps
    assert step.decision == "refused"
    assert "rollback.allow_delete" in step.reason
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_void_compensation_keeps_only_the_runs_own_record(tmp_path):
    report = _run_clinic(
        tmp_path,
        mode="void",
        kind="void",
        retains=["patients"],
        history=False,
        allow_delete=False,
    )
    rollback = report.rollback
    assert rollback.status == "rolled_back", rollback.reason
    assert rollback.retained_rows == 1
    assert "voided record row(s) kept" in rollback.reason
    assert report.transaction_outcome == "ROLLED_BACK"


def test_a_void_that_leaves_a_side_record_is_not_a_rollback(tmp_path):
    report = _run_clinic(
        tmp_path, mode="void", kind="void", retains=["patients"], allow_delete=False
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back"
    assert rollback.retained_rows == 1
    assert rollback.remaining_diff == {
        "history": {"added": 1, "removed": 0, "changed": 0}
    }


def test_another_users_write_anywhere_stops_the_rollback(tmp_path):
    # A compensation runs only while the store holds nothing but the run's
    # own changes. The nurse's note on another patient is hers, by any
    # attribution: nothing is compensated, and she loses nothing.
    for name, attribution in (("audit", "audit"),):
        report = _run_clinic(tmp_path / name, colleague="note", attribution=attribution)
        rollback = report.rollback
        assert rollback.status == "not_rolled_back", rollback.reason
        assert "aren't positively the run's own" in rollback.reason
        assert rollback.steps == []
        assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
        conn = sqlite3.connect(tmp_path / name / "clinic.db")
        assert conn.execute(
            "SELECT body FROM notes WHERE modified_by = 'nurse'"
        ).fetchall() == [("call back",)]

    plain = _run_clinic(tmp_path / "b", colleague="note", attribution=False)
    assert plain.rollback.status == "not_rolled_back"
    assert plain.rollback.remaining_diff == {
        "notes": {"added": 1, "removed": 0, "changed": 0}
    }
    assert plain.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_record_another_actor_changed_after_the_run_is_not_compensated(tmp_path):
    report = _run_clinic(tmp_path, colleague="edit", attribution="audit")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back"
    assert rollback.conflicts >= 1
    assert "no one's change is lost" in rollback.reason
    assert rollback.steps == []
    # The patient, with the nurse's change, is still there.
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT dob, modified_by FROM patients WHERE name = 'Ada Synthetic'"
    ).fetchall() == [("1980-02-04", "nurse")]
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_change_after_the_stop_blocks_the_compensation(tmp_path):
    landed = [{"id": 7, "patient_id": "p1", "type": "Triage", "note": "n"}]
    edited = [{"id": 7, "patient_id": "p1", "type": "Triage", "note": "edited"}]
    runner = RecordingRunner()
    workflow = _single_save_workflow()
    keep_prior_state(workflow, workflow.steps[0], [], {}, tmp_path)
    record = roll_back(
        _stopped([_entry()]),
        workflow,
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader(landed, edited, edited),
        before=records_snapshot([]),
        runner=runner,
        read_records=lambda: edited,
        allow_delete=True,
    )
    [step] = record.steps
    assert step.decision == "refused"
    assert "changed since the last read" in step.reason
    assert record.conflicts == 1
    assert runner.runs == []
    assert record.status == "not_rolled_back"


def test_an_uncomparable_table_is_never_set_aside():
    from openadapt_flow.rollback_state import UNREAD_KEY, changes
    from openadapt_flow.runtime.effects.db_snapshot import Snapshot, Table

    def table(schema):
        return Table(
            mode="compare",
            schema=schema,
            columns=["a"],
            primary_key=["a"],
            generated_key=None,
            defaults={},
        )

    before = Snapshot(tables={"big": table("v1")})
    after = Snapshot(tables={"big": table("v2")})  # redefined in the window
    diff = diff_states(before, after)
    assert not diff.empty
    [change] = changes(diff)
    assert change == ("big", UNREAD_KEY, None, None)


# ---------------------------------------------------------------------------
# A write that can't be undone needs a person's approval first
# ---------------------------------------------------------------------------


def test_saves_without_undo_name_every_save_no_allowed_compensation_covers():
    from openadapt_flow.rollback import saves_without_undo, undoable

    workflow = _mockmed_workflow()  # save_triage has a delete; save_vitals none
    assert saves_without_undo(workflow, allow_delete=True) == ["save_vitals"]
    assert saves_without_undo(workflow, allow_delete=False) == [
        "save_triage",
        "save_vitals",
    ]
    assert not undoable(
        _mockmed_workflow(proposed=True), "save_triage", allow_delete=True
    )


def test_the_gate_sends_a_save_it_cannot_undo_to_a_person():
    from types import SimpleNamespace

    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.approval_mode.model import ItemDecisionKind

    gate = ApprovalGate.__new__(ApprovalGate)
    workflow = _mockmed_workflow()
    vitals = SimpleNamespace(workflow=workflow, step=workflow.steps[1])
    triage = SimpleNamespace(workflow=workflow, step=workflow.steps[0])

    check, decision = gate._check_undo_or_person(vitals)
    assert check.passed and decision is None  # off unless rollback is on

    gate.undo_or_person = True
    gate.undo_allows_delete = True
    check, decision = gate._check_undo_or_person(triage)
    assert check.passed and decision is None
    check, decision = gate._check_undo_or_person(vitals)
    assert not check.passed
    assert check.detail == "no_approved_compensation"
    assert decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert decision.needs_person

    gate.undo_allows_delete = False  # a delete isn't an undo here
    _check, decision = gate._check_undo_or_person(triage)
    assert decision is not None and decision.needs_person


def test_a_run_with_no_person_to_ask_refuses_a_save_it_cannot_undo():
    from types import SimpleNamespace

    from openadapt_flow.__main__ import _require_undo_or_person
    from openadapt_flow.approval_mode.model import ApprovalMode

    plan = SimpleNamespace(allow_delete=True)
    workflow = _mockmed_workflow()
    with pytest.raises(SystemExit, match="save_vitals"):
        _require_undo_or_person(workflow, plan, None)
    with pytest.raises(SystemExit, match="Nothing ran"):
        _require_undo_or_person(
            workflow, plan, SimpleNamespace(mode=ApprovalMode.NOT_GATED)
        )
    gate = SimpleNamespace(mode=ApprovalMode.EACH_ITEM)
    _require_undo_or_person(workflow, plan, gate)  # a person sees every save
    assert gate.undo_or_person is True and gate.undo_allows_delete is True
    covered = workflow.model_copy(update={"steps": workflow.steps[:1]})
    _require_undo_or_person(covered, plan, None)  # every save is undoable


# ---------------------------------------------------------------------------
# The app's own bookkeeping: one versioned profile per app
# ---------------------------------------------------------------------------


def _profile(**extra_rules):
    from openadapt_flow.app_profiles import AppProfile

    return AppProfile.model_validate(
        {
            "schema_version": "openadapt.app-profile/v2",
            "app": "clinic",
            "version": "test.1",
            "bookkeeping_tables": ["log"],
            "rules": [
                {
                    "id": "log",
                    "table": "log",
                    "mode": "append_only",
                    "reason": "audit log",
                },
                *extra_rules.get("rules", []),
            ],
        }
    )


def test_a_profile_cannot_excuse_patient_data():
    with pytest.raises(ValueError, match="patient data"):
        _profile(
            rules=[
                {"id": "p", "table": "patients", "mode": "append_only", "reason": "no"}
            ]
        )


def test_whole_database_rollback_with_the_app_profile(tmp_path):
    # The log is read, kept append-only by the profile: it may grow, and
    # nothing else may move.
    report = _run_clinic(tmp_path, profile=_profile(), allow_tables=())
    rollback = report.rollback
    assert rollback.status == "rolled_back", rollback.reason
    assert rollback.profile_id == "clinic/test.1"
    assert set(rollback.after_stop_diff) == {"patients", "history"}
    assert report.transaction_outcome == "ROLLED_BACK"

    # Without the profile, the log rows are the run's too.
    plain = _run_clinic(tmp_path / "plain", allow_tables=())
    assert plain.rollback.status == "not_rolled_back"
    assert "log" in plain.rollback.remaining_diff


def test_an_edited_row_in_an_append_only_table_is_never_excused(tmp_path):
    from openadapt_flow.rollback_state import DatabaseReader

    db = tmp_path / "clinic.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO log (event) VALUES ('signed in')")
    conn.commit()
    reader = DatabaseReader(
        dialect="sqlite", read_config={"sqlite_database": str(db)}, profile=_profile()
    )
    before = reader.read()
    conn.execute("INSERT INTO log (event) VALUES ('added row: fine')")
    conn.execute("UPDATE log SET event = 'rewritten' WHERE id = 1")
    conn.commit()
    conn.close()
    diff = diff_states(before, reader.read(before=before))
    assert diff.summary()["log"]["unread"]


def test_the_profile_excuses_only_the_acting_users_own_sign_in(tmp_path):
    # The same per-row rules diff_database applies: the acting user's own
    # sign-in counters are the app's bookkeeping; another user's are not.
    from openadapt_flow.rollback_state import DatabaseReader

    db = tmp_path / "emr.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,
          authorized INTEGER, uuid TEXT, last_updated TEXT);
        CREATE TABLE users_secure (id INTEGER PRIMARY KEY, username TEXT,
          password TEXT, last_update_password TEXT,
          login_fail_counter INTEGER DEFAULT 0, last_update TEXT);
        INSERT INTO users VALUES (1, 'clinic2', 1, 'uu-1', '2026-01-01');
        INSERT INTO users VALUES (2, 'bot', 1, 'uu-2', '2026-01-01');
        INSERT INTO users_secure VALUES (1, 'clinic2', 'h1', '2026-01-01', 0, '2026-01-01');
        INSERT INTO users_secure VALUES (2, 'bot', 'h2', '2026-01-01', 0, '2026-01-01');
        """
    )
    conn.commit()
    reader = DatabaseReader(
        dialect="sqlite",
        read_config={"sqlite_database": str(db)},
        profile="openemr",
        actor_names=("bot",),
    )
    before = reader.read()
    conn.execute(
        "UPDATE users_secure SET login_fail_counter = 0, last_update = '2026-10-29' "
        "WHERE username = 'bot'"
    )
    conn.commit()
    after = reader.read(before=before)
    assert not changes(diff_states(before, after, excuse=reader.excuser(after)))
    # Without the rules the row counts.
    assert changes(diff_states(before, after))

    conn.execute(
        "UPDATE users_secure SET login_fail_counter = 5 WHERE username = 'clinic2'"
    )
    conn.commit()
    conn.close()
    after = reader.read(before=before)
    left = changes(diff_states(before, after, excuse=reader.excuser(after)))
    assert [c[0] for c in left] == ["users_secure"]


@pytest.mark.parametrize("attribution", ["audit"])
def test_a_colleagues_edit_to_a_side_row_the_run_created_is_a_conflict(
    tmp_path, attribution
):
    # Review of #1634: the run creates a patient and its history row; a
    # nurse edits the history row before the stop. Never set aside, never
    # deleted with the patient.
    report = _run_clinic(tmp_path, colleague="edit_history", attribution=attribution)
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    # The audit log names the patient, so the patient row conflicts too.
    assert rollback.conflicts == (2 if attribution == "audit" else 1)
    assert rollback.steps == []
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute("SELECT smoker, modified_by FROM history").fetchall() == [
        ("yes", "nurse")
    ]
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_colleagues_view_is_not_a_write(tmp_path):
    report = _run_clinic(tmp_path, colleague="view", attribution="audit")
    assert report.rollback.status == "rolled_back", report.rollback.reason
    assert report.rollback.conflicts == 0


def test_without_attribution_nothing_is_compensated(tmp_path):
    # Review of #1634: a colleague's edit to the run's record can't be told
    # from the run's without attribution, so it is never deleted with it.
    report = _run_clinic(tmp_path, colleague="edit", exclusive=False)
    rollback = report.rollback
    assert rollback.status == "not_rolled_back"
    assert "rollback.attribution isn't set" in rollback.reason
    assert rollback.steps == []
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT dob, modified_by FROM patients WHERE name = 'Ada Synthetic'"
    ).fetchall() == [("1980-02-04", "nurse")]
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_confirmed_write_the_read_cant_see_is_never_nothing_landed(tmp_path):
    # Review of #1634: the patient tables allow-listed, so the read sees no
    # change while the ledger holds a confirmed create.
    report = _run_clinic(
        tmp_path, allow_tables=("log", "patients", "history"), allow_delete=False
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "confirmed a write" in rollback.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_step_left_uncompensated_is_never_rolled_back(tmp_path):
    # The compensation is refused (deletes not allowed) and the read can't
    # see the write: a clean read isn't proof the write was undone.
    report = _run_clinic(tmp_path, allow_tables=("log", "patients", "history"))
    assert report.rollback.status != "rolled_back"
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_the_diff_file_is_owner_only_even_over_a_leftover(tmp_path):
    import os
    import stat

    from openadapt_flow.rollback import _write_private

    path = tmp_path / "rollback" / "diff.json"
    path.parent.mkdir()
    leftover = path.with_name(f".{path.name}.{os.getpid()}")
    leftover.write_text("old")
    leftover.chmod(0o644)
    _write_private(path, {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_rollback_refuses_where_it_isnt_wired():
    from types import SimpleNamespace

    from openadapt_flow.__main__ import _refuse_rollback_unwired

    on = SimpleNamespace(rollback=SimpleNamespace(enabled=True))
    with pytest.raises(SystemExit, match="Nothing ran"):
        _refuse_rollback_unwired(on, "a worklist pool")
    _refuse_rollback_unwired(SimpleNamespace(rollback=None), "a worklist pool")


# ---------------------------------------------------------------------------
# Review of #1634, round 2
# ---------------------------------------------------------------------------


def test_a_colleagues_row_on_the_runs_record_is_a_conflict(tmp_path):
    # The nurse adds a note to the run's patient; the app's delete would take
    # it with the patient. Her write names the run's record: nothing runs.
    report = _run_clinic(tmp_path, colleague="note_on_run_patient", attribution="audit")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert rollback.steps == []
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM notes WHERE modified_by = 'nurse'"
    ).fetchone() == (1,)


def test_a_compensation_that_also_changes_another_row_is_reported(tmp_path):
    # Review of #1634, round 4: the app's delete also takes a row the run
    # didn't write (here, the colleague's note it wipes). Caught right after
    # the compensation, with the diff.
    report = _run_clinic(tmp_path, attribution="audit", during_undo="wipe_seeded")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "also changed" in rollback.reason
    assert rollback.diff_path
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_colleagues_edit_while_the_compensation_runs_is_reported(tmp_path):
    report = _run_clinic(tmp_path, attribution="audit", during_undo="edit_patient")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "while the rollback ran" in rollback.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_a_colleagues_edit_to_an_existing_row_stops_the_rollback(tmp_path):
    for name, attribution in (("audit", "audit"),):
        report = _run_clinic(
            tmp_path / name, colleague="edit_note", attribution=attribution
        )
        assert report.rollback.status == "not_rolled_back", report.rollback.reason
        assert report.rollback.steps == []


def test_audit_attribution_that_names_no_row_is_refused():
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    spec = FrameAttribution(
        kind="audit_read",
        actor_field="actor",
        self=["bot"],
        record_field="record",
        read={"kind": "in_memory"},
    )
    with pytest.raises(ValueError, match="record_key"):
        RollbackPlan(ListReader([]), attribution=spec)


def test_a_rolled_back_run_is_never_resumed(tmp_path):
    from openadapt_flow.__main__ import _was_rolled_back

    assert not _was_rolled_back(tmp_path)
    report = _run_clinic(tmp_path / "c")
    assert report.rollback.status == "rolled_back", report.rollback.reason
    assert _was_rolled_back(tmp_path / "c" / "run")


# ---------------------------------------------------------------------------
# Review of #1634, round 3: silence is never "no one else"
# ---------------------------------------------------------------------------


def test_an_unreadable_audit_log_refuses_the_run_or_compensates_nothing(tmp_path):
    # At arm: no first read, as with an unreadable store (the CLI refuses).
    armed = _run_clinic(
        tmp_path / "a", colleague="edit", attribution="audit", audit_down="arm"
    )
    assert armed.rollback.status == "unarmed"
    # After the stop: attribution can't speak for any row.
    late = _run_clinic(
        tmp_path / "b", colleague="edit", attribution="audit", audit_down="after"
    )
    assert late.rollback.status == "not_rolled_back", late.rollback.reason
    assert late.rollback.steps == []
    conn = sqlite3.connect(tmp_path / "b" / "clinic.db")
    assert conn.execute(
        "SELECT dob FROM patients WHERE name = 'Ada Synthetic'"
    ).fetchall() == [("1980-02-04",)]


def test_a_row_the_audit_log_cant_name_is_a_conflict(tmp_path):
    # ``contacts`` names its patient in ``pid``, not ``patient_id``.
    report = _run_clinic(tmp_path, colleague="contact", attribution="audit")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert rollback.steps == []
    assert "aren't positively the run's own" in rollback.reason


def test_a_last_writer_field_is_refused_for_rollback(tmp_path):
    # Round 6: a last-writer field can't show another user's edit between
    # two of the run's writes (a ledger save and a reversible step, say).
    from openadapt_flow.deployment import RollbackSection
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    spec = FrameAttribution(
        kind="record_field",
        actor_field="modified_by",
        self=["bot"],
        server_set=["modified_by"],
    )
    with pytest.raises(ValueError, match="audit log"):
        RollbackPlan(ListReader([]), attribution=spec)
    with pytest.raises(ValueError, match="audit_read"):
        RollbackSection(enabled=True, attribution=spec)


def test_a_colleagues_new_patient_stops_the_rollback(tmp_path):
    report = _run_clinic(tmp_path, colleague="register", attribution="audit")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert rollback.steps == []
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM patients WHERE name = 'Bo Synthetic'"
    ).fetchone() == (1,)


def test_a_run_nothing_was_undone_for_stays_resumable(tmp_path):
    from openadapt_flow.runtime.durable.resume import was_compensated

    record, runner = _uncertain(tmp_path, [])
    assert record.status == "nothing_landed"
    assert not was_compensated(tmp_path)


def test_the_attended_console_never_continues_a_rolled_back_run(tmp_path):
    from types import SimpleNamespace

    from openadapt_flow.rollback import COMPENSATED_MARKER
    from openadapt_flow.runtime.durable.attended import _refuse_rolled_back

    capability = SimpleNamespace(step_id="save", expected_next_transition=None)
    assert _refuse_rolled_back(tmp_path, capability) is None
    (tmp_path / COMPENSATED_MARKER).parent.mkdir()
    (tmp_path / COMPENSATED_MARKER).touch()
    refused = _refuse_rolled_back(tmp_path, capability)
    assert refused is not None and refused.status == "refused"


# ---------------------------------------------------------------------------
# Review of #1634, round 4: only the run's own changes, at every read
# ---------------------------------------------------------------------------


def _nurse_note_on_run_patient(db: Path) -> None:
    conn = sqlite3.connect(db)
    (pid,) = conn.execute(
        "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
    ).fetchone()
    conn.execute(
        "INSERT INTO notes (patient_id, body, modified_by) "
        "VALUES (?, 'BP 180/110, call MD', 'nurse')",
        (pid,),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("attribution", ["audit"])
def test_a_write_between_the_stop_and_the_compensation_stops_it(tmp_path, attribution):
    # Reads: 1 before the run, 2 after the stop, 3 right before the
    # compensation. The nurse writes just before read 3; the app's patient
    # delete would take her note with it.
    report = _run_clinic(
        tmp_path,
        attribution=attribution,
        on_read={3: _nurse_note_on_run_patient},
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    [step] = rollback.steps
    assert step.decision == "refused"
    assert "aren't positively the run's own" in step.reason
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT body FROM notes WHERE modified_by = 'nurse'"
    ).fetchall() == [("BP 180/110, call MD",)]


def test_attribution_whose_own_user_cant_be_resolved_arms_nothing():
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    spec = FrameAttribution(
        kind="audit_read",
        actor_field="actor",
        self=[{"param": "bot_user"}],
        record_field="record",
        record_key="patient_id",
        event_field="event",
        write_events=["insert"],
        insert_events=["insert"],
        table_field="tbl",
        read={"kind": "in_memory"},
    )
    plan = RollbackPlan(ListReader([]), attribution=spec, audit_read=lambda: [])
    assert plan.arm({}) is None


# ---------------------------------------------------------------------------
# Review of #1634, round 5
# ---------------------------------------------------------------------------


def test_a_delete_on_a_database_numbered_table_rolls_back(tmp_path):
    # The id counter can't be wound back: it may move by the rows the run's
    # compensations deleted, and no more.
    report = _run_clinic(tmp_path / "a", autoinc=True)
    assert report.rollback.status == "rolled_back", report.rollback.reason
    more = _run_clinic(tmp_path / "b", autoinc=True, during_undo="extra_patient")
    assert more.rollback.status == "not_rolled_back", more.rollback.reason
    assert "counter moved" in more.rollback.reason


def test_roll_back_never_compensates_on_a_last_writer_field(tmp_path):
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    spec = FrameAttribution(
        kind="record_field",
        actor_field="modified_by",
        self=["bot"],
        server_set=["modified_by"],
    )
    row = {"id": 7, "patient_id": "p1", "type": "Triage", "modified_by": "bot"}
    attributor = Attributor.from_spec(spec, {})
    assert attributor is not None
    runner = RecordingRunner()
    first, second = _entry(), _entry("save_triage_2")
    second.position = 2
    record = _roll_back(
        _stopped([first, second]),
        _single_save_workflow(),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=ListReader([row]),
        before=records_snapshot([]),
        runner=runner,
        attributor=attributor,
        allow_delete=True,
    )
    # roll_back itself, past the plan's refusal, compensates nothing.
    assert record.status == "not_rolled_back"
    assert "only the app's audit log" in record.reason
    assert runner.runs == []


def _unlogged_history_on_run_patient(db: Path) -> None:
    conn = sqlite3.connect(db)
    (pid,) = conn.execute(
        "SELECT MAX(id) FROM patients WHERE name = 'Ada Synthetic'"
    ).fetchone()
    conn.execute(
        "INSERT INTO history (patient_id, smoker, modified_by) VALUES (?, 'yes', 'nurse')",
        (pid,),
    )
    conn.commit()
    conn.close()


def test_one_logged_insert_never_vouches_for_two_rows(tmp_path):
    # The nurse's second history row for the run's patient isn't in the
    # log (or is filed elsewhere): the run logged one insert, so two rows
    # can't both be its own. Nothing is compensated.
    report = _run_clinic(
        tmp_path,
        attribution="audit",
        on_read={3: _unlogged_history_on_run_patient},
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert all(step.decision != "compensated" for step in rollback.steps)
    conn = sqlite3.connect(tmp_path / "clinic.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM history WHERE modified_by = 'nurse'"
    ).fetchone() == (1,)


def test_rollback_refuses_a_durable_run():
    from openadapt_flow.__main__ import _refuse_rollback_durable

    with pytest.raises(SystemExit, match="durable"):
        _refuse_rollback_durable(object(), True)
    _refuse_rollback_durable(None, True)
    _refuse_rollback_durable(object(), False)


def test_a_step_with_no_declared_effect_is_judged_by_the_reads_and_the_log(tmp_path):
    # OpenEMR, live: pressing Enter in a form field is a ledger entry that
    # stays unconfirmed (no effect declared, no compensation). A whole-
    # database read that shows nothing changed settles it, but only with an
    # audit window in which the run logged no write the read can't see.
    # Round 8: a write to a table the read keeps append-only (a disclosure
    # log) is caught by its log entry.
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    workflow = Workflow(
        name="w",
        steps=[_key_step("press_enter", "press Enter", "Enter")],
    )
    rows = [{"id": 1, "x": "y"}]
    spec = FrameAttribution(
        kind="audit_read",
        actor_field="actor",
        self=["bot"],
        record_field="record",
        record_key="patient_id",
        event_field="event",
        write_events=["insert", "update", "delete"],
        insert_events=["insert"],
        table_field="tbl",
        read={"kind": "in_memory"},
    )

    def settle(reader, log=None, audit=True):
        attributor = None
        if audit:
            entries: list[dict[str, str]] = []
            attributor = Attributor.from_spec(spec, {}, lambda: list(entries))
            assert attributor is not None and attributor.start()
            entries.extend(log or [])
        return roll_back(
            _stopped([_entry("press_enter", status="unknown")]),
            workflow,
            run_dir=tmp_path,
            bundle_dir=tmp_path,
            reader=reader,
            before=records_snapshot(rows),
            runner=RecordingRunner(),
            attributor=attributor,
            exclusive=False,
        )

    assert settle(DatabaseListReader(rows)).status == "nothing_landed"
    hidden = settle(
        DatabaseListReader(rows),
        log=[{"actor": "bot", "event": "insert", "tbl": "extended_log", "record": "2"}],
    )
    assert hidden.status == "not_rolled_back"
    assert "may have written" in hidden.reason
    assert settle(DatabaseListReader(rows), audit=False).status == "not_rolled_back"
    assert settle(ListReader(rows)).status == "not_rolled_back"


# ---------------------------------------------------------------------------
# Review of #1634, round 7
# ---------------------------------------------------------------------------


def test_a_screen_compensation_isnt_an_undo_without_a_fresh_session():
    # An attached browser gets no fresh session, so only an API-only
    # compensation can run: a save with a screen compensation waits for a
    # person, or the run is refused.
    from openadapt_flow.__main__ import _require_undo_or_person
    from openadapt_flow.rollback import saves_without_undo, undoable

    workflow = _clinic_workflow()
    plan = RollbackPlan(ListReader([]), allow_delete=True)
    assert undoable(workflow, "create_patient", allow_delete=True)
    assert not undoable(workflow, "create_patient", allow_delete=True, api_only=True)
    assert saves_without_undo(workflow, allow_delete=True, api_only=True) == [
        "create_patient"
    ]
    with pytest.raises(SystemExit, match="create_patient"):
        _require_undo_or_person(workflow, plan, None, api_only=True)


def test_a_late_write_hands_over_the_store_as_it_is(tmp_path):
    # The nurse edits the run's patient while its delete runs: the store is
    # back, but her write is gone. The diff names the rows she wrote.
    report = _run_clinic(tmp_path, attribution="audit", during_undo="edit_patient")
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "as the stop left them" in rollback.reason
    # The store is back: the summary says so, and the rows she wrote are
    # named apart, as the stop left them.
    assert rollback.remaining_diff == {}
    assert rollback.late_diff_path
    late = json.loads((tmp_path / "run" / rollback.late_diff_path).read_text())
    assert "patients" in late["tables"]


def test_a_rollback_that_isnt_back_is_always_reconciliation(tmp_path):
    # Round 9: a run with no ledger entry (a reversible step) can look
    # halted before any effect, but if the rollback found a write it can't
    # prove absent, a person reconciles.
    report = _stopped([])
    report.execution_outcome = "HALTED"
    from openadapt_flow.ir import RollbackRecord

    report.rollback = RollbackRecord(
        status="not_rolled_back",
        reason="the audit log shows 1 write(s)",
        scope="database",
    )
    assert classify_transaction_outcome(report).value == "RECONCILIATION_REQUIRED"


def test_the_apps_own_logged_bookkeeping_is_accounted_for():
    # An entry is accounted for by a row in exactly the table it names: one
    # that moved, or one the app profile excused (a sign-in uuid backfill),
    # by record, or any excused row when the entry names no record. As in
    # the frame, an insert naming no record (none, or OpenEMR's patient 0)
    # in a bookkeeping table is the app's own. A statement that moved
    # nothing, a write landing after the read, and a write to another
    # schema's same-named table are not (round 13).
    from openadapt_flow.rollback_state import StateDiff, TableDiff
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    spec = FrameAttribution(
        kind="audit_read",
        actor_field="actor",
        self=["bot"],
        record_field="record",
        record_key="patient_id",
        event_field="event",
        write_events=["insert", "update", "delete", "replace"],
        insert_events=["insert", "replace"],
        table_field="tbl",
        read={"kind": "in_memory"},
    )
    attributor = Attributor.from_spec(spec, {})
    assert attributor is not None
    diff = StateDiff(
        tables=[
            TableDiff(table="users", excused=[("k1", {"id": 2})]),
            TableDiff(
                table="history",
                added=[("k2", {"patient_id": "4", "smoker": "no"})],
            ),
            TableDiff(table="site2.extended_log"),
        ]
    )

    def entry(event, table, record):
        return {"actor": "bot", "event": event, "tbl": table, "record": record}

    accounted = [
        entry("insert", "uuid_registry", "0"),
        entry("insert", "uuid_registry", ""),
        entry("update", "users", "0"),  # the excused sign-in backfill
        entry("insert", "history", "4"),  # the run's own new row
        entry("update", "sequences", "4"),  # only a column the profile ignores
    ]
    assert (
        attributor.unaccounted_writes(
            accounted, [diff], {"uuid_registry"}, {"sequences"}
        )
        == []
    )
    hidden = [
        entry("delete", "history", "5"),  # moved nothing on record 5
        entry("update", "patient_data", "4"),  # not in the diff: landed later
        entry("insert", "extended_log", "4"),  # not site2.extended_log
        entry("replace", "extended_log", "4"),
    ]
    assert (
        attributor.unaccounted_writes(hidden, [diff], {"uuid_registry"}, {"sequences"})
        == hidden
    )


# ---------------------------------------------------------------------------
# Review of #1634, round 10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exclusive", [False, True])
def test_a_logged_write_the_reads_cant_see_is_never_rolled_back(tmp_path, exclusive):
    # The save also writes the allow-listed log, and the audit log shows it:
    # the compensation undoes the patient, but that write stays. Never
    # ROLLED_BACK, on a shared store or an exclusive one.
    report = _run_clinic(
        tmp_path, attribution="audit", hidden_log=True, exclusive=exclusive
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "no changed or excused row accounts for (in log)" in rollback.reason
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def _audit_spec():
    from openadapt_flow.runtime.effects.frame import FrameAttribution

    return FrameAttribution(
        kind="audit_read",
        actor_field="actor",
        self=["bot"],
        record_field="record",
        record_key="patient_id",
        event_field="event",
        write_events=["insert", "update", "delete"],
        insert_events=["insert"],
        table_field="tbl",
        read={"kind": "in_memory"},
    )


def _with_log(entries):
    log: list[dict[str, str]] = []
    attributor = Attributor.from_spec(_audit_spec(), {}, lambda: list(log))
    assert attributor is not None and attributor.start()
    log.extend(entries)
    return attributor


def test_a_run_with_no_ledger_never_ends_nothing_landed_over_a_logged_write(tmp_path):
    # A reversible step (no ledger entry) wrote somewhere the read can't see.
    rows = [{"id": 1, "patient_id": "4"}]
    record = roll_back(
        _stopped([]),
        Workflow(name="w", steps=[_key_step("note", "note", "F3")]),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=DatabaseListReader(rows),
        before=records_snapshot(rows),
        runner=RecordingRunner(),
        attributor=_with_log(
            [{"actor": "bot", "event": "insert", "tbl": "extended_log", "record": "4"}]
        ),
        exclusive=False,
    )
    assert record.status == "not_rolled_back"
    report = _stopped([])
    report.execution_outcome = "HALTED"
    report.rollback = record
    assert classify_transaction_outcome(report).value == "RECONCILIATION_REQUIRED"


def test_a_colleagues_write_never_masks_the_runs_own(tmp_path):
    # Round 11: the run's unlogged note and a nurse's logged note on the same
    # patient. The colleague's entry can't vouch for the run's row: a person
    # reconciles, never "halted before any effect".
    before_rows = [{"id": 1, "patient_id": "4"}]
    after_rows = before_rows + [
        {"id": 2, "patient_id": "4"},
        {"id": 3, "patient_id": "4"},
    ]
    record = roll_back(
        _stopped([]),
        Workflow(name="w", steps=[_key_step("note", "note", "F3")]),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=DatabaseListReader(after_rows),
        before=records_snapshot(before_rows),
        runner=RecordingRunner(),
        attributor=_with_log(
            [{"actor": "nurse", "event": "insert", "tbl": "records", "record": "4"}]
        ),
        exclusive=False,
    )
    assert record.status == "not_rolled_back"
    report = _stopped([])
    report.execution_outcome = "HALTED"
    report.rollback = record
    assert classify_transaction_outcome(report).value == "RECONCILIATION_REQUIRED"


def test_nothing_landed_needs_a_readable_audit_log(tmp_path):
    # Round 11: with audit attribution, an unreadable log after the stop can't
    # show the run wrote nowhere the read can't see.
    rows = [{"id": 1, "patient_id": "4"}]
    reads = [[]]
    attributor = Attributor.from_spec(
        _audit_spec(), {}, lambda: reads.pop(0) if reads else None
    )
    assert attributor is not None and attributor.start()
    record = roll_back(
        _stopped([]),
        Workflow(name="w", steps=[_key_step("note", "note", "F3")]),
        run_dir=tmp_path,
        bundle_dir=tmp_path,
        reader=DatabaseListReader(rows),
        before=records_snapshot(rows),
        runner=RecordingRunner(),
        attributor=attributor,
        exclusive=False,
    )
    assert record.status == "not_rolled_back"
    assert "couldn't be read after the stop" in record.reason


def test_an_unreadable_audit_log_at_the_end_is_never_rolled_back(tmp_path):
    # On an exclusive store the late check is skipped; the hidden-write check
    # still needs the log, and an unreadable one proves nothing.
    report = _run_clinic(
        tmp_path, attribution="audit", exclusive=True, during_undo="audit_down"
    )
    rollback = report.rollback
    assert rollback.status == "not_rolled_back", rollback.reason
    assert "couldn't be read" in rollback.reason


def test_a_backfill_on_a_table_over_max_rows_excuses_nothing(tmp_path):
    # Round 13: over max_rows a table keeps cell MACs only for the profile's
    # ref columns, so a uuid backfill beside a clinical edit would look like
    # the backfill alone. Nothing is excused there.
    db = tmp_path / "emr.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, uuid TEXT,
          allergy TEXT);
        INSERT INTO patient_data VALUES (1, 1, 'u1', ''), (2, 2, 'u2', ''),
          (3, 3, 'u3', ''), (4, 4, NULL, '');
        """
    )
    conn.commit()
    for max_rows in (2000, 2):
        reader = DatabaseReader(
            dialect="sqlite",
            read_config={"sqlite_database": str(db)},
            profile="openemr",
            max_rows=max_rows,
        )
        before = reader.read()
        conn.execute(
            "UPDATE patient_data SET uuid = 'u4', allergy = 'penicillin' WHERE id = 4"
        )
        conn.commit()
        after = reader.read(before=before)
        diff = diff_states(before, after, excuse=reader.excuser(after))
        assert [c[0] for c in changes(diff)] == ["patient_data"], max_rows
        conn.execute("UPDATE patient_data SET uuid = NULL, allergy = '' WHERE id = 4")
        conn.commit()
    conn.close()
