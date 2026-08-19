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

The server body is cached in a closed envelope at ``~/.openadapt/policy.json``
(same dir as ``config.toml``). The envelope binds it to the canonical host,
credential identity, organization, policy version, and fetch time. An atomic
temp-file + :func:`os.replace` write prevents partial reads.

BINDING THE POLICY TO A RUN
---------------------------
Resolving a policy is only half a governance control; the other half is making
the resolved value change what a run actually does. :func:`binding_safety` and
:func:`apply_safety_policy` are that half: they validate the resolved ``safety``
block against its exact value domain and project it onto the Flow deployment
config the runner hands to ``openadapt-flow run``.

Two invariants govern the projection:

    * **Strengthen only.** The policy may make a run stricter, never looser. A
      deployment that already selects a stricter posture than the policy demands
      keeps it. This is why ``pixel_verify.consequential_policy: disabled`` (the
      *baseline*, not a prohibition) leaves the deployment's own value alone
      while ``required`` forces the check on.
    * **Refuse rather than guess.** A value outside its known domain, a
      non-authoritative policy, or a deployment posture that cannot be ranked
      raises :class:`PolicyEnforcementError`. The caller turns that into a
      pre-action refusal; it never falls back to "run it anyway".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from engine.auth.store import (
    active_credential,
    auth_header,
    canonical_host_origin,
    token_for_host,
)
from engine.config import DEFAULT_CONFIG_TOML

# Endpoint the cloud serves the resolved (merged) org policy from.
POLICY_PATH = "/api/policy/effective"

# On-disk cache of the last-known-good policy body. Lives beside config.toml in
# ``~/.openadapt/``. Overridable for tests via ``OPENADAPT_POLICY_CACHE``.
DEFAULT_POLICY_CACHE = DEFAULT_CONFIG_TOML.parent / "policy.json"

# Default HTTP timeout (seconds) for a policy fetch. Kept short so a slow/hung
# control plane degrades quickly to cache rather than stalling a run.
DEFAULT_TIMEOUT = 10.0

# A cached org policy is an offline continuity aid, not permanent authority.
# After one day the Desktop must reconnect before it can govern another run.
DEFAULT_CACHE_MAX_AGE_S = 24 * 60 * 60
CACHE_SCHEMA = "openadapt.policy-cache/v3"

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

# The EXACT value domain of every safety key, mirroring the canonical registry
# (openadapt-cloud ``src/lib/settingsRegistry.ts``). A resolved value outside its
# domain is MALFORMED: the engine cannot know which posture it implies, so a run
# that would be governed by it refuses instead of guessing. Booleans are matched
# by identity (``True``/``False``), never by ``==``, so ``1`` is not a boolean.
SAFETY_VALUE_DOMAINS: dict[str, tuple[Any, ...]] = {
    "effect_verification.required_for_consequential": (True, False),
    "halt_on_ambiguous": (True, False),
    "identity_gate.strictness": ("strict", "standard"),
    "pixel_verify.consequential_policy": ("disabled", "required"),
    "unverified_write.allow": (True, False),
    "egress.artifact_policy": ("managed-strict", "customer-boundary"),
    "model_calls.allowed_in_healthy_run": (True, False),
}

# Flow's named execution profiles, ranked weakest -> strictest
# (``openadapt_flow.execution_profiles``). ``demo`` enforces no effect
# contracts, no identity coverage, and permits blanket unverified-write
# approval; ``standard``/``regulated`` are the production contracts.
#
# An ABSENT profile is deliberately NOT ranked here: Flow resolves an omitted
# profile to ``regulated`` (its strictest default), so writing a profile into a
# config that had none would WEAKEN the run. Absent stays absent.
PROFILE_RANK: dict[str, int] = {"demo": 0, "standard": 1, "regulated": 2}

# The weakest profile that still enforces effect contracts, identity coverage,
# settled frames, and no blanket unverified-write approval.
MINIMUM_PRODUCTION_PROFILE = "standard"

# ``source`` value meaning "no authoritative policy was ever obtained" -- neither
# the control plane nor a cached body. The safest values still populate the
# block, but they are the engine's guess at the org's posture, not the org's
# posture: an org that STRENGTHENED a key beyond baseline (e.g. required
# pixel-identity verification) would be silently run without it. A governed run
# refuses on this source rather than execute under an unconfirmed policy.
UNCONFIRMED_POLICY_SOURCE = "fail-closed-default"


class PolicyFetchError(Exception):
    """Raised when the effective-policy endpoint is unreachable or malformed.

    Only :func:`fetch_effective_policy` raises this; :func:`resolve_effective_policy`
    catches it and falls back to cache / the fail-closed default.
    """


