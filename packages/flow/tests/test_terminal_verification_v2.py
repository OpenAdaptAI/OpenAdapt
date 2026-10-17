from __future__ import annotations

import hashlib
import json
from base64 import b64decode
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    ActionKind,
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    IdentityCheck,
    ManagedResultLossEvidence,
    OutcomeContractCounts,
    PostconditionContractEvidence,
    RunReport,
    postcondition_contract_sha256,
    postcondition_step_contract_sha256,
)
from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.receipt import RunReceipt
from openadapt_flow.runner.hosted_adapter import (
    HostedTerminalEvent,
    HostedTerminalEventV1,
    ManagedChildStartEvidence,
    ProductionDeliveryResultLossClosureRequest,
    ProductionDeliveryResultLossClosureResult,
)
from openadapt_flow.terminal_verification_v2 import (
    RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN,
    RESULT_LOSS_CLOSURE_REQUEST_DOMAIN,
    SIGNATURE_DOMAIN,
    SIGNATURE_DOMAIN_V3,
    DeliveryAuthoritySigner,
    ProductionAuthorizationEvidenceManifest,
    ProductionDeliveryPermit,
    ProductionDeliveryPermitArtifact,
    ProductionDeliveryPermitChain,
    ProductionDeliveryPermitPayload,
    ProductionDeliveryReceiptArtifact,
    ProductionDeliveryReceiptPayload,
    ProductionDeliveryResultLossClosureArtifact,
    ProductionDeliveryResultLossClosurePayload,
    ProductionEffectEvidence,
    ProductionEffectEvidenceManifest,
    ProductionEvidenceManifests,
    ProductionExecutionAuthorityPayload,
    ProductionExecutionOutcome,
    ProductionIdentityEvidenceManifest,
    ProductionIdentityResult,
    ProductionPendingDeliveryPermit,
    ProductionPolicyEvidenceManifest,
    ProductionPostconditionEvidenceManifest,
    ProductionRunReceipt,
    ProductionTerminalEffectState,
    ProductionTerminalVerificationContext,
    ProductionTerminalVerificationEnvelope,
    ProductionTerminalVerificationEnvelopeV2,
    ProductionTerminalVerificationError,
    ProductionTerminalVerificationExpected,
    ProductionTerminalVerificationPayload,
    ProductionTerminalVerificationPayloadV2,
    TerminalContractCounts,
    build_evidence_manifest,
    build_production_terminal_verification,
    evidence_runner_signer_sha256,
    project_production_run_receipt,
    rebuild_production_delivery_permit_chain_from_artifacts,
    sign_production_delivery_permit,
    sign_production_delivery_receipt,
    sign_production_delivery_result_loss_closure,
    sign_production_terminal_verification,
    verify_production_delivery_result_loss_closure_binding,
    verify_production_terminal_verification,
    verify_production_terminal_verification_v2_signature,
    verify_production_terminal_verification_v3_signature,
)
from tests.test_run_receipt import _report as _production_report

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
NOW = datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc)

IDS = {
    "run_id": "00000000-0000-4000-8000-000000000001",
    "tenant_id": "00000000-0000-4000-8000-000000000002",
    "workflow_id": "00000000-0000-4000-8000-000000000003",
    "workflow_version_id": "00000000-0000-4000-8000-000000000004",
    "bundle_version_id": "00000000-0000-4000-8000-000000000004",
    "runtime_validation_id": "00000000-0000-4000-8000-000000000005",
    "admission_id": "00000000-0000-4000-8000-000000000006",
    "execution_authority_id": "00000000-0000-4000-8000-000000000008",
}


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _public_key() -> bytes:
    return (
        _private_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _authority_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))


def _postcondition() -> PostconditionContractEvidence:
    step = postcondition_step_contract_sha256(
        workflow_contract_sha256=SHA_A,
        step_index=0,
        action_kind=ActionKind.CLICK,
    )
    contract = postcondition_contract_sha256(
        workflow_contract_sha256=SHA_A,
        step_contract_sha256=step,
        action_kind=ActionKind.CLICK,
        contract_kind="explicit_predicate",
        contract_index=0,
    )
    return PostconditionContractEvidence(
        result_index=0,
        workflow_contract_sha256=SHA_A,
        step_index=0,
        step_contract_sha256=step,
        action_kind=ActionKind.CLICK,
        contract_kind="explicit_predicate",
        contract_index=0,
        contract_sha256=contract,
        verdict="passed",
    )


def _outcome() -> ProductionExecutionOutcome:
    counts = TerminalContractCounts(
        authorization=1,
        identity=1,
        postcondition=1,
        effect=1,
    )
    return ProductionExecutionOutcome(
        profile="standard",
        required_contracts=counts,
        passed_contracts=counts,
        workflow_contract_sha256=SHA_A,
        postcondition_evidence=(_postcondition(),),
        evidence_classes=(
            "authorization",
            "effect_tier_1",
            "identity",
            "postcondition",
        ),
        model_calls=0,
        external_network_calls="none",
    )


def _terminal_postcondition(
    verdict: str,
) -> PostconditionContractEvidence:
    return _postcondition().model_copy(update={"verdict": verdict})


def _halted_outcome() -> ProductionExecutionOutcome:
    return ProductionExecutionOutcome(
        outcome="HALTED",
        profile="standard",
        production_eligible=False,
        execution_completed=False,
        required_contracts=TerminalContractCounts(
            authorization=1,
            identity=1,
            postcondition=1,
            effect=1,
        ),
        passed_contracts=TerminalContractCounts(
            authorization=1,
            identity=1,
            postcondition=0,
            effect=0,
        ),
        workflow_contract_sha256=SHA_A,
        postcondition_evidence=(_terminal_postcondition("unverifiable"),),
        evidence_classes=("authorization", "identity"),
        model_calls=0,
        external_network_calls="none",
    )


def _halted_receipt() -> ProductionRunReceipt:
    return ProductionRunReceipt(
        source_schema_version="openadapt.run-report/v1",
        outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        profile="standard",
        production_eligible=False,
        steps_total=1,
        steps_ok=0,
        heals=0,
        model_calls=0,
        est_cost_microusd=0,
        duration_ms=100,
        rung_histogram={},
        evidence_classes=("authorization", "identity"),
        effect_tier_reached="none",
        authorization_required=1,
        authorization_confirmed=1,
        identity_required=1,
        identity_confirmed=1,
        postconditions_required=1,
        postconditions_confirmed=0,
        effects_required=1,
        effects_confirmed=0,
        identity_armed=1,
        identity_applicable=1,
        over_halt_count=0,
        substrate="web",
        provenance="production",
        receipt_builder_version="1.2.3",
        external_network_calls="none",
        bundle_digest=SHA_A,
        source_receipt_digest=SHA_B,
        source_receipt_sha256=SHA_B,
        generated_at="2026-08-18T12:00:00Z",
    )


def _reconciliation_outcome() -> ProductionExecutionOutcome:
    return ProductionExecutionOutcome(
        outcome="HALTED",
        profile="standard",
        production_eligible=False,
        execution_completed=False,
        required_contracts=TerminalContractCounts(
            authorization=1,
            identity=1,
            postcondition=1,
            effect=1,
        ),
        passed_contracts=TerminalContractCounts(
            authorization=1,
            identity=1,
            postcondition=0,
            effect=0,
        ),
        workflow_contract_sha256=SHA_A,
        postcondition_evidence=(_terminal_postcondition("unverifiable"),),
        evidence_classes=("authorization", "identity"),
        model_calls=0,
        external_network_calls="none",
    )


def _reconciliation_receipt() -> ProductionRunReceipt:
    return _halted_receipt().model_copy(
        update={
            "transaction_outcome": "RECONCILIATION_REQUIRED",
            "evidence_classes": ("authorization", "identity"),
            "identity_confirmed": 1,
        }
    )


def _actual_non_success_report(
    transaction_outcome: str,
) -> RunReport:
    base = _production_report()
    assert base.outcome_envelope is not None
    base_result = base.results[0]
    postcondition = base.outcome_envelope.postcondition_evidence[0].model_copy(
        update={"verdict": "unverifiable"}
    )
    if transaction_outcome == "HALTED_BEFORE_EFFECT":
        identity = IdentityCheck(
            status="verified",
            mode="structured",
            coverage=1.0,
        )
        result = base_result.model_copy(
            update={
                "risk": "irreversible",
                "ok": False,
                "identity": identity,
                "postconditions_ok": None,
                "effect_verified": False,
                "effect_evidence": [],
                "delivery_attempted": False,
                "safety_halt": True,
            }
        )
        passed = OutcomeContractCounts(
            authorization=1,
            identity=1,
            postcondition=0,
            effect=0,
        )
        evidence_classes = ["authorization", "identity"]
    else:
        uncertainty = ActionDeliveryUncertainty(
            operation="guarded_coordinate_click",
            native=False,
            observed_at="2026-07-27T15:34:57+00:00",
            cause_type="ConnectionResetError",
            verification_attempted=True,
            postconditions_confirmed=False,
            effects_confirmed=False,
            resolved_by_contract=False,
        )
        effect = EffectVerificationEvidence(
            effect_contract_hash=base_result.effect_contract_hashes[0],
            substrate="rest",
            verifier_identity="sha256:" + SHA_B,
            verification_tier=1,
            initial_verdict="indeterminate",
            final_verdict="indeterminate",
            observed_effect="unknown",
        )
        result = base_result.model_copy(
            update={
                "risk": "irreversible",
                "ok": False,
                "postconditions_ok": False,
                "effect_verified": False,
                "effect_evidence": [effect],
                "delivery_attempted": True,
                "delivery_uncertainty": uncertainty,
                "safety_halt": True,
            }
        )
        passed = OutcomeContractCounts(
            authorization=1,
            identity=1,
            postcondition=0,
            effect=0,
        )
        evidence_classes = ["authorization", "identity"]
    envelope = ExecutionOutcomeEnvelope(
        outcome="HALTED",
        profile="standard",
        production_eligible=False,
        execution_completed=False,
        required_contracts=OutcomeContractCounts(
            authorization=1,
            identity=1,
            postcondition=1,
            effect=1,
        ),
        passed_contracts=passed,
        workflow_contract_sha256=base.workflow_contract_sha256,
        postcondition_evidence=[postcondition],
        evidence_classes=evidence_classes,
        model_calls=0,
        external_network_calls="observed",
    )
    return RunReport.model_validate(
        base.model_dump(mode="json")
        | {
            "run_id_sha256": hashlib.sha256(IDS["run_id"].encode()).hexdigest(),
            "execution_outcome": "HALTED",
            "transaction_outcome": transaction_outcome,
            "transaction_billable": False,
            "transaction_platform_fault": False,
            "production_eligible": False,
            "execution_completed": False,
            "outcome_envelope": envelope.model_dump(mode="json"),
            "success": False,
            "results": [result.model_dump(mode="json")],
        }
    )


