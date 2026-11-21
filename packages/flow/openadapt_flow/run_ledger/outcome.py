"""The outcome rule: which of five buckets a run lands in, and why.

Each collector turns what its files say into :class:`Signal` values. A signal
names a bucket through a closed reason code
(:data:`openadapt_types.run_ledger.RUN_LEDGER_REASON_OUTCOME`) and says what it
rests on (``basis``):

* ``deterministic``: a read of the system of record, a guard decision, a
  receipt, a person's recorded answer.
* ``model``: a model's or an agent's own judgement ("looks fine", "done").

:func:`decide` picks the run's bucket. The stronger finding wins, in the order
CAUGHT_ERROR > UNKNOWN > ESCALATED > FAILED > VERIFIED. Two rules keep a
model from upgrading anything:

1. A ``model`` signal can never yield VERIFIED. An agent that says it is done,
   with nothing that read the save back, is UNKNOWN (``completed_unverified``).
2. Because CAUGHT_ERROR outranks everything, a model's VERIFIED can't
   override a deterministic refutation either way.

The mapping tables below translate every terminal outcome the engine writes
on main into a reason. An outcome missing from a table is UNKNOWN
(``unmapped_outcome``), never another bucket. ``docs/RUN_LEDGER.md`` is the
written-down version of this module, and
``tests/test_run_ledger_outcome.py`` checks it as a table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from openadapt_types.run_ledger import (
    RUN_LEDGER_OUTCOME_PRECEDENCE,
    RUN_LEDGER_REASON_OUTCOME,
    RunLedgerOutcome,
)

Basis = Literal["deterministic", "model"]


@dataclass(frozen=True)
class Signal:
    """One finding from one source: a reason code and what it rests on."""

    reason: str
    basis: Basis = "deterministic"
    source: str = ""

    def __post_init__(self) -> None:
        if self.reason not in RUN_LEDGER_REASON_OUTCOME:
            raise ValueError(f"unknown ledger reason {self.reason!r}")

    @property
    def outcome(self) -> RunLedgerOutcome:
        return RUN_LEDGER_REASON_OUTCOME[self.reason]


_RANK = {outcome: rank for rank, outcome in enumerate(RUN_LEDGER_OUTCOME_PRECEDENCE)}

#: Within one bucket, the reason that explains it best comes first.
_REASON_ORDER = tuple(RUN_LEDGER_REASON_OUTCOME)


def decide(signals: Iterable[Signal]) -> tuple[RunLedgerOutcome, str]:
    """The run's bucket and reason. No signal at all is UNKNOWN (``no_sources``)."""

    effective: list[Signal] = []
    for signal in signals:
        if signal.basis == "model" and signal.outcome is RunLedgerOutcome.VERIFIED:
            # A model's judgement can't verify a save.
            signal = Signal("completed_unverified", "model", signal.source)
        effective.append(signal)
    if not effective:
        return RunLedgerOutcome.UNKNOWN, "no_sources"
    best = min(
        effective,
        key=lambda s: (_RANK[s.outcome], _REASON_ORDER.index(s.reason)),
    )
    return best.outcome, best.reason


# -- the engine's terminal outcomes on main ---------------------------------------

#: ``openadapt_flow.transaction.TransactionOutcome`` (``report.json``
#: ``transaction_outcome``). VERIFIED still needs an independent read-back:
#: see :func:`verified_or_tier`.
TRANSACTION_OUTCOME_REASON: dict[str, str] = {
    "VERIFIED": "readback_verified",
    "HALTED_BEFORE_EFFECT": "halted_before_effect",
    "RECONCILIATION_REQUIRED": "reconciliation_required",
    "FAILED_PLATFORM": "platform_failed",
    "CANCELED": "canceled_no_write",
    "REJECTED_POLICY": "policy_rejected_no_write",
    "COMPLETED_UNVERIFIED": "completed_unverified",
    "ROLLED_BACK": "rolled_back",
    "SAVED_AWAITING_CHECK": "saved_awaiting_check",
}

#: ``openadapt_flow.execution_profiles.ExecutionOutcome`` (``report.json``
#: ``execution_outcome``), used only when ``transaction_outcome`` is absent.
EXECUTION_OUTCOME_REASON: dict[str, str] = {
    "VERIFIED": "readback_verified",
    "COMPLETED_UNVERIFIED": "completed_unverified",
    # A halt says the run stopped; it doesn't say whether before the write.
    "HALTED": "write_unconfirmed",
    "FAILED": "crashed_no_write",
    "ROLLED_BACK": "rolled_back",
}

