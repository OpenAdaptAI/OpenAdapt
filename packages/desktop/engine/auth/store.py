"""Shared keychain credential store -- the single source of truth for auth.

Both providers (:mod:`engine.auth.paste`, :mod:`engine.auth.browser_pkce`)
write the SAME :class:`~engine.auth.provider.Credential` here, and every
outbound hosted call (``push``, the needs-attention count poll, the
ingest-report emitter) reads the active bearer token through
:func:`auth_header`.

Secrets go to the OS secure store (macOS Keychain / Windows Credential Manager
/ Linux Secret Service) via ``keyring`` -- NEVER to ``config.toml`` or any
plaintext file (spec section 3e). The keyring service name is
``ai.openadapt.desktop`` and the account is the credential's ``host``.

Value encoding (spec section 3a/3e, review 2.1 P0-5): the PRIMARY value stored
under ``account = host`` is the RAW bearer token, because a second surface --
the tray's ``keychain.get_ingest_token`` -- reads that exact keyring value and
sends it straight as ``Authorization: Bearer <value>``. Storing a JSON blob
there would make the tray send a garbage bearer header and 401. The rest of the
:class:`~engine.auth.provider.Credential` (kind / refresh / org / expiry) is
kept in a COMPANION entry under ``account = host + "|cred"`` so both surfaces
share one credential without a JSON-blob-as-token 401.

A tiny non-secret pointer records which host is "active" so the no-arg
:func:`auth_header` can resolve a token without being told the host. That
pointer is stored in the keyring too (account ``__active_host__``), so it
inherits the same secure lifecycle and clean-uninstall purge.

Headless degrade (review 2.2 P0-1): every keyring call site is wrapped so a box
with no Secret Service backend (headless Linux BYOC runner, locked-down Windows,
CI) DEGRADES to "no credential" instead of raising ``NoKeyringError``.
``auth_header`` runs on every outbound hosted call, so a raise here would crash
the entire cloud/BYOC lane. Backend-missing must behave exactly like "not logged
in".

Token resolution precedence for :func:`auth_header` (spec section 3e):
    ``OPENADAPT_INGEST_TOKEN`` env  ->  keychain (active host)  ->  {} (no auth)
"""

from __future__ import annotations

import json
import os

from loguru import logger

from engine.auth.provider import Credential

SERVICE_NAME = "ai.openadapt.desktop"
_ACTIVE_HOST_ACCOUNT = "__active_host__"
_PAIRING_STAGE_ACCOUNT = "__pairing_stage__"
_PAIRING_STAGE_VERSION = 1

# Suffix for the companion account that holds the full Credential JSON. The bare
# ``host`` account holds the RAW bearer token (what the tray reads).
_CRED_SUFFIX = "|cred"

# Suffix for the runner-lane credential (EXPERIMENTAL, spec 2.1): the per-runner
# id + bearer token minted by POST /api/runners/register. Kept separate from the
# user session credential -- deleting one never clobbers the other, and the raw
# ``host`` account keeps holding the session token the tray reads.
_RUNNER_SUFFIX = "|runner"

# Default hosted control-plane base URL. Overridable per-call and via config.
DEFAULT_HOST = "https://app.openadapt.ai"

# Env var that lets the headless / CI / BYOC-server path supply an ingest token
# with zero interaction and zero keychain access.
INGEST_TOKEN_ENV = "OPENADAPT_INGEST_TOKEN"


def _keyring():
    """Return the keyring module, or None if the package cannot be imported.

    A non-None return does NOT guarantee a usable backend -- on a headless box
    ``import keyring`` succeeds but ``get/set/delete_password`` raise
    ``NoKeyringError``. Those runtime failures are handled at the call sites
    (:func:`_kr_get` / :func:`_kr_set` / :func:`_kr_delete`), which degrade
    rather than propagate. Isolated so tests can inject a fake backend.
    """
    try:
        import keyring

        return keyring
    except Exception:  # pragma: no cover - keyring is a hard dep, defensive only
        logger.warning("keyring unavailable; hosted credentials cannot be persisted")
        return None


def _kr_get(kr, account: str) -> str | None:
    """Read a keyring value, degrading to None when no backend is available."""
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE_NAME, account)
    except Exception as exc:  # NoKeyringError / backend failure -> "no credential"
        logger.warning("keyring read unavailable ({e}); treating as no credential", e=exc)
        return None


