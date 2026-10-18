"""After a stop, end to end on MockMed: change ledger, declared undo, lint.

These drive the REAL Replayer against the REAL MockMed system of record (the
in-process ``fault_server``) through the ``RestRecordVerifier``, the same way
``test_replayer_effects`` does. The backend and vision are scripted; each
keypress writes to (or deletes from) the MockMed record store, so every
verdict below reads the actual records, never the screen.

The story: a two-step MockMed workflow saves a Triage encounter (step 1,
confirmed), then saves a Vitals encounter that the server rejects (step 2,
refuted). The run stops with the Triage row already written. The bundle
declares an undo for step 1: delete that Triage row, then check the record
store shows none.
"""

from __future__ import annotations

import argparse
import json

import pytest
import requests

from openadapt_flow.after_stop import (
    UndoAlreadyRun,
    UndoNotApproved,
    UndoNotOffered,
    approve_undo,
    change_ledger_lines,
    offer_undos,
    read_undo_result,
    run_undo,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    RunReport,
    Step,
    UndoStep,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.policy import lint_workflow
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match


class MockMedBackend(FakeBackend):
    """Each Enter saves the next encounter; Delete removes the Triage row.

    Models the MockMed app's persistence boundary: the first save lands, the
    second is rejected by the server (``optimistic``: the screen still says
    "Saved"), and the Delete key removes the saved Triage encounter.
    """

    def __init__(self, sor_url: str):
        super().__init__(viewport=(300, 200))
        self.sor_url = sor_url
        self.saves = 0
        self.deletes = 0

    def press(self, key):
        super().press(key)
        if key == "Enter":
            self.saves += 1
            kind, fault = (
                ("Triage", "") if self.saves == 1 else ("Vitals", "optimistic")
            )
            url = f"{self.sor_url}/api/encounter"
            if fault:
                url += f"?fault={fault}"
            requests.post(
                url,
                json={"patient_id": "p1", "type": kind, "note": "n"},
                timeout=5,
            )
        elif key == "Delete":
            self.deletes += 1
            for row in _records(self.sor_url):
                if row["patient_id"] == "p1" and row["type"] == "Triage":
                    requests.delete(
                        f"{self.sor_url}/api/encounter/{row['id']}", timeout=5
                    )


def _records(url: str) -> list[dict]:
    return requests.get(f"{url}/api/db", timeout=5).json()["records"]


def _vision() -> FakeVision:
    vision = FakeVision()
    hit = Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    vision.text_results = {"Saved": hit, "Deleted": hit}
    return vision


def _save(step_id: str, kind: str) -> Step:
    return Step(
        id=step_id,
        intent=f"save {kind} encounter",
        action=ActionKind.KEY,
        key="Enter",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
            )
        ],
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": "p1", "type": kind},
                expected_count=1,
                risk="irreversible",
                probe=f"one {kind} encounter for the patient",
                timeout_s=1.0,
            )
        ],
    )


UNDO_TRIAGE = UndoStep(
    id="undo-triage",
    undoes="save_triage",
    intent="delete the Triage encounter saved at step 1",
    source="hand_written",
    steps=[
        Step(
            id="delete_triage",
            intent="delete the Triage encounter",
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
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"patient_id": "p1", "type": "Triage"},
                    expected_count=0,
                    risk="irreversible",
                    probe="no Triage encounter for the patient",
                    timeout_s=1.0,
                )
            ],
        )
    ],
)


def _workflow(*, with_undo: bool = True) -> Workflow:
    return Workflow(
        name="mockmed-two-saves",
        steps=[_save("save_triage", "Triage"), _save("save_vitals", "Vitals")],
        undo_steps=[UNDO_TRIAGE] if with_undo else [],
    )


@pytest.fixture
def mockmed():
    url, _db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _halted_run(tmp_path, url, *, with_undo=True):
    """Run the two-save workflow until it stops at step 2."""
    workflow = _workflow(with_undo=with_undo)
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    backend = MockMedBackend(url)
    replayer = Replayer(
        backend,
        vision=_vision(),
        effect_verifier=RestRecordVerifier(url),
        poll_interval_s=0.01,
    )
    run_dir = tmp_path / "run"
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    return workflow, bundle, run_dir, report, backend


