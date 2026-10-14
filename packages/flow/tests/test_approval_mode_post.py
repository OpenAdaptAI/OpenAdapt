"""Post-run checks R1 and R2, the item decision, and the problem-rate rule.

Every report here is synthetic and made up. The point is the safety rule: an
item counts as saved without a person only on a consistent ``VERIFIED``
outcome whose every save was confirmed on the first read by the bound
system-of-record verifier, over a run that used no repair, model call or
rescue. Everything else goes to a person.
"""

from __future__ import annotations

import pytest

from openadapt_flow.approval_mode.model import (
    ApprovalMode,
    ApprovalModeItemEvidence,
    CheckOutcome,
    ItemDecisionKind,
    OffReason,
    SwitchThresholds,
)
from openadapt_flow.approval_mode.post import (
    ItemDecision,
    check_verified,
    check_whole_run_clean,
    decide_problems_only,
    outcome_is_uncertain,
    too_many_problems,
)
from openadapt_flow.approval_mode.switch import SwitchLogEvent
from openadapt_flow.ir import (
    Anchor,
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    HealEvent,
    InterstitialActionResult,
    OutcomeContractCounts,
    Predicate,
    PredicateKind,
    Resolution,
    RunReport,
    StepResult,
)
from tests._approval_mode_support import BUNDLE_DIGEST, VERIFIER_IDENTITY, make_record

HASH = "sha256:" + "1" * 64


def _evidence(**overrides) -> EffectVerificationEvidence:
    fields = {
        "effect_contract_hash": HASH,
        "substrate": "rest",
        "verifier_identity": VERIFIER_IDENTITY,
        "verification_tier": 1,
        "initial_verdict": "confirmed",
        "final_verdict": "confirmed",
        "observed_effect": "present",
    }
    fields.update(overrides)
    return EffectVerificationEvidence(**fields)


def _save(**overrides) -> StepResult:
    fields = {
        "step_id": "save",
        "intent": "save referral",
        "ok": True,
        "risk": "irreversible",
        "effect_verified": True,
        "effect_contract_hashes": [HASH],
        "effect_evidence": [_evidence()],
        "resolution": Resolution(
            rung="template", point=(5, 5), confidence=0.99, elapsed_ms=1.0
        ),
    }
    fields.update(overrides)
    return StepResult(**fields)


def _verified_report(*results: StepResult, **overrides) -> RunReport:
    counts = OutcomeContractCounts(authorization=1, effect=1)
    fields = {
        "workflow_name": "referral",
        "started_at": "2026-10-12T10:00:00+00:00",
        "execution_profile": "regulated",
        "execution_outcome": "VERIFIED",
        "production_eligible": True,
        "execution_completed": True,
        "success": True,
        "transaction_outcome": "VERIFIED",
        "bundle_content_digest": BUNDLE_DIGEST,
        "outcome_envelope": ExecutionOutcomeEnvelope(
            outcome="VERIFIED",
            profile="regulated",
            production_eligible=True,
            execution_completed=True,
            required_contracts=counts,
            passed_contracts=counts,
            evidence_classes=["authorization", "effect_tier_1"],
        ),
        "results": list(results) or [_save()],
    }
    fields.update(overrides)
    return RunReport(**fields)


def _halted_report(transaction: str, execution: str = "HALTED") -> RunReport:
    return RunReport(
        workflow_name="referral",
        started_at="2026-10-12T10:00:00+00:00",
        execution_profile="regulated",
        execution_outcome=execution,
        execution_completed=False,
        transaction_outcome=transaction,
        bundle_content_digest=BUNDLE_DIGEST,
        results=[_save(ok=False, effect_verified=False)],
    )


RECORD = make_record(rung_floor={"save": "template"})


# -- R1 ------------------------------------------------------------------------


def test_r1_passes_on_a_first_read_system_of_record_confirmation():
    outcome = check_verified(_verified_report(), RECORD, saves_without_person=1)
    assert outcome.passed, outcome


@pytest.mark.parametrize(
    "evidence_change,detail",
    [
        ({"initial_verdict": "refuted"}, "read_back_not_confirmed_first_time"),
        ({"observed_effect": "conflicting"}, "read_back_not_confirmed_first_time"),
        ({"reconciliation_completed": True}, "read_back_reconciled"),
        ({"reconciliation_actions": 1}, "read_back_reconciled"),
        ({"verification_tier": 2}, "read_back_not_system_of_record"),
        ({"verification_tier": 3}, "read_back_not_system_of_record"),
        ({"verifier_identity": None}, "verifier_identity_missing"),
        ({"verifier_identity": "sha256:" + "9" * 64}, "verifier_identity_not_bound"),
    ],
)
def test_r1_refuses_any_read_back_that_does_not_match(evidence_change, detail):
    report = _verified_report(_save(effect_evidence=[_evidence(**evidence_change)]))
    outcome = check_verified(report, RECORD, saves_without_person=1)
    assert not outcome.passed
    assert outcome.detail == detail


