"""``explain`` reads ``transaction_outcome``, not only the coarse HALTED label.

A halt the engine left at ``RECONCILIATION_REQUIRED`` may have written. Its
explanation must ask a person to check the record and must not suggest
re-running the same command. Only an outcome that proves no business effect
may suggest a re-run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.ir import HaltObservation, RunReport, StepResult
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
)


def _halted_run(
    tmp_path: Path, transaction: Optional[str], *, pending_status: Optional[str]
) -> Path:
    run_dir = tmp_path / "run"
    RunReport(
        workflow_name="local-quickstart",
        started_at="2026-10-09T12:00:00+00:00",
        execution_outcome="HALTED",
        transaction_outcome=transaction,
        execution_profile="standard",
        results=[
            StepResult(
                step_id="step_005",
                intent="click 'Save Encounter'",
                ok=False,
                safety_halt=True,
                effect_verified=False,
                postconditions_ok=True,
                elapsed_ms=50.0,
            )
        ],
        success=False,
        halt=HaltObservation(
            state_id="step_005",
            intent="click 'Save Encounter'",
            reason="record_written refuted against the system of record",
        ),
        model_calls=0,
    ).save(run_dir)
    if pending_status is not None:
        CheckpointStore(run_dir).write_pending(
            PendingEscalation(
                workflow_name="local-quickstart",
                step_index=5,
                step_id="step_005",
                category="effect_escalated",
                proposed_options=["Abort the run and discard the pending escalation"],
                status=pending_status,
            )
        )
    return run_dir


def _explain(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["explain", str(run_dir)]) == 0
    return capsys.readouterr().out


def test_reconciliation_required_asks_for_a_record_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _halted_run(tmp_path, "RECONCILIATION_REQUIRED", pending_status=None)
    out = _explain(run_dir, capsys)
    assert out.splitlines()[0].startswith("Result: Check the record.")
    assert "transaction: RECONCILIATION_REQUIRED" in out
    assert "check the record in the application before anyone runs this" in out
    assert "Don't re-run the same command until the record is checked" in out
    assert "fix the cause, then re-run the same command" not in out
    assert "Nothing was written" not in out


def test_reconciliation_with_a_pending_decision_names_approve_and_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _halted_run(tmp_path, "RECONCILIATION_REQUIRED", pending_status="pending")
    out = _explain(run_dir, capsys)
    assert "A decision is waiting for a person" in out
    assert f"openadapt-flow approve {run_dir}" in out
    assert f"openadapt-flow resume {run_dir}" in out


def test_rejected_pause_is_reported_as_final(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _halted_run(
        tmp_path, "RECONCILIATION_REQUIRED", pending_status="rejected"
    )
    out = _explain(run_dir, capsys)
    assert "A person rejected this run" in out
    assert "openadapt-flow resume" not in out


def test_halted_before_effect_may_suggest_a_re_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _halted_run(tmp_path, "HALTED_BEFORE_EFFECT", pending_status=None)
    out = _explain(run_dir, capsys)
    assert out.splitlines()[0].startswith("Result: Stopped before saving.")
    assert "Nothing was written." in out
    assert "fix the cause, then re-run the same command" in out


def test_legacy_halt_without_transaction_outcome_is_conservative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _halted_run(tmp_path, None, pending_status=None)
    out = _explain(run_dir, capsys)
    assert "transaction: not recorded" in out
    assert out.splitlines()[0].startswith("Result: Check the record.")
    assert "fix the cause, then re-run the same command" not in out


def test_launcher_spelling_in_explain_hints(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _halted_run(tmp_path, "RECONCILIATION_REQUIRED", pending_status="pending")
    monkeypatch.setattr("sys.argv", ["/venv/bin/openadapt", "flow", "explain"])
    out = _explain(run_dir, capsys)
    assert f"openadapt flow approve {run_dir}" in out
