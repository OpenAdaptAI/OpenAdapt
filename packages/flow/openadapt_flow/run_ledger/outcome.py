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
    # A halt or a failure says the run stopped; neither says whether before
    # the write, so neither is "no write" without more evidence.
    "HALTED": "write_unconfirmed",
    "FAILED": "write_unconfirmed",
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

#: ``openadapt_flow.agent.runner.RunOutcome.status``. A timeout or an error
#: can come after a request went out, and the status alone doesn't say it
#: didn't: UNKNOWN, not FAILED. The ``*_no_write`` reasons are only for a
#: source that also shows nothing was sent (the agent guard's own events).
AGENT_RUN_STATUS_REASON: dict[str, str] = {
    "success": "completed_unverified",
    "halt": "write_unconfirmed",
    "refused": "policy_rejected_no_write",
    "timeout": "write_unconfirmed",
    "error": "write_unconfirmed",
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


#: Guard refusal codes (``summary.json`` ``blocked[].code``) where the
#: approval was not given and no person said no: nobody answered in time.
#: The save was blocked, so nothing was written; a person still has to act.
_APPROVAL_NOT_GIVEN = frozenset(
    {"request_expired", "approval_wait_timed_out", "approval_required"}
)
#: Guard refusal codes where the guard itself could not do its job (P5's
#: ``agent_guard.ledger_row._FAILED_CODES`` and the approval plumbing). The
#: save was blocked before it was sent.
_GUARD_COULD_NOT_CHECK = frozenset(
    {
        "baseline_unreadable",
        "screenshot_required",
        "wrong_state",
        "task_invalid",
        "task_unreadable",
        "readback_provider_failed",
        "already_answered",
        "answer_fields_invalid",
        "answer_malformed",
        "answer_unreadable",
        "approver_root_changed",
        "frame_digest_mismatch",
        "frame_unreadable",
        "initiator_changed",
        "request_unknown",
        "store_unsafe",
        "approval_not_authenticated_person",
        "approval_request_unwritable",
    }
)


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
    if code == "declined":
        # P5 writes ``declined`` only for a readable answer that isn't
        # "approve": a person said no. Not a catch (skeptic #16).
        return "person_declined"
    if code in _APPROVAL_NOT_GIVEN:
        return "approval_declined"
    if code in _GUARD_COULD_NOT_CHECK:
        return "policy_rejected_no_write"
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


#: P1 readiness ``case.json`` ``status`` (``openadapt.readiness-case/v1``,
#: ``readiness/compare.py CASE_STATUSES``). A shadow run holds every save,
#: so nothing it proposed was written: never VERIFIED. A person's save is the
#: real one, so a match is ESCALATED (a person did the step), and a mismatch
#: or a wrong record is the error the shadow caught. ``FAILED_SCOPE`` means a
#: write reached the app anyway: UNKNOWN, since nothing read it back.
#: ``NOT_CHECKED`` is refined by :func:`shadow_reason`.
READINESS_CASE_REASON: dict[str, str] = {
    "MATCHED": "shadow_matched",
    "MISMATCHED": "shadow_field_mismatch",
    "WRONG_RECORD": "shadow_wrong_record",
    "NOT_COMPARABLE": "shadow_not_compared",
    "NOT_CHECKED": "shadow_not_compared",
    "FAILED_SCOPE": "shadow_write_landed",
}

#: P1 ``run.json`` ``status`` for a run that proposed nothing
#: (``readiness/runner.py``). ``TIMEOUT`` and ``NO_WRITE_HELD`` are set only
#: after the shadow guard counted the saves that reached the app, so with
#: ``landed_writes == 0`` they are failures with evidence that nothing was
#: written. ``AGENT_ERROR`` is the default before the guard counted anything:
#: its ``landed_writes: 0`` is not evidence, so it is UNKNOWN.
READINESS_RUN_NO_WRITE_REASON: dict[str, str] = {
    "TIMEOUT": "timed_out_no_write",
    "NO_WRITE_HELD": "agent_gave_up",
    "AGENT_ERROR": "write_unconfirmed",
}


def shadow_reason(
    case_status: Optional[str],
    run_status: Optional[str],
    landed_writes: Optional[int],
    *,
    egress_refused: bool = False,
) -> str:
    """The reason for one shadow run (J1).

    ``landed_writes`` is P1's count of saves that reached the app (``None``
    when unknown). A landed write wins: something was saved and nothing read
    it back. A missing ``case.json`` is never a match.

    ``EXTRA_TARGET``: a write came from, or could have come from, a tab,
    popup or worker the hold didn't guard; the hold can't vouch that nothing
    was written there. ``EGRESS_REFUSED`` (or ``run.json`` ``egress.refused``):
    the run's model traffic wasn't scrubbed or metered as required, so P1
    keeps no scorecard for it. Both are UNKNOWN, never a match. A healthy
    ``--egress blocked`` run also says ``mode: blocked``; only ``refused``
    tells the two apart.
    """

    if case_status == "FAILED_SCOPE" or (landed_writes or 0) > 0:
        return "shadow_write_landed"
    if run_status == "EXTRA_TARGET":
        return "shadow_extra_target"
    if run_status == "EGRESS_REFUSED" or egress_refused:
        return "egress_refused"
    if case_status in ("MISMATCHED", "WRONG_RECORD"):
        # The proposal was held, so the error the shadow caught stands even
        # when the landed count is unknown.
        return READINESS_CASE_REASON[case_status]
    if landed_writes is None:
        return "shadow_not_compared"
    if (
        case_status in (None, "NOT_CHECKED")
        and run_status in READINESS_RUN_NO_WRITE_REASON
    ):
        return READINESS_RUN_NO_WRITE_REASON[str(run_status)]
    if case_status is None:
        return "shadow_not_compared"
    return outcome_reason(READINESS_CASE_REASON, case_status)


#: P7 fallback ``status`` (``wrap/fallback.py``) on ``path: agent_fallback``.
#: ``None`` means the reason depends on the record check (see
#: :func:`fallback_reason`).
FALLBACK_STATUS_REASON: dict[str, Optional[str]] = {
    "VERIFIED": None,
    "GATE_REFUSED": None,
    "COST_CAP_EXCEEDED": None,
    # A save went through without the gate's approval.
    "GATE_NOT_PROVED": "self_approved",
    "RECONCILIATION_REQUIRED": "reconciliation_required",
    # The agent was never started; the program's proved pre-effect halt
    # stands and a person picks the task up.
    "FALLBACK_GATE_UNATTENDED": None,
    "FALLBACK_NO_AGENT": None,
    "FALLBACK_COST_UNBOUNDED": None,
    "FALLBACK_REQUEST_REUSED": None,
}
_FALLBACK_NOT_STARTED = (
    "FALLBACK_GATE_UNATTENDED",
    "FALLBACK_NO_AGENT",
    "FALLBACK_COST_UNBOUNDED",
    "FALLBACK_REQUEST_REUSED",
)


def fallback_reason(
    status: object,
    *,
    verified: bool,
    confirmed: bool = False,
    nothing_written: bool,
    something_written: bool,
    program_halted_before_effect: bool,
) -> str:
    """The reason for one P7 fallback run (J5). ``verified`` is P7's own
    flag (every gate approved by the person and a passed record check);
    ``confirmed`` is the record check itself: readable and passed.
    ``nothing_written`` needs a readable check with zero changes;
    ``something_written`` is a check that counted a change."""

    if not isinstance(status, str) or status not in FALLBACK_STATUS_REASON:
        return (
            "readback_refuted"
            if something_written and not confirmed
            else ("unmapped_outcome")
        )
    fixed = FALLBACK_STATUS_REASON[status]
    if fixed is not None:
        return fixed
    if status == "VERIFIED":
        if not verified:
            return "self_approved"
        # A record check that isn't readable (or predates ``readable``)
        # doesn't confirm the save; P7 itself now says RECONCILIATION_REQUIRED.
        return "readback_verified" if confirmed else "reconciliation_required"
    if status == "GATE_REFUSED":
        # The person at the gate said no. With the record check showing no
        # write, it is a decline, not a catch (skeptic #16).
        return "person_declined" if nothing_written else "write_unconfirmed"
    if status == "COST_CAP_EXCEEDED":
        return "policy_rejected_no_write" if nothing_written else "write_unconfirmed"
    if status in _FALLBACK_NOT_STARTED:
        return (
            "halted_before_effect"
            if program_halted_before_effect
            else "write_unconfirmed"
        )
    return "unmapped_outcome"


__all__ = [
    "AGENT_RUN_STATUS_REASON",
    "EFFECT_VERDICT_REASON",
    "EXECUTE_OUTCOME_REASON",
    "EXECUTION_OUTCOME_REASON",
    "FALLBACK_STATUS_REASON",
    "GUARD_VERDICT_REASON",
    "READINESS_CASE_REASON",
    "READINESS_RUN_NO_WRITE_REASON",
    "Signal",
    "TRANSACTION_OUTCOME_REASON",
    "WRAP_STATUS_REASON",
    "decide",
    "fallback_reason",
    "guard_block_reason",
    "shadow_reason",
    "outcome_reason",
    "tier_reason",
    "verified_or_tier",
]
