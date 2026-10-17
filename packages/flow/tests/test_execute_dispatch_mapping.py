"""Execute outcome mapping fails closed: no "no effect" claim without proof.

``halted_before_effect``, ``rejected_policy`` and ``failed_platform`` each tell
the caller that no business effect happened. The local projection may return
one of them only when the run's own transaction outcome proves it; a missing,
unknown or contradictory value maps to ``reconciliation_required``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("openadapt_types")

from openadapt_types.execute import ExecuteTerminalOutcomeV1 as E  # noqa: E402

from openadapt_flow.execute.dispatch import (  # noqa: E402
    _map_outcome,
    project_run_report,
)
from openadapt_flow.transaction import TransactionOutcome as T  # noqa: E402

COARSE = ["VERIFIED", "COMPLETED_UNVERIFIED", "HALTED", "FAILED", "ROLLED_BACK", ""]
COARSE += ["garbage"]

NO_EFFECT_CLAIMS = {E.HALTED_BEFORE_EFFECT, E.REJECTED_POLICY, E.FAILED_PLATFORM}

EXPECTED = {
    T.VERIFIED: E.VERIFIED,
    T.HALTED_BEFORE_EFFECT: E.HALTED_BEFORE_EFFECT,
    T.CANCELED: E.HALTED_BEFORE_EFFECT,
    T.REJECTED_POLICY: E.REJECTED_POLICY,
    T.FAILED_PLATFORM: E.FAILED_PLATFORM,
    T.ROLLED_BACK: E.ROLLED_BACK_VERIFIED,
    T.RECONCILIATION_REQUIRED: E.RECONCILIATION_REQUIRED,
    T.COMPLETED_UNVERIFIED: E.RECONCILIATION_REQUIRED,
}


@pytest.mark.parametrize("coarse", COARSE)
def test_missing_transaction_outcome_requires_reconciliation(coarse: str) -> None:
    assert _map_outcome(coarse, None) is E.RECONCILIATION_REQUIRED


def test_every_transaction_outcome_has_an_explicit_mapping() -> None:
    assert set(EXPECTED) == set(T)


@pytest.mark.parametrize("txn", list(T))
@pytest.mark.parametrize("coarse", [c for c in COARSE if c != "VERIFIED"])
def test_no_effect_claim_only_from_a_proving_transaction_outcome(
    txn: T, coarse: str
) -> None:
    mapped = _map_outcome(coarse, txn)
    if txn is T.VERIFIED:
        # A VERIFIED transaction outcome with a contradicting coarse outcome
        # proves nothing either way.
        assert mapped is E.RECONCILIATION_REQUIRED
        return
    assert mapped is EXPECTED[txn]
    if mapped in NO_EFFECT_CLAIMS:
        assert txn in {
            T.HALTED_BEFORE_EFFECT,
            T.CANCELED,
            T.REJECTED_POLICY,
            T.FAILED_PLATFORM,
        }


def test_verified_needs_a_verified_transaction_outcome() -> None:
    assert _map_outcome("VERIFIED", T.VERIFIED) is E.VERIFIED
    assert _map_outcome("VERIFIED", T.RECONCILIATION_REQUIRED) is (
        E.RECONCILIATION_REQUIRED
    )
    assert _map_outcome("VERIFIED", None) is E.RECONCILIATION_REQUIRED


def test_completed_unverified_is_never_a_policy_rejection() -> None:
    mapped = _map_outcome("COMPLETED_UNVERIFIED", T.COMPLETED_UNVERIFIED)
    assert mapped is not E.REJECTED_POLICY
    assert mapped is E.RECONCILIATION_REQUIRED


def test_unstamped_halted_report_requires_reconciliation() -> None:
    report = SimpleNamespace(
        transaction_outcome=None,
        execution_outcome="HALTED",
        outcome_envelope=None,
        model_calls=0,
        bundle_content_digest="x",
        results=[
            SimpleNamespace(
                step_id="step_submit", risk="irreversible", effect_evidence=[]
            )
        ],
    )
    result = project_run_report(report, workflow_digest="sha256:" + "c" * 64)
    assert result.outcome is E.RECONCILIATION_REQUIRED
    assert result.delivery_uncertain is True


def test_unknown_transaction_outcome_string_requires_reconciliation() -> None:
    report = SimpleNamespace(
        transaction_outcome="SOMETHING_NEW",
        execution_outcome="HALTED",
        outcome_envelope=None,
        model_calls=0,
        bundle_content_digest="x",
    )
    result = project_run_report(report, workflow_digest="sha256:" + "c" * 64)
    assert result.outcome is E.RECONCILIATION_REQUIRED
    assert result.delivery_uncertain is True
