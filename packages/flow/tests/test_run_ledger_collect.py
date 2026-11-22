"""Collectors read what real producers write.

The end-to-end tests drive the real commit guard: a scripted agent (no model)
saves a note in MockMed's fault server on localhost, the guard checks the
record on screen and reads the save back through verify-run, and the run's
files are then read into a ledger row. No browser, no network beyond
localhost, synthetic data only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmark.harness.guard_demo import NOTE, FakeAgent, person_approves, verify_yaml
from openadapt_flow.agent_guard.commit_guard import CommitGuard
from openadapt_flow.guard import GuardedSave, GuardTask
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.run_ledger.collect import (
    collect,
    collect_compile_agent,
    collect_run_receipt,
)
from openadapt_flow.run_ledger.store import build_row
from openadapt_flow.verify_run import parse_verify_config

FIXTURES = Path(__file__).parent / "fixtures" / "run_ledger"


class _Page:
    """The scripted agent's tab: what the guard lets through reaches MockMed."""

    def __init__(self, agent: FakeAgent, patient: str, fault: str = "") -> None:
        self.agent, self.patient, self.fault = agent, patient, fault
        self.pending: dict[str, Any] = {}
        self.guard: Any = None

    def screen_text(self) -> str:
        return self.agent.open_chart(self.patient)

    def screenshot(self) -> bytes:
        from benchmark.harness.guard_demo import tiny_png

        return tiny_png()

    def continue_request(self, request_id: str) -> None:
        held = self.pending.pop(request_id, None)
        if held is not None and held.method == "POST":
            self.agent.click_save(self.patient, NOTE, fault=self.fault)
            self.guard.on_request_done(held.network_id)

    def block_request(self, request_id: str, **_: Any) -> None:
        self.pending.pop(request_id, None)

    def notify(self, message: str, *, level: str) -> None:
        pass

    def clear_notice(self) -> None:
        pass

    def save(self, rid: str = "r1") -> None:
        from openadapt_flow.agent_guard.requests import HeldRequest

        held = HeldRequest(
            request_id=rid,
            method="POST",
            url=self.agent.base + "/api/encounter",
            resource_type="Fetch",
            post_data=json.dumps({"note": NOTE}),
            content_type="application/json",
            network_id="n-" + rid,
        )
        self.pending[rid] = held
        self.guard.on_request(held)


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _guarded_run(
    sor: str,
    run: Path,
    *,
    patient: str = "p1",
    fault: str = "",
    approval: str = "not_gated",
) -> tuple[CommitGuard, _Page]:
    run.mkdir(parents=True, exist_ok=True)
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(sor)), run / "guard", approval=approval
    )
    task = GuardTask(
        task_id="task-ledger-0001",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
        authorization_ref="ticket-0001",
    )
    page = _Page(FakeAgent(sor), patient, fault)
    guard = CommitGuard(
        guarded, task, page, app_origin=sor, events_path=run / "guard_events.jsonl"
    )
    page.guard = guard
    return guard, page


def _finish(guard: CommitGuard, run: Path, ended: str = "read_back") -> None:
    """What agent_guard.session writes when the run ends."""

    summary = guard.summary.to_dict()
    summary.update({"ended": ended, "seconds": 2.5, "recording": None, "notes": []})
    (run / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))


def test_guarded_save_that_reads_back_is_verified(sor, tmp_path):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run)
    page.save()
    guard.poll()
    _finish(guard, run)
    row, _, _ = build_row(run, task_type="intake")
    assert row.outcome.value == "VERIFIED"
    assert row.readback_tier == "independent_readback"
    assert row.task_id == "task-ledger-0001"
    assert row.executor_kind == "agent"
    assert row.duration_s == 2.5
    assert "summary.json" in row.sources and "guard_events.jsonl" in row.sources
    assert any(s.endswith("effect-receipt.json") for s in row.sources)
    # The row carries no record values.
    assert NOTE not in json.dumps(row.to_json())


