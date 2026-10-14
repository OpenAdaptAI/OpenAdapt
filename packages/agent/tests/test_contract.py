"""The four-outcome result contract and its safety rules.

Report fixtures copy the outcome fields of real openadapt-flow 1.35.1 runs on
the synthetic MockMed app (clean save, optimistic banner, timeout after save,
duplicate referral, missing referral). Only the fields the classifier reads are
kept.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from openadapt_agent.contract import (
    OUTCOMES,
    REASONS,
    RETRYABLE_REASONS,
    RUN_RESULT_SCHEMA,
    build_result,
    ledger_outcome,
    parse_failed_checks,
    reason_for_run,
)


def flow_report(execution: str, transaction: str | None, *, success: bool, eligible: bool):
    report = {
        "success": success,
        "execution_outcome": execution,
        "execution_profile": "standard",
        "production_eligible": eligible,
        "execution_completed": execution == "VERIFIED",
        "model_calls": 0,
        "external_network_calls": "observed",
        "results": [],
    }
    if transaction is not None:
        report["transaction_outcome"] = transaction
    report["outcome_envelope"] = {
        "outcome": execution,
        "profile": "standard",
        "production_eligible": eligible,
        "execution_completed": execution == "VERIFIED",
        "model_calls": 0,
        "external_network_calls": "observed",
    }
    return report


VERIFIED = flow_report("VERIFIED", "VERIFIED", success=True, eligible=True)
BANNER_LIE = flow_report("HALTED", "RECONCILIATION_REQUIRED", success=False, eligible=False)
DUPLICATE_REFERRAL = flow_report("HALTED", "HALTED_BEFORE_EFFECT", success=False, eligible=False)
MISSING_REFERRAL = flow_report("HALTED", "REJECTED_POLICY", success=False, eligible=False)
OPEN_PAUSE = {"status": "pending", "durably_paused": True, "category": "disambiguation"}


def test_production_verified_run_is_done_with_local_proof_not_an_error():
    reason = reason_for_run(exit_code=0, report=VERIFIED)
    result = build_result(
        reason, workflow="enter_referral", run_id="run-1", request_id="ref-1", mode="production"
    )
    assert result["outcome"] == "done"
    assert result["label"] == "Done and checked"
    assert result["proof"] == "local"
    assert result["record_changed"] == "yes"
    assert result["safe_to_retry"] is False
    assert "no signed receipt" in result["what_happened"]


def test_uncertain_delivery_is_never_reported_as_nothing_changed():
    attention = {"status": "pending", "durably_paused": True, "category": "postcondition"}
    reason = reason_for_run(exit_code=1, report=BANNER_LIE, attention=attention)
    result = build_result(
        reason,
        workflow="enter_referral",
        run_id="run-2",
        request_id="ref-2",
        mode="production",
        transaction_outcome="RECONCILIATION_REQUIRED",
    )
    assert result["outcome"] == "not_sure_if_saved"
    assert result["label"] == "Check the record"
    assert result["safe_to_retry"] is False
    assert result["record_changed"] == "unknown"
    assert result["next_step"] == "check_record"
    text = (result["what_happened"] + result["next_action"]).lower()
    assert "did not change" not in text
    assert "nothing was written" not in text
    assert "don't retry" in text


@pytest.mark.parametrize("timed_out,report", [(True, None), (True, VERIFIED)])
def test_timeout_after_start_is_not_sure_if_saved(timed_out, report):
    reason = reason_for_run(exit_code=None, report=report, timed_out=timed_out)
    assert reason == "timed_out"
    assert REASONS[reason].outcome == "not_sure_if_saved"
    assert REASONS[reason].safe_to_retry is False


def test_halt_before_effect_with_open_pause_needs_review():
    reason = reason_for_run(exit_code=1, report=DUPLICATE_REFERRAL, attention=OPEN_PAUSE)
    assert reason == "record_not_confirmed"
    spec = REASONS[reason]
    assert spec.outcome == "needs_review"
    assert spec.safe_to_retry is False
    assert spec.record_changed == "no"


def test_policy_rejection_with_open_pause_waits_for_the_person():
    attention = {"status": "pending", "durably_paused": True, "category": "identity"}
    reason = reason_for_run(exit_code=1, report=MISSING_REFERRAL, attention=attention)
    assert REASONS[reason].outcome == "needs_review"


def test_policy_rejection_without_a_pause_did_not_run_and_may_retry():
    reason = reason_for_run(exit_code=1, report=MISSING_REFERRAL)
    assert reason == "policy_refused"
    assert REASONS[reason].outcome == "did_not_run"
    assert reason in RETRYABLE_REASONS


def test_person_rejected_pause_ends_the_run_without_retry():
    attention = {"status": "rejected", "durably_paused": False, "category": "identity"}
    reason = reason_for_run(exit_code=1, report=DUPLICATE_REFERRAL, attention=attention)
    assert reason == "stopped_by_person"
    assert REASONS[reason].safe_to_retry is False
    assert reason not in RETRYABLE_REASONS


def test_rejecting_an_uncertain_run_keeps_it_uncertain():
    attention = {"status": "rejected", "durably_paused": False, "category": "effect_escalated"}
    reason = reason_for_run(exit_code=1, report=BANNER_LIE, attention=attention)
    assert REASONS[reason].outcome == "not_sure_if_saved"


def test_coarse_halted_alone_never_says_nothing_was_written():
    coarse = {"success": False, "execution_outcome": "HALTED"}
    reason = reason_for_run(exit_code=1, report=coarse)
    assert REASONS[reason].outcome == "not_sure_if_saved"
    assert REASONS[reason].record_changed == "unknown"


def test_inconsistent_verified_report_is_never_done():
    lying = dict(VERIFIED, success=False)
    assert reason_for_run(exit_code=0, report=lying) == "result_unreadable"
    nonzero = reason_for_run(exit_code=1, report=VERIFIED)
    assert REASONS[nonzero].outcome == "not_sure_if_saved"


def test_refusal_before_start_did_not_run_with_closed_checks():
    output = (
        "  [REFUSE] Certification passed: policy 'clinical-write' has 8 violations\n"
        "  [REFUSE] Encrypted bundle: plaintext Jane Roe bundle\n"
    )
    checks = parse_failed_checks(output)
    assert checks == ["not_certified", "not_encrypted"]
    reason = reason_for_run(exit_code=2, report=None, refused_before_start=True)
    result = build_result(
        reason,
        workflow="enter_referral",
        run_id="run-3",
        request_id="ref-3",
        mode="production",
        failed_checks=checks,
    )
    assert result["outcome"] == "did_not_run"
    assert result["failed_checks"] == ["not_certified", "not_encrypted"]
    assert "Jane Roe" not in json.dumps(result)


def test_pregate_refusal_text_maps_to_one_check():
    text = "run REFUSED: Standard and Regulated actuation requires a signed qualification admission."
    assert parse_failed_checks(text) == ["no_readiness_test"]
    assert parse_failed_checks("something new") == ["other"]


def test_process_that_never_started_is_a_platform_error():
    reason = reason_for_run(exit_code=None, report=None, process_started=False)
    assert reason == "platform_error"
    assert REASONS[reason].outcome == "did_not_run"


def test_every_result_validates_against_the_output_schema():
    for reason in REASONS:
        result = build_result(
            reason,
            workflow="w",
            run_id="run-x",
            request_id="req-x",
            mode="sandbox",
            sandbox={"case": "normal", "engine": "flow"},
        )
        jsonschema.validate(result, RUN_RESULT_SCHEMA)


def test_only_done_claims_a_save_and_only_proven_cases_allow_retry():
    for name, spec in REASONS.items():
        if spec.record_changed == "yes":
            assert spec.outcome == "done", name
        if spec.safe_to_retry:
            assert spec.record_changed == "no", name
            assert spec.outcome == "did_not_run", name
        if spec.outcome == "not_sure_if_saved":
            assert spec.safe_to_retry is False, name
            assert spec.next_step == "check_record", name
    assert set(OUTCOMES) <= {spec.outcome for spec in REASONS.values()}


def test_simulated_sandbox_done_is_labelled_simulated():
    result = build_result(
        "saved_and_checked",
        workflow="add_triage_note",
        run_id="run-4",
        request_id="demo-4",
        mode="sandbox",
        sandbox={"case": "normal", "engine": "simulated"},
    )
    assert result["proof"] == "simulated"
    assert result["what_happened"].startswith("Simulated sandbox result")
    assert result["sandbox"] == {"case": "normal", "engine": "simulated"}


def test_ledger_outcome_keeps_uncertain_results_uncertain():
    assert ledger_outcome("timed_out") == "RECONCILIATION_REQUIRED"
    assert ledger_outcome("interrupted") == "RECONCILIATION_REQUIRED"
    assert ledger_outcome("record_not_confirmed") == "HALTED_BEFORE_EFFECT"
    assert ledger_outcome("saved_and_checked", "VERIFIED") == "VERIFIED"
    assert ledger_outcome("invalid_input") is None


def test_unknown_reason_falls_back_to_uncertain():
    result = build_result(
        "made_up", workflow=None, run_id=None, request_id=None, mode="production"
    )
    assert result["reason"] == "result_unreadable"
    assert result["outcome"] == "not_sure_if_saved"