class PolicyEnforcementError(Exception):
    """Raised when a resolved policy cannot be BOUND to a run.

    Distinct from :class:`PolicyFetchError`, which is about obtaining a policy.
    This one means the engine holds a policy it cannot faithfully enforce --
    it is not authoritative, a value is outside its known domain, or the
    deployment posture cannot be ranked against it. The caller must refuse the
    run; there is no permissive fallback.
    """


def _policy_cache_path() -> Path:
    """Return the policy cache path, honoring the ``OPENADAPT_POLICY_CACHE`` override."""
    override = os.environ.get("OPENADAPT_POLICY_CACHE", "").strip()
    return Path(override) if override else DEFAULT_POLICY_CACHE


def _credential_binding_hmac(host: str) -> str | None:
    """Return a keyed, destination-bound identity for the active bearer.

    The bearer is the HMAC key. It is never stored and it is not passed through
    an unkeyed password-hash operation. The public message binds the result to
    this cache contract and exact hosted origin.
    """

    token = token_for_host(host)
    if not token:
        return None
    origin = canonical_host_origin(host)
    if not origin:
        return None
    message = f"openadapt.policy-cache-credential/v2\0{origin}".encode()
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _credential_org_id(host: str) -> str | None:
    """Return the keychain org only when it belongs to this host and bearer."""

    credential = active_credential()
    if not credential:
        return None
    if canonical_host_origin(str(credential.get("host") or "")) != canonical_host_origin(host):
        return None
    if str(credential.get("token") or "").strip() != token_for_host(host):
        return None
    org_id = credential.get("org_id")
    return org_id if isinstance(org_id, str) and org_id else None


def _safe_policy_origin(host: str) -> str:
    """Return the exact permitted origin for policy fetch and cache binding."""

    parsed_host = urlsplit(str(host or "").strip())
    if parsed_host.scheme.lower() == "http" and parsed_host.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise PolicyFetchError("A remote policy host must use HTTPS.")
    origin = canonical_host_origin(host)
    if not origin:
        raise PolicyFetchError("Policy host is not a valid HTTP(S) origin.")
    return origin


def _policy_version(policy: Mapping[str, Any]) -> int | None:
    """Return a valid monotonic policy version, else ``None``."""

    value = policy.get("policy_version")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _policy_sha256(policy: Mapping[str, Any]) -> str:
    """Bind one exact policy body without retaining another sensitive copy."""

    canonical = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"openadapt.policy-cache-body/v1\0")
    digest.update(canonical)
    return digest.hexdigest()


def _policy_authority_sha256(policy: Mapping[str, Any]) -> str:
    """Bind only version-controlled organization policy authority.

    The effective-policy response also carries per-request and per-user fields.
    Those fields can change without an organization policy version change. They
    must not disable a valid refresh. The fields below define the organization
    authority that must move only with ``policy_version``.
    """

    authority = {
        key: policy.get(key)
        for key in (
            "org_id",
            "baseline_version",
            "org",
            "safety",
            "grounding_model",
        )
    }
    canonical = json.dumps(
        authority,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"openadapt.policy-authority/v1\0")
    digest.update(canonical)
    return digest.hexdigest()