def _undo_replayer(url):
    backend = MockMedBackend(url)
    replayer = Replayer(
        backend,
        vision=_vision(),
        effect_verifier=RestRecordVerifier(url),
        poll_interval_s=0.01,
    )
    return replayer, backend


# -- 1. Change ledger --------------------------------------------------------


def test_halt_after_an_earlier_write_lists_that_write_as_confirmed(tmp_path, mockmed):
    workflow, _bundle, run_dir, report, _ = _halted_run(tmp_path, mockmed)

    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    # The Triage row really is in the record store: the app is half-changed.
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]

    ledger = report.change_ledger
    assert [e.step_id for e in ledger] == ["save_triage", "save_vitals"]
    first, second = ledger
    assert first.position == 1
    assert first.status == "confirmed"
    assert first.may_need_undo is True
    assert first.undo_id == "undo-triage"
    assert [(e.status, e.observed_effect) for e in first.effects] == [
        ("confirmed", "present")
    ]
    assert first.effects[0].description == (
        "record_written: one Triage encounter for the patient"
    )
    # The rejected write is refuted and proven absent: nothing to undo there.
    assert second.status == "refuted"
    assert second.may_need_undo is False

    # The ledger is in the persisted report, not just in memory.
    persisted = RunReport.model_validate_json((run_dir / "report.json").read_text())
    assert persisted.change_ledger == ledger
    # PHI-free: no selector value reaches the ledger.
    assert "p1" not in json.dumps([e.model_dump() for e in ledger])

    # The halt message names what may need undoing.
    from openadapt_flow.__main__ import _change_ledger_epilogue

    message = _change_ledger_epilogue(
        report, run_dir, argparse.Namespace(bundle=str(tmp_path / "bundle"))
    )
    assert "step `save_triage` (save Triage encounter): confirmed" in message
    assert "may need undoing" in message
    assert "declared undo: `undo-triage` (needs a person)" in message
    assert "Flow never runs an undo on its own" in message

    # So do explain and REPORT.md.
    from openadapt_flow.report import render_run_report
    from openadapt_flow.scaffold_verifier import explain_run

    explained = explain_run(run_dir)
    assert "Changes before the stop (in order):" in explained
    assert "1. step `save_triage`" in explained
    report_md = render_run_report(run_dir).read_text()
    assert "## Changes before the stop" in report_md
    assert "`undo-triage`" in report_md


def test_ledger_without_a_declared_undo_says_to_repair_by_hand(tmp_path, mockmed):
    _workflow, _bundle, _run_dir, report, _ = _halted_run(
        tmp_path, mockmed, with_undo=False
    )
    lines = "\n".join(change_ledger_lines(report))
    assert "No undo is declared for step(s) save_triage" in lines


# -- 3. Declared undo: offered, approved, run, verified ----------------------


def test_declared_undo_is_offered_approved_run_and_verified(tmp_path, mockmed):
    workflow, bundle, run_dir, _report, _ = _halted_run(tmp_path, mockmed)

    offers = offer_undos(run_dir, workflow)
    assert [o.undo_id for o in offers] == ["undo-triage"]
    assert offers[0].ledger_entry.status == "confirmed"
    # Offering ran nothing.
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]

    approval = approve_undo(run_dir, workflow, "undo-triage", approver="nurse-a")
    assert approval.offer_digest == offers[0].offer_digest

    replayer, backend = _undo_replayer(mockmed)
    result = run_undo(
        run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle
    )

    assert backend.deletes == 1
    assert result.status == "verified", result.reason
    assert result.approver == "nurse-a"
    assert any("CONFIRMED" in line for line in result.effect_results)
    # The undo landed in the system of record.
    assert _records(mockmed) == []
    assert read_undo_result(run_dir, "undo-triage") == result

    # An approval is used once.
    with pytest.raises(UndoAlreadyRun):
        run_undo(run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle)
    assert backend.deletes == 1