def test_r1_refuses_a_save_without_a_read_back():
    report = _verified_report(_save(effect_evidence=[], effect_contract_hashes=[HASH]))
    assert check_verified(report, RECORD, saves_without_person=1).detail == (
        "save_without_read_back"
    )


def test_r1_refuses_a_read_back_that_covers_a_different_contract():
    report = _verified_report(
        _save(effect_contract_hashes=[HASH, "sha256:" + "2" * 64])
    )
    assert check_verified(report, RECORD, saves_without_person=1).detail == (
        "read_back_does_not_cover_save"
    )


def test_r1_refuses_an_approved_unverified_write():
    report = _verified_report(
        _save(), _save(step_id="other", effect_approved_unverified=True)
    )
    assert check_verified(report, RECORD, saves_without_person=1).detail == (
        "approved_unverified_write"
    )


def test_r1_refuses_when_no_save_passed_the_gate():
    assert check_verified(
        _verified_report(), RECORD, saves_without_person=0
    ).detail == ("no_save_checked")


def test_r1_fails_a_verified_execution_outcome_with_no_transaction_outcome():
    report = _verified_report()
    report.transaction_outcome = None
    outcome = check_verified(report, RECORD, saves_without_person=1)
    assert outcome.detail == "transaction_outcome_missing"
    assert outcome_is_uncertain(report)


def test_r1_fails_an_inconsistent_outcome_pair():
    report = _verified_report()
    report.execution_outcome = "COMPLETED_UNVERIFIED"
    assert check_verified(report, RECORD, saves_without_person=1).detail == (
        "outcome_inconsistent"
    )
    assert outcome_is_uncertain(report)


def test_r1_refuses_another_bundle_or_profile():
    report = _verified_report(bundle_content_digest="b" * 64)
    assert check_verified(report, RECORD, saves_without_person=1).detail == (
        "bundle_mismatch"
    )


@pytest.mark.parametrize(
    "transaction",
    ["RECONCILIATION_REQUIRED", "COMPLETED_UNVERIFIED", "ROLLED_BACK"],
)
def test_check_the_record_and_finished_not_checked_are_uncertain(transaction):
    execution = {
        "RECONCILIATION_REQUIRED": "HALTED",
        "COMPLETED_UNVERIFIED": "COMPLETED_UNVERIFIED",
        "ROLLED_BACK": "HALTED",
    }[transaction]
    report = _halted_report(transaction, execution)
    assert outcome_is_uncertain(report)
    assert not check_verified(report, RECORD, saves_without_person=1).passed


@pytest.mark.parametrize(
    "transaction",
    ["HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED", "FAILED_PLATFORM"],
)
def test_nothing_written_outcomes_are_not_uncertain(transaction):
    execution = "FAILED" if transaction == "FAILED_PLATFORM" else "HALTED"
    assert not outcome_is_uncertain(_halted_report(transaction, execution))


# -- R2 ------------------------------------------------------------------------


def _anchor() -> Anchor:
    return Anchor(template="t.png", region=(0, 0, 10, 10), click_point=(5, 5))


@pytest.mark.parametrize(
    "change,detail",
    [
        ({"drift_oracle_calls": 1}, "drift_rescue"),
        ({"postcondition_drift_rescues": ["Saved"]}, "drift_rescue"),
        (
            {
                "resolution": Resolution(
                    rung="grounder", point=(5, 5), confidence=0.9, elapsed_ms=1.0
                )
            },
            "grounder_rung",
        ),
        (
            {
                "resolution": Resolution(
                    rung="ocr", point=(5, 5), confidence=0.9, elapsed_ms=1.0
                )
            },
            "rung_below_floor",
        ),
        (
            {
                "heal": HealEvent(
                    step_id="save",
                    rung_used="ocr",
                    old_anchor=_anchor(),
                    new_anchor=_anchor(),
                )
            },
            "repair_applied",
        ),
        (
            {
                "interstitial_actions": [
                    InterstitialActionResult(
                        interstitial="session-timeout",
                        action="key",
                        key="Escape",
                        expected_clearance=Predicate(
                            kind=PredicateKind.TEXT_ABSENT, text="Session"
                        ),
                        before_frame_sha256="0" * 64,
                    )
                ]
            },
            "new_dialog_dismissal",
        ),
    ],
)
def test_r2_refuses_any_workaround_anywhere_in_the_run(change, detail):
    report = _verified_report(_save(**change))
    outcome = check_whole_run_clean(report, RECORD)
    assert not outcome.passed
    assert outcome.detail == detail


