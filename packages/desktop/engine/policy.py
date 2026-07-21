"""policy -- fail-closed effective-policy fetch + cache for the desktop engine.

The cloud control plane serves the ORG's resolved policy at
``GET /api/policy/effective`` (bearer-authed). That payload merges three tiers:

    * ``user`` -- Tier-1 per-user preferences the local UI may edit;
    * ``org``  -- Tier-2 org defaults (read-only on the desktop; admin edits go
      to the cloud API, never written locally); and
    * ``safety`` -- Tier-3 safety guardrails that gate whether a run may proceed
      at all (effect verification, halt-on-ambiguous, identity gate, ...).

FAIL-CLOSED CONTRACT (the whole point of this module)
-----------------------------------------------------
Safety must never *weaken* because the network is down, the cache is stale, or
the server omitted a key. Therefore:

    * :func:`resolve_effective_policy` ALWAYS routes its result through
      :func:`harden_safety`, which guarantees EVERY key in
      :data:`SAFE_SAFETY_DEFAULTS` is present. Any missing or ``None`` safety
      value is replaced with its SAFEST default (fail-closed).
    * If there is neither network nor a cache, the resolver returns a
      fully-populated fail-closed default (no ``user``/``org`` prefs, not an
      admin, safety = the safest values).
    * A missing ``safety`` object in a server response is treated exactly like
      an empty one and hardened to the safe defaults.

Network and cache failures NEVER raise out of :func:`resolve_effective_policy`
or :func:`load_cached_policy` -- they degrade, mirroring the keychain gate in
:mod:`engine.auth.store` (``_kr_get`` returns ``None`` rather than propagating a
missing-backend error). Only :func:`fetch_effective_policy` raises, so its
caller can decide whether to fall back to cache.

The raw server body is cached to ``~/.openadapt/policy.json`` (same dir as
``config.toml``) with an atomic temp-file + :func:`os.replace` write so a
half-written file can never be read back.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from engine.auth.store import auth_header
from engine.config import DEFAULT_CONFIG_TOML

# Endpoint the cloud serves the resolved (merged) org policy from.
POLICY_PATH = "/api/policy/effective"

# On-disk cache of the last-known-good policy body. Lives beside config.toml in
# ``~/.openadapt/``. Overridable for tests via ``OPENADAPT_POLICY_CACHE``.
DEFAULT_POLICY_CACHE = DEFAULT_CONFIG_TOML.parent / "policy.json"

# Default HTTP timeout (seconds) for a policy fetch. Kept short so a slow/hung
# control plane degrades quickly to cache rather than stalling a run.
DEFAULT_TIMEOUT = 10.0

# The SAFEST value for every safety key the contract defines. A missing or
# unreachable value MUST resolve to the entry here (fail-closed): more checking,
# stricter gates, no unverified writes, no model calls, managed-strict egress.
SAFE_SAFETY_DEFAULTS: dict[str, Any] = {
    "effect_verification.required_for_consequential": True,
    "halt_on_ambiguous": True,
    "identity_gate.strictness": "strict",
    "pixel_verify.consequential_policy": "disabled",
    "unverified_write.allow": False,
    "egress.artifact_policy": "managed-strict",
    "model_calls.allowed_in_healthy_run": False,
}


class PolicyFetchError(Exception):
    """Raised when the effective-policy endpoint is unreachable or malformed.

    Only :func:`fetch_effective_policy` raises this; :func:`resolve_effective_policy`
    catches it and falls back to cache / the fail-closed default.
    """


def _policy_cache_path() -> Path:
    """Return the policy cache path, honoring the ``OPENADAPT_POLICY_CACHE`` override."""
    override = os.environ.get("OPENADAPT_POLICY_CACHE", "").strip()
    return Path(override) if override else DEFAULT_POLICY_CACHE


def fetch_effective_policy(host: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Fetch the org's effective policy over the network and refresh the cache.

    Performs a bearer ``GET {host}/api/policy/effective`` using the active
    credential resolved by :func:`~engine.auth.store.auth_header`, modeled on
    :meth:`engine.auth.paste.PasteTokenProvider._validate`. On success, the RAW
    response body is written atomically to the cache file so a later offline
    :func:`load_cached_policy` returns the last-known-good policy.

    Args:
        host: Hosted control-plane base URL (e.g. ``https://app.openadapt.ai``).
        timeout: HTTP timeout in seconds.

    Returns:
        The parsed policy dict (NOT yet hardened -- the caller hardens).

    Raises:
        PolicyFetchError: On any non-2xx response, network error, or invalid JSON.
    """
    url = f"{host.rstrip('/')}{POLICY_PATH}"
    headers = {**auth_header(), "Accept": "application/json"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise PolicyFetchError(f"Could not reach {host}: {exc}") from exc

    if resp.status_code == 401:
        raise PolicyFetchError("Policy request was rejected (401).")
    if resp.status_code >= 400:
        raise PolicyFetchError(f"Policy request failed ({resp.status_code}).")

    try:
        policy = resp.json()
    except ValueError as exc:
        raise PolicyFetchError(f"Policy response was not valid JSON: {exc}") from exc
    if not isinstance(policy, dict):
        raise PolicyFetchError("Policy response was not a JSON object.")

    _write_cache(policy)
    return policy


def _write_cache(policy: dict[str, Any]) -> None:
    """Atomically persist the raw policy body to the cache file.

    Writes to a temp file in the cache directory, then :func:`os.replace`s it
    into place so a reader can never observe a half-written file. Degrades
    (logs, does not raise) if the cache cannot be written -- a fetched policy is
    still usable in-memory even when the disk is read-only.
    """
    path = _policy_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".policy.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(policy, fh)
            os.replace(tmp, path)
        except Exception:
            # Clean up the temp file on any failure so we don't leak turds.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # pragma: no cover - disk-failure defensive path
        logger.warning("Could not persist policy cache: {e}", e=exc)


def load_cached_policy() -> dict[str, Any] | None:
    """Read the last-cached policy body, or ``None`` if absent/unreadable.

    Degrade-not-raise (mirrors :func:`engine.auth.store._kr_get`): a missing
    file, unreadable file, or corrupt JSON all resolve to ``None`` rather than
    propagating an error, because callers use this as a fallback and must never
    crash the run over a bad cache.

    Returns:
        The parsed cached policy dict, or ``None``.
    """
    path = _policy_cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Cached policy at {p} is corrupt; ignoring", p=path)
        return None
    if not isinstance(data, dict):
        return None
    return data


def harden_safety(policy: dict[str, Any]) -> dict[str, Any]:
    """Return ``policy`` with a fully-populated, fail-closed ``safety`` block.

    Every key in :data:`SAFE_SAFETY_DEFAULTS` is guaranteed present in the
    returned policy's ``safety`` object. A key that is MISSING or ``None`` is
    filled with its safe default. Values the server DID provide are preserved
    as-is -- the server is authoritative when it speaks; we only fill gaps.
    A missing/invalid ``safety`` object is treated as an empty one.

    Does not mutate the input; returns a shallow copy with a fresh ``safety``.

    Args:
        policy: A raw or partial policy dict.

    Returns:
        The policy with a complete ``safety`` block (fail-closed on gaps).
    """
    hardened = dict(policy)
    raw_safety = hardened.get("safety")
    if not isinstance(raw_safety, dict):
        raw_safety = {}

    safety: dict[str, Any] = {}
    for key, safe_default in SAFE_SAFETY_DEFAULTS.items():
        value = raw_safety.get(key)
        # A MISSING or explicitly-null value fails closed to the safe default.
        safety[key] = safe_default if value is None else value

    hardened["safety"] = safety
    return hardened


def _fail_closed_default() -> dict[str, Any]:
    """Build the fully fail-closed policy used when neither network nor cache exists."""
    return {
        "safety": dict(SAFE_SAFETY_DEFAULTS),
        "user": {},
        "org": {},
        "is_admin": False,
        "role": "member",
        "policy_version": None,
        "source": "fail-closed-default",
    }


def resolve_effective_policy(
    host: str, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Resolve the effective policy, always fail-closed on safety.

    Resolution order:
        1. Try :func:`fetch_effective_policy` (network); on success tag
           ``source="network"``.
        2. On ANY fetch failure, fall back to :func:`load_cached_policy`; on a
           hit tag ``source="cache"``.
        3. If there is neither network nor cache, return the fully-populated
           fail-closed default (``source="fail-closed-default"``).

    The chosen policy ALWAYS passes through :func:`harden_safety`, so every
    safety key is guaranteed present at its safe default when missing.

    This function NEVER raises -- the dispatcher command surface relies on it to
    return a usable, safe policy no matter what.

    Args:
        host: Hosted control-plane base URL.
        timeout: HTTP timeout in seconds for the network fetch.

    Returns:
        The hardened effective policy with a ``source`` field of
        ``"network"`` | ``"cache"`` | ``"fail-closed-default"``.
    """
    source = "network"
    policy: dict[str, Any] | None
    try:
        policy = fetch_effective_policy(host, timeout=timeout)
    except PolicyFetchError as exc:
        logger.warning("Policy fetch failed ({e}); falling back to cache", e=exc)
        policy = load_cached_policy()
        source = "cache"

    if policy is None:
        # No network AND no cache: the safest possible posture.
        return _fail_closed_default()

    hardened = harden_safety(policy)
    hardened["source"] = source
    return hardened
