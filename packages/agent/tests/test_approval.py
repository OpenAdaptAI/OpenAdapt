"""Runs that wait for a person to approve a save (Flow approval modes).

openadapt-flow gates Standard and Regulated runs on a person approving each
save (``each_item``). These tests pin how this server reports that: a run
waiting for an approval is ``needs_review`` with what the person must do,
never a timeout or a failure, and this server never turns the pause off
(``not_gated``) unless the operator chose it, in which case every result
records it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FlowCliStub

import openadapt_agent.runner as runner_mod
from openadapt_agent.approval import waiting_for_person
from openadapt_agent.bridge import AgentBridge
from openadapt_agent.contract import REASONS, RUN_RESULT_SCHEMA, reason_for_run
from openadapt_agent.runner import FlowRunner, RunnerConfig


def _write_request(run_dir: Path, *, expires_in_s: float = 600.0) -> str:
    """An approval request as openadapt-flow writes it (item-approval/)."""
    request_id = uuid.uuid4().hex
    folder = run_dir / "item-approval"
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    (folder / f"request-{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": "openadapt.item-approval-request/v1",
                "request_id": request_id,
                "run_id": run_dir.name,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=expires_in_s)).isoformat(),
            }
        )
    )
    return request_id


def _approval_report(transaction: str, decision: str = "approval_required") -> dict:
    return {
        "workflow_name": "Demo Triage",
        "success": False,
        "execution_outcome": "HALTED",
        "transaction_outcome": transaction,
        "execution_profile": "standard",
        "production_eligible": False,
        "results": [{"step_id": "s1", "intent": "Open the patient chart", "ok": True}],
        "approval": {
            "schema_version": "openadapt.approval-mode-item/v1",
            "mode": "each_item",
            "decision": decision,
            "needs_person": True,
        },
        "model_calls": 0,
    }


def _bridge(bundles_root, runner_config, **kwargs):
    return AgentBridge(bundles_root, runner_config, allow_run=True, **kwargs)


def _run(bridge, request_id="ref-0001", wait=10):
    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": next(iter(bridge.catalog)),
            "inputs": {"note": "fresh note"},
            "request_id": request_id,
            "wait_seconds": wait,
        },
    )


# -- the request files --------------------------------------------------------


def test_waiting_for_person_reads_flow_request_files(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    assert waiting_for_person(run_dir) is False
    request_id = _write_request(run_dir)
    assert waiting_for_person(run_dir) is True
    (run_dir / "item-approval" / f"answer-{request_id}.json").write_text("{}")
    assert waiting_for_person(run_dir) is False

    other = tmp_path / "run-2"
    other.mkdir()
    consumed = _write_request(other)
    (other / "item-approval" / f"consumed-{consumed}").write_text("consumed\n")
    assert waiting_for_person(other) is False

    expired = tmp_path / "run-3"
    expired.mkdir()
    _write_request(expired, expires_in_s=-60)
    assert waiting_for_person(expired) is False

    linked = tmp_path / "run-4"
    linked.mkdir()
    (linked / "item-approval").symlink_to(run_dir / "item-approval")
    assert waiting_for_person(linked) is False


# -- the run command ----------------------------------------------------------


def test_runs_never_turn_the_approval_pause_off_by_default(
    monkeypatch, runner_config, bundle_dir, success_report
):
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    FlowRunner(runner_config).run(workflow="w", bundle_dir=bundle_dir, params={})
    assert "--approval-mode" not in stub.calls[0]
    assert "not_gated" not in stub.calls[0]


def test_operator_chosen_approval_mode_is_passed_and_recorded(
    monkeypatch, tmp_path, bundles_root, success_report
):
    report = dict(success_report, approval={"mode": "not_gated", "decision": "not_gated"})
    stub = FlowCliStub(exit_code=0, report=report)
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        timeout_s=5.0,
        approval_mode="not_gated",
    )
    bridge = _bridge(bundles_root, config)
    result = _run(bridge)
    cmd = stub.calls[0]
    assert cmd[cmd.index("--approval-mode") + 1] == "not_gated"
    assert result["outcome"] == "done"
    assert result["technical"]["approval_mode"] == "not_gated"
    # The choice is kept in this server's own run record too.
    assert bridge.dispatch("get_run", {"run_id": result["run_id"]})["technical"][
        "approval_mode"
    ] == "not_gated"


# -- while the run waits --------------------------------------------------------


def test_run_waiting_for_approval_is_needs_review_with_what_to_do(
    monkeypatch, bundles_root, runner_config, success_report
):
    answered = threading.Event()
    stub = FlowCliStub(exit_code=0, report=success_report)

    def flow_waiting_for_a_person(cmd, *args, **kwargs):
        run_dir = Path(cmd[cmd.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        request_id = _write_request(run_dir)
        assert answered.wait(10)
        (run_dir / "item-approval" / f"answer-{request_id}.json").write_text("{}")
        return stub(cmd, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "_run_flow_process", flow_waiting_for_a_person)
    bridge = _bridge(bundles_root, runner_config)
    started = time.monotonic()
    waiting = _run(bridge, wait=30)
    assert time.monotonic() - started < 10
    assert waiting["outcome"] == "needs_review"
    assert waiting["reason"] == "waiting_for_approval"
    assert waiting["safe_to_retry"] is False
    assert waiting["next_step"] == "wait_for_person"
    # An earlier save in the same run may have been made: never "no".
    assert waiting["record_changed"] == "unknown"
    assert "approve" in waiting["next_action"]
    assert "item-approval" in waiting["next_action"]
    assert "Nothing was written" not in waiting["what_happened"]
    import jsonschema

    jsonschema.validate(waiting, RUN_RESULT_SCHEMA)
    again = bridge.dispatch("get_run", {"run_id": waiting["run_id"], "wait_seconds": 0})
    assert again["reason"] == "waiting_for_approval"

    answered.set()
    done = bridge.dispatch("get_run", {"run_id": waiting["run_id"], "wait_seconds": 10})
    assert done["outcome"] == "done"


def test_agent_timeout_does_not_count_time_a_person_spends_approving():
    sleeper = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    completed = runner_mod._run_flow_process(
        sleeper,
        capture_output=True,
        text=True,
        timeout=0.5,
        awaiting_person=lambda: True,
    )
    assert completed.returncode == 0
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runner_mod._run_flow_process(
            sleeper,
            capture_output=True,
            text=True,
            timeout=0.5,
            awaiting_person=lambda: False,
        )
    assert time.monotonic() - started < 1.4


# -- after Flow stops waiting -----------------------------------------------------


def test_flow_giving_up_on_an_approval_is_needs_review_not_a_failure(
    monkeypatch, bundles_root, runner_config
):
    stub = FlowCliStub(exit_code=1, report=_approval_report("HALTED_BEFORE_EFFECT"))
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = _bridge(bundles_root, runner_config)
    pause = {"id": "a" * 24, "status": "pending", "durably_paused": True,
             "category": "operator_review"}
    monkeypatch.setattr(bridge.attended, "for_run_dir", lambda run_dir: pause)
    result = _run(bridge)
    assert result["outcome"] == "needs_review"
    assert result["reason"] == "approval_needed"
    assert result["record_changed"] == "no"
    assert result["safe_to_retry"] is False
    assert "approval" in result["what_happened"]
    assert "didn't match" not in result["what_happened"]
    assert result["needs_attention_id"] == "a" * 24
    assert result["technical"]["approval_mode"] == "each_item"


def test_an_approval_pause_never_weakens_an_uncertain_save():
    report = _approval_report("RECONCILIATION_REQUIRED")
    assert reason_for_run(exit_code=1, report=report) == "save_not_confirmed"


def test_a_declined_save_reads_as_ended_by_a_person():
    report = _approval_report("HALTED_BEFORE_EFFECT", decision="declined")
    rejected = {"status": "rejected", "durably_paused": False, "category": "operator_review"}
    assert reason_for_run(exit_code=1, report=report, attention=rejected) == "stopped_by_person"


def test_approval_reasons_never_invite_a_retry():
    for reason in ("waiting_for_approval", "approval_needed"):
        spec = REASONS[reason]
        assert spec.outcome == "needs_review"
        assert spec.safe_to_retry is False
        assert spec.next_step == "wait_for_person"
    assert REASONS["waiting_for_approval"].record_changed == "unknown"


# -- server options -----------------------------------------------------------------


def test_serve_records_an_operator_chosen_approval_mode(
    monkeypatch, bundles_root, tmp_path, capsys
):
    from contextlib import contextmanager

    from openadapt_agent.cli import main

    @contextmanager
    def fake_attended(**kwargs):
        yield None

    captured = {}
    monkeypatch.setattr("openadapt_agent.flow_service.open_attended_service", fake_attended)
    monkeypatch.setattr("openadapt_agent.mcp.serve", lambda bridge: captured.update(b=bridge))
    monkeypatch.setattr("openadapt_agent.approval.flow_supports_approval_modes", lambda: True)
    argv = ["serve", "--mode", "production", "--bundles", str(bundles_root),
            "--runs-dir", str(tmp_path / "runs")]
    assert main([*argv, "--approval-mode", "not_gated"]) == 0
    assert captured["b"].runner_config.approval_mode == "not_gated"
    assert "approval mode not_gated" in capsys.readouterr().err
    assert main(argv) == 0
    assert captured["b"].runner_config.approval_mode is None

    monkeypatch.setattr("openadapt_agent.approval.flow_supports_approval_modes", lambda: False)
    assert main([*argv, "--approval-mode", "not_gated"]) == 2
    assert "approval modes" in capsys.readouterr().err


# -- a real Flow process that waits for a person -----------------------------------
#
# The tests above stub _run_flow_process. These start a real child process
# that writes an approval request the way Flow does, so they also cover how the
# runner and the service find that request in the run folder.

_WAITING_FLOW = """
import json, sys, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