def test_r2_refuses_model_calls_and_repairs_at_run_level():
    with_model = _verified_report()
    with_model.model_calls = 1
    assert check_whole_run_clean(with_model, RECORD).detail == "model_call"
    assert check_whole_run_clean(_verified_report(heal_count=1), RECORD).detail == (
        "repair_applied"
    )


def test_r2_refuses_a_step_with_no_passing_case_rung():
    record = make_record(rung_floor={})
    assert check_whole_run_clean(_verified_report(), record).detail == (
        "rung_not_seen_in_passing_cases"
    )


def test_r2_accepts_declared_dialogs_and_equal_or_stronger_rungs():
    record = make_record(
        rung_floor={"save": "ocr"},
        allowed_interstitials={"save": ("session-timeout",)},
    )
    report = _verified_report(
        _save(
            interstitial_actions=[
                InterstitialActionResult(
                    interstitial="session-timeout",
                    action="key",
                    key="Escape",
                    expected_clearance=Predicate(
                        kind=PredicateKind.TEXT_ABSENT, text="Session"
                    ),
                    before_frame_sha256="0" * 64,
                )
            ]
        )
    )
    assert check_whole_run_clean(report, record).passed


# -- the decision ----------------------------------------------------------------

PASS_R1 = CheckOutcome(code="verified", passed=True)
PASS_R2 = CheckOutcome(code="whole_run_clean", passed=True)


def test_both_checks_pass_means_no_person():
    decision = decide_problems_only(
        _verified_report(), r1=PASS_R1, r2=PASS_R2, gate_decision=None
    )
    assert decision.kind is ItemDecisionKind.NO_PERSON
    assert not decision.needs_person


def test_r2_failure_keeps_done_and_checked_but_goes_to_a_person():
    report = _verified_report()
    decision = decide_problems_only(
        report,
        r1=PASS_R1,
        r2=CheckOutcome(code="whole_run_clean", passed=False, detail="drift_rescue"),
        gate_decision=None,
    )
    assert decision.kind is ItemDecisionKind.PROBLEM
    assert decision.needs_person
    assert decision.switch_off is OffReason.SCREEN_CHANGED
    # The outcome word is never changed by the approval mode.
    assert report.transaction_outcome == "VERIFIED"


def test_check_the_record_switches_off_with_uncertain_result():
    report = _halted_report("RECONCILIATION_REQUIRED")
    decision = decide_problems_only(
        report,
        r1=check_verified(report, RECORD, saves_without_person=1),
        r2=PASS_R2,
        gate_decision=None,
    )
    assert decision.needs_person
    assert decision.switch_off is OffReason.UNCERTAIN_RESULT
    assert decision.reason == "reconciliation_required"


def test_a_stop_that_wrote_nothing_is_a_counted_problem_without_switch_off():
    report = _halted_report("HALTED_BEFORE_EFFECT")
    decision = decide_problems_only(
        report,
        r1=check_verified(report, RECORD, saves_without_person=0),
        r2=PASS_R2,
        gate_decision=None,
    )
    assert decision.kind is ItemDecisionKind.PROBLEM
    assert decision.counts_toward_problems
    assert decision.switch_off is None


def test_gate_decision_wins_and_an_uncertain_earlier_save_still_switches_off():
    gate = ItemDecision(
        kind=ItemDecisionKind.APPROVAL_REQUIRED,
        reason="switch_on",
        needs_person=True,
        counts_toward_problems=False,
        switch_off=None,
    )
    decision = decide_problems_only(
        _halted_report("RECONCILIATION_REQUIRED"),
        r1=PASS_R1,
        r2=PASS_R2,
        gate_decision=gate,
    )
    assert decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert decision.switch_off is OffReason.UNCERTAIN_RESULT
    assert decision.needs_person


# -- the evidence block refuses a no-person claim without its checks -------------


def _passing_pre() -> list[CheckOutcome]:
    return [
        CheckOutcome(code=code, passed=True, step_id="save")
        for code in (
            "switch_on",
            "signed_in_user",
            "right_record",
            "same_screens",
            "record_check_ready",
        )
    ]


