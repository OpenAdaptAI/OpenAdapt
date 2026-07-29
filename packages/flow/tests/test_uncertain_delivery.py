"""Post-dispatch uncertainty verifies exactly once and never blind-retries."""

from __future__ import annotations

from openadapt_flow.backend import ActionDeliveryUncertain
from openadapt_flow.execution_profiles import ExecutionOutcome, ExecutionProfile
from openadapt_flow.ir import ActionKind
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.durable import (
    ApprovalRequired,
    CheckpointStore,
    issue_resume_approval,
    resume,
)
from openadapt_flow.runtime.durable.program_checkpoint import bundle_version
from openadapt_flow.runtime.effects import EffectState, EffectVerdict, Verdict
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_execution_profiles import (
    _gate,
    _key_workflow,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend


class _StoreVerifier:
    substrate = "independent-test-store"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, store: list[dict[str, str]], verdict: Verdict) -> None:
        self.store = store
        self.verdict = verdict
        self.verify_calls = 0

    def capture_pre_state(self):
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=list(self.store),
        )

    def verify(self, effect, before):
        del before
        self.verify_calls += 1
        return EffectVerdict(
            verdict=self.verdict,
            kind=effect.kind,
            substrate=self.substrate,
            reason=f"scripted {self.verdict.value} verdict",
            matched_records=list(self.store),
        )


class _UncertainAfterWriteBackend(FakeBackend):
    def __init__(self, store: list[dict[str, str]]) -> None:
        super().__init__()
        self.store = store
        self.actuation_count = 0

    def act_guarded_coordinate(
        self,
        x,
        y,
        *,
        expected_frame_sha256,
        double=False,
    ):
        # The ordinary guard proves this is an admitted real actuation.  The
        # independent store changes, then the transport/API loses certainty
        # before it can return a delivery receipt.
        super().act_guarded_coordinate(
            x,
            y,
            expected_frame_sha256=expected_frame_sha256,
            double=double,
        )
        self.actuation_count += 1
        self.store.append({"record_id": "synthetic-1"})
        raise ActionDeliveryUncertain(
            operation="guarded_coordinate_click",
            native=False,
            cause_type="ConnectionResetError",
        )


class _UntypedFailureAfterWriteBackend(_UncertainAfterWriteBackend):
    def act_guarded_coordinate(self, *args, **kwargs):
        try:
            return super().act_guarded_coordinate(*args, **kwargs)
        except ActionDeliveryUncertain as exc:
            raise ConnectionResetError("synthetic record data") from exc


def _click_workflow():
    workflow = _key_workflow(
        "uncertain-delivery",
        with_effect=True,
        with_postcondition=True,
    )
    workflow.steps[0].action = ActionKind.CLICK
    workflow.steps[0].key = None
    return workflow


