"""Plain results, work items, the Needs-you inbox, and Results.

The safety contract these tests pin:

* A label may claim "nothing was written" only when Flow's transaction outcome
  proves it (shared cases in ``src/lib/outcome-cases.json``).
* An item whose save may have gone through blocks every further item of the
  task until a person checks the record. Nothing is retried blindly.
* Item input values reach Flow only through a private params file, never
  argv, events, or logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from openadapt_desktop.outcomes import halt_kind, plain_result, plain_result_for_transaction
from openadapt_desktop.work_items import WorkItemError, summarize, validate_inputs

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((REPO / "src" / "lib" / "outcome-cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['execution']}-{c['transaction']}")
def test_plain_result_matches_the_shared_cases(case: dict) -> None:
    result = plain_result(
        case["execution"],
        case["transaction"],
        pre_action_refusal=bool(case.get("pre_action_refusal")),
    )
    assert result["key"] == case["key"]
    assert result["nothing_written"] is case["nothing_written"]
    assert result["safe_to_retry"] is case["safe_to_retry"]


SWITCH_CASES = json.loads((REPO / "src" / "lib" / "outcome-cases.json").read_text())[
    "switch_cases"
]


@pytest.mark.parametrize("case", SWITCH_CASES, ids=lambda c: c["name"])
def test_the_switch_never_changes_an_outcome_word(case: dict) -> None:
    """Shared with src/lib/approvalMode.test.ts: same cases, same answers."""

    from openadapt_desktop.outcomes import consistent_transaction_outcome
    from openadapt_desktop.work_items import public_item, status_after_run

    key = plain_result(case["execution"], case["transaction"])["key"]
    assert key == case["key"]
    consistent = consistent_transaction_outcome(case["execution"], case["transaction"])
    assert status_after_run(key, case["approval"], consistent) == case["status"]
    item = public_item(
        {
            "item_id": "item-1",
            "batch_id": "b",
            "workflow_id": "wf",
            "position": 0,
            "status": case["status"],
            "execution_outcome": case["execution"],
            "transaction_outcome": case["transaction"],
            "approval_json": json.dumps(case["approval"]),
        }
    )
    assert item["result"] == case["key"]
    assert item["saved_without_person"] is case["saved_without_person"]


def test_coarse_halt_never_claims_nothing_was_written() -> None:
    for execution in ("HALTED", "halt", "FAILED", "unknown", "ROLLED_BACK"):
        result = plain_result(execution, None)
        assert result["nothing_written"] is False
        assert result["safe_to_retry"] is False


def test_transaction_only_records_map_conservatively() -> None:
    assert plain_result_for_transaction("HALTED_BEFORE_EFFECT")["nothing_written"] is True
    assert plain_result_for_transaction("RECONCILIATION_REQUIRED")["key"] == "check_record"
    assert plain_result_for_transaction("SOMETHING_NEW")["key"] == "check_record"
    assert plain_result_for_transaction(None)["nothing_written"] is False


def test_halt_kind_reads_the_right_record_check() -> None:
    report = {
        "execution_outcome": "HALTED",
        "halt": {"state_id": "step_2"},
        "results": [
            {"step_id": "step_1", "ok": True, "resolution": {"rung": "structural"}},
            {
                "step_id": "step_2",
                "ok": False,
                "identity": {"status": "mismatch", "expected": "A1", "observed": "B2"},
            },
        ],
    }
    assert halt_kind(report) == "wrong_record"
    report["results"][1] = {"step_id": "step_2", "ok": False, "resolution": None}
    assert halt_kind(report) == "screen_changed"
    report["results"][1] = {"step_id": "step_2", "ok": False, "effect_verified": False}
    assert halt_kind(report) == "save_not_confirmed"
    assert halt_kind({"execution_outcome": "VERIFIED", "results": []}) is None


DECLARED = [
    {"name": "member_id", "type": "string", "secret": False, "required": True, "example": "M-1"},
    {"name": "note", "type": "string", "secret": False, "required": True, "example": "Reviewed"},
    {"name": "password", "type": "string", "secret": True, "required": True, "example": None},
]


def test_inputs_must_name_every_declared_input_and_no_secret() -> None:
    assert validate_inputs({"member_id": "M-2", "note": "Ok"}, DECLARED) == {
        "member_id": "M-2",
        "note": "Ok",
    }
    with pytest.raises(WorkItemError, match="missing: note"):
        # A missing value must not fall back to the recorded example.
        validate_inputs({"member_id": "M-2"}, DECLARED)
    with pytest.raises(WorkItemError, match="protected input"):
        validate_inputs({"member_id": "M-2", "note": "x", "password": "p"}, DECLARED)
    with pytest.raises(WorkItemError, match="no input named"):
        validate_inputs({"member_id": "M-2", "note": "x", "extra": "y"}, DECLARED)
    with pytest.raises(WorkItemError, match="can't be empty"):
        validate_inputs({"member_id": "  ", "note": "x"}, DECLARED)


def test_an_omitted_optional_input_never_takes_the_recorded_value() -> None:
    """Attack attempt: leave out an optional input and Flow fills it from the
    recording, so every item types the demonstration's value and the record
    check confirms it. Desktop requires every declared input instead."""

    declared = [
        DECLARED[0],
        {"name": "note", "type": "string", "secret": False, "required": False,
         "example": "Reviewed"},
    ]
    with pytest.raises(WorkItemError, match="missing: note"):
        validate_inputs({"member_id": "M-2"}, declared)
    # An empty value is an explicit empty value, not the recorded one.
    assert validate_inputs({"member_id": "M-2", "note": ""}, declared) == {
        "member_id": "M-2",
        "note": "",
    }


# --------------------------------------------------------------- dispatcher


class _Audit:
    def log(self, *_a, **_k) -> None:
        pass


class _Storage:
    def get_captures(self, **_k):
        return []

    def get_storage_usage(self):
        return {}


def _verified_report(*, transaction: str | None, execution: str, halt: dict | None = None,
                     results: list | None = None) -> dict:
    envelope = {
        "version": "openadapt.execution-outcome/v1",
        "outcome": execution,
        "profile": "standard",
        "production_eligible": execution == "VERIFIED",
        "execution_completed": execution in {"VERIFIED", "COMPLETED_UNVERIFIED"},
        "required_contracts": {"authorization": 1, "identity": 0, "postcondition": 0, "effect": 0},
        "passed_contracts": {"authorization": 1, "identity": 0, "postcondition": 0, "effect": 0},
        "evidence_classes": ["authorization"],
        "model_calls": 0,
        "external_network_calls": "none",
        "compensation_actions": 0,
    }
    report = {
        "success": execution == "VERIFIED",
        "execution_outcome": execution,
        "execution_profile": "standard",
        "production_eligible": execution == "VERIFIED",
        "execution_completed": envelope["execution_completed"],
        "outcome_envelope": envelope,
        "model_calls": 0,
        "results": results or [],
    }
    if transaction is not None:
        report["transaction_outcome"] = transaction
    if halt is not None:
        report["halt"] = halt
    return report


class _Bridge:
    """Flow stand-in that returns the next scripted report for each run."""

    def __init__(self, reports: list[dict]) -> None:
        self.reports = list(reports)
        self.calls: list[dict] = []

    def ensure_browser_runtime(self, _progress) -> None:
        pass

    def supports_approval_gate(self) -> bool:
        return True

    def run(self, bundle, config, out_dir=None, params_file=None, env_overrides=None, **_k):
        from openadapt_desktop.flow_bridge import FlowResult

        staged = None
        if params_file is not None:
            staged = {
                "path": Path(params_file),
                "mode": oct(Path(params_file).stat().st_mode & 0o777),
                "values": json.loads(Path(params_file).read_text()),
            }
        self.calls.append(
            {
                "bundle": bundle,
                "config": config,
                "params_file": staged,
                "approval_mode": _k.get("approval_mode"),
                "approval_wait_s": _k.get("approval_wait_s"),
            }
        )
        report = self.reports.pop(0)
        (out_dir / "report.json").write_text(json.dumps(report))
        return FlowResult(
            ok=report.get("success") is True,
            returncode=0 if report.get("success") is True else 1,
            out_dir=out_dir,
            stdout="saved note for M-4821\n",
        )


@pytest.fixture
def task(tmp_path: Path):
    flow_ir = pytest.importorskip("openadapt_flow.ir")
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    bundle = config.data_dir / "bundles" / "wf-note"
    flow_ir.Workflow(
        name="Add a claim note",
        params={"member_id": "M-0001", "note": "Reviewed"},
        steps=[flow_ir.Step(id="step-1", intent="Click Save", action=flow_ir.ActionKind.CLICK)],
    ).save(bundle)
    db.insert_capture("cap-1", str(tmp_path / "cap"), "2026-10-08T00:00:00+00:00",
                      task_description="Add a claim note")
    db.update_capture("cap-1", duration_secs=74.0)
    db.insert_bundle("wf-note", str(bundle), capture_id="cap-1")
    events: list[tuple[str, dict]] = []
    services = EngineServices(config, db=db, storage=_Storage(), audit=_Audit())
    disp = EngineDispatcher(config, services=services, emit=lambda e, d: events.append((e, d)))
    yield disp, db, events
    db.close()


TARGET = {"backend": "web", "url": "https://emr.example.test"}


def _run(disp, member: str, note: str = "Reviewed", **extra) -> dict:
    return disp.dispatch(
        "run_item",
        {
            "workflow_id": "wf-note",
            "inputs": {"member_id": member, "note": note},
            "target": TARGET,
            "batch_id": "batch-1",
            **extra,
        },
    )


def test_item_values_reach_flow_only_through_a_private_file(task) -> None:
    disp, _db, events = task
    bridge = _Bridge([_verified_report(transaction="VERIFIED", execution="VERIFIED")])
    disp.services._flow_bridge = bridge

    result = _run(disp, "M-4821")

    assert result["ok"] is True
    assert result["item"]["result"] == "done_checked"
    assert result["item"]["status"] == "done"
    staged = bridge.calls[0]["params_file"]
    assert staged["values"] == {"member_id": "M-4821", "note": "Reviewed"}
    assert staged["mode"] == "0o600"
    assert not staged["path"].exists()
    assert "M-4821" not in json.dumps(events)
    assert "M-4821" not in repr(bridge.calls[0]["config"])


def test_report_forwards_only_a_consistent_transaction_outcome(task) -> None:
    disp, _db, _events = task
    disp.services._flow_bridge = _Bridge(
        [
            _verified_report(transaction="HALTED_BEFORE_EFFECT", execution="HALTED",
                             halt={"state_id": "step_1", "intent": "Click Save",
                                   "reason": "Different member"}),
            _verified_report(transaction="HALTED_BEFORE_EFFECT", execution="VERIFIED"),
        ]
    )

    stopped = _run(disp, "M-1")
    assert stopped["report"]["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
    assert stopped["item"]["result"] == "stopped_before_saving"
    assert stopped["item"]["nothing_written"] is True

    inconsistent = _run(disp, "M-2")
    assert inconsistent["report"]["transaction_outcome"] is None


def test_an_uncertain_save_blocks_more_items_until_a_person_checks(task) -> None:
    disp, db, _events = task
    bridge = _Bridge(
        [
            _verified_report(transaction="RECONCILIATION_REQUIRED", execution="HALTED",
                             halt={"state_id": "step_1", "intent": "Click Save",
                                   "reason": "Delivery uncertain"}),
            _verified_report(transaction="VERIFIED", execution="VERIFIED"),
        ]
    )
    disp.services._flow_bridge = bridge

    first = _run(disp, "M-1")
    assert first["item"]["result"] == "check_record"
    assert first["item"]["actions"] == ["confirmed_saved", "retry_after_check", "done_by_person"]

    blocked = _run(disp, "M-2")
    assert blocked["pre_action_refusal"] is True
    assert "may have gone through" in blocked["error"]
    assert len(bridge.calls) == 1

    # A blind retry of the uncertain item is refused.
    refused = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "retry"},
    )
    assert refused["ok"] is False
    unchecked = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "retry_after_check"},
    )
    assert unchecked["ok"] is False
    assert "Look at the record first" in unchecked["error"]
    assert len(bridge.calls) == 1

    # Closing a possible save also needs the person to have looked.
    unlooked = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "confirmed_saved"},
    )
    assert unlooked["ok"] is False
    assert "Look at the record first" in unlooked["error"]
    by_hand = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "done_by_person"},
    )
    assert by_hand["ok"] is False
    assert db.get_work_item(first["item"]["item_id"])["status"] == "open"

    confirmed = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "confirmed_saved",
         "record_checked": True},
    )
    assert confirmed["ok"] is True
    assert db.get_work_item(first["item"]["item_id"])["status"] == "confirmed_saved"

    after = _run(disp, "M-2")
    assert after["item"]["result"] == "done_checked"


def test_a_proven_no_write_stop_can_run_again_once(task) -> None:
    disp, db, _events = task
    bridge = _Bridge(
        [
            _verified_report(
                transaction="HALTED_BEFORE_EFFECT",
                execution="HALTED",
                halt={"state_id": "step_1", "intent": "Click Save", "reason": "Mismatch"},
                results=[{"step_id": "step_1", "intent": "Click Save", "ok": False,
                          "identity": {"status": "mismatch", "expected": "M-1",
                                       "observed": "M-9"}}],
            ),
            _verified_report(transaction="VERIFIED", execution="VERIFIED"),
        ]
    )
    disp.services._flow_bridge = bridge

    first = _run(disp, "M-1")
    item = first["item"]
    assert item["halt_kind"] == "wrong_record"
    assert item["expected"] == "M-1" and item["observed"] == "M-9"
    assert item["actions"] == ["retry", "skip", "decline", "teach"]
    inbox = disp.dispatch("list_attention_items", {})["items"]
    assert [entry["item_id"] for entry in inbox] == [item["item_id"]]
    assert disp.dispatch("get_needs_attention", {})["count"] == 1

    retried = disp.dispatch(
        "resolve_attention_item",
        {"item_id": item["item_id"], "action": "retry", "target": TARGET},
    )
    assert retried["ok"] is True
    assert retried["item"]["result"] == "done_checked"
    assert retried["item"]["retry_of"] == item["item_id"]
    assert db.get_work_item(item["item_id"])["status"] == "retried"
    assert disp.dispatch("list_attention_items", {})["items"] == []

    summary = disp.dispatch("get_results_summary", {})
    assert summary["total"]["done_checked"] == 1
    assert summary["total"]["needed_person"] == 1
    assert summary["total"]["wrong_record_prevented"] == 1
    assert summary["tasks"]["wf-note"]["baseline_seconds"] == 74.0
    assert summary["tasks"]["wf-note"]["estimated_seconds_saved"] == 74


def test_a_done_item_is_not_repeated_by_accident(task) -> None:
    disp, _db, _events = task
    bridge = _Bridge([_verified_report(transaction="VERIFIED", execution="VERIFIED")])
    disp.services._flow_bridge = bridge
    _run(disp, "M-7")

    repeat = _run(disp, "M-7")

    assert repeat["pre_action_refusal"] is True
    assert "already done" in repeat["error"]
    assert len(bridge.calls) == 1


def test_bad_inputs_refuse_before_flow(task) -> None:
    disp, db, _events = task
    bridge = _Bridge([])
    disp.services._flow_bridge = bridge

    result = disp.dispatch(
        "run_item",
        {"workflow_id": "wf-note", "inputs": {"member_id": "M-1"}, "target": TARGET},
    )

    assert result["pre_action_refusal"] is True
    assert bridge.calls == []
    assert db.list_work_items() == []


def test_task_inputs_and_workflow_card_fields(task) -> None:
    disp, _db, _events = task
    inputs = disp.dispatch("get_task_inputs", {"workflow_id": "wf-note"})
    assert [item["name"] for item in inputs["inputs"]] == ["member_id", "note"]
    card = disp.dispatch("get_workflows", {})[0]
    assert card["readiness"] == "new"
    assert card["task"] == "Add a claim note"
    assert card["demo_seconds"] == 74.0
    assert card["open_items"] == 0
    assert card["blocked"] is False


def test_skip_and_decline_close_a_no_write_item(task) -> None:
    disp, db, _events = task
    disp.services._flow_bridge = _Bridge(
        [
            _verified_report(transaction="HALTED_BEFORE_EFFECT", execution="HALTED",
                             halt={"state_id": "step_1", "intent": "Save", "reason": "x"}),
            _verified_report(transaction="HALTED_BEFORE_EFFECT", execution="HALTED",
                             halt={"state_id": "step_1", "intent": "Save", "reason": "x"}),
        ]
    )
    one = _run(disp, "M-1")["item"]
    two = _run(disp, "M-2")["item"]

    assert disp.dispatch("resolve_attention_item",
                         {"item_id": one["item_id"], "action": "skip"})["ok"] is True
    assert disp.dispatch("resolve_attention_item",
                         {"item_id": two["item_id"], "action": "decline"})["ok"] is True
    assert db.get_work_item(one["item_id"])["status"] == "skipped"
    assert db.get_work_item(two["item_id"])["status"] == "declined"
    # A checked-save action is not offered for a stop that wrote nothing.
    assert disp.dispatch(
        "resolve_attention_item",
        {"item_id": one["item_id"], "action": "confirmed_saved"},
    )["ok"] is False


def test_summary_counts_only_verified_items_as_done() -> None:
    rows = [
        {"item_id": "a", "batch_id": "b", "workflow_id": "w", "position": 0,
         "status": "done", "execution_outcome": "VERIFIED", "transaction_outcome": "VERIFIED"},
        {"item_id": "b", "batch_id": "b", "workflow_id": "w", "position": 1,
         "status": "confirmed_saved", "execution_outcome": "HALTED",
         "transaction_outcome": "RECONCILIATION_REQUIRED"},
        {"item_id": "c", "batch_id": "b", "workflow_id": "w", "position": 2,
         "status": "open", "execution_outcome": "COMPLETED_UNVERIFIED",
         "transaction_outcome": "COMPLETED_UNVERIFIED"},
    ]
    summary = summarize(rows, baselines={"w": 60.0})
    assert summary["total"]["done_checked"] == 1
    assert summary["total"]["needed_person"] == 2
    assert summary["total"]["done_by_person"] == 1
    assert summary["total"]["waiting"] == 1
    assert summary["tasks"]["w"]["estimated_seconds_saved"] == 60


def test_first_task_can_leave_the_read_only_path_before_anything_ran(task) -> None:
    disp, db, _events = task
    db.set_first_workflow_state(
        stage="review", capture_id="cap-1", workflow_id="wf-note", task="Add a claim note"
    )
    moved = disp.dispatch(
        "set_first_workflow_stage", {"stage": "complete", "workflow_id": "wf-note"}
    )
    assert moved["state"]["stage"] == "complete"

    db.set_first_workflow_state(
        stage="reconciliation", capture_id="cap-1", workflow_id="wf-note",
        task="Add a claim note",
    )
    with pytest.raises(ValueError, match="Invalid first workflow stage transition"):
        disp.dispatch(
            "set_first_workflow_stage", {"stage": "complete", "workflow_id": "wf-note"}
        )


def test_task_inputs_return_where_the_task_was_recorded(task) -> None:
    disp, db, _events = task
    assert disp.dispatch("get_task_inputs", {"workflow_id": "wf-note"})["target"] is None

    db.set_task_target("cap-1", {"backend": "web", "url": "https://emr.example.test"})

    inputs = disp.dispatch("get_task_inputs", {"workflow_id": "wf-note"})
    assert inputs["target"] == {"backend": "web", "url": "https://emr.example.test"}


def test_a_certified_task_is_ready_to_run_only_once_sealed(task) -> None:
    disp, db, _events = task
    db.update_bundle("wf-note", status="certified")
    card = disp.dispatch("get_workflows", {})[0]
    assert card["certified"] is True
    assert card["encrypted"] is False
    assert card["readiness"] == "checking"

    bundle_dir = Path(db.get_bundle("wf-note")["bundle_path"])
    (bundle_dir / "workflow.json.enc").write_bytes(b"sealed")
    card = disp.dispatch("get_workflows", {})[0]
    assert card["encrypted"] is True
    assert card["readiness"] == "ready"
    assert card["version"] == 1
    assert card["capture_id"] == "cap-1"


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00"
)


class _SampleBridge:
    """Stand-in for Flow's tutorial: writes the two sample run reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool]] = []

    def ensure_browser_runtime(self, _progress) -> None:
        pass

    def tutorial(self, out_dir: Path, *, break_it: bool = True, timeout=None):
        self.calls.append((out_dir, break_it))
        for folder, execution, transaction, verdict in (
            ("run", "VERIFIED", "VERIFIED", "confirmed"),
            ("run-broken", "HALTED", "RECONCILIATION_REQUIRED", "refuted"),
        ):
            run_dir = out_dir / folder
            (run_dir / "steps").mkdir(parents=True)
            (run_dir / "steps" / "step_001_after.png").write_bytes(_PNG)
            report = _verified_report(
                transaction=transaction,
                execution=execution,
                results=[
                    {"step_id": "step_000", "intent": "type <note>", "ok": True,
                     "identity": {"status": "verified"}},
                    {"step_id": "step_001", "intent": "click 'Save Encounter'",
                     "ok": execution == "VERIFIED",
                     "identity": {"status": "verified"},
                     "effect_verified": execution == "VERIFIED",
                     "effect_evidence": [{"final_verdict": verdict}],
                     "after_png": "steps/step_001_after.png"},
                ],
            )
            report["params"] = {"note": "Synthetic follow-up"}
            (run_dir / "report.json").write_text(json.dumps(report))