def test_evidence_accepts_a_fully_checked_no_person_item():
    ApprovalModeItemEvidence(
        mode=ApprovalMode.PROBLEMS_ONLY,
        pre_checks=_passing_pre(),
        post_checks=[PASS_R1, PASS_R2],
        saves_without_person=1,
        decision=ItemDecisionKind.NO_PERSON,
        needs_person=False,
        switch_log_recorded=True,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"mode": ApprovalMode.EACH_ITEM},
        {"mode": ApprovalMode.NOT_GATED},
        {"post_checks": [PASS_R1]},
        {
            "post_checks": [
                PASS_R1,
                CheckOutcome(code="whole_run_clean", passed=False, detail="x"),
            ]
        },
        {"pre_checks": []},
        {"saves_without_person": 0},
        {"needs_person": True},
        {"switch_off_reason": OffReason.SCREEN_CHANGED},
        {"switch_log_recorded": False},
    ],
)
def test_evidence_refuses_a_no_person_claim_missing_any_check(change):
    fields = {
        "mode": ApprovalMode.PROBLEMS_ONLY,
        "pre_checks": _passing_pre(),
        "post_checks": [PASS_R1, PASS_R2],
        "saves_without_person": 1,
        "decision": ItemDecisionKind.NO_PERSON,
        "needs_person": False,
        "switch_log_recorded": True,
    }
    fields.update(change)
    with pytest.raises(ValueError):
        ApprovalModeItemEvidence(**fields)


def test_evidence_refuses_a_problem_that_needs_no_person():
    with pytest.raises(ValueError):
        ApprovalModeItemEvidence(
            mode=ApprovalMode.PROBLEMS_ONLY,
            decision=ItemDecisionKind.PROBLEM,
            needs_person=False,
        )


# -- too many problems ------------------------------------------------------------


def _finished(seq: int, decision: ItemDecisionKind, counts: bool) -> SwitchLogEvent:
    return SwitchLogEvent(
        seq=seq,
        kind="item_finished",
        switch_id="switch-test-0001",
        at="2026-10-12T10:00:00+00:00",
        run_ref=f"{seq:064x}",
        decision=decision,
        counts_toward_problems=counts,
        prev_sha256="sha256:" + "0" * 64,
        sha256="sha256:" + "0" * 64,
    )


NO = ItemDecisionKind.NO_PERSON
PROBLEM = ItemDecisionKind.PROBLEM
APPROVAL = ItemDecisionKind.APPROVAL_REQUIRED


def test_three_counted_problems_in_a_row_switch_off():
    events = [
        _finished(1, NO, False),
        *(_finished(i, PROBLEM, True) for i in (2, 3, 4)),
    ]
    assert too_many_problems(events, SwitchThresholds())
    assert not too_many_problems(events[:-1], SwitchThresholds())


def test_items_sent_to_approval_by_item_checks_do_not_count_or_break_a_run():
    events = [
        _finished(1, PROBLEM, True),
        _finished(2, APPROVAL, False),
        _finished(3, PROBLEM, True),
        _finished(4, APPROVAL, False),
    ]
    assert not too_many_problems(events, SwitchThresholds())
    events.append(_finished(5, PROBLEM, True))
    assert too_many_problems(events, SwitchThresholds())


def test_five_problems_in_the_last_twenty_switch_off():
    pattern = [PROBLEM, PROBLEM, NO, NO] * 2 + [PROBLEM] + [NO] * 11
    events = [_finished(i + 1, kind, kind is PROBLEM) for i, kind in enumerate(pattern)]
    assert len(events) == 20
    assert sum(1 for e in events if e.decision is PROBLEM) == 5
    assert too_many_problems(events, SwitchThresholds())
    # The oldest problem falls out of the window after one more clean item.
    events.append(_finished(21, NO, False))
    assert not too_many_problems(events, SwitchThresholds())


def test_engine_refusals_map_to_the_gate_codes():
    from openadapt_flow.approval_mode.post import engine_refusal_decision
    from openadapt_flow.ir import SafetyRefusalEvidence

    def refused(stage, code):
        return _halted_report("HALTED_BEFORE_EFFECT").model_copy(
            update={
                "results": [
                    _save(
                        ok=False,
                        effect_verified=False,
                        safety_refusal_evidence=SafetyRefusalEvidence(
                            stage=stage, code=code, detector_input_sha256="0" * 64
                        ),
                    )
                ]
            }
        )

    lost = engine_refusal_decision(
        refused("effect_strength", "effect_strength_insufficient"), uncertain=False
    )
    assert lost.kind is ItemDecisionKind.STOP_BEFORE_SAVING
    assert lost.switch_off is OffReason.RECORD_CHECK_LOST
    identity = engine_refusal_decision(
        refused("identity_verification", "identity_unverifiable"), uncertain=False
    )
    assert identity.reason == "right_record"
    assert identity.switch_off is None
    assert (
        engine_refusal_decision(_halted_report("HALTED_BEFORE_EFFECT"), uncertain=False)
        is None
    )
