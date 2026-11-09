"""Portable business-decision projection and admission seams.

Flow keeps the complete question, labels, roles, values, and local evidence in
the customer run directory. This module projects only reviewed static copy and
opaque signed bindings into the shared ``openadapt-types`` mobile contract.

The portable task is presentation and authentication data. It is not execution
authority. Flow still authenticates the active request, records the one-use
answer, reacquires the live state, and applies all successor identity and effect
gates before actuation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from openadapt_flow.runtime.durable.business_decision import (
    BusinessDecisionPrincipal,
    BusinessDecisionStore,
    BusinessDecisionSubmission,
)

if TYPE_CHECKING:
    from openadapt_types import (
        BusinessDecisionAnswerReceiptV1 as PortableBusinessDecisionAnswerReceiptV1,
    )
    from openadapt_types import (
        BusinessDecisionAnswerV1 as PortableBusinessDecisionAnswerV1,
    )
    from openadapt_types import (
        BusinessDecisionDeliveryPolicyV1 as PortableBusinessDecisionDeliveryPolicyV1,
    )
    from openadapt_types import (
        BusinessDecisionPresentationV1 as PortableBusinessDecisionPresentationV1,
    )
    from openadapt_types import (
        BusinessDecisionTaskV1 as PortableBusinessDecisionTaskV1,
    )


_ROLE_MAPPING_DOMAIN = b"openadapt.business-decision-role-mapping/v1\x00"
_OPAQUE_ALIAS_DOMAIN = b"openadapt.business-decision-opaque-alias/v1\x00"
_PORTABLE_IDEMPOTENCY_DOMAIN = (
    b"openadapt.business-decision-portable-idempotency/v1\x00"
)
_RUNNER_SIGNATURE_ATTESTATION_DOMAIN = (
    b"openadapt.runner-business-decision-signature-attestation/v1\x00"
)
_RUNNER_RECEIPT_ATTESTATION_DOMAIN = (
    b"openadapt.runner-business-decision-receipt-attestation/v1\x00"
)
_RUNNER_BEARER = re.compile(r"^oar_[A-Za-z0-9_-]{16,200}$")


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _runner_hmac(token: str, domain: bytes, payload: Any) -> str:
    if not isinstance(token, str) or not _RUNNER_BEARER.fullmatch(token):
        raise ValueError("the runner bearer is invalid")
    return (
        "hmac-sha256:"
        + hmac.new(
            token.encode("utf-8"), domain + _canonical_json(payload), hashlib.sha256
        ).hexdigest()
    )


def create_runner_business_decision_signature_attestation(
    task: PortableBusinessDecisionTaskV1,
    presentation: PortableBusinessDecisionPresentationV1,
    delivery_policy: PortableBusinessDecisionDeliveryPolicyV1,
    role_policy: Mapping[str, Any],
    *,
    task_signing_key: bytes,
    expected_task_issuer_key_id: str,
    qualification_signing_key: bytes,
    expected_qualification_issuer_key_id: str,
    expected_tenant_id: str,
    expected_runner_id: str,
    runner_bearer: str,
    verified_at: str,
) -> dict[str, Any]:
    """Attest that the exact customer signatures passed on this runner."""

    from openadapt_types import validate_business_decision_delivery

    if task.issuer_key_id != expected_task_issuer_key_id:
        raise ValueError("the portable task signing key id is not trusted")
    if delivery_policy.issuer_key_id != expected_qualification_issuer_key_id:
        raise ValueError("the qualification signing key id is not trusted")
    if task.tenant_id != expected_tenant_id or task.runner_id != expected_runner_id:
        raise ValueError("the portable task names another tenant or runner")
    _parse(verified_at)
    validate_business_decision_delivery(
        task,
        presentation,
        delivery_policy,
        task_signing_key=task_signing_key,
        qualification_signing_key=qualification_signing_key,
        at=verified_at,
    )
    envelope = {
        "task": _json_value(task),
        "delivery_policy": _json_value(delivery_policy),
        "presentation": _json_value(presentation),
        "role_policy": _json_value(role_policy),
    }
    unsigned = {
        "schema_version": (
            "openadapt.runner-business-decision-signature-attestation/v1"
        ),
        "org_id": expected_tenant_id,
        "runner_id": expected_runner_id,
        "envelope_digest": _digest(envelope),
        "verified_signature_kinds": [
            "business_decision_task",
            "business_decision_delivery_policy",
        ],
        "verified_at": verified_at,
        "signature_algorithm": "hmac-sha256",
    }
    return {
        **unsigned,
        "signature": _runner_hmac(
            runner_bearer, _RUNNER_SIGNATURE_ATTESTATION_DOMAIN, unsigned
        ),
    }


def create_runner_business_decision_receipt_attestation(
    receipt: PortableBusinessDecisionAnswerReceiptV1,
    *,
    receipt_signing_key: bytes,
    expected_receipt_issuer_key_id: str,
    answer_id: str,
    expected_tenant_id: str,
    expected_runner_id: str,
    runner_bearer: str,
) -> str:
    """Bind one signed portable receipt to the authenticated runner route."""

    if receipt.issuer_key_id != expected_receipt_issuer_key_id:
        raise ValueError("the portable receipt signing key id is not trusted")
    if not receipt.verify_hmac(receipt_signing_key):
        raise ValueError("the portable receipt signature is invalid")
    receipt_payload = _json_value(receipt)
    receipt_payload.pop("signature")
    return _runner_hmac(
        runner_bearer,
        _RUNNER_RECEIPT_ATTESTATION_DOMAIN,
        {
            "answer_id": answer_id,
            "org_id": expected_tenant_id,
            "receipt_digest": _digest(receipt_payload),
            "runner_id": expected_runner_id,
        },
    )


def _prefixed_digest(value: str) -> str:
    return value if value.startswith("sha256:") else "sha256:" + value


def _require_key(key: bytes, purpose: str) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError(f"{purpose} key must contain at least 32 bytes")


def business_decision_role_mapping_digest(
    role_refs: Mapping[str, str], *, key: bytes
) -> str:
    """Return a keyed commitment to local-role and opaque-role bindings."""

    _require_key(key, "role mapping")
    payload = _canonical_json(dict(sorted(role_refs.items())))
    return (
        "hmac-sha256:"
        + hmac.new(key, _ROLE_MAPPING_DOMAIN + payload, hashlib.sha256).hexdigest()
    )


def _opaque_alias(purpose: str, value: str, *, key: bytes) -> str:
    _require_key(key, "privacy alias")
    payload = _OPAQUE_ALIAS_DOMAIN + purpose.encode("ascii") + b"\x00" + value.encode()
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{purpose}_{digest}"


def _portable_submission_idempotency_key(task: Any, answer: Any) -> str:
    """Bind Flow's one-use answer to the exact signed portable envelope."""

    payload = _canonical_json(
        {
            "answer_digest": answer.digest,
            "idempotency_scope_digest": task.idempotency_scope_digest,
            "portable_idempotency_key": answer.idempotency_key,
        }
    )
    digest = hashlib.sha256(_PORTABLE_IDEMPOTENCY_DOMAIN + payload).hexdigest()
    return f"portable_{digest}"


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("business decision time must include a timezone")
    return parsed


