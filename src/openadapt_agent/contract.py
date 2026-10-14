"""The partner result contract: one shape, four outcomes, plain words.

Every agent-facing result in this package is derived here: ``run_workflow``,
``get_run``, the deprecated ``run_<id>`` tools, ``get_run_report``, and the
Agent Skill text. A calling agent decides one thing from a result: move on,
wait for a person, have a person check the record, or fix something and try
again. The four outcomes map one-to-one onto those decisions.

Safety rules this module enforces by construction:

- The plain result is a pure function of Flow's ``transaction_outcome``, the
  process facts (did Flow start, did it exit cleanly, did it time out), the
  state of any durable pause, and a consistency check of the persisted report.
  The coarse ``HALTED`` label alone never selects a result.
- Only ``done`` says the change was saved, and only a consistent ``VERIFIED``
  report can produce it. How strong the proof is (``local``, ``sealed``, or
  ``simulated`` in the sandbox) is a separate field. A verified write is never
  reported as an error.
- ``record_changed: "no"`` and ``safe_to_retry: true`` appear only for results
  whose evidence proves nothing was written. Uncertain delivery (including a
  timeout after Flow started) is ``not_sure_if_saved`` with
  ``safe_to_retry: false``.
- A run with an open durable pause is ``needs_review``: a person holds the
  decision, so the caller must not start the same work again.
- Every sentence is fixed copy keyed by a closed reason code. No observed screen
  text, recorded value, input value, or local path can reach a result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "CONTRACT_VERSION",
    "FAILED_CHECKS",
    "LABELS",
    "MODES",
    "NEXT_STEPS",
    "OUTCOMES",
    "PROOF",
    "REASONS",
    "RETRYABLE_REASONS",
    "RUNNING",
    "RUN_RESULT_SCHEMA",
    "TRANSACTION_OUTCOMES",
    "TRANSACTION_REASONS",
    "WORKFLOW_LIST_SCHEMA",
    "Reason",
    "build_result",
    "ledger_outcome",
    "parse_failed_checks",
    "pause_state",
    "reason_for_run",
    "report_consistency",
]

#: Version of the result shape. Bump only for an incompatible change.
CONTRACT_VERSION = "openadapt.run-result/v1"

#: The four terminal outcomes, in the order a reader should learn them.
OUTCOMES = ("done", "needs_review", "not_sure_if_saved", "did_not_run")
#: The one non-terminal value. ``get_run`` resolves it later.
RUNNING = "running"
#: Who the server is serving. ``sandbox`` results come from a synthetic app.
MODES = ("sandbox", "attended", "production")
#: Closed machine-readable next steps, mirrored by the plain ``next_action``.
NEXT_STEPS = (
    "none",
    "wait_for_person",
    "check_record",
    "fix_inputs",
    "fix_setup",
    "retry",
    "call_get_run",
)
#: How strong the evidence behind ``done`` is. ``sealed`` means a signed
#: receipt exists; ``local`` means the record check ran on this computer and
#: its evidence stays here; ``simulated`` means a sandbox result produced
#: without opening any app; ``none`` means no saved change is being claimed.
PROOF = ("sealed", "local", "simulated", "none")
#: What is known about the target record after this run.
RECORD_CHANGED = ("yes", "no", "unknown")

#: Flow ``TransactionOutcome`` values this bridge understands.
TRANSACTION_OUTCOMES = frozenset(
    {
        "VERIFIED",
        "HALTED_BEFORE_EFFECT",
        "RECONCILIATION_REQUIRED",
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    }
)
#: Flow transaction outcomes that prove no business effect occurred.
_NO_EFFECT_OUTCOMES = frozenset(
    {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED", "FAILED_PLATFORM"}
)
_EXECUTION_OUTCOMES = frozenset(
    {"VERIFIED", "COMPLETED_UNVERIFIED", "HALTED", "FAILED", "ROLLED_BACK"}
)

#: Short plain label per outcome group, for a UI badge or a log line.
LABELS = {
    "done": "Done and checked",
    "needs_review": "Stopped before saving",
    "not_sure_if_saved": "Check the record",
    "did_not_run": "Didn't run",
    RUNNING: "Running",
}


@dataclass(frozen=True)
class Reason:
    """Fixed copy and semantics for one closed reason code."""

    outcome: str
    safe_to_retry: bool
    record_changed: str
    next_step: str
    what_happened: str
    next_action: str
    label: Optional[str] = None


_CHECK_RECORD = (
    "Have a person check the record in the app before anything runs again. "
    "Don't retry this request."
)
_WAIT_FOR_PERSON = (
    "A person reviews this run on the computer that ran it. Call get_run "
    "later to see their decision. Don't start another run for this work "
    "until they finish."
)

REASONS: dict[str, Reason] = {
    # -- done ------------------------------------------------------------
    "saved_and_checked": Reason(
        "done",
        False,
        "yes",
        "none",
        "Saved the change and read the record back to confirm it.",
        "Nothing more to do for this request. Don't run it again for the same work.",
    ),
    # -- needs_review: stopped before saving, nothing written -------------
    "unexpected_screen": Reason(
        "needs_review",
        False,
        "no",
        "wait_for_person",
        "Stopped before saving. The app showed a screen the workflow didn't "
        "expect, so nothing was written.",
        _WAIT_FOR_PERSON,
    ),
    "record_not_confirmed": Reason(
        "needs_review",
        False,
        "no",
        "wait_for_person",
        "Stopped before saving. It couldn't confirm it had the right record "
        "open, so nothing was written.",
        _WAIT_FOR_PERSON,
    ),
    "person_needed": Reason(
        "needs_review",
        False,
        "no",
        "wait_for_person",
        "Stopped before saving. The app needs a person, for example to sign "
        "in, so nothing was written.",
        _WAIT_FOR_PERSON,
    ),
    "record_check_not_set_up": Reason(
        "needs_review",
        False,
        "no",
        "wait_for_person",
        "Stopped before saving. This workflow's record check isn't set up "
        "yet, so nothing was written.",
        _WAIT_FOR_PERSON,
    ),
    "stopped_for_review": Reason(
        "needs_review",
        False,
        "no",
        "wait_for_person",
        "Stopped before saving because something didn't match, so nothing was written.",
        _WAIT_FOR_PERSON,
    ),
    # -- not_sure_if_saved: a person checks the record --------------------
    "save_not_confirmed": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "OpenAdapt can't confirm whether the change was saved. It may have gone through.",
        _CHECK_RECORD,
    ),
    "not_checked": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "It finished the steps but didn't check the saved record, so the "
        "change may or may not be saved.",
        _CHECK_RECORD,
        label="Finished, not checked",
    ),
    "change_reversed": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "OpenAdapt found an extra change and reversed it. The record still "
        "needs a person to confirm it's right.",
        _CHECK_RECORD,
    ),
    "timed_out": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "The run didn't finish in time. The change may or may not be saved.",
        _CHECK_RECORD,
    ),
    "result_unreadable": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "The run ended without a result OpenAdapt can trust. The change may "
        "or may not be saved.",
        _CHECK_RECORD,
    ),
    "interrupted": Reason(
        "not_sure_if_saved",
        False,
        "unknown",
        "check_record",
        "The run stopped before it reported a result, for example because "
        "the server closed. The change may or may not be saved.",
        _CHECK_RECORD,
    ),
    # -- did_not_run: proven nothing written -------------------------------
    "invalid_input": Reason(
        "did_not_run",
        True,
        "no",
        "fix_inputs",
        "Didn't start because some inputs aren't valid. Nothing was written.",
        "Fix the inputs listed in invalid_inputs, then send the request again. "
        "You can reuse the same request_id.",
    ),
    "unknown_workflow": Reason(
        "did_not_run",
        True,
        "no",
        "fix_inputs",
        "Didn't start because there's no workflow with that name here. "
        "Nothing was written.",
        "Call list_workflows and use one of the names it returns.",
    ),
    "workflow_unavailable": Reason(
        "did_not_run",
        True,
        "no",
        "fix_setup",
        "Didn't start because this workflow can't be loaded on this computer. "
        "Nothing was written.",
        "Ask the operator to check the workflow files on this computer, then "
        "send the same request again.",
    ),
    "runs_disabled": Reason(
        "did_not_run",
        True,
        "no",
        "fix_setup",
        "Didn't start because this server only lists workflows. Nothing was written.",
        "Ask the operator to start the server with --mode production or "
        "--mode attended, then send the same request again.",
    ),
    "not_ready_to_run": Reason(
        "did_not_run",
        True,
        "no",
        "fix_setup",
        "Didn't start because the workflow hasn't passed the checks it needs "
        "before it can run here. Nothing was written.",
        "Ask the operator to finish setup (see failed_checks), then send the "
        "same request again with the same request_id.",
    ),
    "policy_refused": Reason(
        "did_not_run",
        True,
        "no",
        "fix_setup",
        "Didn't start because a policy, sign-off, or environment check "
        "refused it. Nothing was written.",
        "Ask the operator why this run isn't allowed, then send the same "
        "request again with the same request_id.",
    ),
    "canceled": Reason(
        "did_not_run",
        True,
        "no",
        "retry",
        "The run was canceled before it changed anything. Nothing was written.",
        "If the work is still needed, send the same request again with the "
        "same request_id.",
    ),
    "platform_error": Reason(
        "did_not_run",
        True,
        "no",
        "retry",
        "OpenAdapt hit a problem on this computer before it changed anything. "
        "Nothing was written.",
        "Try again with the same request_id. If it keeps happening, tell the operator.",
    ),
    "app_unreachable": Reason(
        "did_not_run",
        True,
        "no",
        "retry",
        "Didn't start because the app isn't reachable from this computer. "
        "Nothing was written.",
        "Try again with the same request_id once the app is back. If it keeps "
        "happening, tell the operator.",
    ),
    "stopped_by_person": Reason(
        "did_not_run",
        False,
        "no",
        "none",
        "A person ended this run before it saved anything. Nothing was written.",
        "Don't retry this request. If the work is still needed, a person "
        "decides how, and a new attempt uses a new request_id.",
    ),
    "request_id_conflict": Reason(
        "did_not_run",
        False,
        "no",
        "fix_inputs",
        "Didn't start because this request_id was already used for a "
        "different request. Nothing new ran.",
        "Use a new request_id for new work. To see the earlier result, call "
        "get_run with first_run_id.",
    ),
    "retry_limit": Reason(
        "did_not_run",
        False,
        "no",
        "fix_setup",
        "Didn't start because this request_id was already tried too many "
        "times. Nothing new ran.",
        "Ask the operator to look at the earlier runs for this request before "
        "trying again.",
    ),
    # -- not terminal --------------------------------------------------------
    "running": Reason(
        RUNNING,
        False,
        "unknown",
        "call_get_run",
        "Still running.",
        "Call get_run with this run_id to get the result. Don't start the "
        "same work again.",
    ),
}

#: Results that let the same ``request_id`` start a fresh attempt. Each one
#: proves nothing was written AND says the retry may now succeed.
RETRYABLE_REASONS = frozenset(
    {
        "not_ready_to_run",
        "policy_refused",
        "canceled",
        "platform_error",
        "app_unreachable",
        "workflow_unavailable",
        "runs_disabled",
    }
)

#: Closed vocabulary for why Flow refused to start a run.
FAILED_CHECKS: dict[str, str] = {
    "no_readiness_test": "The workflow has no signed readiness test for this setup.",
    "readiness_test_invalid": "The readiness test is invalid, expired, or doesn't match this run.",
    "not_certified": "The workflow isn't certified under the required policy.",
    "profile_not_met": "The workflow doesn't meet the selected run profile.",
    "right_record_check_missing": "A step that needs a right-record check doesn't have one.",
    "record_check_missing": "A step that saves doesn't have a record check.",
    "approval_missing": "A write without a record check has no approval.",
    "popup_handling_not_approved": "Automatic pop-up handling isn't approved.",
    "not_encrypted": "The workflow files must be encrypted for this setup.",
    "integrity_check_failed": "The workflow files changed or their integrity check failed.",
    "workflow_unloadable": "The workflow files couldn't be loaded safely.",
    "app_target_missing": "The run setup doesn't say which app or window to use.",
    "dispatch_mismatch": "The hosted request doesn't match this exact run.",
    "url_override_not_allowed": "This server doesn't let callers change the app address.",
    "other": "Another setup check refused the run. The operator can see which one.",
}

# Flow prints one ``[REFUSE] <title>: ...`` line per failing admission gate.
_GATE_TITLE_CHECKS = (
    ("Execution profile", "profile_not_met"),
    ("Certification passed", "not_certified"),
    ("Identity coverage", "right_record_check_missing"),
    ("Effect coverage", "record_check_missing"),
    ("Approval fallback", "approval_missing"),
    ("Interstitial admission", "popup_handling_not_approved"),
    ("Encrypted bundle", "not_encrypted"),
    ("Sealed manifest", "integrity_check_failed"),
)
# Refusals Flow prints before its gate report. Order matters: the first
# marker found wins, so specific phrases come before general ones.
_PREGATE_CHECKS = (
    ("signed qualification admission", "no_readiness_test"),
    ("qualification authority is invalid", "readiness_test_invalid"),
    ("qualification campaign permit", "readiness_test_invalid"),
    ("bundle could not be loaded safely", "workflow_unloadable"),
    ("explicit execution surface", "app_target_missing"),
    ("execution surface", "app_target_missing"),
    ("cannot weaken a named profile", "not_encrypted"),
    ("requires encrypted bundles", "not_encrypted"),
    ("managed dispatch", "dispatch_mismatch"),
    ("profile", "profile_not_met"),
)
_REFUSE_LINE = re.compile(r"^\s*\[REFUSE\]\s+([^:]+):", re.MULTILINE)


def parse_failed_checks(text: str) -> list[str]:
    """Map Flow's refusal output to closed check codes. Never returns text."""
    found: list[str] = []
    if not isinstance(text, str):
        return ["other"]
    for title in _REFUSE_LINE.findall(text):
        for prefix, code in _GATE_TITLE_CHECKS:
            if title.strip().startswith(prefix) and code not in found:
                found.append(code)
    if not found:
        lowered = text.lower()
        for marker, code in _PREGATE_CHECKS:
            if marker.lower() in lowered:
                found.append(code)
                break
    return found or ["other"]