def _receipt() -> ProductionRunReceipt:
    unsigned = {
        "schema_version": "openadapt.run-receipt/v2",
        "outcome": "VERIFIED",
        "transaction_outcome": "VERIFIED",
        "profile": "standard",
        "production_eligible": True,
        "steps_total": 1,
        "steps_ok": 1,
        "heals": 0,
        "model_calls": 0,
        "est_cost_usd": 0.0,
        "duration_ms": 100,
        "rung_histogram": {"structural": 1},
        "evidence_classes": [
            "authorization",
            "effect_tier_1",
            "identity",
            "postcondition",
        ],
        "effect_tier_reached": "independent_system",
        "authorization_required": 1,
        "authorization_confirmed": 1,
        "identity_required": 1,
        "identity_confirmed": 1,
        "postconditions_required": 1,
        "postconditions_confirmed": 1,
        "effects_required": 1,
        "effects_confirmed": 1,
        "identity_armed": 1,
        "identity_applicable": 1,
        "over_halt_count": 0,
        "substrate": "web",
        "provenance": "production",
        "receipt_builder_version": "1.2.3",
        "external_network_calls": "none",
        "bundle_digest": SHA_A,
        "generated_at": "2026-08-18T12:00:00Z",
    }
    digest = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    source = RunReceipt.model_validate({**unsigned, "receipt_digest": digest})
    return project_production_run_receipt(source)


def _permit_entry(
    *,
    permit_id: str = "permit:1",
    action_request_sha256: str = SHA_D,
    qualification_signer_registry_revision: int = 7,
    qualification_signer_registry_checked_at: str = "2026-08-18T11:59:30Z",
    input_edge_sequence: int = 1,
    authority_sequence: int = 0,
    runtime_delivery_sequence: int = 9,
    issued_at: str = "2026-08-18T12:00:00Z",
    delivered_at: str = "2026-08-18T12:00:01Z",
    one_use_claim_id: str = "00000000-0000-4000-8000-000000000010",
) -> ProductionDeliveryPermit:
    permit_payload = ProductionDeliveryPermitPayload(
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        permit_id=permit_id,
        run_id=IDS["run_id"],
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        run_request_sha256=SHA_C,
        action_request_sha256=action_request_sha256,
        admission_artifact_sha256=SHA_D,
        evidence_identity_sha256=SHA_E,
        environment_digest=SHA_A,
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=(qualification_signer_registry_revision),
        qualification_signer_registry_checked_at=(
            qualification_signer_registry_checked_at
        ),
        qualification_signer_registry_expires_at="2026-08-20T11:00:00Z",
        input_edge_sequence=input_edge_sequence,
        authority_sequence=authority_sequence,
        issued_at=issued_at,
    )
    permit_artifact = sign_production_delivery_permit(permit_payload, _authority_key())
    receipt_payload = ProductionDeliveryReceiptPayload(
        execution_authority_id=permit_payload.execution_authority_id,
        permit_id=permit_payload.permit_id,
        permit_artifact_sha256=permit_artifact.artifact_sha256(),
        authenticated_runner_id_sha256=SHA_B,
        authenticated_session_id_sha256=SHA_C,
        one_use_claim_id=one_use_claim_id,
        runtime_delivery_sequence=runtime_delivery_sequence,
        delivered_at=delivered_at,
    )
    receipt_artifact = sign_production_delivery_receipt(
        receipt_payload, _authority_key()
    )
    return ProductionDeliveryPermit.build(permit_artifact, receipt_artifact)


def _permit_chain() -> ProductionDeliveryPermitChain:
    return ProductionDeliveryPermitChain.build((_permit_entry(),))


def _pending_permit_chain() -> ProductionDeliveryPermitChain:
    permit = _permit_entry().permit_artifact
    pending = ProductionPendingDeliveryPermit.build(
        permit,
        receipt_absence_observed_at="2026-08-18T12:00:02Z",
    )
    return ProductionDeliveryPermitChain.build((), pending=pending)


def _manifests(
    chain: ProductionDeliveryPermitChain,
) -> ProductionEvidenceManifests:
    policy = build_evidence_manifest(
        ProductionPolicyEvidenceManifest,
        admission_policy_sha256=SHA_A,
        governed_policy_contract_sha256=SHA_B,
        governed_runtime_inputs_digest=SHA_C,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        minimum_effect_tier=1,
    )
    authorization = build_evidence_manifest(
        ProductionAuthorizationEvidenceManifest,
        governed_authorization_id_sha256=SHA_A,
        admission_id=IDS["admission_id"],
        admission_artifact_sha256=SHA_D,
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        permit_chain_sha256=chain.permit_chain_sha256,
    )
    identity = build_evidence_manifest(
        ProductionIdentityEvidenceManifest,
        identity_contract_sha256=SHA_D,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=1,
        results=(
            ProductionIdentityResult(
                result_index=0,
                status="verified",
                mode="structured",
                signals=(),
            ),
        ),
    )
    postcondition = build_evidence_manifest(
        ProductionPostconditionEvidenceManifest,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=1,
        records=(_postcondition(),),
    )
    effect = build_evidence_manifest(
        ProductionEffectEvidenceManifest,
        effect_contract_sha256=SHA_E,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=1,
        records=(
            ProductionEffectEvidence(
                result_index=0,
                effect_contract_hash="sha256:" + SHA_A,
                verifier_identity="sha256:" + SHA_B,
                verification_tier=1,
                final_verdict="confirmed",
                observed_effect="present",
                reconciliation_completed=False,
                reconciliation_actions=0,
            ),
        ),
    )
    return ProductionEvidenceManifests.model_validate(
        {
            "policy": policy,
            "authorization": authorization,
            "identity": identity,
            "postcondition": postcondition,
            "effect": effect,
        }
    )


def _halted_manifests(
    chain: ProductionDeliveryPermitChain,
) -> ProductionEvidenceManifests:
    success = _manifests(_permit_chain())
    authorization = build_evidence_manifest(
        ProductionAuthorizationEvidenceManifest,
        governed_authorization_id_sha256=SHA_A,
        admission_id=IDS["admission_id"],
        admission_artifact_sha256=SHA_D,
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        permit_chain_sha256=chain.permit_chain_sha256,
    )
    identity = build_evidence_manifest(
        ProductionIdentityEvidenceManifest,
        identity_contract_sha256=SHA_D,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=1,
        results=(
            ProductionIdentityResult(
                result_index=0,
                status="verified",
                mode="structured",
                signals=(),
            ),
        ),
    )
    postcondition = build_evidence_manifest(
        ProductionPostconditionEvidenceManifest,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=0,
        records=(_terminal_postcondition("unverifiable"),),
    )
    effect = build_evidence_manifest(
        ProductionEffectEvidenceManifest,
        effect_contract_sha256=SHA_E,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=0,
        records=(
            ProductionTerminalEffectState(
                result_index=0,
                effect_contract_hash="sha256:" + SHA_A,
                attempt_state="not_actuated",
                observed_effect="absent",
                effect_verified=False,
                verification_performed=False,
                verifier_identity=None,
                verification_tier=None,
                final_verdict=None,
                resolved_delivery_uncertainty=False,
                absence_basis="not_actuated",
                reconciliation_completed=False,
                reconciliation_actions=0,
            ),
        ),
    )
    return ProductionEvidenceManifests(
        policy=success.policy,
        authorization=authorization,
        identity=identity,
        postcondition=postcondition,
        effect=effect,
    )


def _reconciliation_manifests(
    chain: ProductionDeliveryPermitChain,
) -> ProductionEvidenceManifests:
    halted = _halted_manifests(chain)
    identity = build_evidence_manifest(
        ProductionIdentityEvidenceManifest,
        identity_contract_sha256=SHA_D,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=1,
        results=(
            ProductionIdentityResult(
                result_index=0,
                status="verified",
                mode="structured",
                signals=(),
            ),
        ),
    )
    effect = build_evidence_manifest(
        ProductionEffectEvidenceManifest,
        effect_contract_sha256=SHA_E,
        workflow_contract_sha256=SHA_A,
        required=1,
        confirmed=0,
        records=(
            ProductionTerminalEffectState(
                result_index=0,
                effect_contract_hash="sha256:" + SHA_A,
                attempt_state="delivery_uncertain",
                observed_effect="unknown",
                effect_verified=False,
                verification_performed=False,
                verifier_identity=None,
                verification_tier=None,
                final_verdict=None,
                resolved_delivery_uncertainty=False,
                absence_basis="none",
                reconciliation_completed=False,
                reconciliation_actions=0,
            ),
        ),
    )
    return halted.model_copy(update={"identity": identity, "effect": effect})


