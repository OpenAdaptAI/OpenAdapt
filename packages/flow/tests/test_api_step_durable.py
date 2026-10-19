"""An API step in a durable run (docs/API_TIER.md, "In a durable run").

- the checkpoint before the span is the last step before it; the span's
  steps are checkpointed together after the save was sent;
- a save that may have landed is never sent again by resume, and never
  redone through the GUI: an ordinary resume is refused, a resume that
  authorizes one uncertain retry still stops, and the attended reconcile
  action reads the record back and finishes the run without sending;
- a save that wasn't sent leaves the resume point before the span, so a
  resume runs the API step again from the start.

The system of record is the MockMed fault server; the values are made up.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from openadapt_flow.compiler.api_proposal import admit_api_step
from openadapt_flow.ir import WebFormField, Workflow
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.actuators import (
    ActuationStatus,
    ApiActuationResult,
    ApiHaltKind,
)
from openadapt_flow.runtime.durable import (
    ApprovalRequired,
    issue_resume_approval,
    resume,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRequest,
    AttendedActionStore,
    BoundAttendedExecutor,
    execute_attended_action,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.durable.program_checkpoint import bundle_version
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests.test_api_tier_web_form import (
    TARGET,
    EnterWritesBackend,
    ScriptedActuator,
    _span_workflow,
)
from tests.test_execution_profiles import _sealed
from tests.test_replayer import FakeVision


class SentThenLost(ScriptedActuator):
    """The save lands in the system of record; its response is lost."""

    def actuate(self, binding, params):
        self.calls += 1
        requests.post(
            f"{self.sor_url}/api/encounter",
            json={**TARGET, "note": "api"},
            timeout=5,
        )
        return ApiActuationResult(
            status=ActuationStatus.HALT,
            halt_kind=ApiHaltKind.DELIVERY_UNCERTAIN,
            reason="the save request failed after it was sent; it is not retried",
        )


class AcceptedButNothingSaved(ScriptedActuator):
    """The server answers success, but the record check finds nothing."""

    def actuate(self, binding, params):
        self.calls += 1
        return ApiActuationResult(
            status=ActuationStatus.ACTUATED, reason="POST /save.php -> 200"
        )


def _absolute_check_workflow() -> Workflow:
    """The span program with a record check that needs no before-snapshot.

    The attended reconcile action reads the record as it is now, so it can
    only prove a check that doesn't compare with the state before the save.
    """

    wf = _span_workflow(admitted=False)
    save = wf.steps[-1]
    effect = save.effects[0].model_copy(update={"forbid_collateral_loss": False})
    binding = save.api_binding.model_copy(update={"effects": [effect]})
    save = save.model_copy(update={"effects": [effect], "api_binding": binding})
    wf = wf.model_copy(update={"steps": [*wf.steps[:-1], save]})
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    return wf


class SecondReadFails(RestRecordVerifier):
    """Reads the record check's pre-state once, then can't reach it."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.reads = 0

    def capture_pre_state(self, context: Any = None):
        self.reads += 1
        if self.reads > 1:
            raise ConnectionError("system of record unreachable")
        return super().capture_pre_state(context)


