"""Section 3: explicit transaction / reconciliation outcome taxonomy.

Covers the terminal-outcome classifier (each outcome from its own condition),
the effect journal, caller-supplied idempotency (duplicate suppression + no
blind retry after uncertain delivery), and the billing/success flags.
"""

from __future__ import annotations

import json

from openadapt_flow.execution_profiles import ExecutionProfile, stamp_execution_outcome
from openadapt_flow.ir import (
    ActionKind,
    EffectVerificationEvidence,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.effects import Verdict
from openadapt_flow.runtime.effects.effect import EffectKind, EffectVerdict
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


def _report(coarse: str, results, *, canceled: bool = False) -> RunReport:
    report = RunReport(
        workflow_name="wf",
        started_at="2026-07-26T00:00:00Z",
        execution_outcome=coarse,
        canceled=canceled,
        results=results,
    )
    return report


def _refuted_evidence(observed_effect: str) -> EffectVerificationEvidence:
    return EffectVerificationEvidence(
        effect_contract_hash=_HASH,
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
