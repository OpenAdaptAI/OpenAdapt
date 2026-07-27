"""Section 3: explicit transaction / reconciliation outcome taxonomy.

Covers the terminal-outcome classifier (each outcome from its own condition),
the effect journal, caller-supplied idempotency (duplicate suppression + no
blind retry after uncertain delivery), and the billing/success flags.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.execution_profiles import ExecutionProfile, stamp_execution_outcome
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    EffectVerificationEvidence,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.actuators import ActuationStatus, ApiActuationResult
from openadapt_flow.runtime.effects import Verdict
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import (
    IdempotencyLedger,
    TransactionOutcome,
    build_effect_journal,
    classify_transaction_outcome,
)
from tests.test_replayer import FakeBackend, FakeVision, Match, click_step, make_png
from tests.test_uncertain_delivery import _run

_HASH = "sha256:" + "a" * 64
_OTHER_HASH = "sha256:" + "b" * 64


def _report(coarse: str, results, *, canceled: bool = False) -> RunReport:
    report = RunReport(
        workflow_name="wf",
        started_at="2026-07-26T00:00:00Z",
        execution_outcome=coarse,
        canceled=canceled,
        results=results,
    )
    return report


def _refuted_evidence(
    observed_effect: str, effect_hash: str = _HASH
) -> EffectVerificationEvidence:
    return EffectVerificationEvidence(
        effect_contract_hash=effect_hash,
        substrate="test",
        initial_verdict="refuted",
        final_verdict="refuted",
        observed_effect=observed_effect,
    )


# -- classifier: each outcome from its own condition -------------------------


def test_verified_maps_to_verified():
    report = _report("VERIFIED", [StepResult(step_id="s1", intent="x", ok=True)])
    assert classify_transaction_outcome(report) is TransactionOutcome.VERIFIED


def test_rolled_back_maps_to_rolled_back():
    report = _report("ROLLED_BACK", [StepResult(step_id="s1", intent="x", ok=True)])
    assert classify_transaction_outcome(report) is TransactionOutcome.ROLLED_BACK


def test_rolled_back_requires_exact_settled_effect_coverage():
    compensated = EffectVerificationEvidence(
        effect_contract_hash=_HASH,
        substrate="test",
        initial_verdict="refuted",
        final_verdict="confirmed",
        observed_effect="present",
        reconciliation_completed=True,
        reconciliation_actions=1,
    )
    report = _report(
        "ROLLED_BACK",
        [
            _consequential(
                delivery_attempted=True,
                effect_contract_hashes=[_HASH, _OTHER_HASH],
                effect_evidence=[compensated],
            )
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_halted_before_effect_requires_verifier_established_absence():
    # A governed halt where the verifier proved NO record was written.
    result = StepResult(
        step_id="s1",
        intent="write",
        ok=False,
        safety_halt=True,
        effect_verified=False,
        effect_contract_hashes=[_HASH],
        effect_evidence=[_refuted_evidence("absent")],
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


def test_indeterminate_effect_forces_reconciliation():
    result = StepResult(
        step_id="s1",
        intent="write",
        ok=False,
        effect_verified=False,
        effect_contract_hashes=[_HASH],
        effect_evidence=[
            EffectVerificationEvidence(
                effect_contract_hash=_HASH,
                substrate="test",
                initial_verdict="indeterminate",
                final_verdict="indeterminate",
                observed_effect="unknown",
            )
        ],
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_conflicting_effect_forces_reconciliation():
    # A refuted-but-present write (duplicate / wrong value) may have landed.
    result = StepResult(
        step_id="s1",
        intent="write",
        ok=False,
        effect_verified=False,
        effect_contract_hashes=[_HASH],
        effect_evidence=[_refuted_evidence("conflicting")],
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_refuted_without_count_is_treated_as_unknown_and_reconciled():
    # Fail safe: a refutation that cannot prove absence must not claim "no
    # effect".
    result = StepResult(
        step_id="s1",
        intent="write",
        ok=False,
        effect_verified=False,
        effect_evidence=[_refuted_evidence("unknown")],
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_failed_platform_before_any_effect():
    result = StepResult(step_id="s1", intent="click", ok=False, error="backend crash")
    report = _report("FAILED", [result])
    assert classify_transaction_outcome(report) is TransactionOutcome.FAILED_PLATFORM


def test_canceled_before_effect():
    result = StepResult(step_id="s1", intent="click", ok=False)
    report = _report("FAILED", [result], canceled=True)
    assert classify_transaction_outcome(report) is TransactionOutcome.CANCELED


def test_rejected_policy_from_authorization_gate():
    result = StepResult(
        step_id="<authorization>",
        intent="validate governed run authorization",
        ok=False,
        failure_category="governed_refusal",
        error="not admitted",
    )
    report = _report("HALTED", [result])
    assert classify_transaction_outcome(report) is TransactionOutcome.REJECTED_POLICY


def test_rejected_policy_from_identity_refusal():
    from openadapt_flow.ir import IdentityCheck

    result = StepResult(
        step_id="s1",
        intent="click patient row",
        ok=False,
        identity=IdentityCheck(status="mismatch"),
    )
    report = _report("HALTED", [result])
    assert classify_transaction_outcome(report) is TransactionOutcome.REJECTED_POLICY


def test_completed_unverified_maps_through():
    report = _report(
        "COMPLETED_UNVERIFIED", [StepResult(step_id="s1", intent="x", ok=True)]
    )
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.COMPLETED_UNVERIFIED
    )


def test_uncertainty_dominates_a_concurrent_policy_refusal():
    # An unresolved uncertain delivery must never be downgraded to "no effect"
    # even when a governed-refusal category is also present.
    from openadapt_flow.ir import ActionDeliveryUncertainty

    result = StepResult(
        step_id="s1",
        intent="write",
        ok=False,
        failure_category="governed_refusal",
        delivery_uncertainty=ActionDeliveryUncertainty(
            operation="guarded_coordinate_click",
            native=False,
            observed_at="2026-07-26T00:00:00.000000+00:00",
            cause_type="ConnectionResetError",
            resolved_by_contract=False,
        ),
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


# -- absence requires POSITIVE evidence --------------------------------------
#
# HALTED_BEFORE_EFFECT (and every other outcome asserting no effect) tells a
# customer there is nothing to reconcile. It may therefore only be returned when
# absence was actually ESTABLISHED. An empty ``effect_evidence`` list means
# verification never ran -- unknown, not absent.


def _consequential(**kwargs) -> StepResult:
    """A step the compiler labelled irreversible (the run gate's write signal)."""

    kwargs.setdefault("step_id", "save")
    kwargs.setdefault("intent", "save encounter")
    kwargs.setdefault("ok", False)
    return StepResult(risk="irreversible", **kwargs)


def _receipt():
    from openadapt_flow.ir import ActionDeliveryReceipt

    return ActionDeliveryReceipt(
        receipt_id="r-1",
        operation="guarded_coordinate_click",
        native=False,
        delivered_at="2026-07-27T00:00:00+00:00",
    )


def test_actuated_then_aborted_before_verification_is_reconciliation_required():
    # THE DEFECT: the click was delivered, the run then aborted before any
    # verifier ran, so ``effect_evidence`` is empty. Absence was never
    # established -- the store may hold the write.
    report = _report("HALTED", [_consequential(delivery_receipt=_receipt())])
    outcome = classify_transaction_outcome(report)
    assert outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert outcome is not TransactionOutcome.HALTED_BEFORE_EFFECT


def test_commit_then_client_timeout_is_reconciliation_required():
    # The measured probe case: the backend COMMITS the row and then hangs past
    # the client deadline. The client sees only an error; the store holds the
    # write. A postcondition verdict exists (it is checked after the click), so
    # delivery is proven and verification never happened.
    report = _report(
        "HALTED",
        [
            _consequential(
                delivery_receipt=_receipt(),
                postconditions_ok=False,
                failure_category="runtime_failure",
                error="ReadTimeout",
            )
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_unclassified_failure_on_a_consequential_step_fails_closed():
    # Nothing recorded either way: the defensive ``except Exception`` around the
    # action cannot prove the action never reached the application.
    report = _report(
        "HALTED", [_consequential(failure_category="runtime_failure", error="boom")]
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_halt_before_actuation_remains_halted_before_effect():
    # A typed pre-delivery refusal (structural/OCR resolution refusal) records
    # that no action was admitted. This must NOT be over-corrected away.
    report = _report(
        "HALTED",
        [
            _consequential(
                safety_halt=True,
                failure_category="safety_halt",
                delivery_attempted=False,
                error="Structural safety refusal — no action was admitted",
            )
        ],
    )
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


def test_verifier_established_absence_after_delivery_remains_halted_before_effect():
    # The write WAS delivered, but a verifier read the system of record and
    # observed it absent (the "optimistic" case: the screen lied, nothing
    # landed). Positive evidence of absence -> the absence claim is honest.
    report = _report(
        "HALTED",
        [
            _consequential(
                delivery_receipt=_receipt(),
                effect_verified=False,
                effect_contract_hashes=[_HASH],
                effect_evidence=[_refuted_evidence("absent")],
            )
        ],
    )
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


def test_skipped_consequential_step_does_not_block_an_absence_claim():
    # An unmet guard with ``on_unmet="skip"`` means the step never ran at all.
    report = _report(
        "HALTED",
        [
            _consequential(ok=True, skipped=True),
            StepResult(step_id="s2", intent="halt", ok=False, safety_halt=True),
        ],
    )
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


@pytest.mark.parametrize(
    ("coarse", "canceled", "tail"),
    [
        (
            "HALTED",
            False,
            _consequential(
                step_id="approve",
                safety_halt=True,
                failure_category="safety_halt",
                delivery_attempted=False,
                error="refused before delivery",
            ),
        ),
        (
            "HALTED",
            False,
            StepResult(
                step_id="<authorization>",
                intent="authorize",
                ok=False,
                failure_category="governed_refusal",
            ),
        ),
        ("FAILED", True, StepResult(step_id="cancel", intent="cancel", ok=False)),
        ("FAILED", False, StepResult(step_id="platform", intent="fail", ok=False)),
    ],
    ids=["halted", "rejected-policy", "canceled", "failed-platform"],
)
def test_confirmed_earlier_write_never_yields_an_absence_outcome(
    coarse, canceled, tail
):
    # A known-present write is partial completion, not "before effect". Until
    # the taxonomy has a dedicated partial-completion outcome, every coarse
    # failure bucket must route it to reconciliation.
    confirmed = EffectVerificationEvidence(
        effect_contract_hash=_HASH,
        substrate="test",
        initial_verdict="confirmed",
        final_verdict="confirmed",
        observed_effect="present",
    )
    report = _report(
        coarse,
        [
            _consequential(
                step_id="write_ledger",
                ok=True,
                effect_verified=True,
                effect_contract_hashes=[_HASH],
                effect_evidence=[confirmed],
            ),
            tail,
        ],
        canceled=canceled,
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


@pytest.mark.parametrize(
    ("declared", "evidence"),
    [
        ([_HASH, _OTHER_HASH], [_refuted_evidence("absent")]),
        ([_OTHER_HASH], [_refuted_evidence("absent")]),
        ([_HASH, _HASH], [_refuted_evidence("absent")]),
        (
            [_HASH],
            [_refuted_evidence("absent"), _refuted_evidence("absent")],
        ),
    ],
    ids=["missing", "mismatched", "missing-duplicate", "extra-duplicate"],
)
def test_effect_absence_requires_exact_declared_hash_multiset(declared, evidence):
    report = _report(
        "HALTED",
        [
            _consequential(
                delivery_attempted=True,
                effect_verified=False,
                effect_contract_hashes=declared,
                effect_evidence=evidence,
            )
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_duplicate_declared_hashes_require_duplicate_absence_evidence():
    report = _report(
        "HALTED",
        [
            _consequential(
                delivery_attempted=True,
                effect_verified=False,
                effect_contract_hashes=[_HASH, _HASH],
                effect_evidence=[
                    _refuted_evidence("absent"),
                    _refuted_evidence("absent"),
                ],
            )
        ],
    )
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


def test_api_actuation_halt_is_reconciliation_required():
    # ``ActuationStatus.HALT`` is documented as "the request WAS sent ... the
    # write may have landed". The runtime stamps actuation="api" only once the
    # request was attempted, so this can never be a proven absence.
    report = _report(
        "HALTED",
        [
            _consequential(
                actuation="api",
                delivery_attempted=True,
                effect_verified=False,
                failure_category="safety_halt",
                error="API actuation HALTED step -- run aborted",
            )
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_approved_unverified_write_cannot_claim_absence():
    # Accepting the risk of proceeding without a verifier is not the same as
    # establishing what happened.
    report = _report(
        "HALTED", [_consequential(ok=True, effect_approved_unverified=True)]
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_reversible_step_never_blocks_an_absence_claim():
    # A reversible step that declared no effect cannot leave a write behind.
    report = _report("HALTED", [StepResult(step_id="nav", intent="scroll", ok=True)])
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )


def test_delivered_ambiguous_risk_never_claims_effect_absence():
    report = _report(
        "HALTED",
        [
            StepResult(
                step_id="drag",
                intent="move item",
                ok=True,
                risk_review_required=True,
                delivery_attempted=True,
            ),
            StepResult(step_id="tail", intent="halt", ok=False),
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_unproven_absence_also_blocks_rejected_policy():
    # REJECTED_POLICY asserts "refused before any business effect" too. A later
    # policy refusal cannot retroactively unwrite an earlier delivered step.
    from openadapt_flow.ir import IdentityCheck

    report = _report(
        "HALTED",
        [
            _consequential(delivery_receipt=_receipt()),
            StepResult(
                step_id="s2",
                intent="click patient row",
                ok=False,
                identity=IdentityCheck(status="mismatch"),
            ),
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_unproven_absence_also_blocks_canceled_and_failed_platform():
    delivered = _consequential(delivery_receipt=_receipt())
    canceled = _report("FAILED", [delivered], canceled=True)
    assert (
        classify_transaction_outcome(canceled)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )
    failed = _report("FAILED", [delivered])
    assert (
        classify_transaction_outcome(failed)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_duplicate_conflicting_reading_remains_reconciliation_required():
    # The one case the taxonomy already got right: a configured verifier
    # returned a CONFLICTING reading (the 'duplicate' fault mode).
    report = _report(
        "HALTED",
        [
            _consequential(
                delivery_receipt=_receipt(),
                effect_verified=False,
                effect_contract_hashes=[_HASH],
                effect_evidence=[_refuted_evidence("conflicting")],
            )
        ],
    )
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_journal_reports_delivered_and_unverified_for_the_defect_case():
    # The effect journal must state the same thing the outcome now does:
    # actuation was reached and no verification was performed.
    workflow = Workflow(
        name="wf",
        steps=[Step(id="save", intent="save", action=ActionKind.CLICK)],
    )
    report = _report("HALTED", [_consequential(delivery_receipt=_receipt())])
    entry = build_effect_journal(report, workflow)[0]
    assert entry.attempt_state == "delivered"
    assert entry.verification_performed is False
    assert entry.observed_effect == "unknown"


def test_delivered_write_with_no_recorded_outcome_is_delivery_uncertain():
    workflow = Workflow(
        name="wf",
        steps=[Step(id="save", intent="save", action=ActionKind.CLICK)],
    )
    report = _report(
        "HALTED", [_consequential(failure_category="runtime_failure", error="boom")]
    )
    entry = build_effect_journal(report, workflow)[0]
    assert entry.attempt_state == "delivery_uncertain"
    assert entry.verification_performed is False


def test_safety_halt_category_alone_is_not_pre_delivery_proof():
    result = _consequential(
        safety_halt=True,
        failure_category="safety_halt",
        error="heal policy rejected after action",
    )
    report = _report("HALTED", [result])
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )
    workflow = Workflow(
        name="wf",
        steps=[Step(id="save", intent="save", action=ActionKind.CLICK)],
    )
    assert (
        build_effect_journal(report, workflow)[0].attempt_state == "delivery_uncertain"
    )


# -- billing / success flags -------------------------------------------------


def test_failed_platform_is_not_billable_and_is_a_platform_fault():
    outcome = TransactionOutcome.FAILED_PLATFORM
    assert outcome.is_billable is False
    assert outcome.is_production_success is False
    assert outcome.is_platform_fault is True


def test_completed_unverified_is_never_billable_or_production_success():
    outcome = TransactionOutcome.COMPLETED_UNVERIFIED
    assert outcome.is_billable is False
    assert outcome.is_production_success is False
    assert outcome.is_platform_fault is False


def test_only_verified_is_billable_production_success():
    assert TransactionOutcome.VERIFIED.is_billable is True
    assert TransactionOutcome.VERIFIED.is_production_success is True
    assert TransactionOutcome.VERIFIED.is_platform_fault is False
    for other in (
        TransactionOutcome.HALTED_BEFORE_EFFECT,
        TransactionOutcome.RECONCILIATION_REQUIRED,
        TransactionOutcome.CANCELED,
        TransactionOutcome.REJECTED_POLICY,
        TransactionOutcome.ROLLED_BACK,
    ):
        assert other.is_billable is False
        assert other.is_production_success is False


# -- effect journal ----------------------------------------------------------


def test_effect_journal_records_intended_attempt_and_observed():
    workflow = Workflow(
        name="wf",
        steps=[Step(id="save", intent="save", action=ActionKind.CLICK)],
    )
    result = StepResult(
        step_id="save",
        intent="save",
        ok=False,
        effect_verified=False,
        effect_contract_hashes=[_HASH],
        effect_evidence=[_refuted_evidence("absent")],
    )
    report = _report("HALTED", [result])
    journal = build_effect_journal(report, workflow)
    assert len(journal) == 1
    entry = journal[0]
    assert entry.step_id == "save"
    assert entry.intended_effect_contract_hashes == [_HASH]
    assert entry.attempt_state == "delivered"
    assert entry.observed_effect == "absent"
    assert entry.effect_verified is False
    assert entry.verification_performed is True
    assert entry.collateral_reconciliation_actions == 0


def test_effect_journal_skips_non_consequential_steps():
    workflow = Workflow(
        name="wf",
        steps=[Step(id="nav", intent="scroll", action=ActionKind.SCROLL)],
    )
    result = StepResult(step_id="nav", intent="scroll", ok=True)
    report = _report("COMPLETED_UNVERIFIED", [result])
    assert build_effect_journal(report, workflow) == []


# -- stamping (end-to-end through execution_profiles) ------------------------


def test_stamp_adds_transaction_fields_without_touching_execution_outcome():
    workflow = Workflow(
        name="wf",
        steps=[Step(id="save", intent="save", action=ActionKind.CLICK)],
    )
    report = RunReport(
        workflow_name="wf",
        started_at="2026-07-26T00:00:00Z",
        success=True,
        results=[StepResult(step_id="save", intent="save", ok=True)],
    )
    stamp_execution_outcome(report, workflow, ExecutionProfile.DEMO)
    # Coarse outcome is unchanged; the refined transaction outcome is added.
    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    assert report.transaction_outcome == "COMPLETED_UNVERIFIED"
    assert report.transaction_billable is False
    assert report.transaction_platform_fault is False
    # Round-trips through the persisted report shape.
    reloaded = RunReport.model_validate(json.loads(report.model_dump_json()))
    assert reloaded.transaction_outcome == "COMPLETED_UNVERIFIED"


def test_real_run_that_over_halts_after_delivery_is_reconciliation_required(tmp_path):
    """End-to-end: the click IS delivered, then a postcondition aborts the run.

    This is the shape the premature-abort over-halt produces -- the run never
    reaches effect verification, so a false ``HALTED_BEFORE_EFFECT`` here would
    tell the customer to reconcile nothing while the write may have landed. The
    backend's recorded actions are the independent proof the click went out.
    """

    from openadapt_flow.ir import Postcondition, PostconditionKind

    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))

    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {"Saved": None}  # the confirmation never appears
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.05,
                    )
                ],
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    # The click really was delivered to the application.
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].postconditions_ok is False
    assert report.success is False
    # ...and no verifier ever read the system of record.
    assert report.results[0].effect_evidence == []
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.transaction_outcome != "HALTED_BEFORE_EFFECT"
    assert report.transaction_billable is False


def test_real_run_that_halts_before_actuation_stays_halted_before_effect(tmp_path):
    """The counterpart: resolution fails, nothing is ever clicked."""

    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))

    vision = FakeVision()
    vision.template_results = [None]  # the target is never found
    backend = FakeBackend()
    workflow = Workflow(name="wf", steps=[click_step(risk="irreversible")])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert backend.actions == []  # independent proof nothing was delivered
    assert report.results[0].delivery_attempted is False
    assert report.success is False
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"


class _PathVerifier:
    substrate = "path-test"

    def capture_pre_state(self):
        return EffectState(substrate=self.substrate, reachable=True)

    def verify(self, effect, before):
        del before
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=self.substrate,
        )


class _PathActuator:
    def __init__(self, status: ActuationStatus) -> None:
        self.status = status

    def actuate(self, binding, params):
        del binding, params
        return ApiActuationResult(status=self.status, reason=self.status.value)


class _FailingPathVerifier(_PathVerifier):
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def capture_pre_state(self):
        if self.stage == "pre_state":
            raise RuntimeError("pre-state connector failed")
        return super().capture_pre_state()

    def verify(self, effect, before):
        if self.stage == "verify":
            raise RuntimeError("verification connector failed")
        return super().verify(effect, before)


class _RaisingPathActuator:
    def actuate(self, binding, params):
        del binding, params
        raise RuntimeError("actuator failed")


def _path_effect(name: str) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"path": name},
        risk="irreversible",
    )


def _api_or_gui_workflow() -> tuple[Workflow, Effect, Effect]:
    gui_effect = _path_effect("gui")
    api_effect = _path_effect("api")
    workflow = Workflow(
        name="path-ledger",
        steps=[
            Step(
                id="save",
                intent="save",
                action=ActionKind.KEY,
                key="Enter",
                effects=[gui_effect],
                api_binding=ApiBinding(
                    method="POST",
                    url_template="/save",
                    effects=[api_effect],
                ),
            )
        ],
    )
    return workflow, gui_effect, api_effect


@pytest.mark.parametrize(
    ("status", "responsible_path", "actions"),
    [
        (ActuationStatus.ACTUATED, "api", []),
        (ActuationStatus.UNAVAILABLE, "gui", [("press", "Enter")]),
    ],
)
def test_effect_hashes_track_only_the_responsible_actuation_path(
    tmp_path, status, responsible_path, actions
):
    workflow, gui_effect, api_effect = _api_or_gui_workflow()
    effects = {"gui": gui_effect, "api": api_effect}
    responsible = effects[responsible_path]
    abandoned = effects["api" if responsible_path == "gui" else "gui"]
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=FakeVision(),
        effect_verifier=_PathVerifier(),
        api_actuator=_PathActuator(status),
    ).run(workflow, bundle_dir=tmp_path / "bundle", run_dir=tmp_path / "run")

    result = report.results[0]
    assert result.effect_contract_hashes == [responsible.contract_hash()]
    assert {e.effect_contract_hash for e in result.effect_evidence} == {
        responsible.contract_hash()
    }
    assert abandoned.contract_hash() not in result.effect_contract_hashes
    assert result.delivery_attempted is True
    assert backend.actions == actions


@pytest.mark.parametrize(
    ("stage", "expected_attempted", "expected_outcome"),
    [
        ("pre_state", False, "HALTED_BEFORE_EFFECT"),
        ("actuate", True, "RECONCILIATION_REQUIRED"),
        ("verify", True, "RECONCILIATION_REQUIRED"),
    ],
)
def test_api_boundary_exceptions_produce_a_fail_closed_report(
    tmp_path, stage, expected_attempted, expected_outcome
):
    workflow, _gui_effect, _api_effect = _api_or_gui_workflow()
    verifier = _FailingPathVerifier(stage)
    actuator = (
        _RaisingPathActuator()
        if stage == "actuate"
        else _PathActuator(ActuationStatus.ACTUATED)
    )
    backend = FakeBackend()

    report = Replayer(
        backend,
        vision=FakeVision(),
        effect_verifier=verifier,
        api_actuator=actuator,
    ).run(workflow, bundle_dir=tmp_path / "bundle", run_dir=tmp_path / "run")

    assert backend.actions == []
    assert report.results[0].delivery_attempted is expected_attempted
    assert report.transaction_outcome == expected_outcome


# -- idempotency: duplicate suppression, no re-actuation ---------------------


def _simple_click_run(backend, ledger, run_dir, bundle, key):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    replayer = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.01,
        idempotency_ledger=ledger,
    )
    return replayer.run(
        Workflow(name="wf", steps=[click_step(risk="reversible")]),
        bundle_dir=bundle,
        run_dir=run_dir,
        idempotency_key=key,
    )


def test_idempotency_key_suppresses_duplicate_actuation(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    ledger = IdempotencyLedger(tmp_path / "ledger.json")

    first_backend = FakeBackend()
    first = _simple_click_run(
        first_backend, ledger, tmp_path / "run1", bundle, "order-42"
    )
    assert first.idempotent_replay is False
    assert first_backend.actions == [("click", 110, 105, False)]
    assert ledger.seen("order-42")

    # A fresh backend proves the second run cannot have actuated.
    second_backend = FakeBackend()
    second = _simple_click_run(
        second_backend, ledger, tmp_path / "run2", bundle, "order-42"
    )
    assert second.idempotent_replay is True
    assert second_backend.actions == []
    assert second.transaction_outcome == "REJECTED_POLICY"
    assert second.success is False


def test_idempotency_reservation_survives_new_ledger_instance(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))

    ledger = IdempotencyLedger(tmp_path / "ledger.json")
    _simple_click_run(FakeBackend(), ledger, tmp_path / "run1", bundle, "k")

    # A process restart re-reads the persisted ledger and still suppresses.
    reopened = IdempotencyLedger(tmp_path / "ledger.json")
    backend = FakeBackend()
    report = _simple_click_run(backend, reopened, tmp_path / "run2", bundle, "k")
    assert report.idempotent_replay is True
    assert backend.actions == []


def test_different_keys_do_not_suppress(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    ledger = IdempotencyLedger(tmp_path / "ledger.json")

    backend = FakeBackend()
    _simple_click_run(backend, ledger, tmp_path / "run1", bundle, "a")
    other = FakeBackend()
    report = _simple_click_run(other, ledger, tmp_path / "run2", bundle, "b")
    assert report.idempotent_replay is False
    assert other.actions == [("click", 110, 105, False)]


# -- no blind retry after uncertain delivery (builds on #250) ----------------


def test_reconciliation_required_does_not_auto_retry(tmp_path):
    # A refuted uncertain delivery: the write was attempted exactly once and the
    # runtime does NOT re-actuate. Section 3 surfaces this as
    # RECONCILIATION_REQUIRED (delivery uncertain, not resolved by the contract).
    report, backend, verifier, _workflow, _bundle = _run(tmp_path, Verdict.REFUTED)
    assert backend.actuation_count == 1
    assert verifier.verify_calls == 1
    assert report.results[0].delivery_uncertainty.resolved_by_contract is False
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.transaction_billable is False


def test_indeterminate_uncertain_delivery_is_reconciliation_required(tmp_path):
    report, backend, _verifier, _workflow, _bundle = _run(
        tmp_path, Verdict.INDETERMINATE
    )
    assert backend.actuation_count == 1
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"


def test_resolved_uncertain_delivery_is_verified(tmp_path):
    report, backend, _verifier, _workflow, _bundle = _run(tmp_path, Verdict.CONFIRMED)
    assert backend.actuation_count == 1
    assert report.transaction_outcome == "VERIFIED"
    assert report.transaction_billable is True


def test_effect_verdict_observed_effect_mapping():
    assert (
        EffectVerdict(
            verdict=Verdict.CONFIRMED, kind=EffectKind.RECORD_WRITTEN, observed_count=1
        ).observed_effect
        == "present"
    )
    assert (
        EffectVerdict(
            verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN, observed_count=0
        ).observed_effect
        == "absent"
    )
    assert (
        EffectVerdict(
            verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN, observed_count=3
        ).observed_effect
        == "conflicting"
    )
    assert (
        EffectVerdict(
            verdict=Verdict.INDETERMINATE, kind=EffectKind.RECORD_WRITTEN
        ).observed_effect
        == "unknown"
    )
    assert (
        EffectVerdict(
            verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN
        ).observed_effect
        == "unknown"
    )