#: ``openadapt_types.execute.ExecuteTerminalOutcomeV1`` (the verify-run effect
#: receipt's ``outcome``, and wrap's upper-cased ``receipt_outcome``).
#: ``REJECTED_POLICY`` from verify-run means the write landed but the executor
#: approved it itself: not a clean stop, so UNKNOWN.
EXECUTE_OUTCOME_REASON: dict[str, str] = {
    "VERIFIED": "readback_verified",
    "HALTED_BEFORE_EFFECT": "halted_before_effect",
    "RECONCILIATION_REQUIRED": "reconciliation_required",
    "REJECTED_POLICY": "self_approved",
    "FAILED_PLATFORM": "platform_failed",
    "ROLLED_BACK_VERIFIED": "rolled_back",
}

#: ``openadapt_flow.guard.GuardVerdict`` (agent-guard ``summary.json``).
GUARD_VERDICT_REASON: dict[str, str] = {
    "VERIFIED": "readback_verified",
    "REFUTED": "readback_refuted",
    "RECONCILIATION_REQUIRED": "reconciliation_required",
}

#: ``effect_verdict`` on a verify-run effect receipt.
EFFECT_VERDICT_REASON: dict[str, str] = {
    "CONFIRMED": "readback_verified",
    "REFUTED": "readback_refuted",
    "INDETERMINATE": "reconciliation_required",
}

#: ``openadapt_flow.agent.runner.RunOutcome.status``.
AGENT_RUN_STATUS_REASON: dict[str, str] = {
    "success": "completed_unverified",
    "halt": "write_unconfirmed",
    "refused": "policy_rejected_no_write",
    "timeout": "timed_out_no_write",
    "error": "crashed_no_write",
}

#: Wrap / SkillCache ``status`` codes that decide on their own
#: (``openadapt_flow.integrations.agents.skills``). Others defer to
#: ``receipt_outcome`` and the record check.
WRAP_STATUS_REASON: dict[str, str] = {
    "CACHE_HALTED": "reconciliation_required",
    "RECONCILIATION_REQUIRED": "reconciliation_required",
    "RECEIPT_NOT_VERIFIED": "completed_unverified",
    "MODEL_CALLS_NOT_ZERO": "completed_unverified",
    "RECORD_CHECK_FAILED": "readback_refuted",
    "BASELINE_UNAVAILABLE": "write_unconfirmed",
    "TOOL_ERROR": "write_unconfirmed",
    "REQUEST_ID_CONFLICT": "policy_rejected_no_write",
}


def _guard_block_reason(code: str) -> Optional[str]:
    """A save the guard blocked (``summary.json`` ``blocked[].code``)."""

    if code.startswith("identity_"):
        return "save_blocked_wrong_record"
    if code == "other_origin":
        return "save_blocked_other_origin"
    if code in (
        "already_committed",
        "already_saved",
        "needs_reconciliation",
        "save_waiting_for_person",
        "approval_already_used",
    ):
        return "save_blocked_double_save"
    if code in ("declined", "approval_declined") or code.startswith("approval_"):
        return "approval_declined"
    return None


def guard_block_reason(code: str, *, scope: bool = False) -> str:
    """The reason for one blocked save. A scope refusal (another record, field
    or value than the task's teach scope) is out of scope. An unknown code is
    UNKNOWN: a block the ledger can't name isn't counted as a catch."""

    if scope:
        return "save_blocked_out_of_scope"
    return _guard_block_reason(code) or "unmapped_outcome"


def tier_reason(tier: Optional[str]) -> Optional[str]:
    """What a weaker-than-independent read-back does to a VERIFIED claim."""

    if tier == "independent_readback":
        return None
    if tier == "screen_readback":
        return "screen_readback_only"
    return "completed_unverified"


def verified_or_tier(tier: Optional[str]) -> str:
    """``readback_verified`` when the read-back was independent, else the
    reason it isn't."""

    return tier_reason(tier) or "readback_verified"


def outcome_reason(table: dict[str, str], value: object) -> str:
    """Look ``value`` up in a mapping table; anything else is unmapped."""

    if not isinstance(value, str):
        return "unmapped_outcome"
    return table.get(value.upper(), table.get(value, "unmapped_outcome"))


__all__ = [
    "AGENT_RUN_STATUS_REASON",
    "EFFECT_VERDICT_REASON",
    "EXECUTE_OUTCOME_REASON",
    "EXECUTION_OUTCOME_REASON",
    "GUARD_VERDICT_REASON",
    "Signal",
    "TRANSACTION_OUTCOME_REASON",
    "WRAP_STATUS_REASON",
    "decide",
    "guard_block_reason",
    "outcome_reason",
    "tier_reason",
    "verified_or_tier",
]