def test_undo_that_does_not_land_is_not_verified(tmp_path, mockmed):
    workflow, bundle, run_dir, _report, _ = _halted_run(tmp_path, mockmed)
    approve_undo(run_dir, workflow, "undo-triage", approver="nurse-a")

    class DeleteIgnored(MockMedBackend):
        def press(self, key):
            FakeBackend.press(self, key)  # the app swallows the delete

    replayer = Replayer(
        DeleteIgnored(mockmed),
        vision=_vision(),
        effect_verifier=RestRecordVerifier(mockmed),
        poll_interval_s=0.01,
    )
    result = run_undo(
        run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle
    )
    assert result.status == "not_verified"
    assert "not confirmed" in result.reason or "failed" in result.reason
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]


# -- An undo never runs without approval -------------------------------------


def test_undo_never_runs_without_approval(tmp_path, mockmed):
    workflow, bundle, run_dir, _report, run_backend = _halted_run(tmp_path, mockmed)

    # The halted run itself never ran the undo.
    assert run_backend.deletes == 0
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]

    # Without an approval, run_undo refuses before touching the app.
    replayer, backend = _undo_replayer(mockmed)
    with pytest.raises(UndoNotApproved):
        run_undo(run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle)
    assert backend.actions == []
    assert not (run_dir / "undo" / "undo-triage" / "run").exists()

    # A forged approval for another offer is refused too.
    approval_path = run_dir / "undo" / "undo-triage" / "approval.json"
    approval_path.parent.mkdir(parents=True)
    approval_path.write_text(
        json.dumps(
            {
                "undo_id": "undo-triage",
                "offer_digest": "0" * 64,
                "approver": "someone",
                "approved_at": "2026-10-18T00:00:00+00:00",
            }
        )
    )
    with pytest.raises(UndoNotApproved):
        run_undo(run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle)
    assert backend.actions == []
    approval_path.unlink()

    # An approval stops matching when the run's report changes after it.
    approve_undo(run_dir, workflow, "undo-triage", approver="nurse-a")
    report_path = run_dir / "report.json"
    report_path.write_text(report_path.read_text() + "\n")
    with pytest.raises(UndoNotApproved):
        run_undo(run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle)
    assert backend.actions == []
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]


def test_cli_refuses_to_approve_or_run_without_a_person(tmp_path, mockmed, capsys):
    from openadapt_flow.__main__ import build_parser

    _workflow, bundle, run_dir, _report, _ = _halted_run(tmp_path, mockmed)
    parser = build_parser()

    args = parser.parse_args(["undo", "list", str(run_dir), "--bundle", str(bundle)])
    assert args.func(args) == 0
    assert "undo-triage" in capsys.readouterr().out

    # pytest's stdin is not a terminal: an agent or script can't approve.
    args = parser.parse_args(
        [
            "undo",
            "approve",
            str(run_dir),
            "--bundle",
            str(bundle),
            "--undo",
            "undo-triage",
        ]
    )
    assert args.func(args) == 2
    assert not (run_dir / "undo" / "undo-triage" / "approval.json").exists()

    args = parser.parse_args(
        ["undo", "run", str(run_dir), "--bundle", str(bundle), "--undo", "undo-triage"]
    )
    assert args.func(args) == 3
    assert "Nothing was executed" in capsys.readouterr().err
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]


def test_undo_is_not_offered_for_a_finished_run_or_an_absent_change(tmp_path, mockmed):
    workflow, _bundle, run_dir, _report, _ = _halted_run(tmp_path, mockmed)
    # Step 2's change was proven absent, so an undo for it is never offered.
    undo_vitals = UNDO_TRIAGE.model_copy(
        update={"id": "undo-vitals", "undoes": "save_vitals"}
    )
    with_vitals = workflow.model_copy(
        update={"undo_steps": [*workflow.undo_steps, undo_vitals]}
    )
    assert [o.undo_id for o in offer_undos(run_dir, with_vitals)] == ["undo-triage"]
    with pytest.raises(UndoNotOffered):
        approve_undo(run_dir, with_vitals, "undo-vitals", approver="nurse-a")

    report_path = run_dir / "report.json"
    report = RunReport.model_validate_json(report_path.read_text())
    report.success = True
    report_path.write_text(report.model_dump_json())
    assert offer_undos(run_dir, workflow) == []