def _halted_payload() -> ProductionTerminalVerificationPayload:
    receipt = _halted_receipt()
    outcome = _halted_outcome()
    chain = ProductionDeliveryPermitChain.build(())
    return ProductionTerminalVerificationPayload(
        **IDS,
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        runtime_substrate="web",
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=SHA_C,
        permit_chain=chain,
        permit_count=0,
        acknowledged_permit_count=0,
        pending_permit_count=0,
        final_authority_sequence=0,
        final_runtime_delivery_sequence=0,
        workflow_contract_sha256=SHA_A,
        execution_outcome=outcome,
        execution_outcome_sha256=outcome.artifact_sha256(),
        run_receipt=receipt,
        run_receipt_sha256=hashlib.sha256(
            canonical_json(receipt.model_dump(mode="json"))
        ).hexdigest(),
        run_report_sha256=SHA_B,
        run_report_object_version="version:halted:1",
        run_report_object_sha256=SHA_B,
        evidence_manifests=_halted_manifests(chain),
        verified_at="2026-08-18T12:00:02Z",
        issued_at="2026-08-18T12:00:03Z",
    )


def _reconciliation_payload() -> ProductionTerminalVerificationPayload:
    chain = _pending_permit_chain()
    receipt = _reconciliation_receipt()
    outcome = _reconciliation_outcome()
    return ProductionTerminalVerificationPayload(
        **IDS,
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        runtime_substrate="web",
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=chain.pending.authority_signer_sha256,
        permit_chain=chain,
        permit_count=1,
        acknowledged_permit_count=0,
        pending_permit_count=1,
        final_authority_sequence=0,
        final_runtime_delivery_sequence=0,
        workflow_contract_sha256=SHA_A,
        execution_outcome=outcome,
        execution_outcome_sha256=outcome.artifact_sha256(),
        run_receipt=receipt,
        run_receipt_sha256=hashlib.sha256(
            canonical_json(receipt.model_dump(mode="json"))
        ).hexdigest(),
        run_report_sha256=SHA_B,
        run_report_object_version="version:reconciliation:1",
        run_report_object_sha256=SHA_B,
        evidence_manifests=_reconciliation_manifests(chain),
        verified_at="2026-08-18T12:00:02Z",
        issued_at="2026-08-18T12:00:03Z",
    )


def _acknowledged_reconciliation_payload() -> ProductionTerminalVerificationPayload:
    pending_payload = _reconciliation_payload()
    chain = _permit_chain()
    assert chain.entries
    payload = pending_payload.model_dump(mode="json")
    payload.update(
        {
            "permit_chain": chain.model_dump(mode="json"),
            "permit_count": 1,
            "acknowledged_permit_count": 1,
            "pending_permit_count": 0,
            "final_authority_sequence": chain.entries[-1].authority_sequence,
            "final_runtime_delivery_sequence": (
                chain.entries[-1].runtime_delivery_sequence
            ),
            "execution_authority_signer_sha256": (
                chain.entries[-1].authority_signer_sha256
            ),
            "evidence_manifests": _reconciliation_manifests(chain).model_dump(
                mode="json"
            ),
            "run_report_object_version": "version:reconciliation:acknowledged:1",
        }
    )
    return ProductionTerminalVerificationPayload.model_validate(payload)


def _result_loss_request() -> ProductionDeliveryResultLossClosureRequest:
    marker = ManagedChildStartEvidence.create(
        started_at="2026-08-18T12:00:00Z",
        dispatch_id="00000000-0000-4000-8000-000000000009",
        dispatch_session_id="00000000-0000-4000-8000-000000000010",
        run_id=IDS["run_id"],
        managed_dispatch_binding_sha256="sha256:" + SHA_C,
        authenticated_runner_id_sha256=SHA_B,
        authenticated_session_id_sha256=SHA_C,
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=(_permit_entry().authority_signer_sha256),
        run_store_identity_sha256=SHA_B,
    )
    return ProductionDeliveryResultLossClosureRequest(
        child_start_evidence=marker,
        result_loss_observed_at="2026-08-18T12:00:02Z",
    )


def _result_loss_closure(
    chain: ProductionDeliveryPermitChain,
) -> ProductionDeliveryResultLossClosureArtifact:
    request = _result_loss_request()
    all_permits = (*chain.entries, *((chain.pending,) if chain.pending else ()))
    assert all_permits
    first = all_permits[0]
    final = all_permits[-1]
    pending = chain.pending
    acknowledged = chain.entries[-1] if chain.entries else None
    return sign_production_delivery_result_loss_closure(
        ProductionDeliveryResultLossClosurePayload(
            closure_id="00000000-0000-4000-8000-000000000011",
            closure_request_sha256=request.request_sha256(),
            closed_at="2026-08-18T12:00:02Z",
            result_loss_observed_at="2026-08-18T12:00:02Z",
            receipt_absence_observed_at=(
                pending.receipt_absence_observed_at if pending else None
            ),
            tenant_id=IDS["tenant_id"],
            run_id=IDS["run_id"],
            flow_run_id_sha256=hashlib.sha256(
                IDS["run_id"].encode("utf-8")
            ).hexdigest(),
            dispatch_id="00000000-0000-4000-8000-000000000009",
            dispatch_session_id="00000000-0000-4000-8000-000000000010",
            managed_dispatch_binding_sha256="sha256:" + SHA_C,
            idempotency_key_sha256=SHA_D,
            authenticated_runner_id_sha256=(
                request.child_start_evidence.authenticated_runner_id_sha256
            ),
            authenticated_session_id_sha256=(
                request.child_start_evidence.authenticated_session_id_sha256
            ),
            execution_authority_id=IDS["execution_authority_id"],
            execution_authority_sha256=SHA_A,
            execution_authority_signer_sha256=final.authority_signer_sha256,
            child_started_at="2026-08-18T12:00:00Z",
            child_start_evidence_sha256=(request.child_start_evidence.marker_sha256),
            run_store_identity_sha256=SHA_B,
            permit_chain_sha256=chain.permit_chain_sha256,
            permit_count=len(all_permits),
            acknowledged_permit_count=len(chain.entries),
            pending_permit_count=1 if pending else 0,
            pending_permit_artifact_sha256=(
                pending.permit_artifact_sha256 if pending else None
            ),
            run_request_sha256=first.run_request_sha256,
            pending_action_request_sha256=(
                pending.action_request_sha256 if pending else None
            ),
            final_input_edge_sequence=final.input_edge_sequence,
            final_authority_sequence=final.authority_sequence,
            final_runtime_delivery_sequence=(
                acknowledged.runtime_delivery_sequence if acknowledged else 0
            ),
        ),
        _authority_key(),
    )


