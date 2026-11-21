"""The outcome rule, as a table (docs/RUN_LEDGER.md).

Every terminal outcome the engine writes on main maps to exactly one bucket,
each contract source lands where the doc says, and a model's judgement never
upgrades a deterministic refutation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openadapt_types.execute import ExecuteTerminalOutcomeV1
from openadapt_types.run_ledger import RUN_LEDGER_REASON_OUTCOME

from openadapt_flow.execution_profiles import ExecutionOutcome
from openadapt_flow.guard import GuardVerdict
from openadapt_flow.run_ledger.outcome import (
    AGENT_RUN_STATUS_REASON,
    EXECUTE_OUTCOME_REASON,
    EXECUTION_OUTCOME_REASON,
    GUARD_VERDICT_REASON,
    TRANSACTION_OUTCOME_REASON,
    Signal,
    decide,
    guard_block_reason,
    outcome_reason,
)
from openadapt_flow.run_ledger.store import build_row
from openadapt_flow.transaction import TransactionOutcome

FIXTURES = Path(__file__).parent / "fixtures" / "run_ledger"

V, E, C, F, U = "VERIFIED", "ESCALATED", "CAUGHT_ERROR", "FAILED", "UNKNOWN"

TRANSACTION = {
    "VERIFIED": V,
    "HALTED_BEFORE_EFFECT": E,
    "RECONCILIATION_REQUIRED": U,
    "FAILED_PLATFORM": F,
    "CANCELED": F,
    "REJECTED_POLICY": F,
    "COMPLETED_UNVERIFIED": U,
    "ROLLED_BACK": U,
    "SAVED_AWAITING_CHECK": U,
}
EXECUTE = {
    "verified": V,
    "halted_before_effect": E,
    "reconciliation_required": U,
    # verify-run's REJECTED_POLICY: the write landed but was self-approved.
    "rejected_policy": U,
    "failed_platform": F,
    "rolled_back_verified": U,
}
EXECUTION = {
    "VERIFIED": V,
    "COMPLETED_UNVERIFIED": U,
    "HALTED": U,
    "FAILED": F,
    "ROLLED_BACK": U,
}
GUARD = {"VERIFIED": V, "REFUTED": C, "RECONCILIATION_REQUIRED": U}
AGENT_STATUS = {"success": U, "halt": U, "refused": F, "timeout": F, "error": F}


def _bucket(reason: str) -> str:
    return RUN_LEDGER_REASON_OUTCOME[reason].value


@pytest.mark.parametrize("member", list(TransactionOutcome))
def test_every_transaction_outcome_has_one_bucket(member: TransactionOutcome) -> None:
    assert member.value in TRANSACTION_OUTCOME_REASON, "add it to the table and doc"
    assert (
        _bucket(TRANSACTION_OUTCOME_REASON[member.value]) == TRANSACTION[member.value]
    )


@pytest.mark.parametrize("member", list(ExecuteTerminalOutcomeV1))
def test_every_execute_outcome_has_one_bucket(member: ExecuteTerminalOutcomeV1) -> None:
    reason = outcome_reason(EXECUTE_OUTCOME_REASON, member.value)
    assert reason != "unmapped_outcome", "add it to the table and doc"
    assert _bucket(reason) == EXECUTE[member.value]


@pytest.mark.parametrize("member", list(ExecutionOutcome))
def test_every_execution_outcome_has_one_bucket(member: ExecutionOutcome) -> None:
    assert _bucket(EXECUTION_OUTCOME_REASON[member.value]) == EXECUTION[member.value]


@pytest.mark.parametrize("member", list(GuardVerdict))
def test_every_guard_verdict_has_one_bucket(member: GuardVerdict) -> None:
    assert _bucket(GUARD_VERDICT_REASON[member.value]) == GUARD[member.value]


@pytest.mark.parametrize("status", sorted(AGENT_STATUS))
def test_every_agent_run_status_has_one_bucket(status: str) -> None:
    assert _bucket(AGENT_RUN_STATUS_REASON[status]) == AGENT_STATUS[status]


def test_an_unmapped_outcome_is_unknown_never_another_bucket() -> None:
    assert outcome_reason(TRANSACTION_OUTCOME_REASON, "SOMETHING_NEW") == (
        "unmapped_outcome"
    )
    assert outcome_reason(TRANSACTION_OUTCOME_REASON, None) == "unmapped_outcome"
    assert _bucket("unmapped_outcome") == U


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("identity_mismatch", "save_blocked_wrong_record"),
        ("identity_missing", "save_blocked_wrong_record"),
        ("other_origin", "save_blocked_other_origin"),
        ("already_committed", "save_blocked_double_save"),
        ("needs_reconciliation", "save_blocked_double_save"),
        ("save_waiting_for_person", "save_blocked_double_save"),
        ("declined", "approval_declined"),
        ("baseline_unreadable", "unmapped_outcome"),
    ],
)
def test_guard_block_codes(code: str, reason: str) -> None:
    assert guard_block_reason(code) == reason
    assert guard_block_reason(code, scope=True) == "save_blocked_out_of_scope"


# -- precedence ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (["readback_verified", "readback_refuted"], C),
        (["readback_verified", "reconciliation_required"], U),
        (["approval_held", "reconciliation_required"], U),
        (["approval_held", "timed_out_no_write"], E),
        (["agent_gave_up", "readback_verified"], F),
        (["readback_verified"], V),
        (["shadow_field_mismatch", "halted_before_effect"], C),
        ([], U),
    ],
)
def test_precedence(reasons: list[str], expected: str) -> None:
    outcome, _ = decide(Signal(r) for r in reasons)
    assert outcome.value == expected


def test_a_model_never_verifies() -> None:
    outcome, reason = decide([Signal("readback_verified", "model")])
    assert (outcome.value, reason) == (U, "completed_unverified")


def test_refuted_readback_plus_model_looks_fine_is_caught_error(tmp_path: Path) -> None:
    """A deterministic refutation and an agent that says it's done."""

    run = tmp_path / "run"
    run.mkdir()
    (run / "guard_ledger_row.json").write_text(
        (FIXTURES / "guard_row_ok" / "guard_ledger_row.json")
        .read_text()
        .replace('"VERIFIED"', '"REFUTED"')
    )
    (run / "agent_ledger.json").write_text(
        (FIXTURES / "agent_only_finished" / "agent_ledger.json").read_text()
    )
    row, _, _ = build_row(run)
    assert (row.outcome.value, row.outcome_reason) == (C, "readback_refuted")


