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
    ProductionDeliveryPermitPayload,
    ProductionDeliveryReceiptPayload,
    ProductionEffectEvidence,
    ProductionEffectEvidenceManifest,
    ProductionEvidenceManifests,
    ProductionExecutionAuthorityPayload,
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
    rebuild_production_delivery_permit_chain_from_artifacts,
    sign_production_delivery_permit,
    sign_production_delivery_receipt,
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
        execution_authority_signer_sha256=(payload.execution_authority_signer_sha256),
        permit_chain_sha256=payload.permit_chain.permit_chain_sha256,
        permit_count=payload.permit_count,
        final_authority_sequence=payload.final_authority_sequence,
        final_runtime_delivery_sequence=payload.final_runtime_delivery_sequence,
        authenticated_runner_id_sha256=(
            payload.permit_chain.entries[0].authenticated_runner_id_sha256
        ),
        authenticated_session_id_sha256=(
            payload.permit_chain.entries[0].authenticated_session_id_sha256
        ),
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