def _managed_result_loss_payload(
    chain: ProductionDeliveryPermitChain | None = None,
) -> ProductionTerminalVerificationPayload:
    chain = chain or _pending_permit_chain()
    all_permits = (*chain.entries, *((chain.pending,) if chain.pending else ()))
    assert all_permits
    first = all_permits[0]
    final = all_permits[-1]
    pending = chain.pending
    acknowledged = chain.entries[-1] if chain.entries else None
    request = _result_loss_request()
    closure = _result_loss_closure(chain)
    loss = ManagedResultLossEvidence.create(
        loss_code="report_missing",
        child_started_at="2026-08-18T12:00:00Z",
        child_start_evidence_sha256=request.child_start_evidence.marker_sha256,
        run_store_identity_sha256=SHA_B,
        observed_at=request.result_loss_observed_at,
        run_id=IDS["run_id"],
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        dispatch_id="00000000-0000-4000-8000-000000000009",
        dispatch_session_id="00000000-0000-4000-8000-000000000010",
        managed_dispatch_binding_sha256="sha256:" + SHA_C,
        idempotency_key_sha256=SHA_D,
        authenticated_runner_id_sha256=(
            request.child_start_evidence.authenticated_runner_id_sha256
        ),
        authenticated_session_id_sha256=(
            request.child_start_evidence.authenticated_session_id_sha256
        ),
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=final.authority_signer_sha256,
        delivery_result_loss_closure_artifact_sha256=closure.artifact_sha256(),
        pending_permit_artifact_sha256=(
            pending.permit_artifact_sha256 if pending else None
        ),
        run_request_sha256=first.run_request_sha256,
        pending_action_request_sha256=(
            pending.action_request_sha256 if pending else None
        ),
    )
    outcome = ProductionExecutionOutcome(
        outcome="HALTED",
        profile="standard",
        production_eligible=False,
        execution_completed=False,
        required_contracts=TerminalContractCounts(
            authorization=1,
            identity=0,
            postcondition=0,
            effect=0,
        ),
        passed_contracts=TerminalContractCounts(
            authorization=1,
            identity=0,
            postcondition=0,
            effect=0,
        ),
        workflow_contract_sha256=SHA_A,
        postcondition_evidence=(),
        evidence_classes=("authorization",),
        model_calls=0,
        external_network_calls="observed",
        managed_result_loss_evidence_sha256=loss.evidence_sha256,
    )
    receipt = ProductionRunReceipt(
        source_schema_version="openadapt.run-report/v1",
        outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        profile="standard",
        production_eligible=False,
        steps_total=1,
        steps_ok=0,
        heals=0,
        model_calls=0,
        est_cost_microusd=0,
        duration_ms=0,
        rung_histogram={},
        evidence_classes=("authorization",),
        effect_tier_reached="none",
        authorization_required=1,
        authorization_confirmed=1,
        identity_required=0,
        identity_confirmed=0,
        postconditions_required=0,
        postconditions_confirmed=0,
        effects_required=0,
        effects_confirmed=0,
        identity_armed=0,
        identity_applicable=0,
        over_halt_count=0,
        substrate="web",
        provenance="production",
        receipt_builder_version="1.2.3",
        external_network_calls="observed",
        bundle_digest=SHA_A,
        source_receipt_digest=SHA_B,
        source_receipt_sha256=SHA_B,
        generated_at="2026-08-18T12:00:00Z",
        managed_result_loss_evidence_sha256=loss.evidence_sha256,
    )
    base = _halted_manifests(chain)
    manifests = base.model_copy(
        update={
            "identity": build_evidence_manifest(
                ProductionIdentityEvidenceManifest,
                identity_contract_sha256=SHA_D,
                workflow_contract_sha256=SHA_A,
                required=0,
                confirmed=0,
                results=(),
            ),
            "postcondition": build_evidence_manifest(
                ProductionPostconditionEvidenceManifest,
                workflow_contract_sha256=SHA_A,
                required=0,
                confirmed=0,
                records=(),
            ),
            "effect": build_evidence_manifest(
                ProductionEffectEvidenceManifest,
                effect_contract_sha256=SHA_E,
                workflow_contract_sha256=SHA_A,
                required=0,
                confirmed=0,
                records=(),
            ),
        }
    )
    return ProductionTerminalVerificationPayload(
        **IDS,
        flow_run_id_sha256=loss.flow_run_id_sha256,
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        runtime_substrate="web",
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=final.authority_signer_sha256,
        permit_chain=chain,
        permit_count=len(all_permits),
        acknowledged_permit_count=len(chain.entries),
        pending_permit_count=1 if pending else 0,
        final_authority_sequence=final.authority_sequence,
        final_runtime_delivery_sequence=(
            acknowledged.runtime_delivery_sequence if acknowledged else 0
        ),
        workflow_contract_sha256=SHA_A,
        execution_outcome=outcome,
        execution_outcome_sha256=outcome.artifact_sha256(),
        run_receipt=receipt,
        run_receipt_sha256=hashlib.sha256(
            canonical_json(receipt.model_dump(mode="json"))
        ).hexdigest(),
        run_report_sha256=SHA_B,
        run_report_object_version="version:managed-result-loss:1",
        run_report_object_sha256=SHA_B,
        evidence_manifests=manifests,
        managed_result_loss=loss,
        delivery_result_loss_closure=closure,
        verified_at="2026-08-18T12:00:02Z",
        issued_at="2026-08-18T12:00:03Z",
    )


def _managed_result_loss_acknowledged_payload() -> (
    ProductionTerminalVerificationPayload
):
    return _managed_result_loss_payload(_permit_chain())


def _payload() -> ProductionTerminalVerificationPayload:
    receipt = _receipt()
    chain = _permit_chain()
    return ProductionTerminalVerificationPayload(
        **IDS,
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        runtime_substrate="web",
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=(chain.entries[0].authority_signer_sha256),
        permit_chain=chain,
        permit_count=1,
        acknowledged_permit_count=1,
        pending_permit_count=0,
        final_authority_sequence=0,
        final_runtime_delivery_sequence=9,
        workflow_contract_sha256=SHA_A,
        execution_outcome=_outcome(),
        execution_outcome_sha256=_outcome().artifact_sha256(),
        run_receipt=receipt,
        run_receipt_sha256=hashlib.sha256(
            canonical_json(receipt.model_dump(mode="json"))
        ).hexdigest(),
        run_report_sha256=SHA_B,
        run_report_object_version="version:1",
        run_report_object_sha256=SHA_B,
        evidence_manifests=_manifests(chain),
        verified_at="2026-08-18T12:00:02Z",
        issued_at="2026-08-18T12:00:03Z",
    )


def _expected(
    payload: ProductionTerminalVerificationPayload,
) -> ProductionTerminalVerificationExpected:
    if payload.permit_chain.entries:
        authenticated_runner_id_sha256 = payload.permit_chain.entries[
            0
        ].authenticated_runner_id_sha256
        authenticated_session_id_sha256 = payload.permit_chain.entries[
            0
        ].authenticated_session_id_sha256
    else:
        authenticated_runner_id_sha256 = SHA_B
        authenticated_session_id_sha256 = SHA_C
    return ProductionTerminalVerificationExpected(
        run_id=payload.run_id,
        flow_run_id_sha256=payload.flow_run_id_sha256,
        tenant_id=payload.tenant_id,
        workflow_id=payload.workflow_id,
        workflow_version_id=payload.workflow_version_id,
        bundle_version_id=payload.bundle_version_id,
        bundle_artifact_sha256=payload.bundle_artifact_sha256,
        bundle_content_digest=payload.bundle_content_digest,
        environment_digest=payload.environment_digest,
        environment_contract_sha256=payload.environment_contract_sha256,
        runtime_environment_sha256=payload.runtime_environment_sha256,
        identity_contract_sha256=payload.identity_contract_sha256,
        effect_contract_sha256=payload.effect_contract_sha256,
        runtime_validation_id=payload.runtime_validation_id,
        runtime_substrate=payload.runtime_substrate,
        admission_id=payload.admission_id,
        admission_artifact_sha256=payload.admission_artifact_sha256,
        admission_policy_sha256=payload.admission_policy_sha256,
        evidence_identity_sha256=payload.evidence_identity_sha256,
        admitted_runtime_build_sha256=payload.admitted_runtime_build_sha256,
        evidence_runner_signer_sha256=payload.evidence_runner_signer_sha256,
        qualification_signer_registry_sha256=payload.qualification_signer_registry_sha256,
        qualification_signer_registry_revision=payload.qualification_signer_registry_revision,
        execution_authority_id=payload.execution_authority_id,
        execution_authority_sha256=payload.execution_authority_sha256,
        execution_authority_signer_sha256=(payload.execution_authority_signer_sha256),
        permit_chain_sha256=payload.permit_chain.permit_chain_sha256,
        permit_count=payload.permit_count,
        acknowledged_permit_count=payload.acknowledged_permit_count,
        pending_permit_count=payload.pending_permit_count,
        pending_permit_artifact_sha256=(
            payload.permit_chain.pending.permit_artifact_sha256
            if payload.permit_chain.pending is not None
            else None
        ),
        final_authority_sequence=payload.final_authority_sequence,
        final_runtime_delivery_sequence=payload.final_runtime_delivery_sequence,
        authenticated_runner_id_sha256=authenticated_runner_id_sha256,
        authenticated_session_id_sha256=authenticated_session_id_sha256,
        acknowledged_one_use_claim_ids=tuple(
            entry.one_use_claim_id for entry in payload.permit_chain.entries
        ),
        workflow_contract_sha256=payload.workflow_contract_sha256,
        execution_outcome_sha256=payload.execution_outcome_sha256,
        run_receipt_sha256=payload.run_receipt_sha256,
        run_report_sha256=payload.run_report_sha256,
        run_report_object_version=payload.run_report_object_version,
        run_report_object_sha256=payload.run_report_object_sha256,
        evidence_manifests=payload.evidence_manifests,
        managed_result_loss=payload.managed_result_loss,
        delivery_result_loss_closure=payload.delivery_result_loss_closure,
    )


def test_terminal_v2_signs_and_verifies_exact_production_success() -> None:
    payload = _payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    digest = verify_production_terminal_verification(
        envelope,
        expected=_expected(payload),
        now=NOW,
    )
    assert digest == envelope.artifact_sha256()
    assert (
        envelope.signature
        == sign_production_terminal_verification(payload, _private_key()).signature
    )


def test_terminal_v2_signs_and_verifies_zero_permit_safe_halt() -> None:
    payload = _halted_payload()
    envelope = sign_production_terminal_verification(payload, _private_key())

    digest = verify_production_terminal_verification(
        envelope,
        expected=_expected(payload),
        now=NOW,
    )

    assert digest == envelope.artifact_sha256()
    assert payload.execution_outcome.outcome == "HALTED"
    assert payload.run_receipt.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert payload.permit_count == 0
    assert payload.permit_chain.entries == ()
    assert all(
        isinstance(record, ProductionTerminalEffectState)
        and record.absence_basis in {"not_actuated", "verifier_refuted"}
        for record in payload.evidence_manifests.effect.records
    )


def test_terminal_v2_refuses_zero_permit_verified_claim() -> None:
    data = _halted_payload().model_dump(mode="json")
    data["run_receipt"]["transaction_outcome"] = "VERIFIED"

    with pytest.raises(ValidationError):
        ProductionTerminalVerificationPayload.model_validate(data)


def test_terminal_v2_refuses_safe_halt_without_effect_absence() -> None:
    data = _halted_payload().model_dump(mode="json")
    effect = data["evidence_manifests"]["effect"]
    record = effect["records"][0]
    record.update(
        {
            "attempt_state": "delivery_uncertain",
            "observed_effect": "unknown",
            "absence_basis": "none",
        }
    )
    unsigned = {key: value for key, value in effect.items() if key != "manifest_sha256"}
    effect["manifest_sha256"] = hashlib.sha256(
        b"openadapt-production-effect-evidence-v1\0" + canonical_json(unsigned)
    ).hexdigest()

    with pytest.raises(ValidationError, match="effect absence"):
        ProductionTerminalVerificationPayload.model_validate(data)


