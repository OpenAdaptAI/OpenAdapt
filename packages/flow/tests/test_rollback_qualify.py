"""``rollback qualify``: a stop before and after every save, on a test copy.

Real stores (the MockMed record store; a SQLite database read whole), the
real Replayer and the real ``roll_back`` procedure; scripted screen. The
matrix must find compensations that restore the whole store, refuse ones
that leave a side record, and catch a draft an app keeps on its own before
the save.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import requests

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.rollback_qualify import qualify_rollback
from openadapt_flow.rollback_state import DatabaseReader, VerifierReader
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.undo_proposal import Sources, accept_proposal, write_proposals
from tests.test_rollback import (
    SCHEMA,
    ClinicBackend,
    _key_step,
    _sql_verifier,
    _vision,
)
from tests.test_undo_proposal import (
    MockMedCopy,
    _network,
    _save,
    mockmed,  # noqa: F401 - fixture
)


def test_matrix_qualifies_a_compensation_for_every_save_on_mockmed(tmp_path, mockmed):  # noqa: F811
    workflow = Workflow(
        name="mockmed-two-saves",
        steps=[_save("save_triage", "Triage"), _save("save_vitals", "Vitals")],
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    target = MockMedCopy(mockmed)
    report, new = qualify_rollback(
        Workflow.load(bundle),
        bundle_dir=bundle,
        target=target,
        reader=VerifierReader(RestRecordVerifier(mockmed)),
        sources=Sources(network_lines=_network()),
        work_dir=tmp_path / "work",
    )
    points = [(p.step_id, p.when, p.passed) for p in report.halt_points]
    assert points == [
        ("save_triage", "before_save", True),
        ("save_triage", "after_save", True),
        ("save_vitals", "before_save", True),
        ("save_vitals", "after_save", True),
    ], [p.reason for p in report.halt_points]
    assert report.summary()["saves_with_qualified_compensation"] == 2
    # The second save's halt point undid both saves, latest first.
    last = report.halt_points[-1].rollback
    assert [s["step_id"] for s in last["steps"]] == ["save_vitals", "save_triage"]
    assert last["status"] == "rolled_back"
    assert requests.get(f"{mockmed}/api/db", timeout=5).json()["records"] == []

    written = write_proposals(bundle, new)
    for undo in written.undo_steps:
        assert undo.awaiting_approval
        evidence = json.loads((bundle / undo.proposal.evidence_path).read_text())
        assert evidence["halt_point"]["when"] == "after_save"
        accept_proposal(bundle, undo.id, approver="nurse-a")
    assert not any(u.awaiting_approval for u in Workflow.load(bundle).undo_steps)


# ---------------------------------------------------------------------------
# SQLite clinic
# ---------------------------------------------------------------------------


class ClinicCopy:
    """A SQLite clinic database as a resettable test copy."""

    name = "clinic-test-copy"

    def __init__(self, path: Path, *, thorough: bool = True, drafts: bool = False):
        self.path = path
        self.thorough = thorough
        self.drafts = drafts

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
        conn = sqlite3.connect(self.path)
        conn.executescript(
            SCHEMA + "CREATE TABLE drafts (id INTEGER PRIMARY KEY, body TEXT);"
        )
        conn.commit()
        conn.close()

    def snapshot(self):
        return None  # the whole-database reader is used instead

    def read_records(self):
        state = _sql_verifier(self.path).capture_pre_state()
        return [dict(r) for r in state.records] if state.reachable else None

    def run(self, workflow, *, bundle_dir, run_dir, params):
        backend = DraftingClinic(self.path, thorough=self.thorough, drafts=self.drafts)
        return Replayer(
            backend,
            vision=_vision(),
            effect_verifier=_sql_verifier(self.path),
            poll_interval_s=0.01,
        ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params)


class DraftingClinic(ClinicBackend):
    """Typing a name keeps a draft row when ``drafts`` is on (an autosave)."""

    def __init__(self, path, *, thorough, drafts):
        super().__init__(path, thorough=thorough)
        self.drafts = drafts

    def type_text(self, text):
        super().type_text(text)
        if self.drafts:
            conn = sqlite3.connect(self.path)
            conn.execute("INSERT INTO drafts (body) VALUES (?)", (text,))
            conn.commit()
            conn.close()


def _clinic_workflow() -> Workflow:
    created = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"name": ValueExpr(param="name")},
        expected_count=1,
        count_new_only=True,
        risk="irreversible",
        timeout_s=1.0,
    )
    return Workflow(
        name="create-patient",
        params={"name": "Ada Synthetic"},
        steps=[
            Step(
                id="type_name",
                intent="type the name",
                action=ActionKind.TYPE,
                param="name",
            ),
            _key_step(
                "create_patient", "create the patient", "Enter", effects=[created]
            ),
        ],
    )


def _delete_recording(tmp_path: Path) -> Path:
    demo = Workflow(
        name="delete patient",
        steps=[
            Step(
                id="press_delete",
                intent="delete the patient",
                action=ActionKind.KEY,
                key="Delete",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Deleted",
                        timeout_s=0.2,
                    )
                ],
                risk="irreversible",
            )
        ],
    )
    path = tmp_path / "delete-recording"
    demo.save(path)
    return path


def _qualify_clinic(tmp_path: Path, **copy_kwargs):
    db = tmp_path / "clinic.db"
    target = ClinicCopy(db, **copy_kwargs)
    target.reset()
    workflow = _clinic_workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    reader = DatabaseReader(
        dialect="sqlite", read_config={"sqlite_database": str(db)}, allow_tables=["log"]
    )
    return qualify_rollback(
        Workflow.load(bundle),
        bundle_dir=bundle,
        target=target,
        reader=reader,
        sources=Sources(recordings=[_delete_recording(tmp_path)]),
        work_dir=tmp_path / "work",
    )


def test_matrix_qualifies_a_recorded_delete_over_the_whole_database(tmp_path):
    report, new = _qualify_clinic(tmp_path, thorough=True)
    assert [(p.when, p.passed) for p in report.halt_points] == [
        ("before_save", True),
        ("after_save", True),
    ], [p.reason for p in report.halt_points]
    assert report.scope == "database"
    [(candidate, _result)] = new.values()
    assert candidate.source == "recording"


def test_matrix_refuses_a_delete_that_leaves_the_side_record(tmp_path):
    report, new = _qualify_clinic(tmp_path, thorough=False)
    after = report.halt_points[-1]
    assert after.when == "after_save" and not after.passed
    assert "history: 1 added" in after.reason
    assert after.rollback["remaining_diff"] == {
        "history": {"added": 1, "removed": 0, "changed": 0}
    }
    assert new == {}


def test_matrix_catches_a_draft_the_app_keeps_before_the_save(tmp_path):
    report, _new = _qualify_clinic(tmp_path, thorough=True, drafts=True)
    before = report.halt_points[0]
    assert before.when == "before_save" and not before.passed
    assert "drafts: 1 added" in before.reason
    assert report.summary()["before_save_clean"] == 0


def test_cli_refuses_a_target_not_declared_a_test_copy(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    bundle = tmp_path / "bundle"
    _clinic_workflow().save(bundle)
    config = tmp_path / "deployment.yaml"
    config.write_text("name: prod\nbackend:\n  kind: web\n  url: http://127.0.0.1:9\n")
    assert main(["rollback", "qualify", str(bundle), "--config", str(config)]) == 3
    assert "declared test copy" in capsys.readouterr().err
