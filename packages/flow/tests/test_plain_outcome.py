"""Plain results are a pure function of ``transaction_outcome``.

The safety rule these tests pin: only an outcome that proves no business
effect may say "nothing was written" or allow a retry without a record check.
``RECONCILIATION_REQUIRED`` (and any missing or unknown outcome) must tell a
person to check the record first.
"""

from __future__ import annotations

import re

import pytest

from openadapt_flow.plain_outcome import PlainResult, plain_result
from openadapt_flow.transaction import TransactionOutcome

#: The outcomes ``classify_transaction_outcome`` returns only after every
#: consequential step is positively proven effect-free.
ABSENCE_PROVEN = {
    TransactionOutcome.HALTED_BEFORE_EFFECT,
    TransactionOutcome.REJECTED_POLICY,
    TransactionOutcome.CANCELED,
    TransactionOutcome.FAILED_PLATFORM,
}


def _all_text(result: PlainResult) -> str:
    return " ".join([result.label, result.meaning, result.next_step])


@pytest.mark.parametrize("outcome", list(TransactionOutcome))
def test_every_transaction_outcome_has_a_plain_result(
    outcome: TransactionOutcome,
) -> None:
    result = plain_result(outcome.value)
    assert result.transaction_outcome == outcome.value
    assert result.label and result.meaning and result.next_step


@pytest.mark.parametrize("outcome", list(TransactionOutcome))
def test_only_absence_proving_outcomes_say_nothing_was_written(
    outcome: TransactionOutcome,
) -> None:
    result = plain_result(outcome.value, "HALTED")
    says_nothing_written = "nothing was written" in _all_text(result).lower()
    if outcome in ABSENCE_PROVEN:
        assert result.nothing_written
        assert result.retry_allowed_without_check
        assert says_nothing_written
    else:
        assert not result.nothing_written
        assert not result.retry_allowed_without_check
        assert not says_nothing_written


def test_reconciliation_required_tells_a_person_to_check_the_record() -> None:
    result = plain_result("RECONCILIATION_REQUIRED", "HALTED")
    assert result.label == "Check the record"
    assert result.write_status == "possible"
    assert "may have gone through" in result.meaning
    assert "check the record" in result.next_step.lower()
    text = _all_text(result).lower()
    assert "safe to" not in text
    assert "run it again" not in text


def test_coarse_halted_alone_never_becomes_stopped_before_saving() -> None:
    """A legacy report with no transaction outcome can't claim absence."""

    for coarse in ("HALTED", "FAILED", "ROLLED_BACK", None):
        result = plain_result(None, coarse)
        assert result.key == "check_the_record"
        assert not result.nothing_written


def test_unknown_transaction_outcome_is_conservative() -> None:
    result = plain_result("SOMETHING_NEW", "HALTED")
    assert result.key == "check_the_record"
    assert result.transaction_outcome == "SOMETHING_NEW"


def test_verified_and_unverified_map_one_to_one() -> None:
    assert plain_result("VERIFIED").label == "Done and checked"
    unchecked = plain_result("COMPLETED_UNVERIFIED")
    assert unchecked.label == "Finished, not checked"
    assert unchecked.write_status == "unknown"
    # Without a transaction outcome, the coarse value still picks these two.
    assert plain_result(None, "VERIFIED").key == "done_and_checked"
    assert plain_result(None, "COMPLETED_UNVERIFIED").key == "finished_not_checked"


@pytest.mark.parametrize("outcome", [*list(TransactionOutcome), None])
def test_plain_words_carry_no_engine_terms(outcome) -> None:
    value = outcome.value if outcome is not None else None
    text = _all_text(plain_result(value, "HALTED"))
    assert "—" not in text  # no em dashes in short copy
    assert not re.search(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b", text)
    for term in ("VERIFIED", "HALTED", "oracle", "admission", "Seal"):
        assert term not in text
