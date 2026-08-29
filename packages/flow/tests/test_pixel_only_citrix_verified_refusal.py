"""Pixel-only Citrix cannot be VERIFIED without a system-of-record read.

Browser + independent API is the easy case. A Citrix/RDP window with only
pixels, a green banner, or an on-screen read-back is the weak case. The
Standard gate still admits persisted-state so the run can execute with
halt-on-doubt. The outcome classifier refuses VERIFIED. If a write may
already have been delivered, the transaction is RECONCILIATION_REQUIRED.

This is not live Citrix Production evidence.
"""

from __future__ import annotations

import pytest

from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
    stamp_execution_outcome,
)
from openadapt_flow.transaction import TransactionOutcome
from openadapt_flow.verification import VERIFIED_EFFECT_TIER, VerificationTier
from tests.test_execution_profiles import (
    _verified_production_report,
    _workflow,
)


def _with_tier(report, tier: VerificationTier, *, substrate: str):
    copy = report.model_copy(deep=True)
    copy.results[0].effect_evidence[0].verification_tier = tier
    copy.results[0].effect_evidence[0].substrate = substrate
    return copy


@pytest.mark.parametrize(
    "tier, substrate, expected_execution, expected_transaction",
    [
        (
            VerificationTier.INDEPENDENT_SYSTEM,
            "rest",
            ExecutionOutcome.VERIFIED,
            TransactionOutcome.VERIFIED,
        ),
        (
            VerificationTier.INDEPENDENT_SESSION,
            "session",
            ExecutionOutcome.VERIFIED,
            TransactionOutcome.VERIFIED,
        ),
        (
            VerificationTier.PERSISTED_STATE_REACQUISITION,
            "onscreen",
            ExecutionOutcome.COMPLETED_UNVERIFIED,
            TransactionOutcome.RECONCILIATION_REQUIRED,
        ),
        (
            VerificationTier.IMMEDIATE_SCREEN,
            "onscreen",
            ExecutionOutcome.COMPLETED_UNVERIFIED,
            TransactionOutcome.RECONCILIATION_REQUIRED,
        ),
    ],
)
@pytest.mark.parametrize(
    "profile", [ExecutionProfile.STANDARD, ExecutionProfile.REGULATED]
)
def test_production_verified_requires_independent_sor_read(
    tier,
    substrate,
    expected_execution,
    expected_transaction,
    profile,
) -> None:
    workflow = _workflow()
    report = _with_tier(
        _verified_production_report(workflow), tier, substrate=substrate
    )

    assert classify_execution_outcome(report, workflow, profile) is expected_execution

    stamped = report.model_copy(deep=True)
    stamp_execution_outcome(stamped, workflow, profile)
    assert stamped.execution_outcome == expected_execution.value
    assert stamped.transaction_outcome == expected_transaction.value
    assert stamped.production_eligible is (
        expected_execution is ExecutionOutcome.VERIFIED
    )
    assert stamped.transaction_billable is (
        expected_transaction is TransactionOutcome.VERIFIED
    )


def test_demo_pixel_only_stays_completed_unverified() -> None:
    workflow = _workflow()
    report = _with_tier(
        _verified_production_report(workflow),
        VerificationTier.PERSISTED_STATE_REACQUISITION,
        substrate="onscreen",
    )
    stamp_execution_outcome(report, workflow, ExecutionProfile.DEMO)
    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.transaction_outcome == TransactionOutcome.COMPLETED_UNVERIFIED.value
    assert report.production_eligible is False
    assert report.transaction_billable is False


def test_halt_before_delivery_is_not_verified() -> None:
    workflow = _workflow()
    report = _verified_production_report(workflow)
    report.halt = "identity mismatch"
    report.success = False
    report.results[0].ok = False
    report.results[0].safety_halt = True
    report.results[0].delivery_attempted = False
    report.results[0].actuation = None
    report.results[0].delivery_receipt = None
    report.results[0].effect_verified = None
    report.results[0].effect_contract_hashes = []
    report.results[0].effect_evidence = []
    report.results[0].postconditions_ok = None

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.transaction_outcome == TransactionOutcome.HALTED_BEFORE_EFFECT.value
    assert report.production_eligible is False


def test_verified_effect_tier_is_independent_session() -> None:
    assert VERIFIED_EFFECT_TIER is VerificationTier.INDEPENDENT_SESSION
    assert VerificationTier.INDEPENDENT_SYSTEM.is_independent_system_of_record()
    assert VerificationTier.INDEPENDENT_SESSION.is_independent_system_of_record()
    assert not (
        VerificationTier.PERSISTED_STATE_REACQUISITION.is_independent_system_of_record()
    )
    assert not VerificationTier.IMMEDIATE_SCREEN.is_independent_system_of_record()
