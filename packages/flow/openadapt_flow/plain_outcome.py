"""Plain-language results for people, derived only from ``transaction_outcome``.

Every person-facing surface (the ``demo`` proof page, ``explain``, the replay
finisher) needs one short label for how a run ended, one sentence on what that
means, and one next step. The coarse ``execution_outcome`` cannot supply them
safely: ``HALTED`` covers both "stopped, nothing was written" and "stopped, a
write may have landed". Only :class:`~openadapt_flow.transaction.TransactionOutcome`
separates those, so this module reads that field and nothing else.

Safety rule
-----------

A label may say "nothing was written" or invite a retry only when the
transaction outcome itself proves no business effect. ``classify_transaction_outcome``
returns ``HALTED_BEFORE_EFFECT``, ``REJECTED_POLICY``, ``CANCELED``, and
``FAILED_PLATFORM`` only after every consequential step is positively proven
effect-free, so exactly those four set :attr:`PlainResult.nothing_written`.
``RECONCILIATION_REQUIRED`` and ``ROLLED_BACK`` tell a person to check the
record before anything is retried. A missing or unknown transaction outcome
(for example a legacy report) never claims absence: a stop is presented as
"Check the record".

This is presentation only. It changes no exit code, gate, or report field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from openadapt_flow.transaction import TransactionOutcome

#: What is known about the business write after the run.
WriteStatus = Literal["confirmed", "none", "possible", "unknown"]

#: Coarse visual tone a renderer may use. Never used to decide wording.
Tone = Literal["done", "stopped", "check", "unchecked", "failed"]


@dataclass(frozen=True)
class PlainResult:
    """One run's result in business words, plus the exact engine term."""

    #: Stable machine key (``done_and_checked``, ``check_the_record``, ...).
    key: str
    #: Short label for a badge or a card header.
    label: str
    #: One or two plain sentences on what the result means.
    meaning: str
    #: One plain next step for the person.
    next_step: str
    write_status: WriteStatus
    tone: Tone
    #: The exact engine value this result came from (``None`` when absent).
    transaction_outcome: Optional[str]

    @property
    def nothing_written(self) -> bool:
        """True only when the engine proved no business write happened."""

        return self.write_status == "none"

    @property
    def retry_allowed_without_check(self) -> bool:
        """True only when a retry needs no record check first."""

        return self.write_status == "none"


_DONE = PlainResult(
    key="done_and_checked",
    label="Done and checked",
    meaning="It saved the entry and read the record back to confirm it.",
    next_step="Nothing to do.",
    write_status="confirmed",
    tone="done",
    transaction_outcome=TransactionOutcome.VERIFIED.value,
)


def _stopped_before_saving(outcome: TransactionOutcome, why: str) -> PlainResult:
    return PlainResult(
        key="stopped_before_saving",
        label="Stopped before saving",
        meaning=f"{why} Nothing was written.",
        next_step="Fix what didn't match, then run it again.",
        write_status="none",
        tone="stopped",
        transaction_outcome=outcome.value,
    )


_CHECK_THE_RECORD_NEXT = "Check the record before anyone runs this again."

_BY_OUTCOME: dict[TransactionOutcome, PlainResult] = {
    TransactionOutcome.VERIFIED: _DONE,
    TransactionOutcome.HALTED_BEFORE_EFFECT: _stopped_before_saving(
        TransactionOutcome.HALTED_BEFORE_EFFECT,
        "Something didn't match, so it stopped before changing anything and "
        "asked a person.",
    ),
    TransactionOutcome.REJECTED_POLICY: _stopped_before_saving(
        TransactionOutcome.REJECTED_POLICY,
        "It wasn't cleared to run this task here, so it stopped before "
        "changing anything.",
    ),
    TransactionOutcome.CANCELED: _stopped_before_saving(
        TransactionOutcome.CANCELED,
        "The run was canceled before it changed anything.",
    ),
    TransactionOutcome.FAILED_PLATFORM: PlainResult(
        key="did_not_finish",
        label="Didn't finish",
        meaning=(
            "OpenAdapt hit a problem of its own before it could change the "
            "record. Nothing was written."
        ),
        next_step="Read the report, fix the problem, then run it again.",
        write_status="none",
        tone="failed",
        transaction_outcome=TransactionOutcome.FAILED_PLATFORM.value,
    ),
    TransactionOutcome.RECONCILIATION_REQUIRED: PlainResult(
        key="check_the_record",
        label="Check the record",
        meaning=(
            "A save may have gone through. A person checks the record before "
            "anything is retried."
        ),
        next_step=_CHECK_THE_RECORD_NEXT,
        write_status="possible",
        tone="check",
        transaction_outcome=TransactionOutcome.RECONCILIATION_REQUIRED.value,
    ),
    TransactionOutcome.ROLLED_BACK: PlainResult(
        key="check_the_record",
        label="Check the record",
        meaning=(
            "OpenAdapt undid an extra change it found. A person checks the "
            "record before anything is retried."
        ),
        next_step=_CHECK_THE_RECORD_NEXT,
        write_status="possible",
        tone="check",
        transaction_outcome=TransactionOutcome.ROLLED_BACK.value,
    ),
    TransactionOutcome.COMPLETED_UNVERIFIED: PlainResult(
        key="finished_not_checked",
        label="Finished, not checked",
        meaning="It did the steps but did not confirm the saved result.",
        next_step="Add a record check before you rely on this result.",
        write_status="unknown",
        tone="unchecked",
        transaction_outcome=TransactionOutcome.COMPLETED_UNVERIFIED.value,
    ),
}


def plain_result(
    transaction_outcome: Optional[str],
    execution_outcome: Optional[str] = None,
) -> PlainResult:
    """Map the engine's transaction outcome to a :class:`PlainResult`.

    ``execution_outcome`` is consulted only when ``transaction_outcome`` is
    missing or unrecognized, and then only to pick between "Done and checked"
    (``VERIFIED`` maps one to one), "Finished, not checked", and the
    conservative "Check the record". It never produces a "nothing was
    written" result.
    """

    if transaction_outcome:
        try:
            return _BY_OUTCOME[TransactionOutcome(str(transaction_outcome))]
        except ValueError:
            pass
    coarse = str(execution_outcome or "").upper()
    if coarse == "VERIFIED":
        return _DONE
    if coarse == "COMPLETED_UNVERIFIED":
        return PlainResult(
            key="finished_not_checked",
            label="Finished, not checked",
            meaning="It did the steps but did not confirm the saved result.",
            next_step="Add a record check before you rely on this result.",
            write_status="unknown",
            tone="unchecked",
            transaction_outcome=transaction_outcome,
        )
    return PlainResult(
        key="check_the_record",
        label="Check the record",
        meaning=(
            "This report doesn't say whether anything was saved. A person "
            "checks the record before anything is retried."
        ),
        next_step=_CHECK_THE_RECORD_NEXT,
        write_status="unknown",
        tone="check",
        transaction_outcome=transaction_outcome,
    )


__all__ = ["PlainResult", "WriteStatus", "plain_result"]
