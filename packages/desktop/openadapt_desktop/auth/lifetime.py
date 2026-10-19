"""Strict Cloud credential lifetime and prefix contracts.

Cloud exposes one credential to a local client: an ``oai_ingest_`` bearer.
Pairing codes and runner-local portal credentials must never cross that role
boundary.  This module also validates the additive lifetime block returned by
Cloud, including its response headers, so the Desktop never invents a safe
deadline from a partial or contradictory response.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

INGEST_TOKEN_RE = re.compile(r"^oai_ingest_[A-Za-z0-9_-]{43}$")
WARNING_DAYS = 14

_LIFETIME_FIELDS = frozenset(
    {
        "expires_at",
        "expires_in_days",
        "expiring_soon",
        "legacy_non_expiring",
        "warning_days",
    }
)


class CredentialContractError(ValueError):
    """Cloud returned a malformed or contradictory credential contract."""


def valid_ingest_token(value: Any) -> bool:
    """Return true only for the exact Cloud ingest-bearer format."""
    return isinstance(value, str) and INGEST_TOKEN_RE.fullmatch(value) is not None


def _header(headers: Mapping[str, str] | Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except (AttributeError, TypeError):
        return None
    return str(value) if value is not None else None


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise CredentialContractError("credential expiry is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise CredentialContractError("credential expiry does not include a timezone")
    return parsed.timestamp()


def parse_credential_lifetime(
    body: Any,
    *,
    headers: Mapping[str, str] | Any | None = None,
    require_headers: bool = False,
    require_fresh: bool = False,
    require_no_store: bool = False,
) -> dict[str, Any]:
    """Validate and normalize Cloud's ``credential`` block.

    ``require_fresh`` applies to a newly minted credential.  Such a response
    must report 89 or 90 remaining whole days, no expiry warning, and no legacy
    exemption.  ``require_headers`` applies to the needs-attention endpoint,
    where the warning and remaining-day headers are part of the contract.
    """
    if not isinstance(body, dict):
        raise CredentialContractError("credential response is not an object")
    block = body.get("credential")
    if not isinstance(block, dict) or set(block) != _LIFETIME_FIELDS:
        raise CredentialContractError("credential lifetime block is incomplete")

    expires_at = block["expires_at"]
    days = block["expires_in_days"]
    expiring = block["expiring_soon"]
    legacy = block["legacy_non_expiring"]
    warning_days = block["warning_days"]

    if type(expiring) is not bool or type(legacy) is not bool:
        raise CredentialContractError("credential lifetime flags are invalid")
    if type(warning_days) is not int or warning_days != WARNING_DAYS:
        raise CredentialContractError("credential warning window is invalid")

    expires_at_timestamp: float | None
    if legacy:
        if expires_at is not None or days is not None or expiring:
            raise CredentialContractError("legacy credential lifetime is contradictory")
        expires_at_timestamp = None
    else:
        if not isinstance(expires_at, str) or not expires_at:
            raise CredentialContractError("credential expiry is missing")
        if type(days) is not int or days < 0:
            raise CredentialContractError("credential remaining days are invalid")
        # Cloud computes the renewal trigger from the exact server-side
        # timestamp. ``expires_in_days`` is floor-rounded display data. Thus
        # day 14 can report either state: false above the exact 14-day
        # boundary, then true at or below it. Other whole-day values are
        # unambiguous. Do not recompute this control from the client clock.
        if (days < WARNING_DAYS and not expiring) or (days > WARNING_DAYS and expiring):
            raise CredentialContractError("credential warning state is contradictory")
        expires_at_timestamp = _timestamp(expires_at)

    if require_fresh and (
        legacy or days not in {89, 90} or expiring or expires_at_timestamp is None
    ):
        raise CredentialContractError("new credential does not have a fresh 90-day lifetime")

    if require_no_store and (_header(headers, "cache-control") or "").strip().lower() != "no-store":
        raise CredentialContractError("credential response is not marked no-store")

    if require_headers:
        if _header(headers, "x-openadapt-credential-warning-days") != str(WARNING_DAYS):
            raise CredentialContractError("credential warning header is missing")
        expires_header = _header(headers, "x-openadapt-credential-expires-in-days")
        expected = None if days is None else str(days)
        if expires_header != expected:
            raise CredentialContractError("credential expiry header disagrees with its body")

    return {
        "expires_at": expires_at,
        "expires_at_timestamp": expires_at_timestamp,
        "expires_in_days": days,
        "expiring_soon": expiring,
        "legacy_non_expiring": legacy,
        "warning_days": warning_days,
    }
