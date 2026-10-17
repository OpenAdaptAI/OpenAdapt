"""REPORT.md states the transaction outcome and warns when a write may exist.

The coarse headline (HALTED, FAILED) is the same for "stopped before saving"
and "a save may have gone through". The report must say which one happened,
and for RECONCILIATION_REQUIRED tell the reader not to retry before the
record is checked.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.ir import ActionDeliveryUncertainty, RunReport, StepResult
from openadapt_flow.report import render_run_report


def _render(
    tmp_path: Path,
    *,
    transaction_outcome: str | None,
    uncertain_step: bool = True,
) -> str:
    results = [
        StepResult(step_id="step_open", intent="click 'Open'", ok=True),
        StepResult(
            step_id="step_submit",
            intent="click 'Submit order'",
            ok=False,
            risk="irreversible",
            delivery_uncertainty=(
                ActionDeliveryUncertainty(
                    operation="click",
                    native=False,
                    observed_at="2026-10-10T00:00:00+00:00",
                    cause_type="TimeoutError",
                )
                if uncertain_step
                else None
            ),
            error="TimeoutError after the click",
        ),
    ]
    report = RunReport(
        workflow_name="submit-order",
        started_at="2026-10-10T00:00:00+00:00",
        execution_outcome="HALTED",
        transaction_outcome=transaction_outcome,
        results=results,
        success=False,
    )
    run_dir = tmp_path / "run"
    report.save(run_dir)
    return render_run_report(run_dir).read_text(encoding="utf-8")


def test_reconciliation_required_is_stated_with_a_do_not_retry_warning(
    tmp_path: Path,
) -> None:
    md = _render(tmp_path, transaction_outcome="RECONCILIATION_REQUIRED")
    head = md.split("## ", 1)[0]

    assert "RECONCILIATION_REQUIRED" in md.splitlines()[0]
    assert "- **Transaction outcome:** `RECONCILIATION_REQUIRED`" in head
    assert "may have landed" in head
    assert "Don't re-run or retry" in head
    assert "`step_submit`" in head
    assert "`step_open`" not in head
    assert "nothing was written" not in md.lower()


def test_halted_before_effect_says_nothing_was_written(tmp_path: Path) -> None:
    md = _render(
        tmp_path, transaction_outcome="HALTED_BEFORE_EFFECT", uncertain_step=False
    )
    head = md.split("## ", 1)[0]

    assert "- **Transaction outcome:** `HALTED_BEFORE_EFFECT`" in head
    assert "nothing was written" in head
    assert "may have landed" not in md
    assert "re-run or retry" not in md


def test_legacy_report_without_transaction_outcome_still_renders(
    tmp_path: Path,
) -> None:
    md = _render(tmp_path, transaction_outcome=None, uncertain_step=False)

    assert md.splitlines()[0].startswith("# ❌ submit-order — HALTED")
    assert "Transaction outcome" not in md
    assert "may have landed" not in md
