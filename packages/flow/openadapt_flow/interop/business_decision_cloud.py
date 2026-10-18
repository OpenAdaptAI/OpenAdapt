"""Outbound Cloud relay for one qualified typed business decision.

The customer runner owns the local request, role map, signing keys, and durable
answer. Cloud displays reviewed remote-safe copy and signs one finite answer.
This module connects those two boundaries without giving the browser execution
authority.

An answer is stored locally before its portable receipt is sent to Cloud. If
receipt delivery is uncertain, a later delivery of the same signed answer is
idempotent. The runner never repeats a business action on this transport path.
Fresh application revalidation and successor actuation happen later in the
normal durable runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional

from openadapt_flow.interop.business_decision import (
    admit_portable_business_decision_answer,
    create_runner_business_decision_receipt_attestation,
    create_runner_business_decision_signature_attestation,
    project_recorded_business_decision_answer_receipt,
)
from openadapt_flow.interop.decision_relay_transport import (
    RelayTransport,
    RelayUncertain,
    resolve_runner_token,
)
from openadapt_flow.runtime.durable.business_decision import BusinessDecisionStore

if TYPE_CHECKING:
    from openadapt_types import (
        BusinessDecisionAnswerV1,
        BusinessDecisionDeliveryPolicyV1,
        BusinessDecisionPresentationV1,
        BusinessDecisionTaskV1,
    )

    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import Policy


REGISTRATIONS_PATH = "/api/business-decisions/registrations"
ANSWERS_POLL_PATH = "/api/business-decisions/answers/poll"

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

_REGISTRATION_RESPONSE_KEYS = frozenset(
    {
        "accepted",
        "created",
        "state",
        "task_id",
        "task_revision",
        "task_digest",
        "presentation_digest",
        "one_use_scope_digest",
        "answer_authority",
        "local_evidence",
    }
)
_POLL_RESPONSE_KEYS = frozenset(
    {
        "delivery",
        "one_use",
        "runner_revalidation_required",
        "effect_outcome",
    }
)
_DELIVERY_KEYS = frozenset(
    {
        "answer_id",
        "answer",
        "answer_digest",
        "lease_id",
        "lease_attempt",
        "lease_expires_at",
    }
)
_RECEIPT_RESPONSE_KEYS = frozenset(
    {
        "accepted",
        "created",
        "state",
        "reason_code",
        "receipt_digest",
        "verified_effect",
    }
)


class BusinessDecisionCloudRefused(RuntimeError):
    """The Cloud relay contract failed. No successor action was authorized."""


@dataclass(frozen=True)
class BusinessDecisionCloudKeys:
    """Exact key and identity bindings for one qualified relay contract."""

    task_signing_key: bytes
    task_issuer_key_id: str
    qualification_signing_key: bytes
    qualification_issuer_key_id: str
    answer_signing_key: bytes
    answer_issuer_key_id: str
    receipt_signing_key: bytes
    receipt_issuer_key_id: str
    role_mapping_key: bytes


@dataclass(frozen=True)
class BusinessDecisionCloudDelivery:
    """One leased, signed Cloud answer for this exact local task."""

    answer_id: str
    answer: BusinessDecisionAnswerV1
    answer_digest: str
    lease_id: str
    lease_attempt: int
    lease_expires_at: str


@dataclass(frozen=True)
class BusinessDecisionCloudCycle:
    """Result of one poll, local admission, and receipt attempt."""

    delivery: BusinessDecisionCloudDelivery
    receipt_digest: str
    receipt_confirmed: bool


def poll_business_decision_cloud_answer(
    transport: RelayTransport,
    *,
    wait_s: float = 25.0,
) -> Optional[BusinessDecisionCloudDelivery]:
    """Poll one runner queue without assuming which local task it names."""

    from openadapt_types import BusinessDecisionAnswerV1

    if not 0 <= wait_s <= 25:
        raise BusinessDecisionCloudRefused("the answer poll wait is invalid")
    try:
        status, raw = transport.post(
            ANSWERS_POLL_PATH,
            {"wait_seconds": wait_s},
            timeout_s=wait_s + 10.0,
        )
    except RelayUncertain:
        return None
    if status == 204:
        return None
    if status >= 500:
        return None
    if status >= 400:
        raise BusinessDecisionCloudRefused("Cloud refused the answer poll")
    body = _exact_object(raw, _POLL_RESPONSE_KEYS, "answer poll response")
    if (
        body["one_use"] is not True
        or body["runner_revalidation_required"] is not True
        or body["effect_outcome"] != "not_reported_by_answer_delivery"
    ):
        raise BusinessDecisionCloudRefused(
            "the answer poll response weakens the runner contract"
        )
    delivery = _exact_object(body["delivery"], _DELIVERY_KEYS, "answer delivery")
    try:
        answer = BusinessDecisionAnswerV1.model_validate(delivery["answer"])
    except ValueError as exc:
        raise BusinessDecisionCloudRefused("the Cloud answer is invalid") from exc
    lease_attempt = delivery["lease_attempt"]
    if (
        not isinstance(delivery["answer_id"], str)
        or not _OPAQUE_ID.fullmatch(delivery["answer_id"])
        or not isinstance(delivery["answer_digest"], str)
        or not _DIGEST.fullmatch(delivery["answer_digest"])
        or delivery["answer_digest"] != answer.digest
        or not isinstance(delivery["lease_id"], str)
        or not _OPAQUE_ID.fullmatch(delivery["lease_id"])
        or not isinstance(lease_attempt, int)
        or isinstance(lease_attempt, bool)
        or lease_attempt < 1
        or not isinstance(delivery["lease_expires_at"], str)
    ):
        raise BusinessDecisionCloudRefused("the answer delivery is invalid")
    _parse_time(delivery["lease_expires_at"], "answer lease expiry")
    return BusinessDecisionCloudDelivery(
        answer_id=delivery["answer_id"],
        answer=answer,
        answer_digest=delivery["answer_digest"],
        lease_id=delivery["lease_id"],
        lease_attempt=lease_attempt,
        lease_expires_at=delivery["lease_expires_at"],
    )


def refuse_unmatched_business_decision_cloud_answer(
    transport: RelayTransport,
    delivery: BusinessDecisionCloudDelivery,
    *,
    runner_token: str,
    tenant_id: str,
    runner_id: str,
    answer_signing_key: bytes,
    expected_answer_issuer_key_id: str,
    receipt_signing_key: bytes,
    receipt_issuer_key_id: str,
    at: str,
    timeout_s: float = 15.0,
) -> bool:
    """Close one leased answer that has no matching local durable task.

    The signed refusal names only values already present in the leased answer.
    It discloses no local task inventory and creates no resume authority.
    """

    from openadapt_types import (
        BusinessDecisionAnswerReceiptReason,
        BusinessDecisionAnswerReceiptState,
        sign_business_decision_answer_receipt_hmac,
    )

    now = _parse_time(at, "answer refusal time")
    if now >= _parse_time(delivery.lease_expires_at, "answer lease expiry"):
        raise BusinessDecisionCloudRefused(
            "the Cloud answer lease expired before refusal"
        )
    answer = delivery.answer
    if answer.issuer_key_id != expected_answer_issuer_key_id or not answer.verify_hmac(
        answer_signing_key
    ):
        raise BusinessDecisionCloudRefused(
            "the unmatched Cloud answer signature is not trusted"
        )
    receipt = sign_business_decision_answer_receipt_hmac(
        key=receipt_signing_key,
        fields={
            "task_id": answer.task_id,
            "task_revision": answer.task_revision,
            "task_digest": answer.task_digest,
            "request_digest": answer.request_digest,
            "answer_digest": delivery.answer_digest,
            "option_id": answer.option_id,
            "state": BusinessDecisionAnswerReceiptState.REFUSED,
            "reason_code": BusinessDecisionAnswerReceiptReason.AUTHORIZATION_REFUSED,
            "runner_decision_receipt_digest": None,
            "decided_at": at,
            "issuer_key_id": receipt_issuer_key_id,
        },
    )
    token = resolve_runner_token(runner_token)
    attestation = create_runner_business_decision_receipt_attestation(
        receipt,
        receipt_signing_key=receipt_signing_key,
        expected_receipt_issuer_key_id=receipt_issuer_key_id,
        answer_id=delivery.answer_id,
        expected_tenant_id=tenant_id,
        expected_runner_id=runner_id,
        runner_bearer=token,
    )
    try:
        status, raw = transport.post(
            _receipt_path(delivery.answer_id),
            {
                "lease_id": delivery.lease_id,
                "receipt": receipt.model_dump(mode="json"),
                "runner_receipt_attestation": attestation,
            },
            timeout_s=timeout_s,
        )
    except RelayUncertain:
        return False
    if status >= 500:
        return False
    if status >= 400:
        raise BusinessDecisionCloudRefused("Cloud refused the unmatched answer receipt")
    body = _exact_object(raw, _RECEIPT_RESPONSE_KEYS, "answer receipt response")
    if (
        body["accepted"] is not True
        or not isinstance(body["created"], bool)
        or body["state"] != receipt.state.value
        or body["reason_code"] != receipt.reason_code.value
        or body["receipt_digest"] != receipt.digest
        or body["verified_effect"] is not False
    ):
        raise BusinessDecisionCloudRefused(
            "the unmatched answer response differs from the signed refusal"
        )
    return True


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BusinessDecisionCloudRefused(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BusinessDecisionCloudRefused(f"{label} must include a timezone")
    return parsed


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BusinessDecisionCloudRefused(f"{label} does not match the exact contract")
    return value


def _receipt_path(answer_id: str) -> str:
    if not _OPAQUE_ID.fullmatch(answer_id):
        raise BusinessDecisionCloudRefused("the answer id is invalid")
    return f"/api/business-decisions/answers/{answer_id}/receipt"


class BusinessDecisionCloudRelay:
    """Connect one local qualified decision to its hosted mobile surface."""

    def __init__(
        self,
        transport: RelayTransport,
        *,
        runner_token: str,
        store: BusinessDecisionStore,
        task: BusinessDecisionTaskV1,
        presentation: BusinessDecisionPresentationV1,
        delivery_policy: BusinessDecisionDeliveryPolicyV1,
        role_policy: Mapping[str, Any],
        role_refs: Mapping[str, str],
        route_ref: str,
        tenant_id: str,
        runner_id: str,
        keys: BusinessDecisionCloudKeys,
    ) -> None:
        self._transport = transport
        self._runner_token = resolve_runner_token(runner_token)
        self._store = store
        self._task = task
        self._presentation = presentation
        self._delivery_policy = delivery_policy
        self._role_policy = dict(role_policy)
        self._role_refs = dict(role_refs)
        self._route_ref = route_ref
        self._tenant_id = tenant_id
        self._runner_id = runner_id
        self._keys = keys
        if task.tenant_id != tenant_id or task.runner_id != runner_id:
            raise BusinessDecisionCloudRefused(
                "the portable task names another tenant or runner"
            )
        if not _OPAQUE_ID.fullmatch(route_ref):
            raise BusinessDecisionCloudRefused("the Cloud route reference is invalid")

    def publish(self, *, at: str, timeout_s: float = 15.0) -> Optional[bool]:
        """Publish this exact task.

        Returns ``True`` for a new registration, ``False`` for an exact replay,
        and ``None`` when delivery is uncertain.
        """

        attestation = create_runner_business_decision_signature_attestation(
            self._task,
            self._presentation,
            self._delivery_policy,
            self._role_policy,
            task_signing_key=self._keys.task_signing_key,
            expected_task_issuer_key_id=self._keys.task_issuer_key_id,
            qualification_signing_key=self._keys.qualification_signing_key,
            expected_qualification_issuer_key_id=(
                self._keys.qualification_issuer_key_id
            ),
            expected_tenant_id=self._tenant_id,
            expected_runner_id=self._runner_id,
            runner_bearer=self._runner_token,
            verified_at=at,
        )
        payload = {
            "task": self._task.model_dump(mode="json"),
            "presentation": self._presentation.model_dump(mode="json"),
            "delivery_policy": self._delivery_policy.model_dump(mode="json"),
            "role_policy": self._role_policy,
            "route_ref": self._route_ref,
            "runner_signature_attestation": attestation,
        }
        try:
            status, raw = self._transport.post(
                REGISTRATIONS_PATH, payload, timeout_s=timeout_s
            )
        except RelayUncertain:
            return None
        if status >= 500:
            return None
        if status >= 400:
            raise BusinessDecisionCloudRefused(
                "Cloud refused the business decision registration"
            )
        body = _exact_object(raw, _REGISTRATION_RESPONSE_KEYS, "registration response")
        if (
            body["accepted"] is not True
            or not isinstance(body["created"], bool)
            or body["state"] != "open"
            or body["task_id"] != self._task.task_id
            or body["task_revision"] != self._task.task_revision
            or body["task_digest"] != self._task.digest
            or body["presentation_digest"] != self._task.presentation_digest
            or body["one_use_scope_digest"] != self._task.idempotency_scope_digest
            or body["answer_authority"] != "withheld_until_authenticated_choice"
            or body["local_evidence"] != "not_accepted_by_cloud"
        ):
            raise BusinessDecisionCloudRefused(
                "the registration response differs from the local task"
            )
        return bool(body["created"])

    def poll(self, *, wait_s: float = 25.0) -> Optional[BusinessDecisionCloudDelivery]:
        """Poll for one answer leased to this runner."""
        delivery = poll_business_decision_cloud_answer(self._transport, wait_s=wait_s)
        if delivery is not None and not self.matches(delivery):
            raise BusinessDecisionCloudRefused(
                "the answer delivery differs from the local task"
            )
        return delivery

    @property
    def task_binding(self) -> tuple[str, int, str]:
        """Return the exact portable task key used by a shared supervisor."""

        return (self._task.task_id, self._task.task_revision, self._task.digest)

    def matches(self, delivery: BusinessDecisionCloudDelivery) -> bool:
        """Return true only when a delivery names this exact task revision."""

        answer = delivery.answer
        return (
            answer.task_id,
            answer.task_revision,
            answer.task_digest,
        ) == self.task_binding

    def record(
        self,
        delivery: BusinessDecisionCloudDelivery,
        *,
        at: str,
        timeout_s: float = 15.0,
    ) -> BusinessDecisionCloudCycle:
        """Store one answer locally and return its portable receipt to Cloud."""

        now = _parse_time(at, "answer time")
        if now >= _parse_time(delivery.lease_expires_at, "answer lease expiry"):
            raise BusinessDecisionCloudRefused(
                "the Cloud answer lease expired before local admission"
            )
        submission, principal = admit_portable_business_decision_answer(
            self._store,
            self._task,
            delivery.answer,
            task_signing_key=self._keys.task_signing_key,
            expected_task_issuer_key_id=self._keys.task_issuer_key_id,
            answer_signing_key=self._keys.answer_signing_key,
            expected_answer_issuer_key_id=self._keys.answer_issuer_key_id,
            expected_tenant_id=self._tenant_id,
            expected_runner_id=self._runner_id,
            role_refs=self._role_refs,
            role_mapping_key=self._keys.role_mapping_key,
            at=at,
        )
        self._store.submit(submission, principal=principal, now=now)
        receipt = project_recorded_business_decision_answer_receipt(
            self._store,
            self._task,
            delivery.answer,
            task_signing_key=self._keys.task_signing_key,
            expected_task_issuer_key_id=self._keys.task_issuer_key_id,
            answer_signing_key=self._keys.answer_signing_key,
            expected_answer_issuer_key_id=self._keys.answer_issuer_key_id,
            signing_key=self._keys.receipt_signing_key,
            issuer_key_id=self._keys.receipt_issuer_key_id,
            expected_tenant_id=self._tenant_id,
            expected_runner_id=self._runner_id,
            role_refs=self._role_refs,
            role_mapping_key=self._keys.role_mapping_key,
            at=at,
        )
        attestation = create_runner_business_decision_receipt_attestation(
            receipt,
            receipt_signing_key=self._keys.receipt_signing_key,
            expected_receipt_issuer_key_id=self._keys.receipt_issuer_key_id,
            answer_id=delivery.answer_id,
            expected_tenant_id=self._tenant_id,
            expected_runner_id=self._runner_id,
            runner_bearer=self._runner_token,
        )
        try:
            status, raw = self._transport.post(
                _receipt_path(delivery.answer_id),
                {
                    "lease_id": delivery.lease_id,
                    "receipt": receipt.model_dump(mode="json"),
                    "runner_receipt_attestation": attestation,
                },
                timeout_s=timeout_s,
            )
        except RelayUncertain:
            return BusinessDecisionCloudCycle(
                delivery=delivery,
                receipt_digest=receipt.digest,
                receipt_confirmed=False,
            )
        if status >= 500:
            return BusinessDecisionCloudCycle(
                delivery=delivery,
                receipt_digest=receipt.digest,
                receipt_confirmed=False,
            )
        if status >= 400:
            raise BusinessDecisionCloudRefused("Cloud refused the answer receipt")
        body = _exact_object(raw, _RECEIPT_RESPONSE_KEYS, "answer receipt response")
        if (
            body["accepted"] is not True
            or not isinstance(body["created"], bool)
            or body["state"] != receipt.state.value
            or body["reason_code"] != receipt.reason_code.value
            or body["receipt_digest"] != receipt.digest
            or body["verified_effect"] is not False
        ):
            raise BusinessDecisionCloudRefused(
                "the answer receipt response differs from the local receipt"
            )
        return BusinessDecisionCloudCycle(
            delivery=delivery,
            receipt_digest=receipt.digest,
            receipt_confirmed=True,
        )

    def serve_once(
        self, *, at: str, wait_s: float = 25.0
    ) -> Optional[BusinessDecisionCloudCycle]:
        """Poll once, store one exact answer, and attempt its receipt."""

        delivery = self.poll(wait_s=wait_s)
        return None if delivery is None else self.record(delivery, at=at)


def build_qualified_business_decision_cloud_relay(
    workflow: "Workflow",
    policy: "Policy",
    store: BusinessDecisionStore,
    transport: RelayTransport,
    *,
    runner_token: str,
    tenant_id: str,
    runner_id: str,
    keys: BusinessDecisionCloudKeys,
    privacy_key: bytes,
    at: str,
) -> BusinessDecisionCloudRelay:
    """Build one relay from the certified bundle and active durable request.

    Presentation copy, role mapping, routes, authentication strength, key IDs,
    and relay capability come from the qualification project. Secret key bytes,
    the runner token, and tenant scope come from the deployment boundary. No
    caller constructs a portable task or policy by hand.
    """

    from openadapt_types import (
        BusinessDecisionContextCardV1,
        BusinessDecisionContextKind,
        BusinessDecisionJudgmentReason,
        BusinessDecisionPresentationClassification,
        BusinessDecisionPresentationOptionV1,
        BusinessDecisionPresentationTextV1,
        BusinessDecisionPresentationV1,
        sign_business_decision_delivery_policy_hmac,
    )

    from openadapt_flow.interop.business_decision import (
        business_decision_role_mapping_digest,
        project_portable_business_decision_task,
    )
    from openadapt_flow.qualification import (
        current_certification_matches,
        workflow_contract_sha256,
    )
    from openadapt_flow.qualified_business_decisions import (
        resolve_qualified_business_decision,
    )
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

    project = workflow.qualification
    if project is None or not current_certification_matches(workflow, policy=policy):
        raise BusinessDecisionCloudRefused(
            "the workflow has no current reproducible certification"
        )
    request, _request_sha256 = store.read_active_request()
    manifest = CheckpointStore(store.run_dir, key=store.checkpoint_key).read_manifest()
    if manifest is None:
        raise BusinessDecisionCloudRefused(
            "the active decision has no durable run manifest"
        )
    if (
        request.run_id != manifest.run_id
        or request.workflow_name != manifest.workflow_name
        or request.workflow_name != workflow.name
        or request.workflow_contract_sha256 != workflow_contract_sha256(workflow)
        or request.bundle_version != bundle_version(manifest.bundle_dir)
    ):
        raise BusinessDecisionCloudRefused(
            "the certified workflow differs from the active durable run"
        )
    matches = [
        item
        for item in project.business_decision_deliveries
        if item.graph_id == request.graph_id and item.state_id == request.state_id
    ]
    if len(matches) != 1:
        raise BusinessDecisionCloudRefused(
            "the active decision has no unique qualified delivery binding"
        )
    binding = matches[0]
    try:
        decision = resolve_qualified_business_decision(workflow, binding)
    except ValueError as exc:
        raise BusinessDecisionCloudRefused(str(exc)) from exc
    if request.decision.contract_sha256() != decision.contract_sha256():
        raise BusinessDecisionCloudRefused(
            "the active request differs from the qualified decision"
        )
    expected_key_ids = {
        "task": (keys.task_issuer_key_id, binding.task_issuer_key_id),
        "qualification": (
            keys.qualification_issuer_key_id,
            binding.qualification_issuer_key_id,
        ),
        "answer": (keys.answer_issuer_key_id, binding.answer_issuer_key_id),
        "receipt": (keys.receipt_issuer_key_id, binding.receipt_issuer_key_id),
    }
    for purpose, (actual, expected) in expected_key_ids.items():
        if actual != expected:
            raise BusinessDecisionCloudRefused(
                f"the deployment {purpose} key id differs from qualification"
            )

    classification = BusinessDecisionPresentationClassification.REVIEWED_REMOTE_SAFE
    egress = binding.egress_review_digest

    def text(value: str) -> BusinessDecisionPresentationTextV1:
        return BusinessDecisionPresentationTextV1(
            text=value,
            classification=classification,
            egress_review_digest=egress,
        )

    context_cards = tuple(
        BusinessDecisionContextCardV1(
            context_id=card.context_id,
            kind=BusinessDecisionContextKind(card.kind),
            label=text(card.label),
            value=text(card.value),
        )
        for card in binding.context_cards
    )
    option_presentations: list[BusinessDecisionPresentationOptionV1] = []
    for option in decision.options:
        copy = binding.option_copy.get(option.id)
        detail = (
            text(copy.detail) if copy is not None and copy.detail is not None else None
        )
        consequence = (
            text(copy.consequence)
            if copy is not None and copy.consequence is not None
            else None
        )
        option_presentations.append(
            BusinessDecisionPresentationOptionV1(
                option_id=option.id,
                label=text(option.label),
                detail=detail,
                consequence=consequence,
            )
        )

    presentation = BusinessDecisionPresentationV1(
        presentation_ref=binding.presentation_ref,
        presentation_revision=binding.presentation_revision,
        decision_contract_digest="sha256:" + binding.decision_contract_sha256,
        decision_contract_revision=binding.decision_contract_revision,
        category=text(binding.category) if binding.category is not None else None,
        title=text(binding.title) if binding.title is not None else None,
        role_label=(
            text(binding.role_label) if binding.role_label is not None else None
        ),
        question=text(decision.question),
        why_judgment_needed=(
            text(binding.why_judgment_needed)
            if binding.why_judgment_needed is not None
            else None
        ),
        context_cards=context_cards,
        options=tuple(option_presentations),
        reason_codes=tuple(
            BusinessDecisionJudgmentReason(value) for value in binding.reason_codes
        ),
        review_contract_digest=binding.review_contract_digest,
    )
    role_mapping_digest = business_decision_role_mapping_digest(
        binding.role_refs, key=keys.role_mapping_key
    )
    delivery_policy = sign_business_decision_delivery_policy_hmac(
        key=keys.qualification_signing_key,
        fields={
            "policy_ref": binding.policy_ref,
            "policy_revision": binding.policy_revision,
            "decision_contract_digest": presentation.decision_contract_digest,
            "decision_contract_revision": binding.decision_contract_revision,
            "presentation_ref": presentation.presentation_ref,
            "presentation_digest": presentation.digest,
            "presentation_egress_review_digest": egress,
            "authorized_role_refs": tuple(
                binding.role_refs[role] for role in decision.authorized_roles
            ),
            "authorized_route_refs": binding.authorized_route_refs,
            "authorized_answer_issuer_key_ids": (binding.answer_issuer_key_id,),
            "role_mapping_digest": role_mapping_digest,
            "required_authn": binding.required_authn,
            "delivery_mode": "remote_answerable",
            "relay_capability_digest": binding.relay_capability_digest,
            "created_at": request.issued_at,
            "expires_at": request.expires_at,
            "issuer_key_id": binding.qualification_issuer_key_id,
        },
    )
    task = project_portable_business_decision_task(
        store,
        signing_key=keys.task_signing_key,
        tenant_id=tenant_id,
        runner_id=runner_id,
        presentation=presentation,
        delivery_policy=delivery_policy,
        delivery_policy_signing_key=keys.qualification_signing_key,
        expected_delivery_policy_issuer_key_id=(binding.qualification_issuer_key_id),
        role_refs=binding.role_refs,
        role_mapping_key=keys.role_mapping_key,
        privacy_key=privacy_key,
        active_relay_capability_digest=binding.relay_capability_digest,
        issuer_key_id=binding.task_issuer_key_id,
        at=at,
    )
    route_ref = binding.registration_route_ref
    role_policy = {
        "schema_version": "openadapt.cloud-business-decision-role-policy/v1",
        "policy_ref": binding.policy_ref,
        "task_digest": task.digest,
    }
    return BusinessDecisionCloudRelay(
        transport,
        runner_token=runner_token,
        store=store,
        task=task,
        presentation=presentation,
        delivery_policy=delivery_policy,
        role_policy=role_policy,
        role_refs=binding.role_refs,
        route_ref=route_ref,
        tenant_id=tenant_id,
        runner_id=runner_id,
        keys=keys,
    )


__all__ = [
    "ANSWERS_POLL_PATH",
    "REGISTRATIONS_PATH",
    "BusinessDecisionCloudCycle",
    "BusinessDecisionCloudDelivery",
    "BusinessDecisionCloudKeys",
    "BusinessDecisionCloudRefused",
    "BusinessDecisionCloudRelay",
    "build_qualified_business_decision_cloud_relay",
    "poll_business_decision_cloud_answer",
    "refuse_unmatched_business_decision_cloud_answer",
]