def test_save_on_another_record_is_caught(sor, tmp_path):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run, fault="wrong_record")
    page.save()
    guard.poll()
    _finish(guard, run)
    row, _, _ = build_row(run)
    assert row.outcome.value in ("CAUGHT_ERROR", "UNKNOWN")
    assert row.outcome.value != "VERIFIED"


def test_wrong_record_on_screen_is_a_blocked_save(sor, tmp_path):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run, patient="p2")
    page.save()
    _finish(guard, run, ended="stopped")
    row, _, _ = build_row(run)
    assert (row.outcome.value, row.outcome_reason) == (
        "CAUGHT_ERROR",
        "save_blocked_wrong_record",
    )


def test_refuted_save_plus_agent_saying_done_is_caught(sor, tmp_path):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run, fault="wrong_record")
    page.save()
    guard.poll()
    _finish(guard, run)
    # The agent's own ledger says it finished ("looks fine").
    (run / "agent_ledger.json").write_text(
        (FIXTURES / "agent_only_finished" / "agent_ledger.json").read_text()
    )
    row, _, _ = build_row(run)
    assert row.outcome.value != "VERIFIED"
    assert row.model.cost_source == "metered"  # the agent's own usage still counts


def test_held_save_waiting_for_a_person_is_escalated_with_unknown_time(
    sor, tmp_path, approver
):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run, approval="each_item")
    page.save()
    guard.poll()
    _finish(guard, run, ended="timeout")
    row, _, _ = build_row(run)
    assert (row.outcome.value, row.outcome_reason) == ("ESCALATED", "approval_held")
    assert [i.kind for i in row.human.intervals] == ["approval"]
    assert row.human.intervals[0].decided_at is None
    assert row.human.intervention_s is None  # unknown, never zero


def test_approved_save_is_verified_and_the_wait_is_counted(sor, tmp_path, approver):
    run = tmp_path / "run"
    guard, page = _guarded_run(sor, run, approval="each_item")
    page.save()
    waiting = guard._waiting
    assert waiting is not None
    g = waiting[0]
    person_approves(
        str(g.dir), g._request.request_id, g._request.digest(), signer=approver[1]
    )
    guard.poll()
    guard.poll()
    _finish(guard, run)
    row, _, _ = build_row(run)
    assert row.outcome.value == "VERIFIED"
    assert row.human.intervention_s is not None and row.human.intervention_s >= 0.0
    assert [i.kind for i in row.human.intervals] == ["approval"]


def test_a_guard_summary_of_another_version_is_unknown(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps({"schema": "openadapt.agent-guard-run/v2", "task_id": "t-0001"})
    )
    row, _, _ = build_row(tmp_path)
    assert (row.outcome.value, row.outcome_reason) == ("UNKNOWN", "schema_unrecognised")


def test_unrelated_summary_json_is_ignored(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"total": 3}))
    assert collect(tmp_path).contributions == []


