"""Which problems turn the problems_only switch off, and how a person re-arms it.

Policy (coordinator decision, 2026-10-24):

- An item-input problem caught before anything was written (a missing or
  invalid value refused before the first step, an input item check, a real
  candidate in the app's duplicate list) sends that item to a person and
  leaves the switch on.
- A program or environment problem (a wrong write the record check caught, a
  failed identity check, a changed screen, any unnamed cause) turns it off.
  An unknown cause counts as a program problem.
- Re-arming takes one signed tap from a person with a pinned approver key,
  offered only after 10 consecutive verified items since the switch went off.
  It is never automatic, and the log records who signed and what they saw.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openadapt_flow.approval_mode.model import (
    CheckOutcome,
    ItemDecisionKind,
    OffReason,
)
from openadapt_flow.approval_mode.post import (
    INPUT_PROBLEM,
    PROGRAM_PROBLEM,
    decide_problems_only,
    problem_class,
)
from openadapt_flow.approval_mode.switch import (
    REARM_CLEAN_ITEMS,
    SwitchLogError,
    rearm,
    rearm_evidence,
    rearm_request,
    rearm_to_sign,
    sign_rearm,
    turn_off,
)
from openadapt_flow.ir import RunReport, StepResult
from tests._approval_mode_support import BUNDLE_DIGEST, make_record, open_log

FAIL = CheckOutcome(code="verified", passed=False, detail="not_verified")
PASS_R2 = CheckOutcome(code="whole_run_clean", passed=True)


def _report(
    transaction: str, halted: StepResult, execution: str = "HALTED"
) -> RunReport:
    return RunReport(
        workflow_name="referral",
        started_at="2026-10-24T10:00:00+00:00",
        execution_profile="regulated",
        execution_outcome=execution,
        execution_completed=False,
        transaction_outcome=transaction,
        bundle_content_digest=BUNDLE_DIGEST,
        results=[halted],
    )


def _decide(report: RunReport):
    return decide_problems_only(report, r1=FAIL, r2=PASS_R2, gate_decision=None)


INVALID_DATE = StepResult(
    step_id="<params>",
    intent="validate typed workflow parameters",
    ok=False,
    failure_category="governed_refusal",
    error="Typed workflow parameter(s) are not valid: parameter 'DOB' is not an "
    "ISO date — refusing to run; nothing was typed; run aborted",
)
DUPLICATE = StepResult(
    step_id="open_duplicate_check",
    intent="click 'Create New Patient'",
    ok=False,
    delivery_attempted=True,
    error="Postconditions failed for step 'open_duplicate_check': expected screen "
    "state not reached (semantic drift) — failed: no_duplicate_candidate (no "
    "listed row has this run's lname, DOB) — run aborted",
)
NOT_FOUND = StepResult(
    step_id="save",
    intent="click 'Save'",
    ok=False,
    delivery_attempted=False,
    error="Target not found for step 'save'",
)
CAUGHT_WRONG_WRITE = StepResult(
    step_id="save",
    intent="click 'Save'",
    ok=False,
    risk="irreversible",
    delivery_attempted=True,
    effect_verified=False,
    error="System-of-record effect verification HALTED step 'save'",
)


@pytest.mark.parametrize(
    "transaction, halted",
    [
        ("REJECTED_POLICY", INVALID_DATE),
        ("HALTED_BEFORE_EFFECT", INVALID_DATE),
        ("HALTED_BEFORE_EFFECT", DUPLICATE),
    ],
)
def test_an_input_problem_leaves_the_switch_on(transaction, halted) -> None:
    report = _report(transaction, halted)
    assert problem_class(report) == INPUT_PROBLEM
    decision = _decide(report)
    assert decision.needs_person
    assert not decision.counts_toward_problems
    assert decision.switch_off is None


def test_a_caught_wrong_write_turns_the_switch_off() -> None:
    report = _report("RECONCILIATION_REQUIRED", CAUGHT_WRONG_WRITE)
    assert problem_class(report) == PROGRAM_PROBLEM
    decision = _decide(report)
    assert decision.counts_toward_problems
    assert decision.switch_off is OffReason.UNCERTAIN_RESULT


def test_an_unnamed_stop_before_any_effect_is_a_program_problem() -> None:
    report = _report("HALTED_BEFORE_EFFECT", NOT_FOUND)
    assert problem_class(report) == PROGRAM_PROBLEM
    assert _decide(report).switch_off is OffReason.SCREEN_CHANGED


def test_a_duplicate_stop_after_a_delivered_save_is_a_program_problem() -> None:
    # The duplicate-list failure counts as an input problem only when nothing
    # that saves was delivered.
    report = _report("HALTED_BEFORE_EFFECT", DUPLICATE)
    report.results.insert(0, CAUGHT_WRONG_WRITE.model_copy(update={"ok": True}))
    assert problem_class(report) == PROGRAM_PROBLEM


def test_an_inconsistent_outcome_is_never_an_input_problem() -> None:
    # "Nothing was written" with an execution outcome that says otherwise.
    report = _report("HALTED_BEFORE_EFFECT", INVALID_DATE, execution="VERIFIED")
    assert problem_class(report) == PROGRAM_PROBLEM


# -- re-arming -------------------------------------------------------------------


def _keypair() -> tuple[Ed25519PrivateKey, dict[str, str]]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, {"approver-1": base64.b64encode(public).decode("ascii")}


def _finish(log, index: int, decision: ItemDecisionKind) -> None:
    ref = f"{index:064x}"
    log.append("item_started", run_ref=ref)
    log.append(
        "item_finished",
        run_ref=ref,
        decision=decision,
        counts_toward_problems=decision is ItemDecisionKind.PROBLEM,
    )


def _switched_off_log(tmp_path, clean: int):
    log = open_log(tmp_path, make_record())
    turn_off(log, OffReason.UNCERTAIN_RESULT)
    for index in range(clean):
        decision = (
            ItemDecisionKind.APPROVED if index % 2 else ItemDecisionKind.NO_PERSON
        )
        _finish(log, index, decision)
    return log


def _signed(log, private) -> object:
    request = rearm_request(log, cause="a malformed date of birth was saved wrong")
    return sign_rearm(
        log,
        request,
        approver_id="person-0001",
        key_id="approver-1",
        private_key=private,
    )


def test_a_signed_rearm_after_ten_verified_items_turns_the_switch_on(tmp_path) -> None:
    private, keys = _keypair()
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    event = rearm(log, _signed(log, private), approver_keys=keys)
    assert event.kind == "rearmed" and event.rearm is not None
    assert event.rearm.approver_id == "person-0001"
    assert event.rearm.cause.startswith("a malformed date")
    assert len(event.rearm.clean_run_refs) == REARM_CLEAN_ITEMS
    assert not log.state(approver_keys=keys).is_off
    # A reader without the pinned approver keys never honours it.
    assert log.state().is_off


def test_a_rearm_before_ten_verified_items_is_refused(tmp_path) -> None:
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS - 1)
    with pytest.raises(SwitchLogError, match="needs 10"):
        rearm_request(log, cause="x")


def test_a_problem_resets_the_verified_streak(tmp_path) -> None:
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    _finish(log, 99, ItemDecisionKind.PROBLEM)
    with pytest.raises(SwitchLogError, match="0 consecutive"):
        rearm_request(log, cause="x")


def test_a_rearm_without_a_valid_signature_is_refused(tmp_path) -> None:
    private, keys = _keypair()
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    signed = _signed(log, private)
    # Signed by a key that isn't pinned.
    other, _ = _keypair()
    with pytest.raises(SwitchLogError, match="registered approver key"):
        rearm(log, _signed(log, other), approver_keys={})
    # Tampered after signing: a different cause than the person saw.
    tampered = signed.model_copy(update={"cause": "nothing happened"})
    with pytest.raises(SwitchLogError, match="does not verify"):
        rearm(log, tampered, approver_keys=keys)
    # Signed by another person's key under this key id.
    forged = _signed(log, other)
    with pytest.raises(SwitchLogError, match="does not verify"):
        rearm(log, forged, approver_keys=keys)
    assert log.state(approver_keys=keys).is_off


def test_a_rearm_is_never_automatic_and_a_later_problem_turns_it_off_again(
    tmp_path,
) -> None:
    private, keys = _keypair()
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS + 5)
    # Fifteen clean items alone change nothing.
    assert log.state(approver_keys=keys).is_off
    rearm(log, _signed(log, private), approver_keys=keys)
    assert not log.state(approver_keys=keys).is_off
    turn_off(log, OffReason.SCREEN_CHANGED)
    state = log.state(approver_keys=keys)
    assert state.off_reason is OffReason.SCREEN_CHANGED
    with pytest.raises(SwitchLogError, match="0 consecutive"):
        rearm_request(log, cause="x")


def test_an_appended_rearm_event_with_a_bad_signature_is_ignored(tmp_path) -> None:
    # Someone with write access to the log appends a forged re-arm directly.
    private, keys = _keypair()
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    other, _ = _keypair()
    log.append("rearmed", rearm=_signed(log, other))
    assert log.state(approver_keys=keys).is_off


def test_a_key_held_in_a_keychain_signs_the_bytes_without_leaving_it(tmp_path) -> None:
    # Desktop signs inside its keychain: it gets the bytes, returns a signature.
    private, keys = _keypair()
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    request = rearm_request(log, cause="a malformed date of birth was saved wrong")
    fields, payload = rearm_to_sign(
        log, request, approver_id="person-0001", key_id="approver-1"
    )
    signature = base64.b64encode(private.sign(payload)).decode("ascii")
    rearm(log, rearm_evidence(fields, signature), approver_keys=keys)
    assert not log.state(approver_keys=keys).is_off


def test_registered_approver_keys_in_hex_verify_a_rearm(tmp_path, monkeypatch) -> None:
    """The principal registry's approvers (hex) are the gate's default keys."""

    from types import SimpleNamespace

    from openadapt_flow.approval_mode import switch
    from openadapt_flow.jit import authority

    private, keys = _keypair()
    hex_key = base64.b64decode(keys["approver-1"]).hex()
    monkeypatch.setattr(
        authority.PrincipalRegistry,
        "load",
        classmethod(
            lambda cls, key_dir: SimpleNamespace(approvers={"approver-1": hex_key})
        ),
    )
    registered = switch.registry_approver_keys(tmp_path)
    assert registered == {"approver-1": hex_key}
    log = _switched_off_log(tmp_path, REARM_CLEAN_ITEMS)
    rearm(log, _signed(log, private), approver_keys=registered)
    assert not log.state(approver_keys=registered).is_off


def test_an_unreadable_registry_gives_no_approver_keys(tmp_path, monkeypatch) -> None:
    from openadapt_flow.approval_mode import switch
    from openadapt_flow.jit import authority

    def broken(cls, key_dir):
        raise ValueError("unreadable")

    monkeypatch.setattr(authority.PrincipalRegistry, "load", classmethod(broken))
    assert switch.registry_approver_keys(tmp_path) == {}