def _run(tmp_path, verdict: Verdict):
    workflow, bundle = _sealed(tmp_path, _click_workflow(), encrypted=False)
    store: list[dict[str, str]] = []
    verifier = _StoreVerifier(store, verdict)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=verifier,
        durable=True,
    )
    assert gate.passed, gate.render()
    authorization = build_runtime_authorization(workflow, gate)
    backend = _UncertainAfterWriteBackend(store)
    vision = _ReadyVision()
    vision.text_results["Saved"] = object()
    report = Replayer(
        backend,
        vision=vision,
        effect_verifier=verifier,
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
    return report, backend, verifier, workflow, bundle


def test_uncertain_delivery_is_verified_only_by_complete_independent_contract(
    tmp_path,
) -> None:
    report, backend, verifier, _workflow, _bundle = _run(tmp_path, Verdict.CONFIRMED)

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    result = report.results[0]
    assert result.ok is True
    assert result.delivery_receipt is None
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.retried is False
    assert result.delivery_uncertainty.postconditions_confirmed is True
    assert result.delivery_uncertainty.effects_confirmed is True
    assert result.delivery_uncertainty.resolved_by_contract is True


def test_untyped_post_dispatch_failure_uses_the_same_effect_reconciliation(
    tmp_path,
) -> None:
    workflow, bundle = _sealed(tmp_path, _click_workflow(), encrypted=False)
    store: list[dict[str, str]] = []
    verifier = _StoreVerifier(store, Verdict.CONFIRMED)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=verifier,
        durable=True,
    )
    backend = _UntypedFailureAfterWriteBackend(store)
    vision = _ReadyVision()
    vision.text_results["Saved"] = object()

    report = Replayer(
        backend,
        vision=vision,
        effect_verifier=verifier,
        governed_authorization=build_runtime_authorization(workflow, gate),
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    result = report.results[0]
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "ConnectionResetError"
    assert "synthetic record data" not in str(report.model_dump(mode="json"))


def test_refuted_uncertain_delivery_halts_without_a_second_actuation(tmp_path) -> None:
    report, backend, verifier, _workflow, _bundle = _run(tmp_path, Verdict.REFUTED)

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    result = report.results[0]
    assert result.ok is False
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.retried is False
    assert result.delivery_uncertainty.effects_confirmed is False
    assert result.delivery_uncertainty.resolved_by_contract is False
    assert "was not retried" in (result.error or "")


def test_indeterminate_uncertain_delivery_halts_without_a_second_actuation(
    tmp_path,
) -> None:
    report, backend, verifier, _workflow, _bundle = _run(
        tmp_path, Verdict.INDETERMINATE
    )

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    result = report.results[0]
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.effects_confirmed is False
    assert result.failure_category == "governed_refusal"


def test_effect_verification_still_runs_when_post_delivery_observation_raises(
    tmp_path,
) -> None:
    workflow, bundle = _sealed(tmp_path, _click_workflow(), encrypted=False)
    store: list[dict[str, str]] = []
    verifier = _StoreVerifier(store, Verdict.CONFIRMED)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=verifier,
        durable=True,
    )
    backend = _UncertainAfterWriteBackend(store)

    class _ObservationBreaksAfterDispatch(_ReadyVision):
        def wait_settled(self, backend, **kwargs):
            del backend, kwargs
            raise RuntimeError("frame context detached")

    report = Replayer(
        backend,
        vision=_ObservationBreaksAfterDispatch(),
        effect_verifier=verifier,
        governed_authorization=build_runtime_authorization(workflow, gate),
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    result = report.results[0]
    assert result.effect_verified is True
    assert result.postconditions_ok is False
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.resolved_by_contract is False


def test_durable_resume_cannot_reenter_uncertain_step_without_explicit_retry(
    tmp_path,
) -> None:
    report, first_backend, _verifier, workflow, bundle = _run(
        tmp_path, Verdict.INDETERMINATE
    )
    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert first_backend.actuation_count == 1

    run_dir = tmp_path / "run"
    second_store: list[dict[str, str]] = []
    second_backend = _UncertainAfterWriteBackend(second_store)
    second_verifier = _StoreVerifier(second_store, Verdict.CONFIRMED)
    resumed_replayer = Replayer(
        second_backend,
        vision=_ReadyVision(),
        effect_verifier=second_verifier,
        governed_authorization=build_runtime_authorization(
            workflow,
            _gate(
                workflow,
                bundle,
                ExecutionProfile.STANDARD,
                verifier=second_verifier,
                durable=True,
            ),
        ),
        durable=True,
        require_settled=True,
    )
    durable_store = CheckpointStore(run_dir)
    manifest = durable_store.read_manifest()
    pending = durable_store.read_pending()
    assert manifest is not None and pending is not None
    ordinary_approval = issue_resume_approval(
        pending,
        approver="operator@example.com",
        resolution="resume after review",
        bundle_version=bundle_version(bundle),
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run_dir,
    )

    try:
        resume(run_dir, resumed_replayer, approval=ordinary_approval)
    except ApprovalRequired as exc:
        assert "may already have actuated" in str(exc)
    else:  # pragma: no cover - makes the no-second-submit invariant explicit
        raise AssertionError("ordinary resume repeated an uncertain delivery")

    assert second_backend.actuation_count == 0
    assert second_verifier.verify_calls == 0
