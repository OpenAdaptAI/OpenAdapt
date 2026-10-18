"""A person's save decision made on their phone, signed by Cloud.

Phone approval is opt-in. When a deployment turns it on, the runner sends
each waiting save to Cloud; Cloud pushes an opaque link to the approver's
phone, the person signs in (AAL2), sees exactly what will be saved, and taps
Approve or Don't save. Cloud signs that decision with its decision key, and
the runner hands it to the waiting run.

This module is the run's half. A Cloud decision is accepted only when:

* it is signed by a key pinned on this runner (key id -> base64 raw Ed25519
  public key in ``$OPENADAPT_CLOUD_DECISION_KEYS``, default
  ``$OPENADAPT_HOME/cloud-decision-keys.json``). No pinned key, no phone
  route: local-only installs keep Desktop approval and nothing else;
* it names the exact request id and request digest (which binds the run,
  the save step, the screenshot and the form's field state) the person saw;
* the approver is a Cloud user who signed in with AAL2. An agent or a model
  can never produce one: Cloud signs only for a person's session;
* its ``link_id`` is single-use: the answer file is created once, and the
  approval is consumed by exactly one save, so a replayed decision has
  nothing to answer.

After an approval the run still re-reads the form before the save and stops
if anything changed while it waited, as for a Desktop approval.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openadapt_flow.approval_mode.model import (
    OPAQUE_ID_PATTERN,
    PREFIXED_SHA256_PATTERN,
    DeclineReason,
)

CLOUD_DECISION_SCHEMA: Literal["openadapt.cloud-item-decision/v1"] = (
    "openadapt.cloud-item-decision/v1"
)
_DOMAIN = b"openadapt.cloud-item-decision/v1\n"
CLOUD_PHONE_ROUTE: Literal["cloud_phone"] = "cloud_phone"
KEYS_ENV = "OPENADAPT_CLOUD_DECISION_KEYS"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class CloudDecisionV1(BaseModel):
    """What Cloud signs when a person approves or declines on their phone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.cloud-item-decision/v1"] = CLOUD_DECISION_SCHEMA
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    decision: Literal["approve", "decline"]
    decline_reason: Optional[DeclineReason] = None
    #: The Cloud user id of the person who decided. Never a name.
    approver_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    auth_method: Literal["cloud_aal2"]
    decided_at: str = Field(min_length=10, max_length=64)
    #: The single-use link the person opened.
    link_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    runner_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    signature: str = Field(min_length=86, max_length=100)

    @model_validator(mode="after")
    def _reason(self) -> "CloudDecisionV1":
        if (self.decision == "decline") != (self.decline_reason is not None):
            raise ValueError("a decline carries exactly one closed reason")
        return self


class CloudDecisionRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def signing_payload(fields: Mapping[str, Any]) -> bytes:
    """The exact bytes Cloud signs: domain prefix plus canonical JSON."""

    unsigned = {k: v for k, v in fields.items() if k != "signature"}
    return _DOMAIN + _canonical(unsigned)


def sign_decision(fields: Mapping[str, Any], private_key: Any) -> CloudDecisionV1:
    """Reference signer (Cloud's signer must match it byte for byte)."""

    unsigned = {k: v for k, v in dict(fields).items() if k != "signature"}
    placeholder = CloudDecisionV1.model_validate(
        {**unsigned, "signature": "A" * 86 + "=="}
    )
    body = placeholder.model_dump(mode="json")
    signature = private_key.sign(signing_payload(body))
    return CloudDecisionV1.model_validate(
        {**body, "signature": base64.b64encode(signature).decode("ascii")}
    )


def default_keys_path() -> Path:
    explicit = os.environ.get(KEYS_ENV)
    if explicit:
        return Path(explicit)
    home = Path(os.environ.get("OPENADAPT_HOME") or Path.home() / ".openadapt")
    return home / "cloud-decision-keys.json"


def load_pinned_keys(path: Optional[Path | str] = None) -> dict[str, str]:
    """The runner's pinned Cloud decision keys. Empty when none are pinned."""

    target = Path(path) if path is not None else default_keys_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def verify_decision(
    raw: Any,
    *,
    request_id: str,
    request_sha256: str,
    pinned_keys: Mapping[str, str],
) -> CloudDecisionV1:
    """Accept a Cloud decision only for this exact request, signed by a pinned key."""

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        if isinstance(raw, CloudDecisionV1):
            decision = raw
        elif isinstance(raw, (bytes, str)):
            decision = CloudDecisionV1.model_validate_json(raw)
        else:
            decision = CloudDecisionV1.model_validate(raw)
    except ValidationError as exc:
        raise CloudDecisionRejected(
            "cloud_decision_malformed", "malformed decision"
        ) from exc
    encoded = pinned_keys.get(decision.key_id)
    if not encoded:
        raise CloudDecisionRejected(
            "cloud_decision_key_not_pinned",
            "the decision key is not pinned on this runner",
        )
    try:
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(encoded, validate=True)
        ).verify(
            base64.b64decode(decision.signature, validate=True),
            signing_payload(decision.model_dump(mode="json")),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise CloudDecisionRejected(
            "cloud_decision_signature_invalid", "the decision signature is invalid"
        ) from exc
    if decision.request_id != request_id:
        raise CloudDecisionRejected(
            "request_mismatch", "the decision is for another request"
        )
    if decision.request_sha256 != request_sha256:
        raise CloudDecisionRejected(
            "request_digest_mismatch",
            "the decision is for another version of this save",
        )
    return decision


__all__ = [
    "CLOUD_DECISION_SCHEMA",
    "CLOUD_PHONE_ROUTE",
    "CloudDecisionRejected",
    "CloudDecisionV1",
    "KEYS_ENV",
    "load_pinned_keys",
    "sign_decision",
    "signing_payload",
    "verify_decision",
]