def test_undo_declaration_is_sealed_bundle_content(tmp_path):
    bundle = tmp_path / "bundle"
    _workflow(with_undo=False).save(bundle)
    before = Workflow.load(bundle).manifest.content_digest
    assert "undo_steps" not in (bundle / "workflow.json").read_text()

    from openadapt_flow.after_stop import declare_undo

    declare_undo(bundle, UNDO_TRIAGE)
    loaded = Workflow.load(bundle)
    assert loaded.undo_steps == [UNDO_TRIAGE]
    assert loaded.manifest.content_digest != before

    # An undo must say what the system of record shows afterwards.
    with pytest.raises(ValueError, match="declares no effect"):
        UndoStep(
            id="blind",
            undoes="save_triage",
            intent="delete it",
            source="hand_written",
            steps=[UNDO_TRIAGE.steps[0].model_copy(update={"effects": []})],
        )
    # And it must name a step of the bundle.
    with pytest.raises(ValueError, match="not in this bundle"):
        declare_undo(
            bundle,
            UNDO_TRIAGE.model_copy(update={"id": "undo-x", "undoes": "nope"}),
        )


# -- 2. Write-ordering lint ---------------------------------------------------


def _click(step_id: str, intent: str, *, armed: bool, **extra) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text=intent,
            context_text="Jane Doe MRN 0001" if armed else None,
        ),
        identity_armed=armed,
        expect=[Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=intent)],
        **extra,
    )


def test_lint_flags_a_write_before_the_identity_check():
    save = _save("save_draft", "Triage")
    workflow = Workflow(
        name="mockmed-draft-first",
        steps=[
            _click("new_encounter", "New Encounter", armed=False),
            save,
            _click("open_patient", "Open", armed=True),
            _click("confirm", "Confirm", armed=False),
        ],
    )
    before = [s.id for s in workflow.steps]

    report = lint_workflow(workflow)
    findings = [f for f in report.findings if f.code == "write_before_identity_check"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.step_id == "save_draft"
    assert "open_patient" in finding.message
    assert finding.suggested_order == [
        "new_encounter",
        "open_patient",
        "save_draft",
        "confirm",
    ]
    assert "Suggested order" in report.render()
    # Advice only: the bundle is not reordered.
    assert [s.id for s in workflow.steps] == before


def test_lint_is_quiet_when_the_identity_check_comes_first():
    workflow = Workflow(
        name="mockmed-check-first",
        steps=[
            _click("open_patient", "Open", armed=True),
            _save("save_draft", "Triage"),
            _click("back", "Back", armed=True),
        ],
    )
    codes = {f.code for f in lint_workflow(workflow).findings}
    assert "write_before_identity_check" not in codes
    assert "write_before_check" not in codes


def test_lint_flags_a_halting_check_after_a_write():
    guard = Guard(
        predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Insurance on file"),
        on_unmet="halt",
    )
    workflow = Workflow(
        name="mockmed-two-tabs",
        steps=[
            _click("open_patient", "Open", armed=True),
            _save("save_triage", "Triage"),
            _click("billing_tab", "Billing", armed=False),
            _click("check_insurance", "Insurance", armed=False, guard=guard),
            _save("save_vitals", "Vitals"),
        ],
    )
    findings = [
        f for f in lint_workflow(workflow).findings if f.code == "write_before_check"
    ]
    assert len(findings) == 1
    assert findings[0].step_id == "save_triage"
    assert findings[0].suggested_order == [
        "open_patient",
        "billing_tab",
        "check_insurance",
        "save_triage",
        "save_vitals",
    ]


def test_lint_flags_an_undo_for_a_missing_step():
    workflow = _workflow(with_undo=False).model_copy(
        update={"undo_steps": [UNDO_TRIAGE.model_copy(update={"undoes": "not_a_step"})]}
    )
    codes = {(f.code, f.severity) for f in lint_workflow(workflow).findings}
    assert ("undo_target_missing", "error") in codes