def _kr_set(kr, account: str, value: str) -> bool:
    """Write a keyring value, degrading to False when no backend is available."""
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE_NAME, account, value)
        return True
    except Exception as exc:  # NoKeyringError / backend failure -> log and skip
        logger.warning("keyring write unavailable ({e}); credential not persisted", e=exc)
        return False


def _kr_delete(kr, account: str) -> None:
    """Delete a keyring value; a missing entry or missing backend is a no-op."""
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE_NAME, account)
    except Exception:
        # keyring raises when the entry is already gone or no backend exists.
        pass


def store_credential(c: Credential) -> None:
    """Persist a credential and mark its host active.

    The RAW bearer token is stored under ``account = host`` (the value the tray
    reads as a bearer token); the full Credential JSON is stored under the
    companion account. Degrades silently (logs) when no keyring backend is
    available -- callers on a headless box fall back to the env-token path.

    Args:
        c: The credential to store. ``c["host"]`` is the keyring account.
    """
    kr = _keyring()
    host = c["host"]
    persisted = _kr_set(kr, host, c["token"])
    _kr_set(kr, host + _CRED_SUFFIX, json.dumps(c))
    _kr_set(kr, _ACTIVE_HOST_ACCOUNT, host)
    if not persisted:
        logger.warning(
            "No keyring backend; credential for {host} not persisted "
            "(use OPENADAPT_INGEST_TOKEN on headless boxes)",
            host=host,
        )


def secure_store_available() -> bool:
    """Return whether a real, unlocked OS credential backend is available.

    Browser pairing consumes a one-use server secret.  Unlike the ordinary
    headless auth path, it must refuse *before* claiming that secret when the
    resulting credential cannot be persisted securely.
    """
    kr = _keyring()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
        priority = getattr(backend, "priority", 0)
        return bool(priority and priority > 0)
    except Exception:
        return False


def store_credential_secure(c: Credential) -> bool:
    """Atomically persist ``c`` for one-click pairing, or clean up and fail.

    All three entries must be writable and readable: the raw token consumed by
    the tray, the companion credential metadata, and the active-host pointer.
    No plaintext fallback is permitted.
    """
    kr = _keyring()
    if kr is None:
        return False

    host = c["host"]
    accounts = (host, host + _CRED_SUFFIX, _ACTIVE_HOST_ACCOUNT)
    values = (c["token"], json.dumps(c), host)
    previous = tuple(_kr_get(kr, account) for account in accounts)

    def rollback() -> None:
        for account, value in zip(accounts, previous):
            if value is None:
                _kr_delete(kr, account)
            else:
                _kr_set(kr, account, value)

    if not all(_kr_set(kr, account, value) for account, value in zip(accounts, values)):
        rollback()
        return False

    if any(_kr_get(kr, account) != value for account, value in zip(accounts, values)):
        rollback()
        return False
    return True


def _strict_get(kr, account: str) -> tuple[bool, str | None]:
    """Read while distinguishing an absent entry from a keychain failure."""
    if kr is None:
        return False, None
    try:
        return True, kr.get_password(SERVICE_NAME, account)
    except Exception:
        return False, None


def _apply_exact(kr, account: str, value: str | None) -> bool:
    """Set or remove one entry and verify its exact resulting value."""
    try:
        if value is None:
            try:
                kr.delete_password(SERVICE_NAME, account)
            except Exception:
                # Missing entries and deletion failures are distinguished by
                # the strict read-back below.
                pass
        else:
            kr.set_password(SERVICE_NAME, account, value)
    except Exception:
        return False
    readable, current = _strict_get(kr, account)
    return readable and current == value


def snapshot_pairing_canonical(host: str) -> dict[str, str | None] | None:
    """Strictly snapshot the exact canonical entries before consuming a claim."""
    kr = _keyring()
    accounts = (host, host + _CRED_SUFFIX, _ACTIVE_HOST_ACCOUNT)
    values: list[str | None] = []
    for account in accounts:
        readable, value = _strict_get(kr, account)
        if not readable:
            return None
        values.append(value)
    return {
        "host": host,
        "token": values[0],
        "credential": values[1],
        "active_host": values[2],
    }


