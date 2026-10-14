"""Check it's practice item and safety drills against Flow's local campaign.

Flow (openadapt-flow PR 486, B276) sets up the drills from the practice case
(``qualify drills BUNDLE --json``), gates the practice save for a person, and
leaves each drill's signed receipt in ``RUN_DIR/qualification-fault/``. Here
Flow is a scripted stand-in, so every report and drill record comes from the
test setup. All values are made up.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.flow_bridge import APPROVAL_WAIT_S, FlowResult
from engine.qualification import add_qualification_case
from engine.qualification_lifecycle import retain_drill_evidence
from tests.test_engine.test_qualification import _bundle, _initialize

pytest.importorskip("openadapt_flow.qualification")

from openadapt_flow.ir import ActionKind, Step, Workflow  # noqa: E402


class _CheckItFlow:
    """Flow stand-in for ``qualify drills`` and ``qualify run-case``."""

    def __init__(self, report: dict, *, fault_status: dict | None = None) -> None:
        self.report = report
        self.fault_status = fault_status
        self.drill_setups = 0
        self.case_calls: list[dict] = []
        self.watches: list[dict] = []
        self.dispatcher: EngineDispatcher | None = None

    def supports_approval_gate(self) -> bool:
        return True

    def supports_local_drills(self) -> bool:
        return True

    def qualify_drills(self, bundle_dir, *, env_overrides=None) -> dict:
        self.drill_setups += 1
        return {"ok": True, "campaign": {"drills": [{"case_id": "x"}] * 5, "skipped": []}}

    def qualify_run_case(self, bundle, config, *, case_id, out_dir, **kwargs) -> FlowResult:
        self.case_calls.append({"case_id": case_id, **kwargs})
        if self.dispatcher is not None:
            with self.dispatcher._approval_lock:
                self.watches.extend(dict(w) for w in self.dispatcher._approval_watches.values())
        (out_dir / "report.json").write_text(json.dumps(self.report))
        if self.fault_status is not None:
            folder = out_dir / "qualification-fault"
            folder.mkdir()
            (folder / "status.json").write_text(json.dumps(self.fault_status))
        return FlowResult(ok=False, returncode=1, out_dir=out_dir)


@pytest.fixture
def check_it(tmp_path: Path):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    bundle = _bundle(
        config.data_dir / "bundles" / "wf-1",
        Step(id="open", intent="Open", action=ActionKind.CLICK),
        Step(id="save", intent="Save", action=ActionKind.CLICK),
        params={"record_id": "example"},
    )
    _initialize(bundle)
    add_qualification_case(bundle, workflow_id="wf-1", case_id="practice-1", kind="representative")
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-1", str(bundle))
    dispatcher = EngineDispatcher(config, services=EngineServices(config, db=db))
    yield dispatcher, bundle
    db.close()


def _results(bundle: Path) -> dict[str, int]:
    project = Workflow.load(bundle).qualification
    assert project is not None
    return {case.id: len(case.results) for case in project.cases}


@pytest.mark.parametrize(
    ("decision", "words"),
    [
        ("approval_required", "Nobody approved the save in time"),
        ("declined", "You chose Don't save"),
    ],
)
def test_a_practice_save_nobody_approved_records_nothing(check_it, decision, words) -> None:
    dispatcher, bundle = check_it
    flow = _CheckItFlow({"success": False, "approval": {"decision": decision}})
    flow.dispatcher = dispatcher
    dispatcher.services._flow_bridge = flow

    result = dispatcher.dispatch(
        "run_qualification_case",
        {
            "workflow_id": "wf-1",
            "case_id": "practice-1",
            "parameters_json": '{"record_id":"case-1"}',
        },
    )

    assert result["ok"] is False
    assert result["not_approved"] is True
    assert words in result["error"]
    # The drills were set up from the practice case before it ran.
    assert flow.drill_setups == 1
    assert result["drill_setup"] == {"ok": True, "drills": 5, "skipped": []}
    # The practice save waits for a person, with the test values on screen.
    (call,) = flow.case_calls
    assert call["approval_wait_s"] == APPROVAL_WAIT_S
    (watch,) = flow.watches
    assert watch["kind"] == "practice"
    assert watch["values"] == [{"name": "record_id", "value": "case-1"}]
    assert all(count == 0 for count in _results(bundle).values())


def test_a_drill_that_couldnt_make_its_change_hasnt_passed(check_it) -> None:
    dispatcher, bundle = check_it
    workflow = Workflow.load(bundle)
    drill = next(
        case for case in workflow.qualification.cases if case.kind.value == "wrong_identity"
    )
    flow = _CheckItFlow(
        {"success": False},
        fault_status={
            "schema": "openadapt.local-drill-evidence/v1",
            "staged": False,
            "declined": "no_structural_locator",
        },
    )
    flow.dispatcher = dispatcher
    dispatcher.services._flow_bridge = flow

    result = dispatcher.dispatch(
        "run_qualification_case",
        {
            "workflow_id": "wf-1",
            "case_id": drill.id,
            "parameters_json": '{"record_id":"case-1"}',
            "fault_target": {"step_id": "save", "actuation_path": "gui"},
        },
    )

    assert result["ok"] is False
    assert "couldn't make its change" in result["error"]
    assert "no_structural_locator" in result["error"]
    # A drill never waits for a person.
    (call,) = flow.case_calls
    assert "approval_wait_s" not in call
    assert flow.watches == []
    assert _results(bundle)[drill.id] == 0


def test_a_drill_needs_a_flow_with_the_local_campaign(check_it) -> None:
    dispatcher, bundle = check_it
    workflow = Workflow.load(bundle)
    drill = next(case for case in workflow.qualification.cases if case.kind.value == "ambiguity")

    class OldFlow:
        def supports_local_drills(self) -> bool:
            return False

        def qualify_run_case(self, *_a, **_k):
            raise AssertionError("must not reach Flow")

    dispatcher.services._flow_bridge = OldFlow()
    result = dispatcher.dispatch(
        "run_qualification_case",
        {"workflow_id": "wf-1", "case_id": drill.id, "parameters_json": '{"record_id":"case-1"}'},
    )
    assert result["ok"] is False
    assert "can't run the safety drills" in result["error"]


def test_a_practice_item_never_runs_on_a_flow_that_cant_wait_for_a_person(check_it) -> None:
    dispatcher, bundle = check_it

    class OldFlow:
        def supports_approval_gate(self) -> bool:
            return False

        def supports_local_drills(self) -> bool:
            return False

        def qualify_run_case(self, *_a, **_k):
            raise AssertionError("must not reach Flow")

    dispatcher.services._flow_bridge = OldFlow()
    result = dispatcher.dispatch(
        "run_qualification_case",
        {
            "workflow_id": "wf-1",
            "case_id": "practice-1",
            "parameters_json": '{"record_id":"case-1"}',
        },
    )
    assert result["ok"] is False
    assert "can't stop for your approval" in result["error"]


def test_drill_evidence_is_kept_only_when_the_drill_made_its_change(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    run_dir = tmp_path / "run"
    folder = run_dir / "qualification-fault"
    folder.mkdir(parents=True)

    assert retain_drill_evidence(bundle, case_id="d-1", run_id="r1", run_dir=run_dir)[1]

    (folder / "status.json").write_text(json.dumps({"staged": True}))
    evidence, problem = retain_drill_evidence(bundle, case_id="d-1", run_id="r1", run_dir=run_dir)
    assert evidence == [] and "signed record is missing" in problem

    receipt, mutation = b'{"receipt":1}', b'{"mutation":1}'
    (folder / "receipt.json").write_bytes(receipt)
    (folder / "mutation.json").symlink_to(folder / "receipt.json")
    assert retain_drill_evidence(bundle, case_id="d-1", run_id="r1", run_dir=run_dir)[0] == []

    (folder / "mutation.json").unlink()
    (folder / "mutation.json").write_bytes(mutation)
    evidence, problem = retain_drill_evidence(bundle, case_id="d-1", run_id="r1", run_dir=run_dir)
    assert problem is None
    assert evidence == [
        {
            "kind": "fault_receipt",
            "sha256": hashlib.sha256(receipt).hexdigest(),
            "relative_path": "d-1/r1/fault-receipt.json",
        },
        {
            "kind": "fault_mutation",
            "sha256": hashlib.sha256(mutation).hexdigest(),
            "relative_path": "d-1/r1/fault-mutation.json",
        },
    ]
    kept = bundle / "qualification-evidence" / "d-1" / "r1"
    assert (kept / "fault-receipt.json").read_bytes() == receipt
    assert (kept / "fault-mutation.json").read_bytes() == mutation


def test_a_case_run_that_stopped_says_where_in_flows_words() -> None:
    report = {
        "results": [
            {"step_id": "open", "intent": "click 'Open'", "ok": True},
            {
                "step_id": "<qualification-environment>",
                "intent": "observe exact qualification environment",
                "ok": False,
                "error": "qualification environment observer does not match the project",
            },
        ]
    }
    assert EngineDispatcher._first_failed_step(report) == (
        "observe exact qualification environment: "
        "qualification environment observer does not match the project"
    )
    assert EngineDispatcher._first_failed_step({"results": [{"ok": True}]}) is None