def report_consistency(report: object) -> tuple[bool, Optional[str], Optional[str]]:
    """Return ``(consistent, execution_outcome, transaction_outcome)``.

    A report is consistent only when its precise fields agree with each other
    and with the legacy ``success`` flag. An inconsistent report can never
    produce ``done`` or a "nothing was written" result.
    """
    if not isinstance(report, dict):
        return False, None, None
    execution = report.get("execution_outcome")
    transaction = report.get("transaction_outcome")
    if execution is not None and (
        not isinstance(execution, str) or execution not in _EXECUTION_OUTCOMES
    ):
        return False, None, None
    if transaction is not None and (
        not isinstance(transaction, str) or transaction not in TRANSACTION_OUTCOMES
    ):
        return False, execution, None
    success = report.get("success")
    if execution is not None and not isinstance(success, bool):
        return False, execution, transaction
    if execution == "VERIFIED" and success is not True:
        return False, execution, transaction
    if execution in {"HALTED", "FAILED", "ROLLED_BACK"} and success is True:
        return False, execution, transaction
    if transaction is not None and execution is not None:
        if (transaction == "VERIFIED") != (execution == "VERIFIED"):
            return False, execution, transaction
    profile = report.get("execution_profile")
    if profile is not None and profile not in {"demo", "standard", "regulated"}:
        return False, execution, transaction
    eligible = report.get("production_eligible")
    if eligible is not None and not isinstance(eligible, bool):
        return False, execution, transaction
    if eligible is True and (
        execution != "VERIFIED" or profile not in {"standard", "regulated"}
    ):
        return False, execution, transaction
    envelope = report.get("outcome_envelope")
    if envelope is not None:
        if not isinstance(envelope, dict) or envelope.get("outcome") != execution:
            return False, execution, transaction
        for report_key, envelope_key in (
            ("execution_profile", "profile"),
            ("production_eligible", "production_eligible"),
            ("execution_completed", "execution_completed"),
            ("model_calls", "model_calls"),
            ("external_network_calls", "external_network_calls"),
        ):
            if report_key in report and envelope.get(envelope_key) != report.get(report_key):
                return False, execution, transaction
    return True, execution, transaction


