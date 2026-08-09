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

from openadapt_flow.console.decision_relay import (
    RelayTransport,
    RelayUncertain,
    resolve_runner_token,
)
from openadapt_flow.interop.business_decision import (
    admit_portable_business_decision_answer,
    create_runner_business_decision_receipt_attestation,
    create_runner_business_decision_signature_attestation,
    project_recorded_business_decision_answer_receipt,
)
from openadapt_flow.runtime.durable.business_decision import BusinessDecisionStore

if TYPE_CHECKING:
    from openadapt_types import (
        BusinessDecisionAnswerV1,
        BusinessDecisionDeliveryPolicyV1,
        BusinessDecisionPresentationV1,
        BusinessDecisionTaskV1,
    )


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

        from openadapt_types import BusinessDecisionAnswerV1

        if not 0 <= wait_s <= 25:
            raise BusinessDecisionCloudRefused("the answer poll wait is invalid")
        try:
            status, raw = self._transport.post(
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
            or answer.task_id != self._task.task_id
            or answer.task_revision != self._task.task_revision
            or answer.task_digest != self._task.digest
        ):
            raise BusinessDecisionCloudRefused(
                "the answer delivery differs from the local task"
            )
        _parse_time(delivery["lease_expires_at"], "answer lease expiry")
        return BusinessDecisionCloudDelivery(
            answer_id=delivery["answer_id"],
            answer=answer,
            answer_digest=delivery["answer_digest"],
            lease_id=delivery["lease_id"],
            lease_attempt=lease_attempt,
            lease_expires_at=delivery["lease_expires_at"],
        )

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


__all__ = [
    "ANSWERS_POLL_PATH",
    "REGISTRATIONS_PATH",
    "BusinessDecisionCloudCycle",
    "BusinessDecisionCloudDelivery",
    "BusinessDecisionCloudKeys",
    "BusinessDecisionCloudRefused",
    "BusinessDecisionCloudRelay",
]
