"""Local-dev qualification signer that cannot enter a production trust map.

Quickstart and MockMed can sign a local pin-confirmation admission with this
key. The key, its issuer workflow, and its ref prefix are public and
intentional. Production verification refuses them. Loading a production
signer registry that contains this public key fails closed.

This is not a Production workflow admission. Campaign trial floors, silent
incorrect success, and over-halt still apply before a production authority
will sign. Local-dev only proves the operator confirmed the mined pins.
"""

from __future__ import annotations

import hashlib
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal, Mapping
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.qualification_admission import (
    QualificationAdmissionError,
    QualificationAdmissionPayload,
    QualificationSignerTrust,
    qualification_signer_key_id,
    sign_qualification_admission,
    verify_qualification_admission,
)

LOCAL_DEV_SEED: Final[bytes] = b"openadapt-flow local-dev qualification signer v1"
LOCAL_DEV_PRIVATE_BYTES: Final[bytes] = hashlib.sha256(LOCAL_DEV_SEED).digest()
LOCAL_DEV_PRIVATE_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.from_private_bytes(
    LOCAL_DEV_PRIVATE_BYTES
)
LOCAL_DEV_PUBLIC_BYTES: Final[bytes] = LOCAL_DEV_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
LOCAL_DEV_PUBLIC_KEY_B64: Final[str] = b64encode(LOCAL_DEV_PUBLIC_BYTES).decode("ascii")
LOCAL_DEV_KEY_ID: Final[str] = qualification_signer_key_id(LOCAL_DEV_PUBLIC_BYTES)
LOCAL_DEV_ISSUER_WORKFLOW: Final[Literal["openadapt-flow/local-dev-admission"]] = (
    "openadapt-flow/local-dev-admission"
)
LOCAL_DEV_REF_PREFIX: Final[str] = "local-dev@"
LOCAL_DEV_PURPOSE: Final[Literal["local-dev"]] = "local-dev"
PRODUCTION_ISSUER_WORKFLOW: Final[str] = (
    "OpenAdaptAI/openadapt-internal/.github/workflows/"
    "production-qualification-admission.yml"
)
LOCAL_DEV_SCHEMA: Final[Literal["openadapt.local-dev-admission/v1"]] = (
    "openadapt.local-dev-admission/v1"
)


class LocalDevAdmissionError(ValueError):
    """A local-dev admission could not be signed or verified."""


def is_local_dev_public_key(public_key: bytes | str) -> bool:
    """True when the bytes or base64 payload is the well-known local-dev key."""

    if isinstance(public_key, str):
        from base64 import b64decode

        try:
            raw = b64decode(public_key, validate=True)
        except ValueError:
            return False
    else:
        raw = public_key
    return raw == LOCAL_DEV_PUBLIC_BYTES


def is_local_dev_key_id(key_id: str) -> bool:
    return key_id == LOCAL_DEV_KEY_ID


def is_local_dev_issuer_workflow(workflow: str) -> bool:
    return workflow == LOCAL_DEV_ISSUER_WORKFLOW


def reject_local_dev_in_production_trust(
    trusted_signers: Mapping[str, QualificationSignerTrust],
) -> None:
    """Refuse a production trust map that contains the local-dev signer."""

    for key_id, trust in trusted_signers.items():
        if is_local_dev_key_id(key_id) or is_local_dev_public_key(trust.public_key):
            raise QualificationAdmissionError(
                "local-dev qualification signer cannot enter a production trust map"
            )
        if any(is_local_dev_issuer_workflow(item) for item in trust.allowed_workflows):
            raise QualificationAdmissionError(
                "local-dev issuer workflow cannot enter a production trust map"
            )


def local_dev_signer_trust() -> dict[str, QualificationSignerTrust]:
    """Trust map that accepts only the local-dev signer.

    Never pass this mapping to a production verifier.
    """

    return {
        LOCAL_DEV_KEY_ID: QualificationSignerTrust(
            public_key=LOCAL_DEV_PUBLIC_KEY_B64,
            allowed_workflows=(LOCAL_DEV_ISSUER_WORKFLOW,),
            allowed_ref_prefixes=(LOCAL_DEV_REF_PREFIX,),
        )
    }


