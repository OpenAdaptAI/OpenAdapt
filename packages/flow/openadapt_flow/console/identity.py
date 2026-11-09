"""Per-request authorization for a deployment-authenticated console.

The resolver is a trusted, server-injected dependency. It must verify its own
identity-provider session and current membership on every call. Neither this
module nor a console capability authenticates an OIDC/SAML token or a passkey.
An identity here supplies authorization and audit attribution only; existing
engine signatures, human attestations and action admission remain mandatory.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

if TYPE_CHECKING:
    from fastapi import Request

_SHA256 = r"^sha256:[0-9a-f]{64}$"
_OPAQUE = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConsoleRole(str, Enum):
    READER = "reader"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


class ConsoleIdentityPolicy(_Frozen):
    """Deployment-owned trust scope; never obtained from an HTTP request."""

    issuer: StrictStr = Field(min_length=1, max_length=512)
    audience: StrictStr = Field(min_length=1, max_length=256)
    tenant: StrictStr = Field(pattern=_OPAQUE)
    max_session_age_s: StrictInt = Field(default=900, ge=1, le=86400)

    @field_validator("issuer", "audience")
    @classmethod
    def _closed_text(cls, value: str) -> str:
        if value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(
                "identity trust scope must be trimmed and contain no controls"
            )
        return value


class ConsoleRequestBinding(_Frozen):
    """Exact HTTP input plus a fresh server nonce for one resolver invocation."""

    method: StrictStr
    authority: StrictStr
    raw_path_hex: StrictStr
    query_hex: StrictStr
    body_sha256: StrictStr = Field(pattern=_SHA256)
    nonce: StrictStr
    created_at: datetime

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @classmethod
    def for_request(
        cls, request: Request, body: bytes, authority: str
    ) -> ConsoleRequestBinding:
        return cls(
            method=request.method,
            authority=authority,
            raw_path_hex=request.scope["raw_path"].hex(),
            query_hex=request.scope["query_string"].hex(),
            body_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
            nonce=secrets.token_urlsafe(32),
            created_at=datetime.now(timezone.utc),
        )


class ConsoleIdentity(_Frozen):
    """Verified session output from the deployment resolver, not a wire DTO.

    The resolver maps verified current membership to exactly one console role.
    The subject is an opaque IdP identifier, never a caller-chosen display name.
    Authentication timestamps describe the verified session. ``verified_at``
    and ``request_binding_sha256`` must be fresh for this invocation.
    """

    issuer: StrictStr = Field(min_length=1, max_length=512)
    audience: StrictStr = Field(min_length=1, max_length=256)
    tenant: StrictStr = Field(pattern=_OPAQUE)
    subject: StrictStr = Field(pattern=_OPAQUE)
    session_sha256: StrictStr = Field(pattern=_SHA256)
    role: ConsoleRole
    authentication_method: Literal["oidc", "saml"]
    authenticated_at: datetime
    verified_at: datetime
    expires_at: datetime
    request_binding_sha256: StrictStr = Field(pattern=_SHA256)

    @field_validator("authenticated_at", "verified_at", "expires_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity times require a timezone")
        return value

    @property
    def operator_ref(self) -> str:
        # A tuple encoded as canonical JSON is unambiguous even when an issuer
        # or subject contains separators. No display name enters the audit id.
        return "enterprise:" + _digest([self.issuer, self.tenant, self.subject])


class ConsoleIdentityResolver(Protocol):
    async def __call__(
        self, request: Request, binding: ConsoleRequestBinding
    ) -> ConsoleIdentity | None:
        """Verify the current session/membership and bind it to this request."""
        ...


class ConsoleIdentityRefused(ValueError):
    """No enterprise authorization; callers return a fixed, payload-free DTO."""


_READ_ENDPOINTS = frozenset(
    {
        "session",
        "health",
        "workflows",
        "workflow_diff",
        "workflow_actions",
        "workflow_detail",
        "runs_list",
        "run_actions",
        "run_detail",
        "run_artifact",
        "json_artifact_list",
        "json_artifact_document",
        "json_artifact_raw",
        "attention_list",
        "attention_notification",
        "save_approval_list",
        "save_approval_detail",
        "save_approval_frame",
        "standard_change_list",
        "service_notices",
        "attention_detail",
        "skills_list",
    }
)
# The complete mutation allowlist. Portal save answers and repair reverts do
# not yet carry this person attribution and deliberately have no entry here.
_ACTION_ROLES = {
    "execute_attention_action": {
        action: ConsoleRole.OPERATOR
        for action in ("continue", "skip", "teach", "escalate", "reject", "reconcile")
    },
    "execute_run_action": {"approve": ConsoleRole.OPERATOR},
    "execute_bundle_action": {"certify": ConsoleRole.OPERATOR},
    "execute_skill_action": {
        "promote": ConsoleRole.ADMINISTRATOR,
        "rollback": ConsoleRole.ADMINISTRATOR,
    },
}


def required_role(endpoint: str, method: str, action_id: str | None) -> ConsoleRole:
    if method == "GET" and endpoint in _READ_ENDPOINTS:
        return ConsoleRole.READER
    if method == "POST":
        role = _ACTION_ROLES.get(endpoint, {}).get(action_id or "")
        if role is not None:
            return role
    raise ConsoleIdentityRefused("console endpoint or action is not authorized")


def authorize_identity(
    identity: ConsoleIdentity | None,
    policy: ConsoleIdentityPolicy,
    binding: ConsoleRequestBinding,
    role: ConsoleRole,
    *,
    now: datetime | None = None,
) -> ConsoleIdentity:
    """Validate trusted resolver output without any capability-only fallback."""
    if type(identity) is not ConsoleIdentity:
        raise ConsoleIdentityRefused("the resolver did not return a verified identity")
    # Revalidate even model_construct/model_copy output at the trust boundary.
    try:
        identity = ConsoleIdentity.model_validate(identity.model_dump(mode="python"))
    except ValueError as exc:
        raise ConsoleIdentityRefused("the resolver identity is invalid") from exc
    current = now or datetime.now(timezone.utc)
    if (
        identity.issuer != policy.issuer
        or identity.audience != policy.audience
        or identity.tenant != policy.tenant
        or identity.request_binding_sha256 != binding.digest
        or identity.verified_at < binding.created_at
        or identity.verified_at > current
        or identity.authenticated_at > identity.verified_at
        or identity.expires_at <= current
        or (current - identity.authenticated_at).total_seconds()
        > policy.max_session_age_s
    ):
        raise ConsoleIdentityRefused("the resolver identity is stale or outside scope")
    allowed = {
        ConsoleRole.READER: frozenset(ConsoleRole),
        ConsoleRole.OPERATOR: frozenset(
            {ConsoleRole.OPERATOR, ConsoleRole.ADMINISTRATOR}
        ),
        ConsoleRole.ADMINISTRATOR: frozenset({ConsoleRole.ADMINISTRATOR}),
    }
    if identity.role not in allowed[role]:
        raise ConsoleIdentityRefused("the identity role cannot perform this action")
    return identity