def test_terminal_effect_state_requires_exact_verifier_and_verdict_binding() -> None:
    record = _halted_payload().evidence_manifests.effect.records[0]
    assert isinstance(record, ProductionTerminalEffectState)
    data = record.model_dump(mode="json")

    with pytest.raises(ValidationError, match="verified state"):
        ProductionTerminalEffectState.model_validate(data | {"effect_verified": True})
    with pytest.raises(ValidationError, match="verifier identity"):
        ProductionTerminalEffectState.model_validate(
            data
            | {
                "attempt_state": "delivered",
                "verification_performed": True,
                "verification_tier": 1,
                "final_verdict": "refuted",
                "absence_basis": "verifier_refuted",
            }
        )

    refuted = ProductionTerminalEffectState.model_validate(
        data
        | {
            "attempt_state": "delivered",
            "verification_performed": True,
            "verifier_identity": "sha256:" + SHA_B,
            "verification_tier": 1,
            "final_verdict": "refuted",
            "absence_basis": "verifier_refuted",
        }
    )
    assert refuted.effect_verified is False


def test_terminal_v2_signs_and_verifies_reconciliation_proof() -> None:
    payload = _reconciliation_payload()
    envelope = sign_production_terminal_verification(payload, _private_key())

    digest = verify_production_terminal_verification(
        envelope,
        expected=_expected(payload),
        now=NOW,
    )

    assert digest == envelope.artifact_sha256()
    assert payload.run_receipt.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert payload.permit_count == 1
    assert payload.acknowledged_permit_count == 0
    assert payload.pending_permit_count == 1
    assert payload.final_runtime_delivery_sequence == 0
    assert payload.permit_chain.pending is not None
    assert payload.permit_chain.pending.delivery_state == "UNRESOLVED"
    assert payload.permit_chain.pending.delivery_receipt_artifact is None
    assert payload.permit_chain.pending.actuation_replay_authorized is False
    record = payload.evidence_manifests.effect.records[0]
    assert isinstance(record, ProductionTerminalEffectState)
    assert record.attempt_state == "delivery_uncertain"
    assert record.observed_effect == "unknown"


def test_terminal_v2_signs_managed_result_loss_without_effect_absence() -> None:
    payload = _managed_result_loss_payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    loss = payload.managed_result_loss

    assert loss is not None
    assert payload.run_receipt.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert payload.pending_permit_count == 1
    assert payload.evidence_manifests.effect.required == 0
    assert payload.evidence_manifests.effect.records == ()
    assert loss.delivery_state == "CLOSED_UNRESOLVED_RESULT_LOSS"
    assert loss.child_report_retained is False
    assert loss.effect_absence_claimed is False
    assert loss.not_received_claimed is False
    assert loss.blind_retry_authorized is False
    assert loss.actuation_replay_authorized is False
    assert envelope.payload.managed_result_loss == loss
    assert payload.execution_outcome.external_network_calls == "observed"
    assert payload.run_receipt.external_network_calls == "observed"


def test_managed_result_loss_requires_observed_closure_network_io() -> None:
    raw = _managed_result_loss_payload().model_dump(mode="json")
    outcome = ProductionExecutionOutcome.model_validate(raw["execution_outcome"])
    outcome = outcome.model_copy(update={"external_network_calls": "none"})
    receipt = ProductionRunReceipt.model_validate(raw["run_receipt"])
    receipt = receipt.model_copy(update={"external_network_calls": "none"})
    raw["execution_outcome"] = outcome.model_dump(mode="json")
    raw["execution_outcome_sha256"] = outcome.artifact_sha256()
    raw["run_receipt"] = receipt.model_dump(mode="json")
    raw["run_receipt_sha256"] = hashlib.sha256(
        canonical_json(receipt.model_dump(mode="json"))
    ).hexdigest()

    with pytest.raises(ValueError, match="managed result loss terminal binding"):
        ProductionTerminalVerificationPayload.model_validate(raw)


def test_terminal_v2_signs_ack_won_managed_result_loss_without_uncertainty() -> None:
    payload = _managed_result_loss_acknowledged_payload()
    envelope = sign_production_terminal_verification(payload, _private_key())

    digest = verify_production_terminal_verification(
        envelope,
        expected=_expected(payload),
        now=NOW,
    )

    assert digest == envelope.artifact_sha256()
    assert payload.acknowledged_permit_count == 1
    assert payload.pending_permit_count == 0
    assert payload.permit_chain.pending is None
    assert payload.managed_result_loss is not None
    assert payload.managed_result_loss.pending_permit_artifact_sha256 is None
    assert payload.managed_result_loss.pending_action_request_sha256 is None
    assert payload.evidence_manifests.effect.records == ()


@pytest.mark.parametrize(
    "identity_field",
    ["authenticated_runner_id_sha256", "authenticated_session_id_sha256"],
)
def test_ack_won_result_loss_closure_rejects_receipt_identity_mismatch(
    identity_field: str,
) -> None:
    payload = _managed_result_loss_acknowledged_payload()
    closure = payload.delivery_result_loss_closure
    loss = payload.managed_result_loss
    assert closure is not None
    assert loss is not None
    mismatched_payload = closure.payload.model_copy(update={identity_field: SHA_E})
    mismatched_closure = sign_production_delivery_result_loss_closure(
        mismatched_payload,
        _authority_key(),
    )
    loss_values = loss.model_dump(mode="json")
    loss_values.pop("evidence_sha256")
    loss_values[identity_field] = SHA_E
    loss_values["delivery_result_loss_closure_artifact_sha256"] = (
        mismatched_closure.artifact_sha256()
    )
    mismatched_loss = ManagedResultLossEvidence.create(**loss_values)

    with pytest.raises(
        ValueError,
        match="authenticated delivery identity",
    ):
        verify_production_delivery_result_loss_closure_binding(
            mismatched_closure,
            permit_chain=payload.permit_chain,
            result_loss=mismatched_loss,
            tenant_id=payload.tenant_id,
            terminal_verified_at=payload.verified_at,
        )


@pytest.mark.parametrize(
    "field",
    [
        "child_report_retained",
        "effect_absence_claimed",
        "not_received_claimed",
        "blind_retry_authorized",
        "actuation_replay_authorized",
    ],
)
def test_managed_result_loss_refuses_a_positive_prohibited_claim(field: str) -> None:
    loss = _managed_result_loss_payload().managed_result_loss
    assert loss is not None
    raw = loss.model_dump(mode="json")
    raw[field] = True

    with pytest.raises(ValidationError):
        ManagedResultLossEvidence.model_validate(raw)


def test_pending_delivery_chain_rebuilds_from_exact_retained_permit() -> None:
    chain = _pending_permit_chain()
    assert chain.pending is not None

    rebuilt = rebuild_production_delivery_permit_chain_from_artifacts(
        (),
        pending_permit_artifact=chain.pending.permit_artifact.canonical_bytes(),
        receipt_absence_observed_at=chain.pending.receipt_absence_observed_at,
    )

    assert rebuilt == chain
    with pytest.raises(
        ProductionTerminalVerificationError,
        match="pending delivery binding is incomplete",
    ):
        rebuild_production_delivery_permit_chain_from_artifacts(
            (),
            pending_permit_artifact=chain.pending.permit_artifact.canonical_bytes(),
        )


def test_pending_reconciliation_refuses_effect_absence_or_replay() -> None:
    payload = _reconciliation_payload()
    assert payload.permit_chain.pending is not None
    pending = payload.permit_chain.pending.model_dump(mode="json")

    pending["actuation_replay_authorized"] = True
    with pytest.raises(ValidationError):
        ProductionPendingDeliveryPermit.model_validate(pending)

    data = payload.model_dump(mode="json")
    effect = data["evidence_manifests"]["effect"]
    effect["records"][0]["observed_effect"] = "absent"
    effect["records"][0]["absence_basis"] = "not_actuated"
    with pytest.raises(ValidationError):
        ProductionTerminalVerificationPayload.model_validate(data)


def test_terminal_acceptor_rechecks_pending_permit_digest() -> None:
    payload = _reconciliation_payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    expected = _expected(payload).model_copy(
        update={"pending_permit_artifact_sha256": SHA_A}
    )

    with pytest.raises(
        ProductionTerminalVerificationError,
        match="pending permit",
    ):
        verify_production_terminal_verification(
            envelope,
            expected=expected,
            now=NOW,
        )


def test_verified_terminal_refuses_a_pending_permit() -> None:
    success = _payload()
    pending_chain = _pending_permit_chain()
    data = success.model_dump(mode="json")
    data.update(
        {
            "permit_chain": pending_chain.model_dump(mode="json"),
            "acknowledged_permit_count": 0,
            "pending_permit_count": 1,
            "final_runtime_delivery_sequence": 0,
            "evidence_manifests": _manifests(pending_chain).model_dump(mode="json"),
        }
    )

    with pytest.raises(ValidationError, match="VERIFIED proof is incomplete"):
        ProductionTerminalVerificationPayload.model_validate(data)


