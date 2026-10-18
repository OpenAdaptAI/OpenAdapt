"""Pixel-only Citrix cannot be VERIFIED without an independent read.

Browser + independent API is the easy case. A Citrix/RDP window with only
pixels, a green banner, or an on-screen read-back is the weak case. The
Standard gate still admits persisted-state so the run can execute with
halt-on-doubt. The outcome classifier refuses VERIFIED for a same-screen
check and for a same-session reopen (check level ``reopened``); it needs a
separate session or the system of record, and a workflow that sets
``minimum_readback: system_of_record`` needs the latter. If a write may
already have been delivered, the transaction is RECONCILIATION_REQUIRED.

This is not live Citrix Production evidence.
"""

from __future__ import annotations

import pytest

from openadapt_flow.check_level import CheckLevel
from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
    stamp_execution_outcome,
)
from openadapt_flow.readback_ladder import level_for_tier
from openadapt_flow.transaction import TransactionOutcome
from openadapt_flow.verification import VERIFIED_EFFECT_TIER, VerificationTier
from tests.test_execution_profiles import (
    _verified_production_report,
    _workflow,
)

V = (ExecutionOutcome.VERIFIED, TransactionOutcome.VERIFIED)
U = (ExecutionOutcome.COMPLETED_UNVERIFIED, TransactionOutcome.RECONCILIATION_REQUIRED)
S, R = ExecutionProfile.STANDARD, ExecutionProfile.REGULATED
SOR = "system_of_record"


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
        # A same-session reopen is never VERIFIED by itself under Standard.
        (S, None, VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", U),
        (S, None, VerificationTier.IMMEDIATE_SCREEN, "onscreen", U),
        (S, SOR, VerificationTier.INDEPENDENT_SYSTEM, "rest", V),
        (S, SOR, VerificationTier.INDEPENDENT_SESSION, "session", U),
        (S, SOR, VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", U),
        (R, None, VerificationTier.INDEPENDENT_SYSTEM, "rest", V),
        (R, None, VerificationTier.INDEPENDENT_SESSION, "session", V),
        (R, None, VerificationTier.PERSISTED_STATE_REACQUISITION, "onscreen", U),
        (R, None, VerificationTier.IMMEDIATE_SCREEN, "onscreen", U),
        (R, SOR, VerificationTier.INDEPENDENT_SESSION, "session", U),
    ],
)
def test_production_verified_needs_an_independent_read(
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
    assert stamped.readback.minimum == (minimum or "separate_session")
    assert stamped.readback.checked_how == level_for_tier(tier, confirmed=True).label
    assert stamped.readback.below_minimum is (
        expected_execution is not ExecutionOutcome.VERIFIED
        and CheckLevel.from_label(stamped.readback.checked_how)
        < CheckLevel.from_label(stamped.readback.minimum)
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