def _request_revision(store: BusinessDecisionStore, request: Any) -> int:
    revision = 1
    current = request
    for _ in range(4096):
        predecessor_sha = current.supersedes_request_sha256
        if predecessor_sha is None:
            return revision
        current = store.authenticate_request(predecessor_sha)
        revision += 1
    raise ValueError("business decision renewal chain exceeds the supported limit")


def _verify_presentation(
    request: Any,
    presentation: PortableBusinessDecisionPresentationV1,
    *,
    decision_digest: str,
    decision_revision: int,
) -> None:
    from openadapt_types import business_decision_presentation_text_digest

    if (
        presentation.decision_contract_digest != decision_digest
        or presentation.decision_contract_revision != decision_revision
        or presentation.question.text != request.decision.question
        or presentation.question.content_digest
        != business_decision_presentation_text_digest(request.decision.question)
    ):
        raise ValueError("the reviewed presentation differs from the decision contract")
    expected_options = tuple(
        (option.id, option.label) for option in request.decision.options
    )
    actual_options = tuple(
        (option.option_id, option.label.text) for option in presentation.options
    )
    if actual_options != expected_options:
        raise ValueError("the reviewed option presentation differs from the contract")
    for option, expected in zip(presentation.options, request.decision.options):
        if option.label.content_digest != business_decision_presentation_text_digest(
            expected.label
        ):
            raise ValueError(
                "the reviewed option presentation digest differs from the contract"
            )