def fetch_effective_policy(host: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Fetch the org's effective policy over the network and refresh the cache.

    Performs a bearer ``GET {host}/api/policy/effective`` using the active
    credential resolved by :func:`~engine.auth.store.auth_header`, modeled on
    :meth:`engine.auth.paste.PasteTokenProvider._validate`. On success, the RAW
    response body is written atomically to the cache file so a later offline
    :func:`load_cached_policy` can return it only to the same principal.

    Args:
        host: Hosted control-plane base URL (e.g. ``https://app.openadapt.ai``).
        timeout: HTTP timeout in seconds.

    Returns:
        The parsed policy dict (NOT yet hardened -- the caller hardens).

    Raises:
        PolicyFetchError: On any non-2xx response, network error, or invalid JSON.
    """
    origin = _safe_policy_origin(host)
    url = f"{origin}{POLICY_PATH}"
    headers = {**auth_header(origin), "Accept": "application/json"}
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
    if _policy_version(policy) is None:
        raise PolicyFetchError("Policy response did not include a valid policy version.")

    org_id = policy.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        raise PolicyFetchError("Policy response did not identify its organization.")
    credential_org_id = _credential_org_id(origin)
    if credential_org_id is not None and credential_org_id != org_id:
        raise PolicyFetchError("Policy organization did not match the active credential.")

    cached = load_cached_policy(origin, max_age_s=float("inf"))
    if cached is not None:
        cached_version = _policy_version(cached)
        policy_version = _policy_version(policy)
        if cached_version is not None and policy_version is not None:
            if policy_version < cached_version:
                raise PolicyFetchError("Policy response version moved backwards.")
            if (
                policy_version == cached_version
                and _policy_authority_sha256(policy)
                != _policy_authority_sha256(cached)
            ):
                raise PolicyFetchError("Policy response changed without a new version.")

    _write_cache(policy, origin)
    return policy


def _write_cache(policy: dict[str, Any], host: str) -> None:
    """Atomically persist a host, credential, org, and time-bound cache envelope.

    Writes to a temp file in the cache directory, then :func:`os.replace`s it
    into place so a reader can never observe a half-written file. Degrades
    (logs, does not raise) if the cache cannot be written -- a fetched policy is
    still usable in-memory even when the disk is read-only.
    """
    path = _policy_cache_path()
    host_origin = canonical_host_origin(host)
    credential_binding_hmac = _credential_binding_hmac(host)
    org_id = policy.get("org_id")
    policy_version = _policy_version(policy)
    if (
        not host_origin
        or not credential_binding_hmac
        or not isinstance(org_id, str)
        or not org_id
        or policy_version is None
    ):
        logger.warning("Could not bind policy cache to the current hosted credential")
        return
    envelope = {
        "schema": CACHE_SCHEMA,
        "binding": {
            "host_origin": host_origin,
            "credential_binding_hmac": credential_binding_hmac,
            "org_id": org_id,
            "policy_version": policy_version,
            "policy_sha256": _policy_sha256(policy),
        },
        "fetched_at": datetime.now(UTC).isoformat(),
        "policy": policy,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".policy.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh)
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


def load_cached_policy(
    host: str,
    *,
    now: datetime | None = None,
    max_age_s: float = DEFAULT_CACHE_MAX_AGE_S,
) -> dict[str, Any] | None:
    """Read only a fresh cache bound to this exact host and credential.

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
    if not isinstance(data, dict) or set(data) != {
        "schema",
        "binding",
        "fetched_at",
        "policy",
    }:
        return None
    binding = data.get("binding")
    policy = data.get("policy")
    if data.get("schema") != CACHE_SCHEMA or not isinstance(binding, dict):
        return None
    if set(binding) != {
        "host_origin",
        "credential_binding_hmac",
        "org_id",
        "policy_version",
        "policy_sha256",
    }:
        return None
    if not isinstance(policy, dict):
        return None
    expected_origin = canonical_host_origin(host)
    expected_credential = _credential_binding_hmac(host)
    if not expected_origin or not expected_credential:
        return None
    if binding.get("host_origin") != expected_origin:
        return None
    if not hmac.compare_digest(
        str(binding.get("credential_binding_hmac") or ""), expected_credential
    ):
        return None
    policy_org_id = policy.get("org_id")
    if not isinstance(policy_org_id, str) or not policy_org_id:
        return None
    if binding.get("org_id") != policy_org_id:
        return None
    credential_org_id = _credential_org_id(host)
    if credential_org_id is not None and credential_org_id != policy_org_id:
        return None
    if binding.get("policy_version") != policy.get("policy_version"):
        return None
    if _policy_version(policy) is None:
        return None
    if binding.get("policy_sha256") != _policy_sha256(policy):
        return None
    try:
        fetched_at = datetime.fromisoformat(str(data.get("fetched_at")))
        if fetched_at.tzinfo is None:
            return None
        age_s = ((now or datetime.now(UTC)) - fetched_at.astimezone(UTC)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    if age_s < 0 or age_s > max_age_s:
        return None
    return policy


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
        policy = load_cached_policy(host)
        source = "cache"

    if policy is None:
        # No network AND no cache: the safest possible posture.
        return _fail_closed_default()

    hardened = harden_safety(policy)
    hardened["source"] = source
    return hardened


# --------------------------------------------------------------------------
# Binding a resolved policy to a run (the half that makes the control real).
# --------------------------------------------------------------------------


def _in_domain(key: str, value: Any) -> bool:
    """Whether ``value`` is one of the exact allowed values for ``key``.

    Identity comparison for booleans (so ``1`` is not ``True``) and exact
    equality for the string enums.
    """
    domain = SAFETY_VALUE_DOMAINS[key]
    for allowed in domain:
        if isinstance(allowed, bool):
            if value is allowed:
                return True
        elif isinstance(value, str) and value == allowed:
            return True
    return False


def binding_safety(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a resolved policy and return the ``safety`` block it binds.

    This is the gate a governed run passes before any action. It refuses --
    rather than degrading -- in exactly the three cases where enforcement would
    otherwise be a guess:

        1. the policy is not authoritative (``source`` is
           :data:`UNCONFIRMED_POLICY_SOURCE`, i.e. neither the control plane nor
           a cached body was available), so an org-strengthened key would be
           silently dropped;
        2. the ``safety`` block is absent or is not an object; or
        3. any safety key is missing or carries a value outside
           :data:`SAFETY_VALUE_DOMAINS`.

    Args:
        policy: A policy as returned by :func:`resolve_effective_policy`.

    Returns:
        The validated ``safety`` block (a copy).

    Raises:
        PolicyEnforcementError: In any of the three cases above.
    """
    source = policy.get("source")
    if source == UNCONFIRMED_POLICY_SOURCE:
        raise PolicyEnforcementError(
            "no authoritative safety policy is available (control plane "
            "unreachable and no cached policy); refusing rather than running "
            "on assumed defaults"
        )
    safety = policy.get("safety")
    if not isinstance(safety, Mapping):
        raise PolicyEnforcementError("resolved policy carries no safety block")

    validated: dict[str, Any] = {}
    for key in SAFE_SAFETY_DEFAULTS:
        if key not in safety:
            raise PolicyEnforcementError(f"safety key '{key}' is missing")
        value = safety[key]
        if not _in_domain(key, value):
            allowed = ", ".join(repr(v) for v in SAFETY_VALUE_DOMAINS[key])
            raise PolicyEnforcementError(
                f"safety key '{key}' has an unknown value {value!r}; expected one of: {allowed}"
            )
        validated[key] = value
    return validated


def apply_safety_policy(
    deployment: Mapping[str, Any], safety: Mapping[str, Any]
) -> dict[str, Any]:
    """Project a validated ``safety`` block onto a Flow deployment config.

    STRENGTHEN-ONLY. Every rule below either tightens the run or leaves the
    deployment's own value untouched; none of them relaxes a posture the
    operator already selected.

    * ``pixel_verify.consequential_policy`` -- ``required`` arms
      ``runtime.pixel_verify_enabled``. ``disabled`` is the platform BASELINE,
      not a prohibition, so it leaves the deployment's own value alone.
    * ``model_calls.allowed_in_healthy_run`` -- ``False`` forces
      ``runtime.allow_model_grounding = False`` (fully local, zero outbound
      calls). ``True`` is a permission, not a requirement: value left alone.
    * ``effect_verification.required_for_consequential``,
      ``unverified_write.allow``, ``identity_gate.strictness``, and
      ``halt_on_ambiguous`` -- any of these at its strict value requires a
      PRODUCTION execution profile, which is what enforces effect contracts,
      identity coverage, settled frames, and no blanket unverified-write
      approval. A ``demo`` profile is escalated to ``standard``; an ABSENT
      profile is left absent, because Flow defaults an omitted profile to
      ``regulated`` -- stricter than anything this projection would write.
    * ``egress.artifact_policy`` -- validated but NOT projected here. Artifact
      egress is governed by the upload path (``engine.upload_manager`` /
      ``engine.hosted``), not by the replay runtime; saying so is more honest
      than silently claiming an enforcement that does not happen.

    Args:
        deployment: The operator's deployment config as a mapping.
        safety: A safety block already validated by :func:`binding_safety`.

    Returns:
        A new deployment mapping with the policy applied. The input is not mutated.

    Raises:
        PolicyEnforcementError: If ``runtime`` is not an object, or the config
            declares an execution profile that cannot be ranked (so the engine
            cannot prove it is not weakening it).
    """
    bound = dict(deployment)
    raw_runtime = bound.get("runtime", {})
    if raw_runtime is None:
        raw_runtime = {}
    if not isinstance(raw_runtime, Mapping):
        raise PolicyEnforcementError("deployment config 'runtime' must be an object")
    runtime = dict(raw_runtime)

    if safety["pixel_verify.consequential_policy"] == "required":
        runtime["pixel_verify_enabled"] = True

    if safety["model_calls.allowed_in_healthy_run"] is False:
        runtime["allow_model_grounding"] = False

    requires_production_profile = (
        safety["effect_verification.required_for_consequential"] is True
        or safety["unverified_write.allow"] is False
        or safety["identity_gate.strictness"] == "strict"
        or safety["halt_on_ambiguous"] is True
    )
    if requires_production_profile:
        profile = runtime.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or profile not in PROFILE_RANK:
                raise PolicyEnforcementError(
                    f"deployment config declares an unrankable execution profile {profile!r}; "
                    "the engine cannot prove the safety policy is enforced"
                )
            if PROFILE_RANK[profile] < PROFILE_RANK[MINIMUM_PRODUCTION_PROFILE]:
                runtime["profile"] = MINIMUM_PRODUCTION_PROFILE

    bound["runtime"] = runtime
    return bound
