"""What a pixel-only Citrix/RDP run can be, by read-back rung.

Browser + independent API is the easy case. A Citrix/RDP window with only
pixels, a green banner, or an on-screen read-back is the weak case. Under the
default read-back minimum (R2) a reopen of the record after a state reset
(tier 3) is enough for Standard; the screen the save left (tier 4) is not.
Regulated keeps its tier-2 floor. A workflow that sets ``minimum_readback:
R3`` needs a system-of-record read. When a write may already have been
delivered and the check was too weak, the transaction is
RECONCILIATION_REQUIRED.

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

V = (ExecutionOutcome.VERIFIED, TransactionOutcome.VERIFIED)
U = (ExecutionOutcome.COMPLETED_UNVERIFIED, TransactionOutcome.RECONCILIATION_REQUIRED)
S, R = ExecutionProfile.STANDARD, ExecutionProfile.REGULATED


def _with_tier(report, tier: VerificationTier, *, substrate: str):
    copy = report.model_copy(deep=True)
    copy.results[0].effect_evidence[0].verification_tier = tier
    copy.results[0].effect_evidence[0].substrate = substrate
    return copy


@pytest.mark.parametrize(
    "profile, minimum, tier, substrate, expected",
    [
        (S, None, VerificationTier.INDEPENDENT_SYSTEM, "rest", V),
        (S, None, VerificationTier.INDEPENDENT_SESSION, "session", V),
        (S, None, VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", V),
        (S, None, VerificationTier.IMMEDIATE_SCREEN, "onscreen", U),
        (S, "R1", VerificationTier.IMMEDIATE_SCREEN, "onscreen", V),
        (S, "R3", VerificationTier.INDEPENDENT_SYSTEM, "rest", V),
        (S, "R3", VerificationTier.INDEPENDENT_SESSION, "session", U),
        (S, "R3", VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", U),
        (R, None, VerificationTier.INDEPENDENT_SYSTEM, "rest", V),
        (R, None, VerificationTier.INDEPENDENT_SESSION, "session", V),
        (R, None, VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", U),
        (R, None, VerificationTier.IMMEDIATE_SCREEN, "onscreen", U),
        (R, "R1", VerificationTier.IMMEDIATE_SCREEN, "onscreen", U),
        (R, "R3", VerificationTier.INDEPENDENT_SESSION, "session", U),
    ],
)
def test_production_verified_follows_the_readback_minimum(
    profile, minimum, tier, substrate, expected
) -> None:
    workflow = _workflow()
    workflow.minimum_readback = minimum
    report = _with_tier(
        _verified_production_report(workflow), tier, substrate=substrate
    )
    expected_execution, expected_transaction = expected

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
    assert stamped.readback is not None
    assert stamped.readback.minimum == (minimum or "R2")
    assert stamped.readback.below_minimum is (
        expected_execution is not ExecutionOutcome.VERIFIED
        and stamped.readback.reached < stamped.readback.minimum
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


def test_verified_effect_tier_is_the_default_r2_floor() -> None:
    assert VERIFIED_EFFECT_TIER is VerificationTier.PERSISTED_STATE_REACQUISITION
    assert VerificationTier.INDEPENDENT_SYSTEM.is_independent_of_actuation()
    assert VerificationTier.INDEPENDENT_SESSION.is_independent_of_actuation()
    assert not (
        VerificationTier.PERSISTED_STATE_REACQUISITION.is_independent_of_actuation()
    )
    assert not VerificationTier.IMMEDIATE_SCREEN.is_independent_of_actuation()


def test_only_tier_one_is_called_a_system_of_record_read() -> None:
    # A separate application session (tier 2) is independent of the acting
    # session but is not a read of the system of record.
    assert VerificationTier.INDEPENDENT_SYSTEM.reads_system_of_record()
    for tier in (
        VerificationTier.INDEPENDENT_SESSION,
        VerificationTier.PERSISTED_STATE_REACQUISITION,
        VerificationTier.IMMEDIATE_SCREEN,
    ):
        assert not tier.reads_system_of_record()
        assert not tier.is_independent_system_of_record()
    assert VerificationTier.INDEPENDENT_SYSTEM.is_independent_system_of_record()
