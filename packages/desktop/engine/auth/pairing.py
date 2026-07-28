"""Narrow one-click Cloud pairing for the installed Desktop application.

Only ``openadapt://connect`` is accepted.  The URI is parsed again inside the
Python engine even though the Tauri shell validates it first, so neither IPC
nor an operating-system protocol invocation can become a general command,
browser-navigation, or arbitrary-network surface.

The same claim -> validate -> promote -> confirm -> rollback path also redeems
the authorization code delivered to the RFC 8252 loopback listener in
:mod:`engine.auth.browser_pkce`. That code IS an ``oap_`` pairing secret, so
browser login adds a way to OBTAIN one rather than a second protocol: see
:func:`claim_pairing`.
"""

from __future__ import annotations

import re
import socket
from typing import Any, Literal, NoReturn, cast
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx

from engine.auth.lifetime import (
    CredentialContractError,
    parse_credential_lifetime,
    valid_ingest_token,
)
from engine.auth.provider import Credential
from engine.auth.store import (
    DEFAULT_HOST,
    clear_pairing_stage,
    commit_pairing_stage,
    load_pairing_stage,
    mark_pairing_stage,
    restore_pairing_stage,
    secure_store_available,
    snapshot_pairing_canonical,
    stage_pairing_credential,
)

PAIRING_SECRET_RE = re.compile(r"^oap_[A-Za-z0-9_-]{43}$")
# RFC 7636 code verifier: 43-128 unreserved characters.
PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
ALLOWED_FIELDS = frozenset({"pairing", "host", "destination_kind"})
ALLOWED_DESTINATIONS = frozenset({"openadapt-managed", "local"})
API_TIMEOUT_S = 8.0


class PairingError(RuntimeError):
    """A safe, user-facing pairing failure with no secret-bearing text."""