# -- each contract source, as a table ------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "outcome", "reason"),
    [
        ("shadow_mismatch", C, "shadow_field_mismatch"),
        ("shadow_match", E, "halted_before_effect"),
        ("shadow_scorecard", C, "shadow_field_mismatch"),
        ("flight_held", E, "gate_held"),
        ("flight_refused", C, "gate_refused"),
        ("guard_row_diff", C, "readback_refuted"),
        ("guard_row_ok", V, "readback_verified"),
        ("exception_2fa", E, "second_factor"),
        ("exception_open", E, "exception_card"),
        ("unknown_schema", U, "schema_unrecognised"),
        ("compiled_verified", V, "readback_verified"),
        ("compiled_screen", U, "screen_readback_only"),
        ("compiled_halted", E, "halted_before_effect"),
        ("wrap_verified", V, "readback_verified"),
        ("wrap_lost", U, "lost_reply"),
        ("task_ledger_question", E, "question_asked"),
        ("agent_only_finished", U, "completed_unverified"),
        ("agent_only_gave_up", F, "agent_gave_up"),
    ],
)
def test_fixture_outcomes(fixture: str, outcome: str, reason: str) -> None:
    row, _, _ = build_row(FIXTURES / fixture)
    assert (row.outcome.value, row.outcome_reason) == (outcome, reason)


def test_an_empty_run_is_unknown_no_sources(tmp_path: Path) -> None:
    row, _, _ = build_row(tmp_path)
    assert (row.outcome.value, row.outcome_reason) == (U, "no_sources")
    assert row.sources == ()


def test_conflicting_task_ids_are_unknown(tmp_path: Path) -> None:
    for name in ("guard_row_ok", "flight_held"):
        src = next((FIXTURES / name).glob("*.json"))
        (tmp_path / src.name).write_text(src.read_text())
    row, _, _ = build_row(tmp_path)
    assert row.outcome_reason == "task_id_conflict"