class Fixture:
    def __init__(self, tmp_path, workflow: Workflow | None = None) -> None:
        self.url, self.db, self.stop = fault_serve()
        self.url = self.url.rstrip("/")
        self.workflow, self.bundle = _sealed(
            tmp_path, workflow or _span_workflow(), encrypted=False
        )
        self.run_dir = tmp_path / "run"

    def replayer(
        self, actuator: Any, *, verifier_url: str | None = None, verifier: Any = None
    ) -> Replayer:
        self.backend = EnterWritesBackend(self.url)
        return Replayer(
            self.backend,
            vision=FakeVision(),
            effect_verifier=verifier or RestRecordVerifier(verifier_url or self.url),
            web_form_actuator=actuator,
            poll_interval_s=0.01,
            durable=True,
        )

    def run(self, actuator: Any, **kw: Any):
        return self.replayer(actuator, **kw).run(
            self.workflow, bundle_dir=self.bundle, run_dir=self.run_dir
        )

    def store(self) -> CheckpointStore:
        return CheckpointStore(self.run_dir)

    def records(self) -> list[dict[str, Any]]:
        return self.db.snapshot()["records"]

    def approval(self, **extra: Any):
        store = self.store()
        manifest, pending = store.read_manifest(), store.read_pending()
        assert manifest is not None and pending is not None
        return issue_resume_approval(
            pending,
            approver="operator@example.com",
            resolution="checked the record",
            bundle_version=bundle_version(self.bundle),
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            run_dir=self.run_dir,
            **extra,
        )


@pytest.fixture
def fx(tmp_path):
    fixture = Fixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.stop()


def test_a_durable_run_uses_the_api_step_and_checkpoints_after_it(fx):
    actuator = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    report = fx.run(actuator)

    assert report.success is True, [r.error for r in report.results]
    assert actuator.calls == 1 and fx.backend.actions == []
    checkpoints = fx.store().checkpoints()
    assert [c.actuation for c in checkpoints] == ["api_folded", "api"]
    assert [c.step_index for c in checkpoints] == [0, 1]
    assert checkpoints[-1].effect_verified is True
    assert [r["note"] for r in fx.records()] == ["api"]


def test_an_uncertain_send_is_never_resent_by_resume(fx):
    first = SentThenLost(fx.url, ActuationStatus.HALT)
    report = fx.run(first)

    assert first.calls == 1 and fx.backend.actions == []
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    store = fx.store()
    # The folded step is checkpointed with the save: the pause is at the save.
    assert [c.actuation for c in store.checkpoints()] == ["api_folded"]
    pending = store.read_pending()
    assert pending.step_id == "step_001" and pending.resume_from_index == 1
    assert pending.delivery_uncertainty is not None

    # An ordinary resume can't repeat a save that may have landed.
    second = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    with pytest.raises(ApprovalRequired, match="may already have actuated"):
        resume(fx.run_dir, fx.replayer(second), approval=fx.approval())
    assert second.calls == 0

    # Even a resume that authorizes one uncertain retry doesn't resend an API
    # save or redo it through the GUI on an empty form.
    resumed = resume(
        fx.run_dir,
        fx.replayer(second),
        approval=fx.approval(authorize_uncertain_retry=True),
    )
    assert second.calls == 0 and fx.backend.actions == []
    assert resumed.success is False
    assert "never sends it again" in resumed.results[-1].error
    assert [r["note"] for r in fx.records()] == ["api"]


def test_reconcile_reads_the_record_back_and_finishes_without_sending(tmp_path):
    fx = Fixture(tmp_path, _absolute_check_workflow())
    try:
        _reconcile(fx)
    finally:
        fx.stop()


def _reconcile(fx: Fixture) -> None:
    first = SentThenLost(fx.url, ActuationStatus.HALT)
    fx.run(first)
    capability = AttendedActionStore(fx.run_dir).read()
    assert capability is not None and "reconcile" in capability.allowed_actions

    second = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    decision = execute_attended_action(
        fx.run_dir,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="reconcile-api-step-001",
            action="reconcile",
            disposition="reconciliation_requested",
        ),
        operator="staff",
        decided_by="human",
        executor=BoundAttendedExecutor(lambda _manifest: fx.replayer(second)),
    )

    assert decision.status == "completed", decision.message
    assert decision.report_success is True
    assert second.calls == 0 and fx.backend.actions == []
    assert [r["note"] for r in fx.records()] == ["api"]