def stage_pairing_credential(
    pairing_id: str,
    credential: Credential,
    previous: dict[str, str | None],
    device_name: str,
) -> bool:
    """Durably stage one claimed token plus its exact rollback snapshot."""
    kr = _keyring()
    readable, current = _strict_get(kr, _PAIRING_STAGE_ACCOUNT)
    if not readable or current is not None or previous.get("host") != credential["host"]:
        return False
    payload = json.dumps(
        {
            "version": _PAIRING_STAGE_VERSION,
            "pairing_id": pairing_id,
            "credential": credential,
            "previous": previous,
            "device_name": device_name,
            "state": "claimed",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if not _apply_exact(kr, _PAIRING_STAGE_ACCOUNT, payload):
        return False
    readable, stored = _strict_get(kr, _PAIRING_STAGE_ACCOUNT)
    return readable and stored == payload


def load_pairing_stage() -> dict | None:
    """Load the single crash-recovery record, failing closed if it is unreadable."""
    readable, raw = _strict_get(_keyring(), _PAIRING_STAGE_ACCOUNT)
    if not readable:
        raise RuntimeError("pairing recovery keychain entry is unreadable")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("pairing recovery keychain entry is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("pairing recovery keychain entry is invalid")
    return value


def _pairing_stage_values(
    stage: dict,
) -> tuple[str, tuple[str | None, ...], tuple[str, ...]] | None:
    credential = stage.get("credential")
    previous = stage.get("previous")
    if (
        stage.get("version") != _PAIRING_STAGE_VERSION
        or not isinstance(credential, dict)
        or not isinstance(previous, dict)
        or not isinstance(credential.get("host"), str)
        or previous.get("host") != credential["host"]
        or not isinstance(credential.get("token"), str)
    ):
        return None
    if any(
        previous.get(key) is not None and not isinstance(previous.get(key), str)
        for key in ("token", "credential", "active_host")
    ):
        return None
    host = credential["host"]
    prior = (
        previous.get("token"),
        previous.get("credential"),
        previous.get("active_host"),
    )
    current = (credential["token"], json.dumps(credential), host)
    return host, prior, current


def mark_pairing_stage(pairing_id: str, state: str) -> bool:
    """Persist a bounded recovery state without ever changing its identity."""
    if state not in {
        "claimed",
        "canonical_written",
        "confirm_ambiguous",
        "abort_acknowledged",
    }:
        return False
    try:
        stage = load_pairing_stage()
    except RuntimeError:
        return False
    if stage is None or stage.get("pairing_id") != pairing_id:
        return False
    stage["state"] = state
    payload = json.dumps(stage, sort_keys=True, separators=(",", ":"))
    return _apply_exact(_keyring(), _PAIRING_STAGE_ACCOUNT, payload)


def commit_pairing_stage(pairing_id: str) -> bool:
    """CAS-promote the staged credential into the three canonical entries."""
    try:
        stage = load_pairing_stage()
    except RuntimeError:
        return False
    if stage is None or stage.get("pairing_id") != pairing_id:
        return False
    values = _pairing_stage_values(stage)
    if values is None:
        return False
    host, previous, replacement = values
    kr = _keyring()
    accounts = (host, host + _CRED_SUFFIX, _ACTIVE_HOST_ACCOUNT)
    observed: list[str | None] = []
    for account in accounts:
        readable, value = _strict_get(kr, account)
        if not readable:
            return False
        observed.append(value)
    if tuple(observed) == replacement:
        mark_pairing_stage(pairing_id, "canonical_written")
        return True
    if any(
        value not in {prior, new}
        for value, prior, new in zip(observed, previous, replacement)
    ):
        # A concurrent login changed canonical state after the snapshot. Never
        # overwrite it or later "restore" stale values over it.
        return False

    committed = [
        _apply_exact(kr, account, value)
        for account, value in zip(accounts, replacement)
    ]
    if not all(committed):
        for rollback_account, rollback_value in zip(accounts, previous):
            _apply_exact(kr, rollback_account, rollback_value)
        return False
    mark_pairing_stage(pairing_id, "canonical_written")
    return True


def restore_pairing_stage(pairing_id: str) -> bool:
    """Restore the exact snapshot, but only over this stage's own replacement."""
    try:
        stage = load_pairing_stage()
    except RuntimeError:
        return False
    if stage is None or stage.get("pairing_id") != pairing_id:
        return False
    values = _pairing_stage_values(stage)
    if values is None:
        return False
    host, previous, replacement = values
    kr = _keyring()
    accounts = (host, host + _CRED_SUFFIX, _ACTIVE_HOST_ACCOUNT)
    observed: list[str | None] = []
    for account in accounts:
        readable, value = _strict_get(kr, account)
        if not readable:
            return False
        observed.append(value)
    if tuple(observed) == previous:
        return True
    if any(
        value not in {prior, new}
        for value, prior, new in zip(observed, previous, replacement)
    ):
        return False
    restored = [
        _apply_exact(kr, account, value)
        for account, value in zip(accounts, previous)
    ]
    return all(restored)


def clear_pairing_stage(pairing_id: str) -> bool:
    """Delete only the exact staged transaction after resolution."""
    try:
        stage = load_pairing_stage()
    except RuntimeError:
        return False
    if stage is None:
        return True
    if stage.get("pairing_id") != pairing_id:
        return False
    return _apply_exact(_keyring(), _PAIRING_STAGE_ACCOUNT, None)


def load_credential(host: str) -> Credential | None:
    """Load a stored credential for ``host``, or None if absent/unreadable.

    Prefers the companion JSON entry (full Credential). Falls back to a bare
    raw bearer token stored under ``account = host`` (e.g. set by another
    surface), reconstructing a minimal ``ingest_token`` credential so the two
    surfaces can share one credential.
    """
    kr = _keyring()
    raw = _kr_get(kr, host + _CRED_SUFFIX)
    if raw:
        try:
            return json.loads(raw)  # type: ignore[return-value]
        except (json.JSONDecodeError, TypeError):
            logger.warning("stored credential for {host} is corrupt; ignoring", host=host)
            return None
    token = _kr_get(kr, host)
    if token:
        cred: Credential = {
            "kind": "ingest_token",
            "token": token,
            "refresh_token": None,
            "org_id": None,
            "host": host,
            "expires_at": None,
        }
        return cred
    return None


def clear_credential(host: str) -> None:
    """Delete the stored credential for ``host`` (used by logout / uninstall purge)."""
    kr = _keyring()
    _kr_delete(kr, host)
    _kr_delete(kr, host + _CRED_SUFFIX)
    if _kr_get(kr, _ACTIVE_HOST_ACCOUNT) == host:
        _kr_delete(kr, _ACTIVE_HOST_ACCOUNT)


def active_host() -> str | None:
    """Return the host most recently stored, or None."""
    return _kr_get(_keyring(), _ACTIVE_HOST_ACCOUNT)


def active_credential() -> Credential | None:
    """Return the credential for the active host, or None."""
    host = active_host()
    if not host:
        return None
    return load_credential(host)


def store_runner_credential(host: str, runner_id: str, runner_token: str) -> None:
    """Persist the per-runner id + token minted by ``POST /api/runners/register``.

    Same keychain discipline as the session credential (service
    ``ai.openadapt.desktop``); nothing token-shaped ever lands in
    ``config.toml``. Degrades to a warning when no keyring backend exists.
    """
    kr = _keyring()
    payload = json.dumps({"runner_id": runner_id, "runner_token": runner_token})
    if not _kr_set(kr, host + _RUNNER_SUFFIX, payload):
        logger.warning(
            "No keyring backend; runner credential for {host} not persisted", host=host
        )


def load_runner_credential(host: str) -> dict | None:
    """Load the runner credential for ``host`` (``{runner_id, runner_token}``), or None."""
    raw = _kr_get(_keyring(), host + _RUNNER_SUFFIX)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("stored runner credential for {host} is corrupt; ignoring", host=host)
        return None
    if not isinstance(data, dict) or not data.get("runner_token"):
        return None
    return data


def clear_runner_credential(host: str) -> None:
    """Delete the stored runner credential for ``host`` (deregister / purge)."""
    _kr_delete(_keyring(), host + _RUNNER_SUFFIX)


def auth_header() -> dict[str, str]:
    """Resolve the active bearer token to an HTTP ``Authorization`` header.

    Resolution order (spec section 3e): ``OPENADAPT_INGEST_TOKEN`` env, then
    the active host's stored credential. Returns an empty dict when no
    credential is available so callers can decide how to handle "unauthed".

    Returns:
        ``{"Authorization": "Bearer <token>"}`` or ``{}``.
    """
    env_token = os.environ.get(INGEST_TOKEN_ENV, "").strip()
    if env_token:
        return {"Authorization": f"Bearer {env_token}"}

    cred = active_credential()
    if cred and cred.get("token"):
        return {"Authorization": f"Bearer {cred['token']}"}

    return {}
