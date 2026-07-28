"""Credential lifetime: report the deadline, and renew it without an outage.

A Cloud ingest credential lives 90 days. Two things follow, and this module
owns both:

* **Warn early.** ``GET /api/needs-attention/count`` -- the one authenticated
  call this machine already makes on a timer -- returns how long the credential
  it just used has left. :func:`credential_status` reads it, and
  :func:`expiry_warning` turns it into a line the operator sees.
* **Renew without downtime.** ``POST /api/ingest-tokens/rotate`` mints the
  replacement FIRST and only then shortens the outgoing credential to a bounded
  overlap. So :func:`rotate_credential` can store the replacement knowing the
  credential currently in the keychain keeps working meanwhile: if the write
  fails, or the process dies between the two, the machine is still connected.

Nothing here decides that a credential is still valid. Expiry is enforced
server-side on every request; this is a warning and a renewal, not a check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from loguru import logger

from engine.auth.lifetime import (
    CredentialContractError,
    parse_credential_lifetime,
    valid_ingest_token,
)
from engine.auth.provider import Credential
from engine.auth.store import (
    DEFAULT_HOST,
    INGEST_TOKEN_ENV,
    clear_rotation_stage,
    clear_superseded_rotation_stage,
    commit_rotation_stage,
    load_credential,
    load_rotation_stage,
    secure_store_available,
    snapshot_pairing_canonical,
    stage_rotation_credential,
)

COUNT_PATH = "/api/needs-attention/count"
ROTATE_PATH = "/api/ingest-tokens/rotate"
API_TIMEOUT_S = 10.0

# Mirrors the server's own threshold. The server sends `warning_days` with
# every answer, so this is only the fallback when it does not.
DEFAULT_WARNING_DAYS = 14


class RotationError(RuntimeError):
    """A safe, user-facing renewal failure that never contains a secret."""


def credential_status(host: str = DEFAULT_HOST, token: str | None = None) -> dict[str, Any] | None:
    """Ask Cloud how long the credential for ``host`` has left.

    Args:
        host: Hosted base URL.
        token: Bearer to ask about. Defaults to the stored credential.

    Returns:
        The server's ``credential`` block, or None when there is no credential,
        Cloud cannot be reached, or the answer is not readable. None means
        "unknown", never "fine".
    """
    if token is not None and not valid_ingest_token(token):
        return None
    bearer = token if token is not None else _stored_token(host)
    if not bearer:
        return None
    try:
        response = httpx.get(
            f"{host.rstrip('/')}{COUNT_PATH}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return None
    if not 200 <= response.status_code < 300:
        return None
    try:
        lifetime = parse_credential_lifetime(
            response.json(),
            headers=response.headers,
            require_headers=True,
            require_no_store=True,
        )
    except (AttributeError, TypeError, ValueError, CredentialContractError):
        return None
    return lifetime


def expiry_warning(status: dict[str, Any] | None) -> str | None:
    """Render the operator-visible warning, or None when there is nothing to say.

    Returns None for an unknown status too: inventing "your credential is fine"
    from a failed request would be worse than saying nothing.
    """
    if not status:
        return None
    if status.get("legacy_non_expiring") is True:
        return (
            "This connection was created before connections expired, so it has "
            "no renewal date. Run `openadapt-desktop rotate` to move it to a "
            "90-day credential."
        )
    if status.get("expiring_soon") is not True:
        return None
    days = status.get("expires_in_days")
    if not isinstance(days, int):
        return "This connection expires soon. Run `openadapt-desktop rotate` to renew it."
    if days <= 0:
        return "This connection expires today. Run `openadapt-desktop rotate` to renew it."
    unit = "day" if days == 1 else "days"
    return (
        f"This connection expires in {days} {unit}. Run `openadapt-desktop rotate` "
        "to renew it; the current one keeps working while the new one takes over."
    )


def rotate_credential(host: str = DEFAULT_HOST) -> Credential:
    """Renew the stored credential for ``host`` without interrupting this machine.

    Args:
        host: Hosted base URL.

    Returns:
        The newly stored ``Credential``.

    Raises:
        RotationError: If there is nothing to renew, Cloud refuses, or the
            replacement could not be stored. In every failure case the existing
            credential is left in place and keeps working.
    """
    host = host.rstrip("/")
    recovered = recover_pending_rotation(host)
    if recovered is not None:
        return recovered
    if not secure_store_available():
        raise RotationError(
            "Credential renewal needs an unlocked operating-system keychain. "
            "Unlock it before OpenAdapt asks Cloud for a one-time replacement."
        )
    current = load_credential(host)
    if current is None or not valid_ingest_token(current.get("token")):
        raise RotationError(
            "There is no stored connection for this workspace. "
            "Run `openadapt-desktop login` to connect."
        )
    previous = snapshot_pairing_canonical(host)
    if previous is None:
        raise RotationError(
            "OpenAdapt could not safely snapshot the current keychain connection. "
            "Cloud did not receive a renewal request."
        )

    try:
        response = httpx.post(
            f"{host}{ROTATE_PATH}",
            json={},
            headers={"Authorization": f"Bearer {current['token']}"},
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise RotationError(
            "OpenAdapt did not receive the renewal response. The old credential "
            "remains valid for at most seven days if Cloud rotated it. Sign in "
            "again; do not retry the same renewal."
        ) from exc

    if response.status_code == 401:
        raise RotationError(
            "This connection is no longer valid. Run `openadapt-desktop login` to connect again."
        )
    if response.status_code == 409:
        raise RotationError(
            "This connection was already renewed once, and the replacement "
            "cannot be issued twice. Run `openadapt-desktop login` to connect again."
        )
    if response.status_code != 201:
        raise RotationError(
            f"Renewal did not complete with the required response ({response.status_code}). "
            "Keep the current credential and sign in again if Cloud already rotated it."
        )

    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError) as exc:
        raise RotationError(
            "Cloud renewed the connection but returned an unreadable one-time response. "
            "The old credential remains valid for at most seven days. Sign in again."
        ) from exc
    try:
        token, previous_id, expires_at = _validated_rotation(body, response.headers)
    except RotationError as exc:
        raise RotationError(
            "Cloud renewed the connection, but its one-time response did not "
            "match the Desktop credential contract. The old credential remains "
            "valid for at most seven days. Sign in again; do not retry the same renewal."
        ) from exc

    replacement: Credential = {
        "kind": "ingest_token",
        "token": token,
        "refresh_token": None,
        "org_id": current.get("org_id"),
        "host": host,
        "expires_at": expires_at,
    }
    # Retain the one-time response before changing any canonical entry. A crash
    # after this point resumes from the exact retained replacement and does not
    # issue an unsafe second rotation request.
    if not stage_rotation_credential(previous_id, replacement, previous):
        raise RotationError(
            "Cloud renewed the connection, but OpenAdapt could not retain its "
            "one-time response. The old credential remains valid for at most "
            "seven days. Sign in again; do not retry the same renewal."
        )
    _validate_staged_replacement(previous_id, replacement, require_fresh=True)
    if not commit_rotation_stage(previous_id):
        raise RotationError(
            "OpenAdapt retained the renewed credential but could not finish its "
            "keychain update. Recovery state remains available. Restart OpenAdapt "
            "before you sign in again."
        )
    if not clear_rotation_stage(previous_id):
        raise RotationError(
            "The renewed credential is active, but OpenAdapt could not clear its "
            "recovery record. Restart OpenAdapt to finish recovery."
        )
    logger.info("Renewed the hosted credential for {host}", host=host)
    return replacement


def recover_pending_rotation(host: str | None = None) -> Credential | None:
    """Promote an exact retained one-time response without another Cloud call."""
    try:
        stage = load_rotation_stage()
    except RuntimeError as exc:
        raise RotationError(
            "OpenAdapt could not read the retained credential renewal state."
        ) from exc
    if stage is None:
        return None
    previous_id, credential = _validated_rotation_stage(stage)
    if host is not None and credential["host"] != host.rstrip("/"):
        raise RotationError("A credential renewal for another Cloud host needs recovery first.")
    if clear_superseded_rotation_stage(previous_id):
        logger.info(
            "Cleared a stale credential renewal after a later login for {host}",
            host=credential["host"],
        )
        return None
    _validate_staged_replacement(previous_id, credential, require_fresh=False)
    if not commit_rotation_stage(previous_id):
        raise RotationError(
            "OpenAdapt could not safely finish the retained credential renewal. "
            "Recovery state remains available."
        )
    if not clear_rotation_stage(previous_id):
        raise RotationError("The renewed credential is active, but its recovery record remains.")
    logger.info("Recovered the renewed hosted credential for {host}", host=credential["host"])
    return credential


def _validate_staged_replacement(
    previous_id: str,
    credential: Credential,
    *,
    require_fresh: bool,
) -> None:
    """Prove the retained replacement bearer before canonical promotion.

    The immediate check must still show a new 89/90-day credential. Recovery
    can occur later, so it binds the exact stored expiry while permitting the
    display day count to decrease.
    """
    try:
        response = httpx.get(
            f"{credential['host']}{COUNT_PATH}",
            headers={"Authorization": f"Bearer {credential['token']}"},
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise RotationError(
            "OpenAdapt retained the renewed credential but could not verify it. "
            "The old credential remains active, and recovery will retry the "
            "retained replacement without another renewal request."
        ) from exc
    if not 200 <= response.status_code < 300:
        raise RotationError(
            "OpenAdapt retained the renewed credential, but Cloud did not accept "
            f"it during verification ({response.status_code}). The old credential "
            "remains active. Sign in again if verification does not recover."
        )
    try:
        lifetime = parse_credential_lifetime(
            response.json(),
            headers=response.headers,
            require_headers=True,
            require_fresh=require_fresh,
            require_no_store=True,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise RotationError(
            "OpenAdapt retained the renewed credential, but Cloud returned an "
            "incomplete verification contract. The old credential remains active."
        ) from exc
    if (
        lifetime["legacy_non_expiring"]
        or lifetime["expires_at_timestamp"] != credential["expires_at"]
    ):
        raise RotationError(
            "OpenAdapt retained the renewed credential, but Cloud verified a "
            "different expiry. The old credential remains active."
        )
    # The recovery stage identity must remain unchanged across the network
    # check. A concurrent operation cannot swap in another retained bearer and
    # then have this successful response authorize its promotion.
    try:
        current_stage = load_rotation_stage()
    except RuntimeError as exc:
        raise RotationError("The retained credential changed during verification.") from exc
    if current_stage is None:
        raise RotationError("The retained credential changed during verification.")
    try:
        current_previous_id, current_credential = _validated_rotation_stage(current_stage)
    except RotationError as exc:
        raise RotationError("The retained credential changed during verification.") from exc
    if current_previous_id != previous_id or current_credential != credential:
        raise RotationError("The retained credential changed during verification.")


def _stored_token(host: str) -> str | None:
    """Resolve the bearer this machine would actually send."""
    import os

    env_token = os.environ.get(INGEST_TOKEN_ENV, "").strip()
    if valid_ingest_token(env_token):
        return env_token
    cred = load_credential(host.rstrip("/"))
    return cred.get("token") if cred and valid_ingest_token(cred.get("token")) else None


def _validated_rotation(body: Any, headers: Any) -> tuple[str, str, float]:
    """Validate the exact one-time response and its seven-day overlap."""
    expected = {"token", "record", "previous_id", "previous_expires_at", "credential"}
    if not isinstance(body, dict) or set(body) != expected:
        raise RotationError(
            "Cloud renewed the connection but returned an incomplete one-time response. "
            "The old credential remains valid for at most seven days. Sign in again."
        )
    token = body.get("token")
    previous_id = body.get("previous_id")
    record = body.get("record")
    if not valid_ingest_token(token):
        raise RotationError("Renewal response contained a malformed credential")
    if not _is_canonical_uuid(previous_id):
        raise RotationError("Renewal response contained an invalid previous credential id")
    credential_block = body.get("credential")
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "id",
            "org_id",
            "name",
            "token_prefix",
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked_at",
            "rotated_to_id",
        }
        or not _is_canonical_uuid(record.get("id"))
        or not isinstance(record.get("org_id"), str)
        or not 1 <= len(record["org_id"]) <= 128
        or not isinstance(record.get("name"), str)
        or record.get("token_prefix") != token[:20]
        or record.get("last_used_at") is not None
        or not isinstance(credential_block, dict)
        or record.get("expires_at") != credential_block.get("expires_at")
        or record.get("revoked_at") is not None
        or record.get("rotated_to_id") is not None
    ):
        raise RotationError("Renewal response contained a contradictory credential record")
    try:
        _parse_timestamp(record.get("created_at"))
        lifetime = parse_credential_lifetime(
            body,
            headers=headers,
            require_fresh=True,
            require_no_store=True,
        )
        previous_expiry = _parse_timestamp(body.get("previous_expires_at"))
    except CredentialContractError as exc:
        raise RotationError("Renewal response contained an invalid credential lifetime") from exc
    now = datetime.now(timezone.utc)
    if previous_expiry > now + timedelta(days=7, minutes=5):
        raise RotationError("Renewal response exceeded the seven-day overlap contract")
    expiry = lifetime["expires_at_timestamp"]
    if not isinstance(expiry, float):
        raise RotationError("Renewal response contained an invalid credential lifetime")
    return token, previous_id, expiry


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CredentialContractError("credential overlap expiry is missing")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise CredentialContractError("credential overlap expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise CredentialContractError("credential overlap expiry does not include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _validated_rotation_stage(stage: Any) -> tuple[str, Credential]:
    if not isinstance(stage, dict) or set(stage) != {
        "version",
        "previous_id",
        "credential",
        "previous",
        "state",
    }:
        raise RotationError("The retained credential renewal state is malformed")
    previous_id = stage.get("previous_id")
    credential = stage.get("credential")
    if (
        stage.get("version") != 1
        or stage.get("state") not in {"received", "canonical_written"}
        or not _is_canonical_uuid(previous_id)
        or not isinstance(credential, dict)
        or set(credential) != {"kind", "token", "refresh_token", "org_id", "host", "expires_at"}
        or credential.get("kind") != "ingest_token"
        or credential.get("refresh_token") is not None
        or not valid_ingest_token(credential.get("token"))
        or not isinstance(credential.get("host"), str)
        or not isinstance(credential.get("expires_at"), (int, float))
        or isinstance(credential.get("expires_at"), bool)
    ):
        raise RotationError("The retained credential renewal state is malformed")
    return previous_id, credential  # type: ignore[return-value]