def test_a_save_the_record_check_refutes_is_not_redone_by_resume(fx):
    first = AcceptedButNothingSaved(fx.url, ActuationStatus.ACTUATED)
    report = fx.run(first)
    assert report.success is False and fx.records() == []
    assert fx.store().read_pending().resume_from_index == 1

    second = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    resumed = resume(fx.run_dir, fx.replayer(second), approval=fx.approval())
    assert second.calls == 0 and fx.backend.actions == []
    assert resumed.success is False
    assert "never sends it again" in resumed.results[-1].error


def test_a_save_that_wasnt_sent_resumes_the_whole_api_step(fx):
    # The record check's pre-state can't be read, so nothing is sent.
    first = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    report = fx.run(first, verifier_url="http://127.0.0.1:9")
    assert first.calls == 0 and report.success is False
    store = fx.store()
    assert store.checkpoints() == []
    assert store.read_pending().resume_from_index == 0

    second = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    resumed = resume(fx.run_dir, fx.replayer(second), approval=fx.approval())
    assert resumed.success is True, [r.error for r in resumed.results]
    assert second.calls == 1 and fx.backend.actions == []
    assert [c.actuation for c in fx.store().checkpoints()] == ["api_folded", "api"]
    assert [r["note"] for r in fx.records()] == ["api"]


def test_a_gui_fallback_that_halted_at_the_save_resumes_on_the_gui(fx):
    # The API step is unavailable, so the GUI steps run; the GUI save then
    # can't read its record check's pre-state and stops before pressing.
    first = ScriptedActuator(fx.url, ActuationStatus.UNAVAILABLE)
    report = fx.run(first, verifier=SecondReadFails(fx.url))
    assert "did not pass" in report.results[0].api_fallback
    assert report.success is False and fx.records() == []
    checkpoints = fx.store().checkpoints()
    assert [c.step_id for c in checkpoints] == ["step_000"]
    assert checkpoints[0].actuation != "api_folded"

    # The steps before the save ran on the GUI, so the GUI save resumes.
    second = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
    resumed = resume(fx.run_dir, fx.replayer(second), approval=fx.approval())
    assert resumed.success is True, [r.error for r in resumed.results]
    assert second.calls == 0
    assert ("press", "Enter") in [(a[0], a[1]) for a in fx.backend.actions]
    assert [r["note"] for r in fx.records()] == ["gui"]


def test_an_identity_checked_save_with_a_right_record_contract_runs_durably(tmp_path):
    # The referral's case: the save checks the right record on screen and the
    # API step was admitted with --identity. The API step checks the request
    # and the record check select the same record, so the durable checkpoint
    # has its identity evidence and the run never fails after the save.
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    wf = _span_workflow(admitted=False)
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={**TARGET, "note": ValueExpr(param="note")},
        expected_count=1,
        timeout_s=2.0,
    )
    save = wf.steps[-1]
    spec = save.api_binding.web_form.model_copy(
        update={
            "fields": [
                *save.api_binding.web_form.fields,
                WebFormField(name="form_note", source="param", param="note"),
            ]
        }
    )
    binding = save.api_binding.model_copy(
        update={"effects": [effect], "web_form": spec}
    )
    save = save.model_copy(
        update={"identity_armed": True, "effects": [effect], "api_binding": binding}
    )
    wf = wf.model_copy(
        update={"steps": [*wf.steps[:-1], save], "params": {**wf.params, "note": "api"}}
    )
    wf, _ = admit_api_step(
        wf, admitted_by="reviewer", identity={"subject_name": "note"}
    )
    fx = Fixture(tmp_path, wf)
    try:
        actuator = ScriptedActuator(fx.url, ActuationStatus.ACTUATED)
        report = fx.run(actuator)
        assert report.success is True, [r.error for r in report.results]
        assert actuator.calls == 1
        save_result = report.results[-1]
        assert save_result.actuation == "api"
        assert save_result.identity is not None
        assert save_result.identity.status == "verified"
        assert [c.actuation for c in fx.store().checkpoints()] == ["api_folded", "api"]
    finally:
        fx.stop()