def project_portable_business_decision_task(
    store: BusinessDecisionStore,
    *,
    signing_key: bytes,
    tenant_id: str,
    runner_id: str,
    presentation: PortableBusinessDecisionPresentationV1,
    delivery_policy: PortableBusinessDecisionDeliveryPolicyV1,
    delivery_policy_signing_key: bytes,
    expected_delivery_policy_issuer_key_id: str,
    role_refs: Mapping[str, str],
    role_mapping_key: bytes,
    privacy_key: bytes,
    active_relay_capability_digest: str,
    issuer_key_id: str,
    at: str,
) -> PortableBusinessDecisionTaskV1:
    """Build one signed remote-safe task from the active authenticated request."""

    from openadapt_types import (
        BusinessDecisionDeliveryMode,
        sign_business_decision_task_hmac,
        validate_business_decision_delivery,
    )

    request, _request_sha256 = store.read_active_request()
    now = _parse(at)
    if delivery_policy.issuer_key_id != expected_delivery_policy_issuer_key_id:
        raise ValueError("the business decision policy key id is not trusted")
    if not delivery_policy.verify_hmac(delivery_policy_signing_key):
        raise ValueError("the business decision delivery policy signature is invalid")
    if not (
        _parse(delivery_policy.created_at) <= now < _parse(delivery_policy.expires_at)
    ):
        raise ValueError("the business decision delivery policy is not active")
    if _parse(delivery_policy.created_at) > _parse(request.issued_at):
        raise ValueError("the delivery policy was issued after the decision request")
    if _parse(delivery_policy.expires_at) < _parse(request.expires_at):
        raise ValueError("the delivery policy expires before the decision request")

    expected_roles = set(request.decision.authorized_roles)
    if set(role_refs) != expected_roles:
        raise ValueError(
            "the portable role mapping must cover the exact decision roles"
        )
    if len(set(role_refs.values())) != len(role_refs):
        raise ValueError("portable role references must map to one local role each")
    role_mapping_digest = business_decision_role_mapping_digest(
        role_refs, key=role_mapping_key
    )

    decision_digest = _prefixed_digest(request.decision.contract_sha256())
    decision_revision = delivery_policy.decision_contract_revision
    _verify_presentation(
        request,
        presentation,
        decision_digest=decision_digest,
        decision_revision=decision_revision,
    )
    expected_role_refs = tuple(
        role_refs[role] for role in request.decision.authorized_roles
    )
    expected_policy = {
        "decision_contract_digest": (
            delivery_policy.decision_contract_digest,
            decision_digest,
        ),
        "presentation_ref": (
            delivery_policy.presentation_ref,
            presentation.presentation_ref,
        ),
        "presentation_digest": (
            delivery_policy.presentation_digest,
            presentation.digest,
        ),
        "authorized_role_refs": (
            delivery_policy.authorized_role_refs,
            expected_role_refs,
        ),
        "role_mapping_digest": (
            delivery_policy.role_mapping_digest,
            role_mapping_digest,
        ),
        "relay_capability_digest": (
            delivery_policy.relay_capability_digest,
            _prefixed_digest(active_relay_capability_digest),
        ),
    }
    for name, (actual, expected) in expected_policy.items():
        if actual != expected:
            raise ValueError(f"the delivery policy {name} differs")

    required_evidence = {
        evidence_id
        for option in request.decision.options
        for evidence_id in option.required_evidence
    }
    if (
        delivery_policy.delivery_mode is BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE
        and required_evidence
    ):
        raise ValueError(
            "a remote business answer cannot require protected local evidence"
        )

    option_bindings = tuple(
        {
            "option_id": option.id,
            "target_binding_digest": _digest(
                {
                    "decision_contract_digest": decision_digest,
                    "option_id": option.id,
                    "target": option.target,
                }
            ),
        }
        for option in request.decision.options
    )
    program_scope_digest = _digest(
        [frame.model_dump(mode="json") for frame in request.program_scope]
    )
    request_revision = _request_revision(store, request)
    run_alias = _opaque_alias("run", request.run_id, key=privacy_key)
    pause_alias = _opaque_alias("pause", request.pause_binding_sha256, key=privacy_key)
    task_id = _opaque_alias("task", request.digest, key=privacy_key)
    idempotency_scope_digest = _digest(
        {
            "tenant_id": tenant_id,
            "runner_id": runner_id,
            "run_id": run_alias,
            "pause_id": pause_alias,
            "request_digest": request.digest,
        }
    )
    fields: dict[str, object] = {
        "task_id": task_id,
        "task_revision": request_revision,
        "tenant_id": tenant_id,
        "runner_id": runner_id,
        "run_id": run_alias,
        "pause_id": pause_alias,
        "pause_binding_digest": request.pause_binding_sha256,
        "request_id": request.request_id,
        "request_revision": request_revision,
        "request_digest": request.digest,
        "supersedes_request_digest": request.supersedes_request_digest,
        "bundle_digest": _prefixed_digest(request.bundle_version),
        "workflow_contract_digest": _prefixed_digest(request.workflow_contract_sha256),
        "governed_runtime_inputs_digest": (
            _prefixed_digest(request.governed_runtime_inputs_digest)
            if request.governed_runtime_inputs_digest is not None
            else None
        ),
        "decision_contract_digest": decision_digest,
        "decision_contract_revision": decision_revision,
        "delivery_policy_digest": delivery_policy.digest,
        "program_scope_digest": program_scope_digest,
        "control_frames_digest": _prefixed_digest(request.control_frames_sha256),
        "presentation_ref": presentation.presentation_ref,
        "presentation_digest": presentation.digest,
        "options": option_bindings,
        "authorized_role_refs": expected_role_refs,
        "authorized_route_refs": delivery_policy.authorized_route_refs,
        "authorized_answer_issuer_key_ids": (
            delivery_policy.authorized_answer_issuer_key_ids
        ),
        "role_mapping_digest": role_mapping_digest,
        "required_authn": delivery_policy.required_authn,
        "delivery_mode": delivery_policy.delivery_mode,
        "local_evidence_required": bool(required_evidence),
        "required_evidence_count": len(required_evidence),
        "relay_capability_digest": delivery_policy.relay_capability_digest,
        "idempotency_scope_digest": idempotency_scope_digest,
        "created_at": request.issued_at,
        "expires_at": request.expires_at,
        "issuer_key_id": issuer_key_id,
    }
    task = sign_business_decision_task_hmac(key=signing_key, fields=fields)
    validate_business_decision_delivery(
        task,
        presentation,
        delivery_policy,
        task_signing_key=signing_key,
        qualification_signing_key=delivery_policy_signing_key,
        at=at,
    )
    return task


