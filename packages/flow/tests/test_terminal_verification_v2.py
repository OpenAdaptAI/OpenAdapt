from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from openadapt_flow.ir import (
    ActionKind,
    PostconditionContractEvidence,
    postcondition_contract_sha256,
    postcondition_step_contract_sha256,
)
from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.receipt import RunReceipt
from openadapt_flow.terminal_verification_v2 import (
    ProductionAuthorizationEvidenceManifest,
    ProductionDeliveryPermit,
    ProductionDeliveryPermitChain,
    ProductionEffectEvidence,
    ProductionEffectEvidenceManifest,
    ProductionEvidenceManifests,
    ProductionExecutionOutcome,
    ProductionIdentityEvidenceManifest,
    ProductionIdentityResult,
    ProductionPolicyEvidenceManifest,
    ProductionPostconditionEvidenceManifest,
    ProductionRunReceipt,
    ProductionTerminalVerificationError,
    ProductionTerminalVerificationExpected,
    ProductionTerminalVerificationPayload,
    TerminalContractCounts,
    build_evidence_manifest,
    evidence_runner_signer_sha256,
    project_production_run_receipt,
    sign_production_terminal_verification,
    verify_production_terminal_verification,
)

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


def _permit_chain() -> ProductionDeliveryPermitChain:
    permit = ProductionDeliveryPermit(
        execution_authority_id="authority:run:1",
        execution_authority_sha256=SHA_A,
        permit_id="permit:1",
        permit_sha256=SHA_B,
        run_request_sha256=SHA_C,
        action_request_sha256=SHA_D,
        admission_artifact_sha256=SHA_D,
        evidence_identity_sha256=SHA_E,
        environment_digest=SHA_A,
        qualification_signer_registry_sha256=SHA_E,
        qualification_signer_registry_revision=7,
        qualification_signer_registry_checked_at="2026-08-18T11:59:30Z",
        qualification_signer_registry_expires_at="2026-08-20T11:00:00Z",
        input_edge_sequence=1,
        authority_sequence=0,
        runtime_delivery_sequence=9,
        issued_at="2026-08-18T12:00:00Z",
        delivered_at="2026-08-18T12:00:01Z",
    )
    return ProductionDeliveryPermitChain.build((permit,))


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
        execution_authority_id="authority:run:1",
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


def _payload() -> ProductionTerminalVerificationPayload:
    receipt = _receipt()
    chain = _permit_chain()
    return ProductionTerminalVerificationPayload(
        **IDS,
        flow_run_id_sha256=SHA_A,
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
        execution_authority_id="authority:run:1",
        execution_authority_sha256=SHA_A,
        permit_chain=chain,
        permit_count=1,
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
        permit_chain_sha256=payload.permit_chain.permit_chain_sha256,
        permit_count=payload.permit_count,
        final_authority_sequence=payload.final_authority_sequence,
        final_runtime_delivery_sequence=payload.final_runtime_delivery_sequence,
        workflow_contract_sha256=payload.workflow_contract_sha256,
        execution_outcome_sha256=payload.execution_outcome_sha256,
        run_receipt_sha256=payload.run_receipt_sha256,
        run_report_sha256=payload.run_report_sha256,
        run_report_object_version=payload.run_report_object_version,
        run_report_object_sha256=payload.run_report_object_sha256,
        evidence_manifests=payload.evidence_manifests,
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


def test_generic_success_cannot_parse_as_terminal_v2() -> None:
    with pytest.raises(ValidationError):
        ProductionTerminalVerificationPayload.model_validate({"status": "success"})


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
    stale = _permit_chain().entries[0].model_dump(mode="json")
    stale["qualification_signer_registry_checked_at"] = "2026-08-18T11:58:00Z"
    with pytest.raises(ValidationError, match="registry check is stale"):
        ProductionDeliveryPermit.model_validate(stale)


def test_permit_registry_check_at_sixty_seconds_is_accepted() -> None:
    fresh = _permit_chain().entries[0].model_dump(mode="json")
    fresh["qualification_signer_registry_checked_at"] = "2026-08-18T11:59:00Z"
    assert (
        ProductionDeliveryPermit.model_validate(fresh).issued_at
        == "2026-08-18T12:00:00Z"
    )


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
    second = first.model_copy(
        update={
            "permit_id": "permit:2",
            "action_request_sha256": SHA_E,
            "input_edge_sequence": 3,
            "authority_sequence": 1,
            "runtime_delivery_sequence": 10,
            "issued_at": "2026-08-18T12:00:02Z",
            "delivered_at": "2026-08-18T12:00:03Z",
        }
    )
    with pytest.raises(ValidationError, match="input-edge sequence"):
        ProductionDeliveryPermitChain.build((first, second))


def test_terminal_supports_multiple_input_edges_with_independent_sequences() -> None:
    first = _permit_chain().entries[0]
    second = first.model_copy(
        update={
            "permit_id": "permit:2",
            "permit_sha256": SHA_C,
            "action_request_sha256": SHA_E,
            "input_edge_sequence": 2,
            "authority_sequence": 1,
            "runtime_delivery_sequence": 10,
            "issued_at": "2026-08-18T12:00:02Z",
            "delivered_at": "2026-08-18T12:00:03Z",
        }
    )
    chain = ProductionDeliveryPermitChain.build((first, second))
    assert chain.entries[-1].authority_sequence == 1
    assert chain.entries[-1].runtime_delivery_sequence == 10


def test_terminal_rejects_registry_change_between_input_edges() -> None:
    first = _permit_chain().entries[0]
    second = first.model_copy(
        update={
            "permit_id": "permit:2",
            "permit_sha256": SHA_C,
            "action_request_sha256": SHA_E,
            "qualification_signer_registry_revision": 8,
            "input_edge_sequence": 2,
            "authority_sequence": 1,
            "runtime_delivery_sequence": 10,
            "issued_at": "2026-08-18T12:00:02Z",
            "delivered_at": "2026-08-18T12:00:03Z",
        }
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
        match="signature is invalid",
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