class LocalDevAdmission(BaseModel):
    """Signed local pin-confirmation. Not a production workflow admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.local-dev-admission/v1"] = LOCAL_DEV_SCHEMA
    purpose: Literal["local-dev"] = LOCAL_DEV_PURPOSE
    admission_id: str
    bundle_content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    effect_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_pack: str
    issued_at: str
    expires_at: str
    issuer_workflow: Literal["openadapt-flow/local-dev-admission"] = (
        LOCAL_DEV_ISSUER_WORKFLOW
    )
    issuer_key_id: str
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str = Field(min_length=88, max_length=88)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _placeholder_digest(label: str) -> str:
    return hashlib.sha256(f"openadapt.local-dev.{label}".encode("utf-8")).hexdigest()


def sign_local_dev_admission(
    *,
    bundle_content_digest: str,
    proposal_sha256: str,
    environment_digest: str,
    identity_contract_sha256: str,
    effect_contract_sha256: str,
    policy_pack: str,
    now: datetime | None = None,
    lifetime: timedelta = timedelta(days=7),
) -> LocalDevAdmission:
    """Sign a local-dev pin confirmation with the well-known test key."""

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = _utc(clock)
    expires = _utc(clock + lifetime)
    unsigned = {
        "schema_version": LOCAL_DEV_SCHEMA,
        "purpose": LOCAL_DEV_PURPOSE,
        "admission_id": str(uuid4()),
        "bundle_content_digest": bundle_content_digest,
        "proposal_sha256": proposal_sha256,
        "environment_digest": environment_digest,
        "identity_contract_sha256": identity_contract_sha256,
        "effect_contract_sha256": effect_contract_sha256,
        "policy_pack": policy_pack,
        "issued_at": issued,
        "expires_at": expires,
        "issuer_workflow": LOCAL_DEV_ISSUER_WORKFLOW,
        "issuer_key_id": LOCAL_DEV_KEY_ID,
    }
    from base64 import b64encode as encode_b64

    from openadapt_flow.qualification_admission import SIGNATURE_DOMAIN, canonical_json

    signature = LOCAL_DEV_PRIVATE_KEY.sign(SIGNATURE_DOMAIN + canonical_json(unsigned))
    return LocalDevAdmission.model_validate(
        {
            **unsigned,
            "algorithm": "ed25519",
            "signature": encode_b64(signature).decode("ascii"),
        }
    )


def production_shaped_local_payload(
    *,
    bundle_content_digest: str,
    environment_digest: str,
    now: datetime | None = None,
) -> QualificationAdmissionPayload:
    """Build a production-shaped payload signed only by the local-dev issuer.

    Campaign counts stay honest: this helper is for tests that prove the
    production verifier rejects the local-dev issuer even when the rest of
    the payload is well-formed. Quickstart writes :class:`LocalDevAdmission`
    instead, which cannot parse as a production envelope.
    """

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    digest_fields = (
        "bundle_artifact_sha256",
        "governed_authorization_template_sha256",
        "application_contract_sha256",
        "substrate_contract_sha256",
        "environment_contract_sha256",
        "runtime_environment_sha256",
        "runtime_contract_sha256",
        "input_policy_sha256",
        "action_policy_sha256",
        "network_policy_sha256",
        "identity_contract_sha256",
        "effect_contract_sha256",
        "operator_contract_sha256",
    )
    values: dict[str, Any] = {
        "admission_id": str(uuid4()),
        "tenant_id": "00000000-0000-4000-8000-000000000001",
        "workflow_id": "00000000-0000-4000-8000-000000000002",
        "workflow_version_id": "00000000-0000-4000-8000-000000000003",
        "bundle_version_id": "00000000-0000-4000-8000-000000000004",
        "runtime_validation_id": "00000000-0000-4000-8000-000000000005",
        "bundle_content_digest": bundle_content_digest,
        "campaign": {
            "artifact_sha256": _placeholder_digest("campaign-artifact"),
            "contract_sha256": _placeholder_digest("campaign-contract"),
            "outcomes_sha256": _placeholder_digest("campaign-outcomes"),
            "oracle_id": "local-dev-oracle",
            "oracle_contract_sha256": _placeholder_digest("oracle"),
            "tasks": [
                {
                    "task": "local-dev-pin-confirmation",
                    "condition": "healthy",
                    "required_trials": 3,
                    "observed_trials": 3,
                }
            ],
            "failure_taxonomy": ["over_halt", "silent_incorrect_success"],
            "decision": "admitted",
        },
        "issuer": {
            "key_id": LOCAL_DEV_KEY_ID,
            "workflow": LOCAL_DEV_ISSUER_WORKFLOW,
            "ref": f"{LOCAL_DEV_REF_PREFIX}quickstart",
        },
        "issued_at": _utc(clock),
        "not_before": _utc(clock),
        "expires_at": _utc(clock + timedelta(days=7)),
    }
    for index, field in enumerate(digest_fields):
        values[field] = _placeholder_digest(f"{field}:{index}:{environment_digest}")
    return QualificationAdmissionPayload.model_validate(values)


def sign_production_shaped_local_admission(
    payload: QualificationAdmissionPayload | None = None,
    **payload_fields: Any,
):
    """Sign a production-shaped envelope with the local-dev key (tests only)."""

    if payload is None:
        payload = production_shaped_local_payload(**payload_fields)
    return sign_qualification_admission(payload, LOCAL_DEV_PRIVATE_KEY)


def verify_local_dev_against_production_trust(
    envelope: Any,
    *,
    trusted_signers: Mapping[str, QualificationSignerTrust],
    expected: Any,
    now: datetime | None = None,
) -> str:
    """Production verification entry used by tests.

    Rejects the local-dev key before signature checks, so a stolen trust map
    cannot smuggle the quickstart signer into Production.
    """

    reject_local_dev_in_production_trust(trusted_signers)
    payload = envelope.payload
    if is_local_dev_key_id(payload.issuer.key_id) or is_local_dev_issuer_workflow(
        payload.issuer.workflow
    ):
        raise QualificationAdmissionError(
            "local-dev qualification signer cannot enter a production trust map"
        )
    return verify_qualification_admission(
        envelope,
        trusted_signers=trusted_signers,
        expected=expected,
        now=now,
    )