def admit_portable_business_decision_answer(
    store: BusinessDecisionStore,
    task: PortableBusinessDecisionTaskV1,
    answer: PortableBusinessDecisionAnswerV1,
    *,
    task_signing_key: bytes,
    expected_task_issuer_key_id: str,
    answer_signing_key: bytes,
    expected_answer_issuer_key_id: str,
    expected_tenant_id: str,
    expected_runner_id: str,
    role_refs: Mapping[str, str],
    role_mapping_key: bytes,
    at: str,
) -> tuple[BusinessDecisionSubmission, BusinessDecisionPrincipal]:
    """Authenticate one remote answer and return Flow's local input models."""

    from openadapt_types import validate_business_decision_answer

    if task.issuer_key_id != expected_task_issuer_key_id:
        raise ValueError("the portable task signing key id is not trusted")
    if answer.issuer_key_id != expected_answer_issuer_key_id:
        raise ValueError("the portable answer signing key id is not trusted")
    if task.tenant_id != expected_tenant_id or task.runner_id != expected_runner_id:
        raise ValueError("the portable task names another tenant or runner")
    validate_business_decision_answer(
        task,
        answer,
        task_signing_key=task_signing_key,
        answer_signing_key=answer_signing_key,
        at=at,
    )
    if (
        business_decision_role_mapping_digest(role_refs, key=role_mapping_key)
        != task.role_mapping_digest
    ):
        raise ValueError("the local role mapping differs from the signed task")
    active_request, _request_sha256 = store.read_active_request()
    if active_request.digest != task.request_digest:
        raise ValueError("the portable task no longer names the active request")

    local_roles = [
        local_role
        for local_role, remote_ref in role_refs.items()
        if remote_ref == answer.authenticated_role_ref
    ]
    if len(local_roles) != 1:
        raise ValueError("the authenticated role reference is not uniquely mapped")

    submission = BusinessDecisionSubmission(
        request_digest=answer.request_digest,
        idempotency_key=_portable_submission_idempotency_key(task, answer),
        option_id=answer.option_id,
        evidence_artifact_sha256s={},
    )
    principal = BusinessDecisionPrincipal(
        operator_ref=answer.authenticated_principal_ref,
        roles=(local_roles[0],),
        authenticated_by=answer.authenticated_route_ref,
        authentication_context_sha256=(
            answer.authentication_context_digest.removeprefix("sha256:")
        ),
    )
    return submission, principal


