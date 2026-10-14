from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_flow_transaction_probe.py"
SPEC = importlib.util.spec_from_file_location("flow_transaction_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(**overrides: object) -> dict:
    row = {
        "fault_mode": "ok",
        "verification": "unverified",
        "trial": 1,
        "transaction_outcome": "VERIFIED",
        "execution_outcome": "VERIFIED",
        "transaction_billable": True,
        "production_success_claimed": True,
        "business_effect": "intended_once",
        "effect_landed": True,
        "targeted_record_count": 1,
        "verification_performed": True,
        "attempt_state": "delivered",
        "model_calls": 0,
        "model_cost_usd": 0.0,
    }
    row.update(overrides)
    return row


def _check(checks: list[dict], check_id: str) -> dict:
    return next(check for check in checks if check["id"] == check_id)


def test_business_effect_reads_only_the_system_of_record() -> None:
    before = [{"id": 1, "patient_id": "p1", "type": "Intake", "note": "old"}]
    after = before + [{"id": 2, "patient_id": "p1", "type": "Triage", "note": "n-1"}]

    result = MODULE._business_effect(before, after, "n-1")

    assert result["business_effect"] == "intended_once"
    assert result["effect_landed"] is True
    assert result["targeted_record_count"] == 1
    assert result["collateral_lost_count"] == 0


def test_business_effect_flags_duplicate_partial_and_collateral_loss() -> None:
    before: list[dict] = []
    duplicate = [
        {"id": 1, "patient_id": "p1", "type": "Triage", "note": "n-1"},
        {"id": 2, "patient_id": "p1", "type": "Triage", "note": "n-1"},
    ]
    assert MODULE._business_effect(before, duplicate, "n-1")["business_effect"] == "duplicate"

    partial = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": ""}]
    assert MODULE._business_effect(before, partial, "n-1")["business_effect"] == "partial"

    assert MODULE._business_effect(before, [], "n-1")["business_effect"] == "absent"

    seeded = [{"id": 9, "patient_id": "p2", "type": "Urgent", "note": "other"}]
    landed = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": "n-1"}]
    lost = MODULE._business_effect(seeded, landed, "n-1")
    assert lost["business_effect"] == "collateral_loss"
    assert lost["collateral_lost_count"] == 1


def test_completed_unverified_may_never_be_success_or_billable() -> None:
    rows = [
        _row(
            transaction_outcome="COMPLETED_UNVERIFIED",
            transaction_billable=True,
            production_success_claimed=False,
        )
    ]

    check = _check(MODULE.evaluate_invariants(rows), "completed_unverified_is_never_success")

    assert check["holds"] is False
    assert check["violation_count"] == 1


def test_completed_unverified_holds_when_not_billed_as_success() -> None:
    rows = [
        _row(
            transaction_outcome="COMPLETED_UNVERIFIED",
            transaction_billable=False,
            production_success_claimed=False,
        )
    ]

    check = _check(MODULE.evaluate_invariants(rows), "completed_unverified_is_never_success")

    assert check["holds"] is True
    assert check["vacuous"] is False


def test_proven_absence_claim_is_a_violation_when_the_write_landed() -> None:
    rows = [
        _row(
            transaction_outcome="HALTED_BEFORE_EFFECT",
            execution_outcome="HALTED",
            transaction_billable=False,
            production_success_claimed=False,
            effect_landed=True,
            business_effect="intended_once",
        )
    ]

    check = _check(MODULE.evaluate_invariants(rows), "no_false_absence_claim")

    assert check["holds"] is False
    assert check["violations"][0]["transaction_outcome"] == "HALTED_BEFORE_EFFECT"


def test_actuated_but_unverified_step_may_not_claim_proven_absence() -> None:
    rows = [
        _row(
            transaction_outcome="HALTED_BEFORE_EFFECT",
            transaction_billable=False,
            production_success_claimed=False,
            attempt_state="delivered",
            verification_performed=False,
            effect_landed=False,
            business_effect="absent",
        )
    ]

    check = _check(
        MODULE.evaluate_invariants(rows),
        "unverified_delivered_write_needs_reconciliation",
    )

    assert check["holds"] is False


def test_reconciliation_required_satisfies_the_unverified_delivery_rule() -> None:
    rows = [
        _row(
            transaction_outcome="RECONCILIATION_REQUIRED",
            transaction_billable=False,
            production_success_claimed=False,
            attempt_state="delivery_uncertain",
            verification_performed=False,
        )
    ]

    check = _check(
        MODULE.evaluate_invariants(rows),
        "unverified_delivered_write_needs_reconciliation",
    )

    assert check["holds"] is True


def test_blind_retry_is_only_excused_for_the_double_accepting_backend() -> None:
    checks = MODULE.evaluate_invariants([_row(fault_mode="timeout", targeted_record_count=2)])
    assert _check(checks, "no_blind_retry_of_consequential_write")["holds"] is False

    checks = MODULE.evaluate_invariants([_row(fault_mode="duplicate", targeted_record_count=2)])
    assert _check(checks, "no_blind_retry_of_consequential_write")["holds"] is True


def test_unexercised_invariant_is_reported_vacuous_not_passing() -> None:
    rows = [
        _row(
            transaction_outcome="HALTED_BEFORE_EFFECT",
            transaction_billable=False,
            production_success_claimed=False,
            effect_landed=False,
            business_effect="absent",
            verification_performed=True,
        )
    ]

    check = _check(MODULE.evaluate_invariants(rows), "verified_requires_a_real_effect")

    assert check["applicable_count"] == 0
    assert check["vacuous"] is True


def test_rejects_less_than_three_counted_trials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least three"):
        MODULE.run_probe(tmp_path, tmp_path / "flow.whl", tmp_path / "out", trials=2)