def test_the_sample_runs_without_a_recording_and_reports_plain_results(task) -> None:
    disp, _db, events = task
    bridge = _SampleBridge()
    disp.services._flow_bridge = bridge

    sample = disp.dispatch("run_sample", {})

    assert sample["ok"] is True
    assert bridge.calls and bridge.calls[0][1] is True
    normal, dropped = sample["runs"]
    assert normal["result"] == "done_checked"
    assert normal["record_checks_confirmed"] == 1
    assert normal["right_record_checks"] == 2
    assert normal["screen"]["width"] == 2 and normal["screen"]["height"] == 1
    # The dropped save may have landed: the sample never claims nothing was
    # written for it, exactly like a real run.
    assert dropped["result"] == "check_record"
    assert dropped["nothing_written"] is False
    assert dropped["record_checks_failed"] == 1
    assert [step["saves"] for step in dropped["steps"]] == [False, True]
    assert ("sample_progress", {"state": "done", "detail": ""}) in events


def test_an_interrupted_item_run_becomes_a_record_to_check(task) -> None:
    disp, db, _events = task
    bridge = _Bridge([_verified_report(transaction="VERIFIED", execution="VERIFIED")])
    disp.services._flow_bridge = bridge
    # Desktop stopped after inserting this item and before its run ended.
    db.insert_work_item(
        "item-lost",
        batch_id="batch-0",
        workflow_id="wf-note",
        position=0,
        label="M-1",
        inputs_json=json.dumps({"member_id": "M-1", "note": "Reviewed"}),
        inputs_digest="x",
    )

    inbox = disp.dispatch("list_attention_items", {})["items"]
    assert [entry["item_id"] for entry in inbox] == ["item-lost"]
    assert inbox[0]["result"] == "check_record"
    assert inbox[0]["nothing_written"] is False
    assert inbox[0]["actions"] == ["confirmed_saved", "retry_after_check", "done_by_person"]

    # Nothing else of this task runs, and the same item is not re-run blindly.
    blocked = _run(disp, "M-2")
    assert blocked["pre_action_refusal"] is True
    again = _run(disp, "M-1")
    assert again["pre_action_refusal"] is True
    assert bridge.calls == []