def project_recorded_business_decision_answer_receipt(
    store: BusinessDecisionStore,
    task: PortableBusinessDecisionTaskV1,
    answer: PortableBusinessDecisionAnswerV1,
    *,
    task_signing_key: bytes,
    expected_task_issuer_key_id: str,
    answer_signing_key: bytes,
    expected_answer_issuer_key_id: str,
    signing_key: bytes,
    issuer_key_id: str,
    expected_tenant_id: str,
    expected_runner_id: str,
    role_refs: Mapping[str, str],
    role_mapping_key: bytes,
    at: str,
) -> PortableBusinessDecisionAnswerReceiptV1:
    """Return a signed portable receipt for an answer retained by Flow."""

    from openadapt_types import (
        sign_business_decision_answer_receipt_hmac,
        validate_business_decision_answer,
    )

    if task.issuer_key_id != expected_task_issuer_key_id:
        raise ValueError("the portable task signing key id is not trusted")
    if answer.issuer_key_id != expected_answer_issuer_key_id:
        raise ValueError("the portable answer signing key id is not trusted")
    if task.tenant_id != expected_tenant_id or task.runner_id != expected_runner_id:
        raise ValueError("the portable task names another tenant or runner")
    if (
        business_decision_role_mapping_digest(role_refs, key=role_mapping_key)
        != task.role_mapping_digest
    ):
        raise ValueError("the local role mapping differs from the signed task")
    retained = store.read_receipt(task.request_digest)
    if retained is None:
        raise ValueError("Flow has not retained the business decision answer")
    local_receipt, _local_receipt_sha = retained
    if _parse(at) < _parse(local_receipt.decided_at):
        raise ValueError("the receipt retrieval time predates the retained answer")
    validate_business_decision_answer(
        task,
        answer,
        task_signing_key=task_signing_key,
        answer_signing_key=answer_signing_key,
        at=local_receipt.decided_at,
    )
    local_roles = [
        local_role
        for local_role, remote_ref in role_refs.items()
        if remote_ref == answer.authenticated_role_ref
    ]
    if len(local_roles) != 1:
        raise ValueError("the authenticated role reference is not uniquely mapped")
    expected_idempotency_sha256 = hashlib.sha256(
        _portable_submission_idempotency_key(task, answer).encode("utf-8")
    ).hexdigest()
    if (
        local_receipt.request_digest != task.request_digest
        or local_receipt.option_id != answer.option_id
        or local_receipt.operator_ref != answer.authenticated_principal_ref
        or local_receipt.authorized_role != local_roles[0]
        or local_receipt.authenticated_by != answer.authenticated_route_ref
        or local_receipt.authentication_context_sha256
        != answer.authentication_context_digest.removeprefix("sha256:")
        or local_receipt.idempotency_key_sha256 != expected_idempotency_sha256
    ):
        raise ValueError("the retained Flow receipt differs from the portable answer")
    return sign_business_decision_answer_receipt_hmac(
        key=signing_key,
        fields={
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "task_digest": task.digest,
            "request_digest": task.request_digest,
            "answer_digest": answer.digest,
            "option_id": answer.option_id,
            "state": "answer_recorded",
            "reason_code": "recorded_pending_revalidation",
            "runner_decision_receipt_digest": local_receipt.digest,
            "decided_at": local_receipt.decided_at,
            "issuer_key_id": issuer_key_id,
        },
    )


__all__ = [
    "admit_portable_business_decision_answer",
    "business_decision_role_mapping_digest",
    "create_runner_business_decision_receipt_attestation",
    "create_runner_business_decision_signature_attestation",
    "project_portable_business_decision_task",
    "project_recorded_business_decision_answer_receipt",
]
