"""Repair as the default response to a stop: routing, the ledger, the funnel.

The CLI end of it (on by default, ``--no-auto-repair``, the fallback when a
run can't be repaired) is in ``tests/test_auto_repair_cli.py``. No browser.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.ir import SelfRepairEvidence, SelfRepairStepRecord
from openadapt_flow.repair.auto_repair import (
    AutoRepair,
    RepairLedger,
    StopRow,
    ledger_row,
    route_stop,
    summarize,
)


def _report(
    *,
    success: bool = False,
    outcome: str | None = "HALTED_BEFORE_EFFECT",
    halted_at: str | None = "step_005",
) -> Any:
    return SimpleNamespace(
        success=success,
        transaction_outcome=outcome,
        execution_outcome="VERIFIED" if success else "HALTED",
        halt=SimpleNamespace(state_id=halted_at) if halted_at else None,
        workflow_name="triage",
    )


@pytest.mark.parametrize(
    ("report", "route"),
    [
        (_report(success=True, outcome=None, halted_at=None), "none"),
        (_report(), "repair"),
        (_report(outcome=None), "repair"),
        (_report(outcome="RECONCILIATION_REQUIRED"), "reconciliation"),
        (_report(outcome="FAILED_AFTER_EFFECT"), "person"),
        (_report(halted_at=None), "person"),
    ],
)
def test_only_a_stop_that_wrote_nothing_goes_to_repair(report: Any, route: str) -> None:
    assert route_stop(report)[0] == route


def _evidence(status: str, *, probe: bool, approved_by: str | None = None) -> Any:
    steps = [
        SelfRepairStepRecord(stage="halt", passed=True, detail="x"),
        SelfRepairStepRecord(stage="propose", passed=True, detail="x", attempt=1),
        SelfRepairStepRecord(stage="probe", passed=probe, detail="x", attempt=1),
    ]
    if probe:
        steps.append(SelfRepairStepRecord(stage="campaign", passed=True, detail="x"))
    return SelfRepairEvidence(
        status=status,  # type: ignore[arg-type]
        halted_step_id="step_005",
        attempts=1,
        model_calls=1,
        est_model_cost_usd=0.01,
        approved_by=approved_by,
        steps=steps,
    )


def _result(status: str, evidence: Any, outcome: str = "VERIFIED") -> Any:
    return SimpleNamespace(
        status=status,
        evidence=evidence,
        report=SimpleNamespace(execution_outcome=outcome),
        halted_report=_report(),
        approval_request_id=None,
    )


def test_a_resumed_repair_fills_the_whole_funnel(tmp_path: Path) -> None:
    result = _result(
        "resumed",
        _evidence("resumed", probe=True, approved_by="decision_portal:person"),
    )
    row = ledger_row(_report(), tmp_path, result=result)
    assert row.route == "repair"
    assert row.proposed == 1 and row.probe_passed and row.cases_passed
    assert row.approved_via == "person" and row.person_decisions == 1
    assert row.resumed and row.resumed_outcome == "VERIFIED"
    summary = summarize([row])
    assert summary["stops"] == 1 and summary["resumed_items"] == 1
    assert summary["repair_success_rate"] == 1.0


def test_failures_count_against_the_rate(tmp_path: Path) -> None:
    rows = [
        ledger_row(
            _report(),
            tmp_path,
            result=_result(
                "resumed",
                _evidence("resumed", probe=True, approved_by="decision_portal:person"),
            ),
        ),
        ledger_row(
            _report(),
            tmp_path,
            result=_result("no_fix", _evidence("no_fix", probe=False)),
        ),
        ledger_row(_report(outcome="RECONCILIATION_REQUIRED"), tmp_path),
    ]
    summary = summarize(rows)
    assert summary["stops"] == 3
    assert summary["to_repair"] == 2
    assert summary["to_reconciliation"] == 1
    assert summary["repairs_proposed"] == 2
    assert summary["probe_passed"] == 1
    assert summary["approved"] == 1
    assert summary["resumed_items"] == 1
    assert summary["repair_success_rate"] == 0.5
    assert summary["repair_status_counts"] == {"no_fix": 1, "resumed": 1}


def test_worklist_rows_count_items_as_stops_and_test_record_runs_as_repairs(
    tmp_path: Path,
) -> None:
    repair = ledger_row(
        _report(),
        tmp_path,
        result=_result(
            "resumed",
            _evidence("resumed", probe=True, approved_by="decision_portal:person"),
        ),
    )
    repair.scope = "test_record"
    items = []
    for _ in range(3):
        row = ledger_row(_report(), tmp_path)
        row.repair_status, row.resumed, row.resumed_outcome = (
            "released",
            True,
            "VERIFIED",
        )
        items.append(row)
    summary = summarize([repair, *items])
    assert summary["stops"] == 3
    assert summary["repair_runs"] == 1
    assert summary["approved_by_person"] == 1
    assert summary["person_decisions"] == 1
    assert summary["resumed_items"] == 3
    assert summary["repair_success_rate"] == 1.0


class _FakeRepair:
    def __init__(self, root: Path, result: Any) -> None:
        self.store = SimpleNamespace(root=root)
        self._result = result

    def run(self, bundle_dir: Any, run_dir: Any) -> Any:
        return self._result


def test_auto_repair_logs_each_stop_and_skips_healthy_runs(tmp_path: Path) -> None:
    healthy = _result("not_needed", SelfRepairEvidence(status="not_needed"))
    AutoRepair(_FakeRepair(tmp_path, healthy)).run("b", tmp_path / "r0")
    assert RepairLedger(tmp_path).rows() == []

    wrong_record = _result(
        "not_repairable",
        SelfRepairEvidence(
            status="not_repairable", reason="a wrong-record halt is never repaired"
        ),
    )
    AutoRepair(_FakeRepair(tmp_path, wrong_record), labels={"drift": "x"}).run(
        "b", tmp_path / "r1"
    )
    wrote = _result("not_repairable", SelfRepairEvidence(status="not_repairable"))
    wrote.halted_report = _report(outcome="RECONCILIATION_REQUIRED")
    AutoRepair(_FakeRepair(tmp_path, wrote)).run("b", tmp_path / "r2")
    rows = RepairLedger(tmp_path).rows()
    assert [r["route"] for r in rows] == ["person", "reconciliation"]
    assert rows[0]["labels"] == {"drift": "x"}
    assert "undo list" in rows[1]["route_reason"]
    assert StopRow(**rows[0]).route == "person"
