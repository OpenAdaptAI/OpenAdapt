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

# Suffix for the companion account that holds the full Credential JSON. The bare
# ``host`` account holds the RAW bearer token (what the tray reads).
_CRED_SUFFIX = "|cred"

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
