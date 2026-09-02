"""Dispatch an admitted Execute request to the local openadapt-flow runner.

The customer runner is this process. Synthetic MockMed admissions (the test
and ``--seed-mockmed`` path) never launch a browser. A digest-pinned compiled
bundle is replayed in-process under the standard profile.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_types.execute import (
    EffectStrengthV1,
    ExecuteRequestV1,
    ExecuteTerminalOutcomeV1,
)

from openadapt_flow.execute.models import AdmittedBundle

Runner = Callable[["AdmittedBundle", "ExecuteRequestV1", Path], "DispatchResult"]


class DispatchError(RuntimeError):
    """Local replay could not run or could not be classified."""


@dataclass(frozen=True)
class DispatchResult:
    """Closed projection of a local run onto the Execute receipt contract."""

    outcome: ExecuteTerminalOutcomeV1
    authorization_passed: bool
    identity_passed: bool
    postcondition_passed: bool
    effect_passed: bool
    minimum_effect_strength: EffectStrengthV1
    observed_effect_strength: EffectStrengthV1 | None
    model_used: bool
    external_network_used: bool
    delivery_uncertain: bool
    compensation_effect_verified: bool
    workflow_digest: str
    evidence_digest: str


def default_runner(
    admission: AdmittedBundle,
    request: ExecuteRequestV1,
    run_dir: Path,
) -> DispatchResult:
    """Run synthetic MockMed or local governed replay."""

    if admission.synthetic or not admission.bundle_dir:
        return synthetic_mockmed(admission, request)
    return live_replay(admission, request, run_dir)


def synthetic_mockmed(
    admission: AdmittedBundle, request: ExecuteRequestV1
) -> DispatchResult:
    """Project the MockMed tutorial outcomes without a browser.

    Honest environment: ``verified`` at independent-system strength.
    Banner-lie / ``break_it`` environment: the screen postcondition passes,
    the independent store does not, and the outcome is
    ``reconciliation_required`` at $0. That is the tutorial ``--break-it``
    classification, not a production Seal.
    """

    strength = EffectStrengthV1(request.minimum_effect_strength)
    if admission.break_it or _request_break_it(request):
        return _result(
            outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=True,
            effect_passed=False,
            minimum_effect_strength=strength,
            observed_effect_strength=None,
            delivery_uncertain=True,
            workflow_digest=request.workflow_digest,
            evidence_tag="mockmed-banner-lie",
        )
    return _result(
        outcome=ExecuteTerminalOutcomeV1.VERIFIED,
        authorization_passed=True,
        identity_passed=True,
        postcondition_passed=True,
        effect_passed=True,
        minimum_effect_strength=strength,
        observed_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
        delivery_uncertain=False,
        workflow_digest=request.workflow_digest,
        evidence_tag="mockmed-verified",
    )


def live_replay(
    admission: AdmittedBundle,
    request: ExecuteRequestV1,
    run_dir: Path,
) -> DispatchResult:
    """Replay a compiled bundle in this process under the standard profile."""

    bundle = Path(admission.bundle_dir or "")
    if not bundle.is_dir():
        raise DispatchError(f"admitted bundle_dir is missing: {bundle}")

    from openadapt_flow.ir import Workflow
    from openadapt_flow.mockmed.fault_server import serve as serve_mockmed
    from openadapt_flow.tutorial import (
        TUTORIAL_BREAK_ENTRY_QUERY,
        TUTORIAL_ENTRY_QUERY,
        run_tutorial_workflow,
    )

    workflow = Workflow.load(bundle)
    run_dir.mkdir(parents=True, exist_ok=True)
    stop: Optional[Callable[[], None]] = None
    try:
        if admission.target_url:
            base_url = admission.target_url
            entry_query = (
                TUTORIAL_BREAK_ENTRY_QUERY
                if admission.break_it
                else TUTORIAL_ENTRY_QUERY
            )
            report = run_tutorial_workflow(
                base_url=base_url,
                workflow=workflow,
                bundle_dir=bundle,
                run_dir=run_dir,
                headed=False,
                entry_query=entry_query,
            )
        else:
            base_url, _db, stop = serve_mockmed(port=0)
            entry_query = (
                TUTORIAL_BREAK_ENTRY_QUERY
                if admission.break_it
                else TUTORIAL_ENTRY_QUERY
            )
            report = run_tutorial_workflow(
                base_url=base_url,
                workflow=workflow,
                bundle_dir=bundle,
                run_dir=run_dir,
                headed=False,
                entry_query=entry_query,
            )
    except Exception as exc:
        raise DispatchError(f"local replay failed: {exc}") from exc
    finally:
        if stop is not None:
            stop()
    return project_run_report(report, workflow_digest=request.workflow_digest)


def project_run_report(report: Any, *, workflow_digest: str) -> DispatchResult:
    """Map a local ``RunReport`` onto the Execute terminal taxonomy."""

    from openadapt_flow.transaction import TransactionOutcome

    txn_raw = getattr(report, "transaction_outcome", None)
    try:
        txn = TransactionOutcome(str(txn_raw)) if txn_raw is not None else None
    except ValueError:
        txn = None
    coarse = str(getattr(report, "execution_outcome", "") or "")
    outcome = _map_outcome(coarse, txn)
    envelope = getattr(report, "outcome_envelope", None)
    auth_ok, ident_ok, post_ok, effect_ok = _contract_booleans(envelope)
    observed = _observed_strength(report)
    delivery_uncertain = bool(
        txn is TransactionOutcome.RECONCILIATION_REQUIRED
        or outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    )
    compensation = outcome is ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED
    if outcome is ExecuteTerminalOutcomeV1.VERIFIED and not (
        auth_ok and ident_ok and post_ok and effect_ok and observed is not None
    ):
        raise DispatchError(
            "local run claimed VERIFIED without complete Execute contracts"
        )
    return _result(
        outcome=outcome,
        authorization_passed=auth_ok,
        identity_passed=ident_ok,
        postcondition_passed=post_ok,
        effect_passed=effect_ok,
        minimum_effect_strength=_minimum_strength(report),
        observed_effect_strength=observed,
        model_used=int(getattr(report, "model_calls", 0) or 0) > 0,
        external_network_used=int(getattr(report, "model_calls", 0) or 0) > 0,
        delivery_uncertain=delivery_uncertain,
        compensation_effect_verified=compensation,
        workflow_digest=workflow_digest,
        evidence_tag=str(getattr(report, "bundle_content_digest", "") or "run"),
    )


def _request_break_it(request: ExecuteRequestV1) -> bool:
    params = request.parameters
    fault = params.get("fault")
    if fault == "optimistic":
        return True
    flag = params.get("break_it")
    return flag is True or flag == "true"


def _map_outcome(coarse: str, txn: Any) -> ExecuteTerminalOutcomeV1:
    from openadapt_flow.transaction import TransactionOutcome

    if txn is TransactionOutcome.VERIFIED or coarse == "VERIFIED":
        return ExecuteTerminalOutcomeV1.VERIFIED
    if txn is TransactionOutcome.RECONCILIATION_REQUIRED:
        return ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    if txn is TransactionOutcome.HALTED_BEFORE_EFFECT:
        return ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
    if txn is TransactionOutcome.REJECTED_POLICY:
        return ExecuteTerminalOutcomeV1.REJECTED_POLICY
    if txn is TransactionOutcome.FAILED_PLATFORM or coarse == "FAILED":
        return ExecuteTerminalOutcomeV1.FAILED_PLATFORM
    if txn is TransactionOutcome.ROLLED_BACK:
        return ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED
    if coarse == "HALTED":
        return ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
    if coarse == "COMPLETED_UNVERIFIED":
        return ExecuteTerminalOutcomeV1.REJECTED_POLICY
    return ExecuteTerminalOutcomeV1.FAILED_PLATFORM


def _contract_booleans(envelope: Any) -> tuple[bool, bool, bool, bool]:
    if envelope is None:
        return False, False, False, False
    required = envelope.required_contracts
    passed = envelope.passed_contracts

    def _exact(name: str) -> bool:
        need = int(getattr(required, name, 0) or 0)
        got = int(getattr(passed, name, 0) or 0)
        if need == 0:
            return True
        return got == need

    return (
        _exact("authorization"),
        _exact("identity"),
        _exact("postcondition"),
        _exact("effect"),
    )


def _observed_strength(report: Any) -> EffectStrengthV1 | None:
    from openadapt_flow.verification import VerificationTier

    mapping = {
        int(VerificationTier.INDEPENDENT_SYSTEM): (
            EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD
        ),
        int(VerificationTier.INDEPENDENT_SESSION): EffectStrengthV1.INDEPENDENT_SESSION,
        int(VerificationTier.PERSISTED_STATE_REACQUISITION): (
            EffectStrengthV1.PERSISTED_STATE_REACQUISITION
        ),
        int(VerificationTier.IMMEDIATE_SCREEN): (
            EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION
        ),
    }
    weakest: int | None = None
    for result in getattr(report, "results", []) or []:
        for evidence in getattr(result, "effect_evidence", []) or []:
            if getattr(evidence, "final_verdict", None) != "confirmed":
                continue
            tier = getattr(evidence, "verification_tier", None)
            if tier is None:
                continue
            value = int(tier)
            weakest = value if weakest is None else max(weakest, value)
    if weakest is None:
        return None
    return mapping.get(weakest)


def _minimum_strength(report: Any) -> EffectStrengthV1:
    from openadapt_flow.verification import VerificationTier

    raw = getattr(report, "governed_minimum_effect_tier", None)
    mapping = {
        int(VerificationTier.INDEPENDENT_SYSTEM): (
            EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD
        ),
        int(VerificationTier.INDEPENDENT_SESSION): EffectStrengthV1.INDEPENDENT_SESSION,
        int(VerificationTier.PERSISTED_STATE_REACQUISITION): (
            EffectStrengthV1.PERSISTED_STATE_REACQUISITION
        ),
        int(VerificationTier.IMMEDIATE_SCREEN): (
            EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION
        ),
    }
    if raw in mapping:
        return mapping[int(raw)]
    return EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD


def _result(
    *,
    outcome: ExecuteTerminalOutcomeV1,
    authorization_passed: bool,
    identity_passed: bool,
    postcondition_passed: bool,
    effect_passed: bool,
    minimum_effect_strength: EffectStrengthV1,
    observed_effect_strength: EffectStrengthV1 | None,
    workflow_digest: str,
    evidence_tag: str,
    model_used: bool = False,
    external_network_used: bool = False,
    delivery_uncertain: bool = False,
    compensation_effect_verified: bool = False,
) -> DispatchResult:
    digest_payload = {
        "authorization_passed": authorization_passed,
        "identity_passed": identity_passed,
        "postcondition_passed": postcondition_passed,
        "effect_passed": effect_passed,
        "evidence_tag": evidence_tag,
        "outcome": outcome.value,
        "workflow_digest": workflow_digest,
    }
    evidence_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    return DispatchResult(
        outcome=outcome,
        authorization_passed=authorization_passed,
        identity_passed=identity_passed,
        postcondition_passed=postcondition_passed,
        effect_passed=effect_passed,
        minimum_effect_strength=minimum_effect_strength,
        observed_effect_strength=observed_effect_strength,
        model_used=model_used,
        external_network_used=external_network_used,
        delivery_uncertain=delivery_uncertain,
        compensation_effect_verified=compensation_effect_verified,
        workflow_digest=workflow_digest,
        evidence_digest=evidence_digest,
    )