@pytest.mark.parametrize(
    "transaction_outcome",
    ["HALTED_BEFORE_EFFECT", "RECONCILIATION_REQUIRED"],
)
def test_terminal_v2_builds_non_success_proof_from_exact_run_report(
    transaction_outcome: str,
) -> None:
    report = _actual_non_success_report(transaction_outcome)
    chain = (
        ProductionDeliveryPermitChain.build(())
        if transaction_outcome == "HALTED_BEFORE_EFFECT"
        else _pending_permit_chain()
    )
    context = ProductionTerminalVerificationContext(
        run_id=IDS["run_id"],
        tenant_id=IDS["tenant_id"],
        workflow_id=IDS["workflow_id"],
        workflow_version_id=IDS["workflow_version_id"],
        bundle_version_id=IDS["bundle_version_id"],
        bundle_artifact_sha256=SHA_B,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        runtime_validation_id=IDS["runtime_validation_id"],
        runtime_substrate="web",
        admission_id=IDS["admission_id"],
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        execution_authority_id=IDS["execution_authority_id"],
        execution_authority_sha256=SHA_A,
        execution_authority_signer_sha256=(
            chain.pending.authority_signer_sha256
            if chain.pending is not None
            else chain.entries[0].authority_signer_sha256
            if chain.entries
            else SHA_C
        ),
        permit_chain=chain,
        run_report_object_version="version:terminal-report:1",
        verified_at="2026-08-18T12:00:02Z",
        issued_at="2026-08-18T12:00:03Z",
    )

    built = build_production_terminal_verification(
        report,
        context=context,
        private_key=_private_key(),
    )
    payload = built.envelope.payload

    assert payload.run_receipt.transaction_outcome == transaction_outcome
    assert payload.run_report_sha256 == hashlib.sha256(built.report_bytes).hexdigest()
    assert (
        verify_production_terminal_verification(
            built.envelope,
            expected=_expected(payload),
            now=NOW,
        )
        == built.envelope.artifact_sha256()
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authenticated_runner_id_sha256", SHA_E, "runner identity"),
        ("authenticated_session_id_sha256", SHA_E, "delivery session"),
        (
            "acknowledged_one_use_claim_ids",
            ("99999999-9999-4999-8999-999999999999",),
            "one-use claims",
        ),
    ],
)
def test_terminal_acceptor_rechecks_each_cloud_delivery_identity(
    field: str, value: object, message: str
) -> None:
    payload = _payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    expected = _expected(payload).model_copy(update={field: value})

    with pytest.raises(ProductionTerminalVerificationError, match=message):
        verify_production_terminal_verification(
            envelope,
            expected=expected,
            now=NOW,
        )


def test_generic_success_cannot_parse_as_terminal_v2() -> None:
    with pytest.raises(ValidationError):
        ProductionTerminalVerificationPayload.model_validate({"status": "success"})


def test_execution_authority_digest_binds_the_exact_run_and_signer() -> None:
    signer_sha256 = _permit_chain().entries[0].authority_signer_sha256
    authority = ProductionExecutionAuthorityPayload(
        execution_authority_id=IDS["execution_authority_id"],
        tenant_id=IDS["tenant_id"],
        run_id=IDS["run_id"],
        flow_run_id_sha256=hashlib.sha256(IDS["run_id"].encode("utf-8")).hexdigest(),
        workflow_id=IDS["workflow_id"],
        workflow_version_id=IDS["workflow_version_id"],
        bundle_version_id=IDS["bundle_version_id"],
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        runtime_validation_id=IDS["runtime_validation_id"],
        runtime_substrate="web",
        runtime_boundary_id="managed-us",
        admission_id=IDS["admission_id"],
        admission_artifact_sha256=SHA_D,
        admission_policy_sha256=SHA_A,
        evidence_identity_sha256=SHA_E,
        environment_digest=SHA_A,
        environment_contract_sha256=SHA_B,
        runtime_environment_sha256=SHA_C,
        identity_contract_sha256=SHA_D,
        effect_contract_sha256=SHA_E,
        admitted_runtime_build_sha256=SHA_C,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(_public_key()),
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        qualification_signer_registry_checked_at="2026-08-18T11:59:30Z",
        qualification_signer_registry_expires_at="2026-08-20T11:00:00Z",
        execution_profile="standard",
        dispatch_binding_sha256="sha256:" + SHA_A,
        execution_authority_signer_sha256=signer_sha256,
        created_at="2026-08-18T12:00:00Z",
    )
    assert len(authority.artifact_sha256()) == 64
    changed = authority.model_copy(update={"environment_digest": SHA_B})
    assert changed.artifact_sha256() != authority.artifact_sha256()


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("qualification_evidence_only", True),
        ("production_eligible", False),
        ("execution_completed", False),
        ("outcome", "COMPLETED_UNVERIFIED"),
    ],
)
def test_terminal_refuses_nonproduction_outcome(path: str, replacement: object) -> None:
    data = _outcome().model_dump(mode="json")
    data[path] = replacement
    with pytest.raises(ValidationError):
        ProductionExecutionOutcome.model_validate(data)


def test_terminal_requires_nonzero_identity_postcondition_and_effect() -> None:
    data = _outcome().model_dump(mode="json")
    data["required_contracts"]["identity"] = 0
    data["passed_contracts"]["identity"] = 0
    with pytest.raises(ValidationError):
        ProductionExecutionOutcome.model_validate(data)


def test_terminal_rejects_report_object_mismatch() -> None:
    payload = _payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    expected = _expected(payload).model_copy(update={"run_report_object_sha256": SHA_D})
    with pytest.raises(
        ProductionTerminalVerificationError,
        match="run_report_object_sha256 does not match live state",
    ):
        verify_production_terminal_verification(
            envelope,
            expected=expected,
            now=NOW,
        )


def test_permit_rejects_stale_registry_check() -> None:
    with pytest.raises(ValidationError, match="registry check is stale"):
        _permit_entry(
            qualification_signer_registry_checked_at="2026-08-18T11:58:00Z",
        )


def test_permit_registry_check_at_sixty_seconds_is_accepted() -> None:
    assert (
        _permit_entry(
            qualification_signer_registry_checked_at="2026-08-18T11:59:00Z",
        ).issued_at
        == "2026-08-18T12:00:00Z"
    )


def test_permit_digest_is_recomputed_from_all_retained_fields() -> None:
    permit = _permit_chain().entries[0]
    data = permit.model_dump(mode="json")
    data["permit_artifact"]["payload"]["action_request_sha256"] = SHA_E
    with pytest.raises(ValidationError, match="permit payload digest is invalid"):
        ProductionDeliveryPermit.model_validate(data)


def test_chain_revalidates_an_in_memory_permit_with_stale_digest() -> None:
    permit = _permit_chain().entries[0]
    changed_payload = permit.permit_artifact.payload.model_copy(
        update={"action_request_sha256": SHA_E}
    )
    changed_artifact = permit.permit_artifact.model_copy(
        update={"payload": changed_payload}
    )
    changed_entry = permit.model_copy(update={"permit_artifact": changed_artifact})
    with pytest.raises(ValidationError, match="permit payload digest is invalid"):
        ProductionDeliveryPermitChain.build((changed_entry,))


def test_permit_signer_revalidates_an_in_memory_payload() -> None:
    payload = (
        _permit_chain()
        .entries[0]
        .permit_artifact.payload.model_copy(update={"flow_run_id_sha256": SHA_A})
    )
    with pytest.raises(ValidationError, match="run identity digest is invalid"):
        sign_production_delivery_permit(payload, _authority_key())


def test_delivery_receipt_must_bind_exact_permit_bytes() -> None:
    entry = _permit_chain().entries[0]
    receipt_payload = entry.delivery_receipt_artifact.payload.model_copy(
        update={"permit_artifact_sha256": SHA_E}
    )
    receipt = sign_production_delivery_receipt(receipt_payload, _authority_key())
    with pytest.raises(ValidationError, match="does not bind its exact permit"):
        ProductionDeliveryPermit.build(entry.permit_artifact, receipt)


def test_delivery_receipt_must_use_the_permit_authority_signer() -> None:
    entry = _permit_chain().entries[0]
    other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    receipt = sign_production_delivery_receipt(
        entry.delivery_receipt_artifact.payload, other_key
    )
    with pytest.raises(ValidationError, match="signer differs"):
        ProductionDeliveryPermit.build(entry.permit_artifact, receipt)


def test_terminal_counts_reject_numeric_boolean_and_float_spellings() -> None:
    counts = {"authorization": 1, "identity": 1, "postcondition": 1, "effect": 1}
    for field, replacement in (
        ("authorization", True),
        ("identity", 1.0),
        ("effect", "1"),
    ):
        with pytest.raises(ValidationError):
            TerminalContractCounts.model_validate({**counts, field: replacement})
    data = _outcome().model_dump(mode="json")
    data["qualification_evidence_only"] = 0
    with pytest.raises(ValidationError):
        ProductionExecutionOutcome.model_validate(data)
    data = _outcome().model_dump(mode="json")
    data["production_eligible"] = 1
    with pytest.raises(ValidationError):
        ProductionExecutionOutcome.model_validate(data)


def test_terminal_rejects_permit_sequence_gap() -> None:
    first = _permit_chain().entries[0]
    second = _permit_entry(
        permit_id="permit:2",
        action_request_sha256=SHA_E,
        input_edge_sequence=3,
        authority_sequence=1,
        runtime_delivery_sequence=10,
        issued_at="2026-08-18T12:00:02Z",
        delivered_at="2026-08-18T12:00:03Z",
    )
    with pytest.raises(ValidationError, match="input-edge sequence"):
        ProductionDeliveryPermitChain.build((first, second))


