"""Adversarial checks: a readiness drill never saves, whatever the caller does.

A drill passes only when the engine's own detector refuses the fault's
changed input before delivery. These tests attack the other direction: a
drill whose fault isn't staged, or isn't caught, must still save nothing, in
every approval mode and on the API path, and must not pass.

The fault driver, backend and verifier are the fault-contract suite's fakes.
All values are made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.ir import ApiBinding
from openadapt_flow.qualification import QualificationCaseKind
from openadapt_flow.qualification_faults import fault_detector_contract_error
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.actuators.api import ActuationStatus, ApiActuationResult
from tests.test_qualification_fault_contract import (
    _fault_authorization,
    _fault_workflow,
    _FaultDriver,
    _ObservedBackend,
    _StrongEffectVerifier,
    _Vision,
)


class _DeliveringApiActuator:
    """An API that would accept the write: any call is a delivered save."""

    def __init__(self) -> None:
        self.calls = 0

    def actuate(self, _binding: ApiBinding, _params: dict[str, str]):
        self.calls += 1
        return ApiActuationResult(
            status=ActuationStatus.ACTUATED,
            reason="fixture accepted the write",
            http_status=201,
        )


def _drill(
    tmp_path: Path,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    actuation_path: str = "gui",
    approval_gate: Any = None,
    api_actuator: Any = None,
):
    workflow, bundle = _fault_workflow(
        tmp_path,
        kind,
        driver,
        api_effect_only=actuation_path == "api",
        actuation_path=actuation_path,
    )
    run_id = f"adversarial-{kind.value}-{actuation_path}"
    authorization = _fault_authorization(
        workflow, kind, driver, run_id=run_id, actuation_path=actuation_path
    )
    if approval_gate is not None:
        authorization = authorization.model_copy(
            update={"approval_mode": approval_gate.mode.value}
        )
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=authorization,
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        api_actuator=api_actuator,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
        approval_gate=approval_gate,
    ).run(
        workflow,
        # Every input supplied, as run-case supplies the case's inputs: the
        # approval gate refuses an input that would come from the recording.
        params=dict(workflow.params),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
        run_id=run_id,
        execution_target_kind="web",
    )
    return report, backend


def _not_passed(report) -> None:
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome != "VERIFIED"
    for receipt in report.qualification_fault_mutations:
        assert fault_detector_contract_error(report, receipt) is not None


@pytest.mark.parametrize("mode", [None, "not_gated", "each_item", "problems_only"])
def test_a_drill_that_isnt_staged_saves_nothing_in_any_mode(tmp_path, mode):
    """The driver declines (it can't stage the fault here)."""

    driver = _FaultDriver(QualificationCaseKind.WRONG_IDENTITY, decline=True)
    gate: Optional[ApprovalGate] = None
    if mode is not None:
        gate = ApprovalGate(
            mode,
            approval_store=ItemApprovalStore(tmp_path / "run"),
            approval_wait_s=0,
        )
    report, backend = _drill(
        tmp_path, QualificationCaseKind.WRONG_IDENTITY, driver, approval_gate=gate
    )

    assert backend.actions == [], "a drill never delivers its fault step"
    assert report.qualification_fault_mutations == []
    _not_passed(report)
    # Nobody was asked to approve a drill's save.
    assert ItemApprovalStore(tmp_path / "run").requests() == []


def test_an_api_drill_that_isnt_staged_never_calls_the_api(tmp_path):
    """Before, an API save at a drill's fault step was delivered when the
    driver declined: only the screen path refused at the input boundary."""

    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT, decline=True)
    api = _DeliveringApiActuator()
    report, backend = _drill(
        tmp_path,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        actuation_path="api",
        api_actuator=api,
    )

    assert api.calls == 0
    assert backend.actions == []
    assert "did not produce its required detector refusal" in (
        report.results[0].error or ""
    )
    _not_passed(report)


def test_an_api_drill_whose_fault_isnt_caught_never_calls_the_api(tmp_path):
    """The driver changes the verifier, but in a way no detector refuses."""

    driver = _FaultDriver(
        QualificationCaseKind.WEAK_EFFECT, non_refusing_effect_mutation=True
    )
    api = _DeliveringApiActuator()
    report, _backend = _drill(
        tmp_path,
        QualificationCaseKind.WEAK_EFFECT,
        driver,
        actuation_path="api",
        api_actuator=api,
        approval_gate=ApprovalGate("not_gated"),
    )

    assert api.calls == 0
    assert len(report.qualification_fault_mutations) == 1
    assert (
        fault_detector_contract_error(report, report.qualification_fault_mutations[0])
        == "fault_detector_refusal_not_observed"
    )
    _not_passed(report)


@pytest.mark.parametrize(
    "kind",
    [
        QualificationCaseKind.AMBIGUITY,
        QualificationCaseKind.WRONG_IDENTITY,
        QualificationCaseKind.STALE_IDENTITY,
        QualificationCaseKind.WEAK_EFFECT,
        QualificationCaseKind.MISSING_EFFECT,
    ],
)
def test_a_staged_drill_still_passes_under_each_item_and_asks_nobody(tmp_path, kind):
    """The approval gate doesn't get in the detector's way, or ask a person."""

    driver = _FaultDriver(kind)
    store = ItemApprovalStore(tmp_path / "run")
    report, backend = _drill(
        tmp_path,
        kind,
        driver,
        approval_gate=ApprovalGate("each_item", approval_store=store),
    )

    assert backend.actions == []
    (receipt,) = report.qualification_fault_mutations
    assert fault_detector_contract_error(report, receipt) is None
    assert store.requests() == []