def test_recording_another_task_keeps_an_uncertain_first_run_locked(task) -> None:
    disp, db, _events = task
    db.set_first_workflow_state(
        stage="reconciliation", capture_id="cap-1", workflow_id="wf-note",
        task="Add a claim note",
    )

    with pytest.raises(ValueError, match="Check the target of the last run"):
        disp.dispatch("set_first_workflow_stage", {"stage": "record"})
    assert db.get_first_workflow_state()["stage"] == "reconciliation"

    db.set_first_workflow_state(
        stage="complete", capture_id="cap-1", workflow_id="wf-note", task="Add a claim note"
    )
    moved = disp.dispatch("set_first_workflow_stage", {"stage": "record"})
    assert moved["state"]["stage"] == "record"


def test_a_task_counts_items_waiting_for_a_new_showing(task) -> None:
    disp, _db, _events = task
    disp.services._flow_bridge = _Bridge(
        [
            _verified_report(transaction="HALTED_BEFORE_EFFECT", execution="HALTED",
                             halt={"state_id": "step_1", "intent": "Save", "reason": "x"}),
        ]
    )
    item = _run(disp, "M-1")["item"]
    assert disp.dispatch("resolve_attention_item",
                         {"item_id": item["item_id"], "action": "teach"})["ok"] is True

    card = disp.dispatch("get_workflows", {})[0]
    assert card["open_items"] == 1
    assert card["teaching_items"] == 1