def test_terminal_supports_multiple_input_edges_with_independent_sequences() -> None:
    first = _permit_chain().entries[0]
    second = _permit_entry(
        permit_id="permit:2",
        action_request_sha256=SHA_E,
        input_edge_sequence=2,
        authority_sequence=1,
        runtime_delivery_sequence=10,
        issued_at="2026-08-18T12:00:02Z",
        delivered_at="2026-08-18T12:00:03Z",
        one_use_claim_id="00000000-0000-4000-8000-000000000011",
    )
    chain = ProductionDeliveryPermitChain.build((first, second))
    assert chain.entries[-1].authority_sequence == 1
    assert chain.entries[-1].runtime_delivery_sequence == 10


def test_delivery_cross_language_vector_is_exact() -> None:
    vector = json.loads(
        Path("tests/fixtures/terminal_verification_v2_delivery_vector.json").read_text(
            encoding="utf-8"
        )
    )
    key = Ed25519PrivateKey.from_private_bytes(
        b64decode(vector["private_key_base64"], validate=True)
    )
    permit_payload = ProductionDeliveryPermitPayload.model_validate(
        vector["permit_payload"]
    )
    permit_artifact = sign_production_delivery_permit(permit_payload, key)
    assert permit_artifact.payload_sha256 == vector["permit_payload_sha256"]
    assert permit_artifact.signature == vector["permit_signature"]
    assert permit_artifact.artifact_sha256() == vector["permit_artifact_sha256"]
    assert permit_artifact.signer.key_id == vector["key_id"]
    assert permit_artifact.signer.public_key == vector["public_key_base64"]
    assert permit_artifact.signer.signer_sha256() == vector["signer_sha256"]

    receipt_payload = ProductionDeliveryReceiptPayload.model_validate(
        vector["receipt_payload"]
    )
    receipt_artifact = sign_production_delivery_receipt(receipt_payload, key)
    assert receipt_artifact.payload_sha256 == vector["receipt_payload_sha256"]
    assert receipt_artifact.signature == vector["receipt_signature"]
    assert (
        receipt_artifact.artifact_sha256() == vector["delivery_receipt_artifact_sha256"]
    )
    entry = ProductionDeliveryPermit.build(permit_artifact, receipt_artifact)
    chain = ProductionDeliveryPermitChain.build((entry,))
    assert chain.permit_chain_sha256 == vector["permit_chain_sha256"]
    rebuilt = rebuild_production_delivery_permit_chain_from_artifacts(
        ((permit_artifact.canonical_bytes(), receipt_artifact.canonical_bytes()),)
    )
    assert rebuilt == chain


def test_delivery_consumer_vector_omits_only_private_signing_material() -> None:
    producer_path = Path("tests/fixtures/terminal_verification_v2_delivery_vector.json")
    consumer_path = Path(
        "tests/fixtures/terminal_verification_v2_delivery_consumer_vector.json"
    )
    producer = json.loads(producer_path.read_text(encoding="utf-8"))
    consumer_raw = consumer_path.read_bytes()
    consumer = json.loads(consumer_raw)

    assert hashlib.sha256(consumer_raw).hexdigest() == (
        "f2e32472ad562b5b67d95d3d9cafaf08b4a2adb9fb5ff3cda2813307beb61c96"
    )
    assert "private_key_base64" not in consumer
    assert consumer == {
        key: value for key, value in producer.items() if key != "private_key_base64"
    }

    signer = DeliveryAuthoritySigner(
        key_id=consumer["key_id"],
        public_key=consumer["public_key_base64"],
    )
    permit = ProductionDeliveryPermitArtifact(
        payload=consumer["permit_payload"],
        payload_sha256=consumer["permit_payload_sha256"],
        signer=signer,
        signature=consumer["permit_signature"],
    )
    receipt = ProductionDeliveryReceiptArtifact(
        payload=consumer["receipt_payload"],
        payload_sha256=consumer["receipt_payload_sha256"],
        signer=signer,
        signature=consumer["receipt_signature"],
    )
    assert permit.artifact_sha256() == consumer["permit_artifact_sha256"]
    assert receipt.artifact_sha256() == consumer["delivery_receipt_artifact_sha256"]
    entry = ProductionDeliveryPermit.build(permit, receipt)
    assert entry.permit_artifact_sha256 == consumer["permit_artifact_sha256"]
    assert (
        ProductionDeliveryPermitChain.build((entry,)).permit_chain_sha256
        == consumer["permit_chain_sha256"]
    )


def test_non_success_terminal_cross_language_vectors_are_exact() -> None:
    fixture = json.loads(
        Path("tests/fixtures/terminal_verification_v2_terminal_vectors.json").read_text(
            encoding="utf-8"
        )
    )
    assert b64decode(fixture["signature_domain_base64"], validate=True) == (
        SIGNATURE_DOMAIN_V3
    )
    key = Ed25519PrivateKey.from_private_bytes(
        b64decode(fixture["private_key_base64"], validate=True)
    )
    payloads = {
        "verified-complete": _payload(),
        "halted-before-effect-zero-permit": _halted_payload(),
        "reconciliation-required-pending-permit": _reconciliation_payload(),
        "reconciliation-required-acknowledged-inconclusive": (
            _acknowledged_reconciliation_payload()
        ),
        "reconciliation-required-managed-result-loss": (_managed_result_loss_payload()),
        "reconciliation-required-managed-result-loss-acknowledged": (
            _managed_result_loss_acknowledged_payload()
        ),
    }

    for vector in fixture["vectors"]:
        raw = b64decode(vector["envelope_canonical_base64"], validate=True)
        envelope = ProductionTerminalVerificationEnvelope.model_validate_json(raw)
        payload = payloads[vector["name"]]
        expected = sign_production_terminal_verification(payload, key)

        assert canonical_json(envelope) == raw
        assert envelope == expected
        assert (
            hashlib.sha256(payload.canonical_bytes()).hexdigest()
            == (vector["payload_canonical_sha256"])
        )
        assert envelope.signature == vector["signature"]
        assert (
            envelope.artifact_sha256()
            == (vector["terminal_verification_artifact_sha256"])
        )
        effect_records = envelope.payload.evidence_manifests.effect.records
        assert (
            effect_records[0].model_dump(mode="json") if effect_records else None
        ) == vector["effect_state"]
        assert (
            envelope.payload.managed_result_loss.model_dump(mode="json")
            if envelope.payload.managed_result_loss is not None
            else None
        ) == vector["managed_result_loss"]
        callback = HostedTerminalEvent.model_validate(vector["callback"])
        assert callback.run_id == payload.run_id
        assert callback.outcome == payload.run_receipt.transaction_outcome
        assert callback.report_sha256 == payload.run_report_sha256
        assert callback.uncertain_delivery == (payload.pending_permit_count == 1)
        assert (
            callback.terminal_verification_artifact_bytes_base64
            == (vector["envelope_canonical_base64"])
        )
        assert callback.terminal_verification_artifact_sha256 == (
            envelope.artifact_sha256()
        )


def test_terminal_consumer_vectors_omit_only_private_signing_material() -> None:
    producer_path = Path(
        "tests/fixtures/terminal_verification_v2_terminal_vectors.json"
    )
    consumer_path = Path(
        "tests/fixtures/terminal_verification_v2_terminal_consumer_vectors.json"
    )
    producer = json.loads(producer_path.read_text(encoding="utf-8"))
    consumer_raw = consumer_path.read_bytes()
    consumer = json.loads(consumer_raw)

    assert hashlib.sha256(consumer_raw).hexdigest() == (
        "8b84b80705de47e511fcadf1b2d6d15a95615dbbabb247f2eb1ab1d542fc903a"
    )
    assert "private_key_base64" not in consumer
    assert consumer == {
        key: value for key, value in producer.items() if key != "private_key_base64"
    }
    assert b64decode(consumer["signature_domain_base64"], validate=True) == (
        SIGNATURE_DOMAIN_V3
    )
    assert len(b64decode(consumer["public_key_base64"], validate=True)) == 32

    for vector in consumer["vectors"]:
        raw = b64decode(vector["envelope_canonical_base64"], validate=True)
        envelope = ProductionTerminalVerificationEnvelope.model_validate_json(raw)
        assert canonical_json(envelope) == raw
        assert (
            verify_production_terminal_verification_v3_signature(envelope)
            == (vector["terminal_verification_artifact_sha256"])
        )
        assert envelope.signature == vector["signature"]


def test_flow_v1_34_terminal_verified_golden_is_exact() -> None:
    golden = json.loads(
        Path("tests/fixtures/flow_v1_34_0_terminal_verified.json").read_text(
            encoding="utf-8"
        )
    )
    assert golden["release_tag"] == "v1.34.0"
    assert golden["source_commit"] == ("30fc60e55778a0e0f92b9776117cafcfe2512249")
    assert golden["annotated_tag_object"] == (
        "7bd2c47182b514053a14e2c7e861694cda1387ba"
    )
    assert golden["source_commit"] != golden["annotated_tag_object"]
    assert (
        b64decode(golden["signature_domain_base64"], validate=True) == SIGNATURE_DOMAIN
    )
    legacy_raw = b64decode(golden["envelope_canonical_base64"], validate=True)
    legacy_envelope = ProductionTerminalVerificationEnvelopeV2.model_validate_json(
        legacy_raw
    )
    assert canonical_json(legacy_envelope) == legacy_raw
    assert legacy_envelope.payload.schema_version == (
        "openadapt.production-terminal-verification/v2"
    )
    legacy_payload_fields = legacy_envelope.payload.model_dump(mode="json")
    assert "acknowledged_permit_count" not in legacy_payload_fields
    assert "pending_permit_count" not in legacy_payload_fields
    assert (
        hashlib.sha256(legacy_envelope.payload.canonical_bytes()).hexdigest()
        == golden["payload_canonical_sha256"]
    )
    assert legacy_envelope.signature == golden["signature"]
    assert (
        verify_production_terminal_verification_v2_signature(legacy_envelope)
        == golden["terminal_verification_artifact_sha256"]
    )
    legacy_callback = HostedTerminalEventV1.model_validate(golden["callback"])
    assert legacy_callback.schema_version == "openadapt.hosted-runner-terminal/v1"
    with pytest.raises(ValidationError):
        ProductionTerminalVerificationEnvelope.model_validate_json(legacy_raw)
    with pytest.raises(ValidationError):
        HostedTerminalEvent.model_validate(golden["callback"])