def _origin(raw: str) -> str:
    parsed = urlparse(str(raw).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise PairingError("Connect link contains an invalid Cloud origin")
    if parsed.hostname.endswith("."):
        raise PairingError("Connect link contains an invalid Cloud origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PairingError("Connect link contains an invalid Cloud origin") from exc
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None and (parsed.scheme, port) not in {
        ("http", 80),
        ("https", 443),
    }:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _validate_destination(host: str, destination_kind: str | None) -> str:
    origin = _origin(host)
    managed_origin = _origin(DEFAULT_HOST)
    kind = destination_kind or ("openadapt-managed" if origin == managed_origin else None)
    if kind not in ALLOWED_DESTINATIONS:
        raise PairingError("Connect link has an unsupported destination")
    if kind == "openadapt-managed":
        if origin != managed_origin:
            raise PairingError("Connect link does not name the managed OpenAdapt service")
        return origin

    hostname = urlparse(origin).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise PairingError("A local connect link must use this computer")
    return origin


def parse_connect_uri(uri: str) -> dict[str, str]:
    """Parse the fixed connect action and reject ambiguity or extra fields."""
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        raise PairingError("Invalid OpenAdapt connect link")
    parsed = urlparse(uri)
    if (
        parsed.scheme != "openadapt"
        or parsed.netloc != "connect"
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise PairingError("Invalid OpenAdapt connect link")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise PairingError("Invalid OpenAdapt connect link") from exc
    if set(query) - ALLOWED_FIELDS or any(len(values) != 1 for values in query.values()):
        raise PairingError("Connect link contains unknown or duplicate fields")
    if set(query) < {"pairing", "host"}:
        raise PairingError("Connect link is missing pairing or host")

    secret = query["pairing"][0]
    if not PAIRING_SECRET_RE.fullmatch(secret):
        raise PairingError("Pairing code is malformed")
    destination_kind = query.get("destination_kind", [None])[0]
    if destination_kind is not None and destination_kind not in ALLOWED_DESTINATIONS:
        raise PairingError("Connect link has an unsupported destination kind")
    host = _validate_destination(query["host"][0], destination_kind)
    result = {"pairing": secret, "host": host}
    if destination_kind:
        result["destination_kind"] = destination_kind
    return result


def credential_expires_at(body: Any) -> float | None:
    """Read the credential deadline Cloud reports, or None when it reports none.

    Cloud is the authority on when a credential dies; this value is stored so
    the local surfaces can WARN early. It is never used to decide that a
    credential is still good -- every request is checked server-side.
    """
    try:
        return cast(float | None, parse_credential_lifetime(body)["expires_at_timestamp"])
    except CredentialContractError:
        return None


def _safe_device_name() -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", socket.gethostname()).strip()[:80]
    return value or "this computer"


def _abort_claim(host: str, pairing_id: str, token: str) -> bool:
    """Revoke the exact claim, returning true only for acknowledged rollback."""
    try:
        response = httpx.post(
            f"{host}/api/local-bridge/pairings/abort",
            json={"pairing_id": pairing_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False
    try:
        revoked = response.json().get("revoked") is True
    except (AttributeError, TypeError, ValueError):
        revoked = False
    # In particular, 409 can mean confirmation won a race. It must never
    # authorize restoring an old local token over a possibly confirmed one.
    return response.status_code == 200 and revoked


def _stage_identity(stage: dict) -> tuple[str, str, str, str, Credential]:
    """Validate all network- and keychain-relevant fields in a recovery stage."""
    pairing_id = stage.get("pairing_id")
    device_name = stage.get("device_name")
    state = stage.get("state")
    credential = stage.get("credential")
    try:
        canonical_pairing_id = str(UUID(pairing_id)) if isinstance(pairing_id, str) else None
    except ValueError:
        canonical_pairing_id = None
    if (
        stage.get("version") != 1
        or not isinstance(pairing_id, str)
        or canonical_pairing_id != pairing_id
        or not isinstance(device_name, str)
        or not 1 <= len(device_name) <= 80
        or state
        not in {
            "claimed",
            "canonical_written",
            "confirm_ambiguous",
            "abort_acknowledged",
        }
        or not isinstance(credential, dict)
        or set(credential) != {"kind", "token", "refresh_token", "org_id", "host", "expires_at"}
        or credential.get("kind") != "ingest_token"
        or credential.get("refresh_token") is not None
        or credential.get("org_id") is not None
        # Cloud now returns a deadline with the credential. Accept a number or
        # None, but nothing else: a corrupt value must not reach the keychain.
        or not isinstance(credential.get("expires_at"), (int, float, type(None)))
        or isinstance(credential.get("expires_at"), bool)
        or not valid_ingest_token(credential.get("token"))
        or not isinstance(credential.get("host"), str)
    ):
        raise PairingError("Desktop found an invalid pairing recovery record in the keychain")
    host = credential["host"]
    destination = "openadapt-managed" if _origin(host) == _origin(DEFAULT_HOST) else "local"
    if _validate_destination(host, destination) != host:
        raise PairingError("Desktop found an invalid pairing recovery record in the keychain")
    return pairing_id, host, credential["token"], state, cast(Credential, credential)


def _paired_result(host: str, device_name: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "kind": "ingest_token",
        "host": host,
        "org_id": None,
        "paired": True,
        "token_storage": "keyring",
        "device_name": device_name,
        "settings_url": f"{host}/dashboard/settings/ingest",
    }


ConfirmationState = Literal["confirmed", "definitive_failure", "ambiguous"]


def _confirm_claim(
    host: str,
    pairing_id: str,
    token: str,
) -> tuple[ConfirmationState, int | None]:
    """Use Cloud's idempotent/current-state confirmation to resolve one retry."""
    status: int | None = None
    for _ in range(2):
        try:
            response = httpx.post(
                f"{host}/api/local-bridge/pairings/confirm",
                json={"pairing_id": pairing_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=API_TIMEOUT_S,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            continue
        status = response.status_code
        if status >= 500:
            continue
        try:
            connected = response.json().get("connected") is True
        except (AttributeError, TypeError, ValueError):
            connected = False
        if 200 <= status < 300 and connected:
            return "confirmed", status
        if 400 <= status < 500:
            return "definitive_failure", status
        # A malformed success or unexpected redirect does not prove whether
        # the idempotent server transition committed; retry/current-read it.
    return "ambiguous", status


def _fail_staged_pairing(stage: dict, message: str) -> NoReturn:
    pairing_id, host, token, _, _ = _stage_identity(stage)
    if _abort_claim(host, pairing_id, token):
        mark_pairing_stage(pairing_id, "abort_acknowledged")
        restored = restore_pairing_stage(pairing_id)
        if restored:
            clear_pairing_stage(pairing_id)
            raise PairingError(f"{message} Your previous Desktop connection is unchanged.")
        raise PairingError(
            f"{message} Cloud rolled back the new claim, but Desktop could not "
            "restore the prior keychain state. Recovery evidence was retained."
        )
    raise PairingError(
        f"{message} Cloud did not acknowledge rollback, so Desktop preserved "
        "the staged token and current keychain state for safe recovery."
    )


def _finish_staged_pairing(stage: dict) -> dict[str, Any]:
    pairing_id, host, token, state, credential = _stage_identity(stage)
    device_name = cast(str, stage["device_name"])
    if state == "abort_acknowledged":
        if restore_pairing_stage(pairing_id) and clear_pairing_stage(pairing_id):
            raise PairingError(
                "Desktop safely rolled back an interrupted connection. "
                "Create a new connection from Cloud settings."
            )
        raise PairingError(
            "Desktop could not finish restoring an interrupted connection; "
            "recovery evidence remains in the keychain."
        )

    headers = {"Authorization": f"Bearer {token}"}
    try:
        validation = httpx.get(
            f"{host}/api/needs-attention/count",
            headers=headers,
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        _fail_staged_pairing(stage, "Cloud could not verify the new credential.")
    if not 200 <= validation.status_code < 300:
        _fail_staged_pairing(
            stage,
            f"Cloud could not verify the new credential ({validation.status_code}).",
        )
    try:
        validation_lifetime = parse_credential_lifetime(
            validation.json(),
            headers=validation.headers,
            require_headers=True,
            require_fresh=True,
            require_no_store=True,
        )
    except (AttributeError, TypeError, ValueError, CredentialContractError):
        _fail_staged_pairing(
            stage,
            "Cloud returned an incomplete credential lifetime contract.",
        )
    if validation_lifetime["expires_at_timestamp"] != credential["expires_at"]:
        _fail_staged_pairing(
            stage,
            "Cloud returned a different credential deadline during validation.",
        )

    if not commit_pairing_stage(pairing_id):
        _fail_staged_pairing(
            stage,
            "Desktop could not atomically write the new credential to the keychain.",
        )

    confirmation, status = _confirm_claim(host, pairing_id, token)
    if confirmation == "confirmed":
        # A crash before this deletion is safe: recovery re-validates the
        # canonical token and idempotently confirms/current-reads the same pair.
        clear_pairing_stage(pairing_id)
        return _paired_result(host, device_name)
    if confirmation == "ambiguous":
        mark_pairing_stage(pairing_id, "confirm_ambiguous")
        raise PairingError(
            "The new credential is stored and working, but Cloud confirmation "
            "is still uncertain. Desktop kept recovery state and will retry "
            "the same idempotent confirmation."
        )
    _fail_staged_pairing(
        stage,
        f"Cloud refused to confirm the new connection ({status}).",
    )
    raise AssertionError("unreachable")


def recover_pending_pairing() -> dict[str, Any] | None:
    """Resume an exact durable pairing stage after a process interruption."""
    try:
        stage = load_pairing_stage()
    except RuntimeError as exc:
        raise PairingError(
            "Desktop could not read pairing recovery state from the keychain"
        ) from exc
    if stage is None:
        return None
    return _finish_staged_pairing(stage)


def connect_uri(uri: str) -> dict[str, Any]:
    """Claim, stage, verify, commit, and confirm one Desktop pairing URI."""
    request = parse_connect_uri(uri)
    return claim_pairing(
        request["host"],
        request["pairing"],
        destination_kind=request.get("destination_kind"),
    )


def claim_pairing(
    host: str,
    pairing_secret: str,
    code_verifier: str | None = None,
    destination_kind: str | None = None,
) -> dict[str, Any]:
    """Claim, stage, verify, commit, and confirm one pairing secret.

    This is the single redemption path. A deep link supplies the secret through
    ``openadapt://connect``; the RFC 8252 loopback listener in
    :mod:`engine.auth.browser_pkce` receives the same kind of secret as an
    authorization ``code`` and supplies the PKCE ``code_verifier`` that Cloud
    bound to it. Everything after the claim -- durable staging, independent
    validation, atomic keychain promotion, idempotent confirmation, and
    acknowledged rollback -- is identical either way.

    Args:
        host: The Cloud origin, already validated by the caller.
        pairing_secret: A one-use ``oap_`` secret.
        code_verifier: The PKCE verifier for a loopback grant, or None for a
            deep-link pairing that carries no challenge.

    Returns:
        The paired result dictionary.

    Raises:
        PairingError: On any failure, with the prior connection preserved.
    """
    if not PAIRING_SECRET_RE.fullmatch(str(pairing_secret)):
        raise PairingError("Pairing code is malformed")
    if code_verifier is not None and not PKCE_VERIFIER_RE.fullmatch(code_verifier):
        raise PairingError("Login proof is malformed")
    # Re-validated here, not only at the caller: every entry point to this
    # redemption path must prove its destination before a secret is spent.
    host = _validate_destination(host, destination_kind)
    if not secure_store_available():
        raise PairingError(
            "Secure pairing needs an unlocked operating-system keychain. "
            "Unlock it, then create a new connection from Cloud settings."
        )

    recovered = recover_pending_pairing()
    if recovered is not None:
        return recovered
    previous = snapshot_pairing_canonical(host)
    if previous is None:
        raise PairingError("Desktop could not safely snapshot the current keychain connection")

    device_name = _safe_device_name()
    payload: dict[str, Any] = {
        "pairing_secret": pairing_secret,
        "device_name": device_name,
    }
    if code_verifier is not None:
        payload["code_verifier"] = code_verifier
    try:
        claim = httpx.post(
            f"{host}/api/local-bridge/pairings/claim",
            json=payload,
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise PairingError("Could not reach OpenAdapt Cloud") from exc
    if claim.status_code == 410:
        raise PairingError("Pairing code expired, was cancelled, or was already used")
    if claim.status_code >= 400:
        raise PairingError(f"Pairing failed ({claim.status_code})")
    if claim.status_code != 201:
        raise PairingError("Pairing response used an unexpected success status")
    try:
        body = claim.json()
    except (AttributeError, TypeError, ValueError) as exc:
        raise PairingError("Pairing response was not valid JSON") from exc
    try:
        token, pairing_id, expiry = _validated_claim(body, claim.headers)
    except PairingError:
        rollback = _claim_abort_coordinates(body)
        if rollback and _abort_claim(host, rollback[0], rollback[1]):
            raise PairingError(
                "Cloud rolled back a pairing response that did not match the "
                "Desktop credential contract. Your previous connection is unchanged."
            ) from None
        raise

    credential: Credential = {
        "kind": "ingest_token",
        "token": token,
        "refresh_token": None,
        "org_id": None,
        "host": host,
        "expires_at": expiry,
    }
    if not stage_pairing_credential(
        pairing_id,
        credential,
        previous,
        device_name,
    ):
        _abort_claim(host, pairing_id, token)
        raise PairingError(
            "Desktop could not durably stage the new credential. "
            "Your previous Desktop connection is unchanged."
        )
    try:
        stage = load_pairing_stage()
    except RuntimeError as exc:
        raise PairingError(
            "Desktop staged the credential but could not read it back; "
            "the prior connection remains available for recovery."
        ) from exc
    if stage is None:
        raise PairingError(
            "Desktop could not recover the newly staged credential from the keychain"
        )
    return _finish_staged_pairing(stage)


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise PairingError("Pairing response did not contain a valid credential")
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise PairingError("Pairing response did not contain a valid credential") from exc
    if canonical != value:
        raise PairingError("Pairing response did not contain a valid credential")
    return canonical


def _validated_claim(body: Any, headers: Any) -> tuple[str, str, float]:
    """Validate the complete Cloud 201 response before staging its bearer."""
    expected = {
        "paired",
        "pairing_id",
        "ingest_token_id",
        "ingest_token",
        "credential",
    }
    if not isinstance(body, dict) or set(body) != expected or body.get("paired") is not True:
        raise PairingError("Pairing response did not match the credential contract")
    pairing_id = _canonical_uuid(body.get("pairing_id"))
    _canonical_uuid(body.get("ingest_token_id"))
    token = body.get("ingest_token")
    if not valid_ingest_token(token):
        raise PairingError("Pairing response contained a malformed credential")
    try:
        lifetime = parse_credential_lifetime(
            body,
            headers=headers,
            require_fresh=True,
            require_no_store=True,
        )
    except CredentialContractError as exc:
        raise PairingError("Pairing response contained an invalid credential lifetime") from exc
    expiry = lifetime["expires_at_timestamp"]
    if not isinstance(expiry, float):
        raise PairingError("Pairing response contained an invalid credential lifetime")
    return token, pairing_id, expiry


def _claim_abort_coordinates(body: Any) -> tuple[str, str] | None:
    """Salvage only the exact values needed to revoke a malformed 201."""
    if not isinstance(body, dict) or not valid_ingest_token(body.get("ingest_token")):
        return None
    try:
        pairing_id = _canonical_uuid(body.get("pairing_id"))
    except PairingError:
        return None
    return pairing_id, cast(str, body["ingest_token"])