def test_a_timed_out_guard_with_a_sent_save_is_unknown_not_failed(tmp_path):
    summary = {
        "schema": "openadapt.agent-guard-run/v1",
        "task_id": "task-0001",
        "mode": "fail_closed",
        "approval": "not_gated",
        "verdict": None,
        "blocked": [],
        "would_block": [],
        "saves_held": 1,
        "hold_ms": [12.0],
        "ended": "timeout",
        "seconds": 30.0,
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    row, _, _ = build_row(tmp_path)
    assert (row.outcome.value, row.outcome_reason) == ("UNKNOWN", "write_unconfirmed")
    summary["hold_ms"] = []
    summary["saves_held"] = 0
    # No events file: only the guard's own count of zero saves shows that
    # nothing was sent.
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    row, _, _ = build_row(tmp_path)
    assert (row.outcome.value, row.outcome_reason) == ("UNKNOWN", "write_unconfirmed")
    summary["requests"] = {"save": 0}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    row, _, _ = build_row(tmp_path)
    assert (row.outcome.value, row.outcome_reason) == ("FAILED", "timed_out_no_write")


def test_observe_only_would_block_is_not_a_verified_run(tmp_path):
    summary = {
        "schema": "openadapt.agent-guard-run/v1",
        "task_id": "task-0001",
        "verdict": "VERIFIED",
        "blocked": [],
        "would_block": [{"code": "identity_mismatch", "missing": []}],
        "hold_ms": [1.0],
        "ended": "read_back",
        "seconds": 3.0,
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    row, _, _ = build_row(tmp_path)
    assert row.outcome_reason == "observe_only_would_block"


# -- the other main-branch sources ------------------------------------------------------


def test_compiled_run_report_is_a_real_run_report_shape():
    from openadapt_flow.ir import RunReport

    doc = json.loads((FIXTURES / "compiled_verified" / "report.json").read_text())
    RunReport.model_validate(doc)
    row, private, _ = build_row(FIXTURES / "compiled_verified")
    assert row.executor_kind == "program"
    assert row.duration_s == 1.8
    assert row.started_at == "2026-11-20T09:00:00.000000Z"
    assert row.ended_at == "2026-11-20T09:00:01.800000Z"
    assert row.model.calls == 0 and row.model.cost_usd == 0.0
    # The inputs are committed, never copied; the salt stays private.
    assert row.inputs_commitment and row.inputs_commitment.startswith("sha256:")
    assert "SYN-PATIENT-0042" not in json.dumps(row.to_json())
    assert private.inputs_salt and len(private.inputs_salt) == 64


def test_run_receipt_screen_readback_is_not_verified(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.run-receipt/v2",
                "outcome": "VERIFIED",
                "effect_tier_reached": "independent_session",
                "model_calls": 0,
                "est_cost_usd": 0.0,
                "duration_ms": 900,
                "receipt_digest": "a" * 64,
            }
        )
    )
    contribution = collect_run_receipt(path)
    assert [s.reason for s in contribution.signals] == ["screen_readback_only"]
    assert contribution.receipt_digest == "a" * 64
    path.write_text(json.dumps({"schema_version": "openadapt.run-receipt/v3"}))
    assert [s.reason for s in collect_run_receipt(path).signals] == [
        "schema_unrecognised"
    ]


def test_compile_agent_ledger_never_reads_value_text(tmp_path):
    from openadapt_flow.compile_agent.agents import AgentLedger

    ledger = AgentLedger(agent="scripted", model="synthetic-model")
    ledger.finished = True
    ledger.summary = "Synthetic summary of what was typed"
    (tmp_path / "run1.json").write_text(
        json.dumps({"seconds": 4.0, "agent": ledger.to_dict(), "gates": []})
    )
    contribution = collect_compile_agent(tmp_path / "run1.json")
    assert contribution.usage is not None and contribution.usage.calls == 0
    assert contribution.executor_label == "scripted"
    row, _, _ = build_row(tmp_path)
    assert "Synthetic summary" not in json.dumps(row.to_json())
    assert row.outcome_reason == "completed_unverified"


def test_unknown_usage_is_null_not_zero():
    row, _, _ = build_row(FIXTURES / "agent_only_gave_up")
    assert row.model.calls == 3
    assert row.model.cost_usd is None and row.model.cost_source == "unknown"
    row, _, _ = build_row(FIXTURES / "flight_refused")
    assert row.model.cost_usd is None and row.model.input_tokens == 9000


def test_intervals_from_contract_sources():
    row, _, _ = build_row(FIXTURES / "task_ledger_question")
    assert [i.kind for i in row.human.intervals] == ["question"]
    assert row.human.intervention_s == 120.0


def test_flight_times_and_label():
    row, _, _ = build_row(FIXTURES / "flight_held")
    assert row.executor_label == "browser-use"
    assert row.duration_s == 150.0
    assert row.model.cost_source == "metered" and row.model.cost_usd == 0.231


def test_wrap_record_given_as_one_file():
    path = FIXTURES / "wrap_verified" / "openadapt-agent" / "runs" / "run-0001.json"
    row, _, _ = build_row(path)
    assert row.outcome.value == "VERIFIED"
    assert row.executor_kind == "program"
    assert row.sources == ("run-0001.json",)
    assert row.task_id == "req-0001"