def test_flow_v1_34_payload_rejects_successor_only_nested_fields() -> None:
    current = _payload()
    legacy_raw = current.model_dump(mode="json")
    legacy_raw["schema_version"] = "openadapt.production-terminal-verification/v2"
    legacy_raw.pop("acknowledged_permit_count")
    legacy_raw.pop("pending_permit_count")
    old_effect = current.evidence_manifests.effect.records[0]
    terminal_effect = ProductionTerminalEffectState(
        result_index=old_effect.result_index,
        effect_contract_hash=old_effect.effect_contract_hash,
        attempt_state="delivered",
        observed_effect="present",
        effect_verified=True,
        verification_performed=True,
        verifier_identity=old_effect.verifier_identity,
        verification_tier=old_effect.verification_tier,
        final_verdict="confirmed",
        resolved_delivery_uncertainty=False,
        absence_basis="none",
        reconciliation_completed=False,
        reconciliation_actions=0,
    )
    effect = build_evidence_manifest(
        ProductionEffectEvidenceManifest,
        effect_contract_sha256=current.effect_contract_sha256,
        workflow_contract_sha256=current.workflow_contract_sha256,
        required=1,
        confirmed=1,
        records=(terminal_effect,),
    )
    legacy_raw["evidence_manifests"]["effect"] = effect.model_dump(mode="json")

    with pytest.raises(ValidationError, match="effect evidence has unexpected"):
        ProductionTerminalVerificationPayloadV2.model_validate(legacy_raw)

    clean = current.model_dump(mode="json")
    clean["schema_version"] = "openadapt.production-terminal-verification/v2"
    clean.pop("acknowledged_permit_count")
    clean.pop("pending_permit_count")
    clean["execution_outcome"]["managed_result_loss_evidence_sha256"] = None
    with pytest.raises(ValidationError, match="execution outcome has unexpected"):
        ProductionTerminalVerificationPayloadV2.model_validate(clean)


def test_terminal_event_versions_reject_the_other_proof_family() -> None:
    current = json.loads(
        Path("tests/fixtures/terminal_verification_v2_terminal_vectors.json").read_text(
            encoding="utf-8"
        )
    )["vectors"][0]["callback"]
    current_as_v1 = dict(current) | {
        "schema_version": "openadapt.hosted-runner-terminal/v1"
    }
    with pytest.raises(ValidationError, match="terminal verification artifact"):
        HostedTerminalEventV1.model_validate(current_as_v1)

    legacy = json.loads(
        Path("tests/fixtures/flow_v1_34_0_terminal_verified.json").read_text(
            encoding="utf-8"
        )
    )["callback"]
    legacy_as_v2 = dict(legacy) | {
        "schema_version": "openadapt.hosted-runner-terminal/v2"
    }
    with pytest.raises(ValidationError, match="terminal verification artifact"):
        HostedTerminalEvent.model_validate(legacy_as_v2)


def test_managed_result_loss_closure_cross_language_vector_is_exact() -> None:
    fixture = json.loads(
        Path("tests/fixtures/terminal_verification_v2_terminal_vectors.json").read_text(
            encoding="utf-8"
        )
    )
    names = (
        "managed_result_loss_closure_vector",
        "managed_result_loss_acknowledged_closure_vector",
    )
    pending_counts: list[int] = []
    for name in names:
        vector = fixture[name]
        request_raw = b64decode(vector["request_canonical_base64"], validate=True)
        closure_raw = b64decode(
            vector["closure_artifact_canonical_base64"], validate=True
        )
        chain_raw = b64decode(vector["permit_chain_canonical_base64"], validate=True)
        result_raw = b64decode(vector["result_canonical_base64"], validate=True)
        request = ProductionDeliveryResultLossClosureRequest.model_validate_json(
            request_raw
        )
        closure = ProductionDeliveryResultLossClosureArtifact.model_validate_json(
            closure_raw
        )
        chain = ProductionDeliveryPermitChain.model_validate_json(chain_raw)
        result = ProductionDeliveryResultLossClosureResult.model_validate_json(
            result_raw
        )

        assert vector["http_method"] == "POST"
        assert vector["http_route"] == (
            "/api/internal/managed-delivery-result-loss-closure"
        )
        assert vector["authorization_credential_source"] == (
            "hosted_dispatch.lease_token"
        )
        assert b64decode(vector["request_digest_domain_base64"], validate=True) == (
            RESULT_LOSS_CLOSURE_REQUEST_DOMAIN
        )
        assert b64decode(vector["payload_signature_domain_base64"], validate=True) == (
            RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN
        )
        assert request.canonical_bytes() == request_raw
        assert request.request_sha256() == vector["request_sha256"]
        assert closure.canonical_bytes() == closure_raw
        assert closure.payload_sha256 == vector["closure_payload_sha256"]
        assert closure.artifact_sha256() == vector["closure_artifact_sha256"]
        assert canonical_json(chain) == chain_raw
        assert chain.permit_chain_sha256 == vector["permit_chain_sha256"]
        assert canonical_json(result) == result_raw
        assert result.artifacts() == (closure, chain)
        pending_counts.append(closure.payload.pending_permit_count)
    assert pending_counts == [1, 0]


def test_delivery_artifact_rebuild_rejects_noncanonical_stored_bytes() -> None:
    entry = _permit_chain().entries[0]
    with pytest.raises(
        ProductionTerminalVerificationError,
        match="not canonical",
    ):
        rebuild_production_delivery_permit_chain_from_artifacts(
            (
                (
                    b" " + entry.permit_artifact.canonical_bytes(),
                    entry.delivery_receipt_artifact.canonical_bytes(),
                ),
            )
        )


def test_terminal_rejects_registry_change_between_input_edges() -> None:
    first = _permit_chain().entries[0]
    second = _permit_entry(
        permit_id="permit:2",
        action_request_sha256=SHA_E,
        qualification_signer_registry_revision=8,
        input_edge_sequence=2,
        authority_sequence=1,
        runtime_delivery_sequence=10,
        issued_at="2026-08-18T12:00:02Z",
        delivered_at="2026-08-18T12:00:03Z",
        one_use_claim_id="00000000-0000-4000-8000-000000000011",
    )
    with pytest.raises(ValidationError, match="changes its production authority"):
        ProductionDeliveryPermitChain.build((first, second))


def test_evidence_manifest_digest_is_recomputed() -> None:
    manifest = _manifests(_permit_chain()).identity.model_dump(mode="json")
    manifest["manifest_sha256"] = SHA_A
    with pytest.raises(ValidationError, match="identity evidence digest is invalid"):
        ProductionIdentityEvidenceManifest.model_validate(manifest)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("environment_digest", SHA_B, "qualification state"),
        ("qualification_signer_registry_revision", 8, "qualification state"),
        ("workflow_contract_sha256", SHA_B, "execution outcome digest"),
        ("execution_outcome_sha256", SHA_B, "execution outcome digest"),
        (
            "workflow_version_id",
            "00000000-0000-4000-8000-000000000007",
            "workflow and bundle versions",
        ),
    ],
)
def test_terminal_refuses_one_field_binding_mutation(
    field: str,
    replacement: object,
    message: str,
) -> None:
    data = _payload().model_dump(mode="json")
    data[field] = replacement
    with pytest.raises(ValidationError, match=message):
        ProductionTerminalVerificationPayload.model_validate(data)


def test_terminal_refuses_run_id_digest_mismatch() -> None:
    data = _payload().model_dump(mode="json")
    data["flow_run_id_sha256"] = SHA_A
    with pytest.raises(ValidationError, match="run identity digest is invalid"):
        ProductionTerminalVerificationPayload.model_validate(data)


def test_terminal_signer_revalidates_an_in_memory_payload() -> None:
    payload = _payload().model_copy(update={"flow_run_id_sha256": SHA_A})
    with pytest.raises(ValidationError, match="run identity digest is invalid"):
        sign_production_terminal_verification(payload, _private_key())


def test_terminal_rejects_wrong_evidence_signer() -> None:
    payload = _payload().model_copy(update={"evidence_runner_signer_sha256": SHA_A})
    with pytest.raises(
        ProductionTerminalVerificationError,
        match="signer does not match",
    ):
        sign_production_terminal_verification(payload, _private_key())


def test_terminal_rejects_mutated_signature() -> None:
    payload = _payload()
    envelope = sign_production_terminal_verification(payload, _private_key())
    replacement = "A" if envelope.signature[-1] != "A" else "B"
    mutated = envelope.model_copy(
        update={"signature": envelope.signature[:-1] + replacement}
    )
    with pytest.raises(
        ProductionTerminalVerificationError,
        match="signature is invalid|envelope is not canonical",
    ):
        verify_production_terminal_verification(
            mutated,
            expected=_expected(payload),
            now=NOW,
        )


def test_terminal_v1_has_no_production_fallback() -> None:
    data = _payload().model_dump(mode="json")
    data["schema_version"] = "openadapt.production-terminal-verification/v1"
    with pytest.raises(ValidationError):
        ProductionTerminalVerificationPayload.model_validate(data)
