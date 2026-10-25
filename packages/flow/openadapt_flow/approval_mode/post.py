"""Post-run checks R1 and R2, the item decision, and the problem-rate rule.

R1 ``verified``: the transaction outcome is present, is ``VERIFIED``, and
agrees with the execution outcome and the outcome envelope. Every effect was
confirmed on the first read, with no reconciliation, by a system-of-record
read at ``INDEPENDENT_SYSTEM`` from a verifier identity the switch record
names. A ``VERIFIED`` execution outcome with no transaction outcome fails.

R2 ``whole_run_clean``: over the whole run, no model call, no repair, no
drift rescue or drift-oracle call, no ``grounder`` rung, no rung below the
step's floor from passing cases, and no dialog dismissal those cases didn't
use.

The engine's outcome words never change here. An item that fails R2 after
R1 passed stays ``VERIFIED`` ("Done and checked"), still goes to a person, and
turns the switch off with ``screen_changed``.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from openadapt_flow.approval_mode.model import (
    UI_READBACK_PILOT,
    CheckOutcome,
    ItemDecisionKind,
    OffReason,
    SwitchThresholds,
)
from openadapt_flow.approval_mode.switch import RUNG_ORDER, SwitchLogEvent

if TYPE_CHECKING:
    from openadapt_flow.approval_mode.switch import SwitchRecord
    from openadapt_flow.ir import RunReport, StepResult

#: Outcomes that may be shown as "nothing was written" (design section 8).
NOTHING_WRITTEN = frozenset(
    {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED", "FAILED_PLATFORM"}
)
_EXECUTION_FOR_NOTHING_WRITTEN = frozenset({"HALTED", "FAILED"})


def outcome_is_uncertain(report: "RunReport") -> bool:
    """True when the item ended "Check the record", "Finished, not checked",
    with an unknown or inconsistent outcome, or rolled back.

    These switch the mode off with ``uncertain_result``.
    """

    transaction = report.transaction_outcome
    execution = report.execution_outcome
    if transaction is None or execution is None:
        return True
    if transaction == "VERIFIED":
        return not _verified_outcome_consistent(report)
    if transaction in NOTHING_WRITTEN:
        return execution not in _EXECUTION_FOR_NOTHING_WRITTEN
    # RECONCILIATION_REQUIRED, COMPLETED_UNVERIFIED, ROLLED_BACK.
    return True


def _verified_outcome_consistent(report: "RunReport") -> bool:
    envelope = report.outcome_envelope
    return (
        report.transaction_outcome == "VERIFIED"
        and report.execution_outcome == "VERIFIED"
        and envelope is not None
        and envelope.outcome == "VERIFIED"
        and report.success is True
        and report.production_eligible is True
    )


def _consequential(result: "StepResult") -> bool:
    return (
        result.risk == "irreversible"
        or result.risk_review_required
        or bool(result.effect_contract_hashes)
        or bool(result.effect_evidence)
        or result.effect_verified is not None
        or result.effect_approved_unverified
        or result.delivery_uncertainty is not None
    )


def check_verified(
    report: "RunReport",
    record: "SwitchRecord",
    *,
    saves_without_person: int,
    gated_results: Optional[Sequence["StepResult"]] = None,
) -> CheckOutcome:
    """R1: a first-read system-of-record confirmation of every save.

    ``gated_results`` are the save results the pre-save gate passed. When
    given, every consequential result in the report must be one of them: a
    save delivered without the gate's checks (its record-check coverage among
    them) never counts as checked, even when its own read-back confirmed.
    """

    def fail(detail: str) -> CheckOutcome:
        return CheckOutcome(code="verified", passed=False, detail=detail)

    if report.transaction_outcome is None:
        return fail("transaction_outcome_missing")
    if report.transaction_outcome != "VERIFIED":
        return fail("not_verified")
    if not _verified_outcome_consistent(report):
        return fail("outcome_inconsistent")
    if report.execution_profile != record.profile:
        return fail("profile_mismatch")
    if report.bundle_content_digest != record.bundle_content_digest:
        return fail("bundle_mismatch")
    if saves_without_person < 1:
        return fail("no_save_checked")
    identities = set(record.verifier_identities)
    evidence_count = 0
    for result in report.results:
        if result.effect_approved_unverified:
            return fail("approved_unverified_write")
        if not _consequential(result) or result.skipped:
            continue
        if gated_results is not None and not any(
            result is gated for gated in gated_results
        ):
            return fail("save_not_gated")
        if result.delivery_uncertainty is not None:
            return fail("delivery_uncertain")
        if not result.effect_evidence:
            return fail("save_without_read_back")
        declared = Counter(result.effect_contract_hashes)
        observed = Counter(item.effect_contract_hash for item in result.effect_evidence)
        if not declared or declared != observed:
            return fail("read_back_does_not_cover_save")
        for item in result.effect_evidence:
            evidence_count += 1
            if (
                item.initial_verdict != "confirmed"
                or item.final_verdict != "confirmed"
                or item.observed_effect != "present"
            ):
                return fail("read_back_not_confirmed_first_time")
            if item.reconciliation_completed or item.reconciliation_actions:
                return fail("read_back_reconciled")
            if item.verification_tier == 2:
                return fail(UI_READBACK_PILOT)
            if item.verification_tier != 1:
                return fail("read_back_not_system_of_record")
            if item.verifier_identity is None:
                return fail("verifier_identity_missing")
            if item.verifier_identity not in identities:
                return fail("verifier_identity_not_bound")
    if evidence_count == 0:
        return fail("save_without_read_back")
    return CheckOutcome(code="verified", passed=True)


def step_screen_problem(
    result: "StepResult",
    record: "SwitchRecord",
) -> Optional[str]:
    """The first reason this step's screen handling differs from passing cases.

    Shared by the pre-save check P4 (for steps so far) and R2 (whole run).
    """

    if result.heal is not None:
        return "repair_applied"
    if result.postcondition_drift_rescues or result.drift_oracle_calls:
        return "drift_rescue"
    for resolution in (result.resolution, result.drag_end_resolution):
        if resolution is None:
            continue
        if resolution.rung == "grounder":
            return "grounder_rung"
        floor = record.rung_floor.get(result.step_id)
        if floor is None:
            return "rung_not_seen_in_passing_cases"
        if RUNG_ORDER.index(resolution.rung) > RUNG_ORDER.index(floor):
            return "rung_below_floor"
    allowed = set(record.allowed_interstitials.get(result.step_id, ()))
    for action in result.interstitial_actions:
        if action.interstitial not in allowed:
            return "new_dialog_dismissal"
    return None


def check_whole_run_clean(report: "RunReport", record: "SwitchRecord") -> CheckOutcome:
    """R2: the whole run used no repair, model call or rescue."""

    def fail(detail: str, step_id: Optional[str] = None) -> CheckOutcome:
        return CheckOutcome(
            code="whole_run_clean", passed=False, detail=detail, step_id=step_id
        )

    if report.model_calls:
        return fail("model_call")
    if report.heal_count:
        return fail("repair_applied")
    if report.rung_counts.get("grounder"):
        return fail("grounder_rung")
    if report.screenshots_may_leave_box:
        return fail("model_egress_possible")
    for result in report.results:
        problem = step_screen_problem(result, record)
        if problem is not None:
            return fail(problem, result.step_id)
    return CheckOutcome(code="whole_run_clean", passed=True)


@dataclass(frozen=True)
class ItemDecision:
    """The decision for one finished item, before it is logged."""

    kind: ItemDecisionKind
    reason: Optional[str]
    needs_person: bool
    counts_toward_problems: bool
    switch_off: Optional[OffReason]


def engine_refusal_decision(
    report: "RunReport", *, uncertain: bool
) -> Optional[ItemDecision]:
    """Map the engine's own pre-delivery refusals onto the gate's codes.

    The runtime already refuses some saves before the approval gate is asked:
    an identity check that didn't verify, or a record-check verifier that
    failed its preflight or pre-state read. Those are the same problems P3
    and P5 catch, so they get the same decision and switch-off. Returns None
    for any other result.
    """

    for result in report.results:
        evidence = result.safety_refusal_evidence
        if evidence is None:
            continue
        if evidence.stage in {"effect_verifier", "effect_strength"}:
            return ItemDecision(
                kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                reason="record_check_ready",
                needs_person=True,
                counts_toward_problems=True,
                switch_off=OffReason.RECORD_CHECK_LOST,
            )
        if evidence.stage == "identity_verification":
            return ItemDecision(
                kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                reason="right_record",
                needs_person=True,
                counts_toward_problems=True,
                switch_off=OffReason.UNCERTAIN_RESULT if uncertain else None,
            )
    return None


#: A problem with the item's own inputs: the program and the app both worked,
#: and this item needs a person. It leaves the switch on.
INPUT_PROBLEM = "input"
#: A problem with the program or its environment. It turns the switch off.
PROGRAM_PROBLEM = "program"
#: Item checks that judge the item's inputs, not the program.
_INPUT_CHECK_CODES = frozenset({"inputs_supplied", "inputs_strict", "channel"})
#: ``StepResult.duplicate_check`` values (set by the duplicate check): a real
#: candidate was found, or the list or record channel couldn't be read.
#: The record channel's own decisions name an exact or close match.
_DUPLICATE_FOUND = frozenset({"candidate", "exact", "close"})
_DUPLICATE_UNREADABLE = "unreadable"


def problem_class(report: "RunReport", reason: Optional[str] = None) -> str:
    """``input`` or ``program`` for a finished item that needs a person.

    An input problem is one the program caught in the item's own values
    before anything was written:

    - a value refused before the first step: a missing required value, or a
      typed value that isn't valid (a date that isn't an ISO date);
    - an item check on the inputs (``inputs_supplied``, ``inputs_strict``);
    - a real duplicate candidate in the app's possible-duplicate list.

    Everything else is a program or environment problem: a wrong write the
    record check caught, a failed identity check, a postcondition that failed
    after delivery, a changed screen, and any cause not named here. An
    unknown cause is a program problem (fail closed), and so is any outcome
    that doesn't read "nothing was written" consistently.
    """

    if report.transaction_outcome not in NOTHING_WRITTEN or outcome_is_uncertain(
        report
    ):
        return PROGRAM_PROBLEM
    if reason in _INPUT_CHECK_CODES:
        return INPUT_PROBLEM
    halted = next(
        (r for r in reversed(report.results) if not r.ok and not r.skipped), None
    )
    if halted is None:
        return PROGRAM_PROBLEM
    if halted.step_id == "<params>":
        return INPUT_PROBLEM
    # The duplicate check says why it failed in a structured field; the error
    # text is never read. A check that couldn't read its list, and any
    # duplicate stop that doesn't say, is a program problem.
    if (
        report.transaction_outcome == "HALTED_BEFORE_EFFECT"
        and getattr(halted, "duplicate_check", None) in _DUPLICATE_FOUND
        and not any(
            r.delivery_uncertainty is not None
            or (r.delivery_attempted is True and _consequential(r))
            for r in report.results
        )
    ):
        return INPUT_PROBLEM
    return PROGRAM_PROBLEM


#: Gate pre-save checks, by the closed off reason their failure means.
_GATE_OFF_REASONS = {
    "signed_in_user": OffReason.ENVIRONMENT_CHANGED,
    "right_record": OffReason.SCREEN_CHANGED,
    "same_screens": OffReason.SCREEN_CHANGED,
    "record_check_ready": OffReason.RECORD_CHECK_LOST,
}


def _program_off_reason(report: "RunReport", reason: Optional[str] = None) -> OffReason:
    """The closed off reason for a program problem that named none."""

    if reason in _GATE_OFF_REASONS:
        return _GATE_OFF_REASONS[reason]
    if any(
        getattr(result, "duplicate_check", None) == _DUPLICATE_UNREADABLE
        for result in report.results
    ):
        return OffReason.RECORD_CHECK_LOST

    for result in report.results:
        evidence = result.safety_refusal_evidence
        if evidence is not None and evidence.stage == "identity_verification":
            return OffReason.SCREEN_CHANGED
    if report.transaction_outcome == "HALTED_BEFORE_EFFECT" and not (
        outcome_is_uncertain(report)
    ):
        # The program stopped before any effect: the app no longer looks the
        # way the program was admitted for.
        return OffReason.SCREEN_CHANGED
    return OffReason.UNCERTAIN_RESULT


def classify_decision(report: "RunReport", decision: ItemDecision) -> ItemDecision:
    """Apply the switch policy to a decision that counts as a problem.

    An input problem goes to a person and leaves the switch on: it no longer
    counts toward ``too_many_problems``. A program problem turns the switch
    off, with the decision's own reason or a closed one for its cause. A
    decision that isn't a problem (a person is waiting to approve, the item
    checks sent it to a person) is unchanged.
    """

    if not decision.needs_person or not (
        decision.counts_toward_problems or decision.switch_off is not None
    ):
        return decision
    if decision.switch_off is not None:
        # The gate, R2 or the engine already turned the switch off for a
        # named reason. Nothing classes that down to an input problem.
        return decision
    if problem_class(report, decision.reason) == INPUT_PROBLEM:
        return dataclasses.replace(
            decision, counts_toward_problems=False, switch_off=None
        )
    if decision.switch_off is None:
        return dataclasses.replace(
            decision,
            counts_toward_problems=True,
            switch_off=_program_off_reason(report, decision.reason),
        )
    return decision


def decide_problems_only(
    report: "RunReport",
    *,
    r1: CheckOutcome,
    r2: CheckOutcome,
    gate_decision: Optional[ItemDecision],
    save_delivered: bool = False,
) -> ItemDecision:
    """The item's decision under ``problems_only``, with the switch policy
    applied (:func:`classify_decision`)."""

    return classify_decision(
        report,
        _decide_problems_only(
            report,
            r1=r1,
            r2=r2,
            gate_decision=gate_decision,
            save_delivered=save_delivered,
        ),
    )


def _decide_problems_only(
    report: "RunReport",
    *,
    r1: CheckOutcome,
    r2: CheckOutcome,
    gate_decision: Optional[ItemDecision],
    save_delivered: bool = False,
) -> ItemDecision:
    """Combine the gate's pre-save decision with R1, R2 and the outcome.

    ``save_delivered`` says the gate let a save through and its input was
    delivered. Then an outcome that reads "nothing was written" contradicts
    the delivery: the read-back can show only that the expected record is
    absent, not that nothing else was written. The outcome word stays the
    engine's; the item goes to a person and the switch turns off
    (``uncertain_result``).
    """

    uncertain = outcome_is_uncertain(report) or (
        save_delivered and report.transaction_outcome in NOTHING_WRITTEN
    )
    if gate_decision is not None:
        # The gate sent the item to a person before a save. An earlier save in
        # the same item may already have landed; Flow's outcome rules decide
        # what the customer reads, and an uncertain result still switches off.
        off = gate_decision.switch_off
        if off is None and uncertain:
            off = OffReason.UNCERTAIN_RESULT
        return ItemDecision(
            kind=gate_decision.kind,
            reason=gate_decision.reason,
            needs_person=True,
            counts_toward_problems=gate_decision.counts_toward_problems
            or off is OffReason.UNCERTAIN_RESULT,
            switch_off=off,
        )
    if not (r1.passed and r2.passed):
        engine = engine_refusal_decision(report, uncertain=uncertain)
        if engine is not None:
            return engine
    if r1.passed and r2.passed:
        return ItemDecision(
            kind=ItemDecisionKind.NO_PERSON,
            reason=None,
            needs_person=False,
            counts_toward_problems=False,
            switch_off=None,
        )
    if r1.passed:
        # "Saved and checked, but OpenAdapt had to work around a changed
        # screen." The outcome word stays VERIFIED.
        return ItemDecision(
            kind=ItemDecisionKind.PROBLEM,
            reason=OffReason.SCREEN_CHANGED.value,
            needs_person=True,
            counts_toward_problems=True,
            switch_off=OffReason.SCREEN_CHANGED,
        )
    reason = (report.transaction_outcome or "unknown_outcome").lower()
    return ItemDecision(
        kind=ItemDecisionKind.PROBLEM,
        reason=reason,
        needs_person=True,
        counts_toward_problems=True,
        switch_off=OffReason.UNCERTAIN_RESULT if uncertain else None,
    )


def too_many_problems(
    finished: list[SwitchLogEvent],
    thresholds: SwitchThresholds,
) -> bool:
    """3 counted problems in a row, or 5 within the last 20 items.

    ``finished`` holds the latest ``item_finished`` event per item, in order.
    Items sent to approval by the item checks or by a stale status are
    recorded with ``counts_toward_problems=False``; they neither count nor
    break a run of problems.
    """

    counted = [
        event
        for event in finished
        if event.counts_toward_problems or event.decision is ItemDecisionKind.NO_PERSON
    ]
    streak = 0
    for event in reversed(counted):
        if event.decision is ItemDecisionKind.NO_PERSON:
            break
        streak += 1
    if streak >= thresholds.problems_in_a_row:
        return True
    window = counted[-thresholds.problems_window :]
    problems = sum(
        1 for event in window if event.decision is not ItemDecisionKind.NO_PERSON
    )
    return problems >= thresholds.problems_in_window