run_dir = Path(sys.argv[sys.argv.index("--run-dir") + 1])
folder = run_dir / "item-approval"
folder.mkdir(parents=True, exist_ok=True)
request_id = uuid.uuid4().hex
expires = datetime.now(timezone.utc) + timedelta(seconds=600)
(folder / f"request-{request_id}.json").write_text(
    json.dumps({"request_id": request_id, "expires_at": expires.isoformat()})
)
time.sleep(float(sys.argv[1]))
(folder / f"answer-{request_id}.json").write_text("{}")
(folder / f"consumed-{request_id}").write_text("consumed\\n")
(run_dir / "report.json").write_text(json.dumps({
    "workflow_name": "Demo Triage",
    "success": True,
    "execution_outcome": "VERIFIED",
    "transaction_outcome": "VERIFIED",
    "execution_profile": "standard",
    "production_eligible": True,
    "results": [],
    "model_calls": 0,
}))
"""


def _waiting_flow_config(tmp_path, *, person_seconds: float, timeout_s: float) -> RunnerConfig:
    script = tmp_path / "waiting_flow.py"
    script.write_text(_WAITING_FLOW)
    return RunnerConfig(
        flow_cli=(sys.executable, str(script), str(person_seconds)),
        runs_dir=tmp_path / "runs",
        timeout_s=timeout_s,
    )


def test_a_real_flow_process_waiting_for_a_person_is_not_killed(tmp_path, bundle_dir):
    config = _waiting_flow_config(tmp_path, person_seconds=3.0, timeout_s=1.5)
    outcome = FlowRunner(config).run(workflow="w", bundle_dir=bundle_dir, params={})
    assert outcome.timed_out is False
    assert outcome.status == "success"


def test_run_workflow_reports_a_real_flow_process_waiting_for_a_person(
    tmp_path, bundles_root
):
    config = _waiting_flow_config(tmp_path, person_seconds=3.0, timeout_s=30.0)
    bridge = _bridge(bundles_root, config)
    waiting = _run(bridge, wait=20)
    assert waiting["outcome"] == "needs_review"
    assert waiting["reason"] == "waiting_for_approval"
    assert waiting["record_changed"] == "unknown"
    done = bridge.dispatch("get_run", {"run_id": waiting["run_id"], "wait_seconds": 20})
    assert done["outcome"] == "done"