# Flow's PHI-safe Needs Attention categories -> needs_review reasons.
_ATTENTION_REASONS = {
    "identity": "record_not_confirmed",
    "disambiguation": "record_not_confirmed",
    "human_required": "person_needed",
    "effect_unverifiable": "record_check_not_set_up",
    "placeholder_effect": "record_check_not_set_up",
    "unmet_guard": "unexpected_screen",
    "postcondition": "unexpected_screen",
    "resolution": "unexpected_screen",
}
_NO_EFFECT_DID_NOT_RUN = {
    "REJECTED_POLICY": "policy_refused",
    "CANCELED": "canceled",
    "FAILED_PLATFORM": "platform_error",
}
#: Reason for each non-VERIFIED transaction outcome when nothing else is known.
#: Proven-no-effect outcomes assume a pause may be open, so they wait for a
#: person rather than invite a retry; ``reason_for_run`` refines them.
TRANSACTION_REASONS = {
    "RECONCILIATION_REQUIRED": "save_not_confirmed",
    "COMPLETED_UNVERIFIED": "not_checked",
    "ROLLED_BACK": "change_reversed",
    **{outcome: "stopped_for_review" for outcome in _NO_EFFECT_OUTCOMES},
}


def pause_state(attention: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Reduce a Flow Needs Attention projection to ``open``, ``rejected``, or None."""
    if not isinstance(attention, Mapping):
        return None
    if attention.get("status") == "rejected":
        return "rejected"
    if attention.get("durably_paused") is True:
        return "open"
    return None


def reason_for_run(
    *,
    exit_code: Optional[int],
    report: object,
    timed_out: bool = False,
    process_started: Optional[bool] = True,
    refused_before_start: bool = False,
    attention: Optional[Mapping[str, Any]] = None,
) -> str:
    """Pick the one closed reason code for a finished run attempt.

    ``refused_before_start`` is True when Flow's admission refused the run
    (exit code 2) and wrote no report. ``process_started`` is False only when
    Flow provably never started. ``attention`` is Flow's PHI-safe Needs
    Attention projection for the run, when one exists.
    """
    report_present = isinstance(report, dict)
    if refused_before_start and not report_present:
        return "not_ready_to_run"
    if timed_out:
        return "timed_out"
    if not report_present:
        return "platform_error" if process_started is False else "result_unreadable"
    consistent, execution, tx = report_consistency(report)
    if not consistent:
        return "result_unreadable"
    if tx is None:
        # Older report shape without a transaction outcome. Only a precise,
        # consistent VERIFIED can be done; anything else is unproven.
        if execution == "VERIFIED" and exit_code in (0, None):
            return "saved_and_checked"
        if execution is None and report.get("success") is True and exit_code in (0, None):
            return "not_checked"
        if execution == "COMPLETED_UNVERIFIED":
            return "not_checked"
        return "result_unreadable"
    if tx == "VERIFIED":
        return "saved_and_checked" if exit_code in (0, None) else "result_unreadable"
    if tx in _NO_EFFECT_OUTCOMES:
        state = pause_state(attention)
        if state == "rejected":
            return "stopped_by_person"
        category = attention.get("category") if isinstance(attention, Mapping) else None
        if state == "open" or tx == "HALTED_BEFORE_EFFECT":
            return _ATTENTION_REASONS.get(category or "", "stopped_for_review")
        return _NO_EFFECT_DID_NOT_RUN[tx]
    return TRANSACTION_REASONS.get(tx, "result_unreadable")


#: Flow ledger value recorded for each reason, so the idempotency ledger keeps
#: a terminal outcome for every attempt. Uncertain results stay uncertain.
_LEDGER_OUTCOMES = {
    "saved_and_checked": "VERIFIED",
    "save_not_confirmed": "RECONCILIATION_REQUIRED",
    "not_checked": "COMPLETED_UNVERIFIED",
    "change_reversed": "ROLLED_BACK",
    "timed_out": "RECONCILIATION_REQUIRED",
    "result_unreadable": "RECONCILIATION_REQUIRED",
    "interrupted": "RECONCILIATION_REQUIRED",
    "canceled": "CANCELED",
    "platform_error": "FAILED_PLATFORM",
    "app_unreachable": "FAILED_PLATFORM",
    "not_ready_to_run": "REJECTED_POLICY",
    "policy_refused": "REJECTED_POLICY",
    "workflow_unavailable": "FAILED_PLATFORM",
    "runs_disabled": "REJECTED_POLICY",
    "stopped_by_person": "HALTED_BEFORE_EFFECT",
}


def ledger_outcome(reason: str, transaction_outcome: Optional[str] = None) -> Optional[str]:
    """Return the Flow ``TransactionOutcome`` value to record for a reason."""
    if transaction_outcome in TRANSACTION_OUTCOMES:
        return transaction_outcome
    spec = REASONS.get(reason)
    if spec is not None and spec.outcome == "needs_review":
        return "HALTED_BEFORE_EFFECT"
    return _LEDGER_OUTCOMES.get(reason)


_SANDBOX_NOTE = " This was a sandbox run on a synthetic app; no real record was touched."
_SIMULATED_PREFIX = "Simulated sandbox result (no app was opened): "


def build_result(
    reason: str,
    *,
    workflow: Optional[str],
    run_id: Optional[str],
    request_id: Optional[str],
    mode: str,
    proof: Optional[str] = None,
    execution_outcome: Optional[str] = None,
    transaction_outcome: Optional[str] = None,
    failed_checks: Iterable[str] = (),
    invalid_inputs: Iterable[Mapping[str, str]] = (),
    needs_attention_id: Optional[str] = None,
    first_run_id: Optional[str] = None,
    replayed: bool = False,
    seconds: Optional[float] = None,
    model_calls: Optional[int] = None,
    sandbox: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Render one PHI-safe result object from a closed reason code."""
    spec = REASONS.get(reason)
    if spec is None:
        reason = "result_unreadable"
        spec = REASONS[reason]
    simulated = bool(sandbox) and sandbox.get("engine") == "simulated"
    if spec.outcome == "done":
        if simulated:
            proof = "simulated"
        elif proof not in {"sealed", "local"}:
            proof = "local"
    else:
        proof = "none"
    what_happened = spec.what_happened
    if spec.outcome == "done" and proof == "local":
        what_happened += " The check ran on this computer, so there's no signed receipt."
    if simulated and spec.outcome != RUNNING:
        what_happened = _SIMULATED_PREFIX + what_happened
    elif mode == "sandbox" and spec.outcome != RUNNING:
        what_happened += _SANDBOX_NOTE
    result: dict[str, Any] = {
        "outcome": spec.outcome,
        "label": spec.label or LABELS[spec.outcome],
        "safe_to_retry": spec.safe_to_retry,
        "what_happened": what_happened,
        "next_action": spec.next_action,
        "next_step": spec.next_step,
        "reason": reason,
        "record_changed": spec.record_changed,
        "proof": proof,
        "run_id": run_id,
        "request_id": request_id,
        "workflow": workflow,
        "mode": mode,
        "contract_version": CONTRACT_VERSION,
    }
    checks = [code for code in failed_checks if code in FAILED_CHECKS]
    if reason == "not_ready_to_run":
        result["failed_checks"] = checks or ["other"]
    if reason == "invalid_input":
        result["invalid_inputs"] = [
            {"input": str(item["input"]), "problem": str(item["problem"])}
            for item in invalid_inputs
            if isinstance(item, Mapping) and "input" in item and "problem" in item
        ]
    if needs_attention_id and spec.outcome in {"needs_review", "not_sure_if_saved"}:
        result["needs_attention_id"] = needs_attention_id
    if first_run_id and reason == "request_id_conflict":
        result["first_run_id"] = first_run_id
    if replayed:
        result["replayed"] = True
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
        result["seconds"] = round(float(seconds), 1)
    if isinstance(model_calls, int) and not isinstance(model_calls, bool) and model_calls >= 0:
        result["model_calls"] = model_calls
    if sandbox:
        result["sandbox"] = {
            key: str(value) for key, value in sandbox.items() if key in {"case", "engine"}
        }
    technical: dict[str, Any] = {}
    if execution_outcome in _EXECUTION_OUTCOMES:
        technical["execution_outcome"] = execution_outcome
    if transaction_outcome in TRANSACTION_OUTCOMES:
        technical["transaction_outcome"] = transaction_outcome
    if technical:
        result["technical"] = technical
    return result


_OUTCOME_ENUM = [*OUTCOMES, RUNNING]

#: MCP ``outputSchema`` for ``run_workflow`` and ``get_run``.
RUN_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": _OUTCOME_ENUM,
            "description": (
                "done: saved and checked. needs_review: stopped before saving; a "
                "person decides. not_sure_if_saved: a person must check the record "
                "before anything runs again; never retry. did_not_run: nothing was "
                "written. running: call get_run later."
            ),
        },
        "label": {"type": "string"},
        "safe_to_retry": {
            "type": "boolean",
            "description": (
                "True only when nothing was written. Reuse the same request_id "
                "when you retry."
            ),
        },
        "what_happened": {"type": "string"},
        "next_action": {"type": "string"},
        "next_step": {"type": "string", "enum": list(NEXT_STEPS)},
        "reason": {"type": "string", "enum": sorted(REASONS)},
        "record_changed": {"type": "string", "enum": list(RECORD_CHANGED)},
        "proof": {"type": "string", "enum": list(PROOF)},
        "run_id": {"type": ["string", "null"]},
        "request_id": {"type": ["string", "null"]},
        "workflow": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": list(MODES)},
        "contract_version": {"type": "string"},
        "failed_checks": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(FAILED_CHECKS)},
        },
        "invalid_inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "problem": {"type": "string"},
                },
                "required": ["input", "problem"],
            },
        },
        "needs_attention_id": {"type": "string"},
        "first_run_id": {"type": "string"},
        "replayed": {"type": "boolean"},
        "receipt_url": {"type": "string"},
        "review_url": {"type": "string"},
        "seconds": {"type": "number"},
        "model_calls": {"type": "integer"},
        "sandbox": {
            "type": "object",
            "properties": {
                "case": {"type": "string"},
                "engine": {"type": "string", "enum": ["flow", "simulated"]},
            },
        },
        "technical": {"type": "object"},
    },
    "required": [
        "outcome",
        "label",
        "safe_to_retry",
        "what_happened",
        "next_action",
        "next_step",
        "reason",
        "record_changed",
        "proof",
        "run_id",
        "request_id",
        "workflow",
        "mode",
        "contract_version",
    ],
}

#: MCP ``outputSchema`` for ``list_workflows``.
WORKFLOW_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": list(MODES)},
        "run_tools_enabled": {"type": "boolean"},
        "how_to_run": {"type": "string"},
        "workflows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "done_means": {"type": "string"},
                    "inputs": {"type": "object"},
                    "available": {"type": "boolean"},
                    "changes_records": {"type": "boolean"},
                },
                "required": ["name", "purpose", "done_means", "inputs", "available"],
            },
        },
    },
    "required": ["mode", "run_tools_enabled", "workflows"],
}
