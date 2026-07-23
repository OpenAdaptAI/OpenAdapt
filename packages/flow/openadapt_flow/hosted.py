"""Cloud connectivity for the openadapt-flow CLI (``login`` / ``push`` /
break-report emitter).

This is a thin, additive wrapper around the existing engine: it changes no
compiler / IR / replay internals. It provides the local loop's governed hosted
control-plane boundary (``app.openadapt.ai``):

* :func:`login` validates an ingest token and prefers OS-keychain storage.
  Plaintext config storage is an explicit opt-in migration fallback.
* :func:`push` uploads only a reviewed, exact-hash sanitized archive to
  ``POST /api/ingest`` with an ``openadapt.sanitization/v1`` manifest.
* :func:`report_break` — serialize a halted run's ``report.json`` (its
  :class:`~openadapt_flow.ir.HaltObservation` / ``RunReport.halt``) into a
  PHI-free diagnostic and ``POST`` it to ``/api/runs/ingest-report`` so a break
  is triageable centrally WITHOUT any recording leaving the machine.

Design notes (grounded in the desktop/tray architecture spec, §3a/§3b/§3c/§3e,
§8):

* **Halt signaling is read from ``report.json``** (``RunReport.halt`` /
  ``HaltObservation``), NEVER from a process exit code. ``replay`` / ``run``
  return 0/1 only; there is no "exit 2 = safe halt". The break emitter parses
  the report, so a wrapping caller (the desktop engine, the cloud runner) reads
  the same source of truth (§8 item 3).
* **Approval freezes the directory to a deterministic ZIP**. The upload sends
  those exact approved bytes, never a post-approval reconstruction.
* **Secrets belong in the OS keychain**. The optional ``hosted`` extra supplies
  ``keyring``; environment injection remains first-class for CI/BYOC. Plaintext
  ``config.toml`` storage requires explicit operator consent.

Token resolution precedence: argument -> ``OPENADAPT_INGEST_TOKEN`` -> OS
keychain -> an existing ``config.toml`` token retained for migration.

Only the existing ``httpx`` dependency (and the stdlib) is used — no new heavy
dependency is introduced.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import idna

__all__ = [
    "DEFAULT_HOST",
    "HostedError",
    "config_path",
    "resolve_host",
    "resolve_token",
    "resolve_deployment_kind",
    "resolve_destination_policy",
    "find_latest_recording",
    "connect",
    "parse_connect_uri",
    "login",
    "push",
    "report_break",
    "report_run",
]

#: The hosted control plane. Overridable per call (``--host``) or via
#: ``~/.openadapt/config.toml`` ``[hosted] host``.
DEFAULT_HOST = "https://app.openadapt.ai"

#: Environment variable read for the ingest token (the non-interactive / CI /
#: BYOC-server path).
TOKEN_ENV = "OPENADAPT_INGEST_TOKEN"
KEYRING_SERVICE = "openadapt-flow"
PAIRING_SECRET_RE = re.compile(r"^oap_[A-Za-z0-9_-]{43}$")

#: Environment variable naming the deployment lane (``cloud`` | ``byoc`` |
#: ``regulated``). Falls back to ``config.toml`` ``[hosted] deployment_lane``.
DEPLOYMENT_KIND_ENV = "OPENADAPT_FLOW_DEPLOYMENT_KIND"

#: Environment variable forcing PHI mode. Raw artifacts still never egress;
#: verified sanitized derivatives may upload to a trusted destination.
PHI_MODE_ENV = "OPENADAPT_FLOW_PHI_MODE"

#: Destination policy is independent of the deployment lane.  A customer-owned
#: endpoint is trusted only when explicitly classified and allowlisted.
DESTINATION_KIND_ENV = "OPENADAPT_FLOW_DESTINATION_KIND"
TRUSTED_HOSTS_ENV = "OPENADAPT_FLOW_TRUSTED_HOSTS"
AUTO_APPROVE_ENV = "OPENADAPT_FLOW_AUTO_APPROVE_SANITIZED"

#: Lanes whose recordings and bundles must NEVER leave the customer
#: machine/tenant through this hosted-ingest path. A customer-owned control
#: plane needs a separately verified destination/trust policy; a lane label is
#: not sufficient evidence that the configured host is inside that boundary.
_DEPLOYMENT_LANES = frozenset({"cloud", "byoc", "regulated"})
_DESTINATION_KINDS = frozenset({"openadapt-managed", "customer-managed", "local"})

#: Network timeouts (seconds). Uploads can be large, so the push timeout is
#: generous; the lightweight validate/report calls use a short timeout.
_UPLOAD_TIMEOUT = 120.0
_API_TIMEOUT = 15.0


class HostedError(RuntimeError):
    """A hosted-connectivity failure (auth, network, or a non-2xx response)."""


@dataclass(frozen=True)
class DestinationPolicy:
    """An authenticated upload destination whose trust was explicitly resolved."""

    kind: str
    host: str
    trusted: bool
    reason: str


# ---------------------------------------------------------------------------
# config.toml (non-secret host/lane config; legacy token migration read)
# ---------------------------------------------------------------------------


def config_path() -> Path:
    """Path to the shared engine config: ``~/.openadapt/config.toml``.

    The root is overridable via ``OPENADAPT_HOME`` so tests (and a sandboxed
    deployment) never touch the real home directory.
    """
    root = os.environ.get("OPENADAPT_HOME")
    base = Path(root) if root else Path.home() / ".openadapt"
    return base / "config.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict (stdlib ``tomllib`` on 3.11+, else a
    minimal ``[table] key = "value"`` fallback for 3.10)."""
    if not path.is_file():
        return {}
    try:
        import tomllib

        with path.open("rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        return _load_toml_minimal(path)


def _load_toml_minimal(path: Path) -> dict[str, Any]:
    """A tiny TOML reader for Python 3.10 (no ``tomllib``).

    Handles exactly what ``config.toml`` needs: ``[table]`` headers and
    ``key = value`` scalar lines (quoted strings, bools, ints, floats). Enough
    for the ``[hosted]`` section; not a general TOML parser.
    """
    data: dict[str, Any] = {}
    table: dict[str, Any] = data
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            table = data.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        table[key.strip()] = _parse_scalar(value.strip())
    return data


def _parse_scalar(raw: str) -> Any:
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a one-level-nested dict (top-level scalars + ``[table]``s of
    scalars) back to TOML. Sufficient for ``config.toml``'s shape."""
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{sub_key} = {_toml_scalar(sub_value)}")
    return "\n".join(lines) + "\n"


def _hosted_config() -> dict[str, Any]:
    """The ``[hosted]`` table from ``config.toml`` (empty when absent)."""
    section = _load_toml(config_path()).get("hosted", {})
    return section if isinstance(section, dict) else {}


def _update_hosted_config(updates: dict[str, Any]) -> Path:
    """Merge ``updates`` into the ``[hosted]`` table and write ``config.toml``
    back with ``0600`` permissions (it may hold the last-resort token)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_toml(path)
    hosted = data.get("hosted")
    if not isinstance(hosted, dict):
        hosted = {}
    hosted.update({k: v for k, v in updates.items() if v is not None})
    data["hosted"] = hosted
    path.write_text(_dump_toml(data))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _remove_hosted_config_key(key: str) -> Path:
    """Remove a migrated secret while preserving non-secret hosted settings."""
    path = config_path()
    data = _load_toml(path)
    section = data.get("hosted")
    if isinstance(section, dict):
        section.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_toml(data))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# host + token resolution
# ---------------------------------------------------------------------------


def resolve_host(host: Optional[str] = None) -> str:
    """Resolve the hosted base URL: ``host`` arg  ->  ``config.toml``  ->
    :data:`DEFAULT_HOST`.

    The returned value is the canonical origin used for both destination-policy
    checks and network requests.  Keeping those values identical prevents a
    token-bearing request from reaching a URL that was only approximately
    equivalent to the one the policy approved.
    """
    resolved = host or _hosted_config().get("host") or DEFAULT_HOST
    return _origin(str(resolved).strip())


def _keyring_token(host: str) -> Optional[str]:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, _origin(host))
    except Exception:  # noqa: BLE001 - unavailable/locked keychain falls through
        return None


def _store_keyring_token(host: str, token: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _origin(host), token)
        return True
    except Exception:  # noqa: BLE001 - caller decides whether plaintext is allowed
        return False


def _delete_keyring_token(host: str) -> bool:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, _origin(host))
        return True
    except Exception:  # noqa: BLE001 - deletion is best-effort cleanup
        return False


def _snapshot_keyring_token(host: str) -> tuple[bool, Optional[str]]:
    """Read the canonical credential without hiding a locked-keychain failure."""
    try:
        import keyring

        return True, keyring.get_password(KEYRING_SERVICE, _origin(host))
    except Exception:  # noqa: BLE001 - pairing must refuse before consuming its code
        return False, None


def _pairing_staging_account(host: str, pairing_id: str) -> str:
    """Return a pairing-specific keyring account, separate from the canonical one."""
    return f"{_origin(host)}#pairing:{UUID(pairing_id)}"


def _store_staged_keyring_token(host: str, pairing_id: str, token: str) -> bool:
    try:
        import keyring

        keyring.set_password(
            KEYRING_SERVICE,
            _pairing_staging_account(host, pairing_id),
            token,
        )
        return True
    except Exception:  # noqa: BLE001 - caller aborts the claimed pairing
        return False


def _delete_staged_keyring_token(host: str, pairing_id: str) -> bool:
    try:
        import keyring

        keyring.delete_password(
            KEYRING_SERVICE,
            _pairing_staging_account(host, pairing_id),
        )
        return True
    except Exception:  # noqa: BLE001 - retained staging remains keychain-protected
        return False


def _keyring_available() -> bool:
    """Return whether a real OS-keychain backend is installed.

    Pairing consumes a one-time server secret, so it refuses before the claim
    when the credential cannot be stored safely. Manual token login remains
    available for environment-injected and explicit plaintext fallback cases.
    """
    try:
        import keyring

        priority = getattr(keyring.get_keyring(), "priority", 0)
        return bool(priority and priority > 0)
    except Exception:  # noqa: BLE001 - unavailable/locked backend
        return False


def resolve_token(token: Optional[str] = None, *, host: Optional[str] = None) -> str:
    """Resolve the ingest token by precedence, or raise :class:`HostedError`.

    Precedence: explicit argument -> environment -> OS keychain -> existing
    plaintext config. Config reading remains for migration, but new plaintext
    storage requires explicit opt-in in :func:`login`.
    """
    resolved_host = resolve_host(host)
    resolved = (
        token
        or os.environ.get(TOKEN_ENV)
        or _keyring_token(resolved_host)
        or _hosted_config().get("token")
    )
    if not resolved:
        raise HostedError(
            "No ingest token. Pass --token, set "
            f"{TOKEN_ENV}, or run `openadapt-flow login` to store one. "
            "Mint a token at <host>/dashboard/settings/ingest."
        )
    return str(resolved)


def resolve_deployment_kind(deployment_kind: Optional[str] = None) -> str:
    """Resolve the deployment lane: arg -> ``OPENADAPT_FLOW_DEPLOYMENT_KIND`` env
    -> ``config.toml`` ``[hosted] deployment_lane`` -> ``"cloud"``.

    The lane describes where execution runs.  It does not, by itself, prove
    where an upload host is located; :func:`resolve_destination_policy` makes
    that independent trust decision."""
    resolved = (
        deployment_kind
        or os.environ.get(DEPLOYMENT_KIND_ENV)
        or _hosted_config().get("deployment_lane")
        or "cloud"
    )
    return str(resolved).strip().lower()


def _origin(host: str) -> str:
    parsed = urlparse(host)
    if not parsed.scheme or not parsed.hostname:
        raise HostedError(f"Upload host is not an absolute URL: {host!r}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise HostedError("Hosted origin must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise HostedError("Hosted origin must not contain URL user information")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise HostedError("Hosted origin must not contain a path, query, or fragment")
    try:
        port_number = parsed.port
    except ValueError as exc:
        raise HostedError("Hosted origin contains an invalid port") from exc

    hostname = parsed.hostname
    if hostname.endswith("."):
        raise HostedError("Hosted origin must not use a trailing-dot hostname")
    try:
        ipv6 = ipaddress.IPv6Address(hostname)
    except ValueError:
        try:
            hostname = (
                idna.encode(hostname, uts46=True, std3_rules=True)
                .decode("ascii")
                .lower()
            )
        except idna.IDNAError as exc:
            raise HostedError("Hosted origin contains an invalid hostname") from exc
        labels = hostname.split(".")
        if len(hostname) > 253 or any(
            not label
            or len(label) > 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        ):
            raise HostedError("Hosted origin contains an invalid hostname")
        authority = hostname
    else:
        authority = f"[{ipv6.compressed}]"

    if port_number is not None and (scheme, port_number) not in {
        ("http", 80),
        ("https", 443),
    }:
        authority = f"{authority}:{port_number}"
    return f"{scheme}://{authority}"


def _trusted_hosts(explicit: Optional[list[str]] = None) -> set[str]:
    configured = _hosted_config().get("trusted_hosts", "")
    raw = explicit or [
        item
        for source in (os.environ.get(TRUSTED_HOSTS_ENV, ""), str(configured))
        for item in source.split(",")
        if item.strip()
    ]
    origins: set[str] = set()
    for item in raw:
        origins.add(_origin(str(item).strip().rstrip("/")))
    return origins


def resolve_destination_policy(
    host: str,
    *,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> DestinationPolicy:
    """Resolve destination trust from endpoint identity, not deployment labels.

    ``app.openadapt.ai`` is the sole implicit OpenAdapt-managed origin.  A
    customer-managed origin must be explicitly classified and appear in the
    exact-origin allowlist.  Local development accepts loopback only.  Unknown,
    cleartext remote, and mismatched destinations are refused.
    """
    origin = _origin(host)
    configured = _hosted_config()
    kind = (
        destination_kind
        or os.environ.get(DESTINATION_KIND_ENV)
        or configured.get("destination_kind")
    )
    if kind is None and origin == _origin(DEFAULT_HOST):
        kind = "openadapt-managed"
    kind = str(kind or "unknown").strip().lower()
    if kind not in _DESTINATION_KINDS:
        raise HostedError(
            f"Destination {origin} has no recognized trust classification. "
            "Set --destination-kind and, for a customer endpoint, add its exact "
            f"origin to --trusted-host (or {TRUSTED_HOSTS_ENV})."
        )
    parsed = urlparse(origin)
    if kind == "openadapt-managed":
        if origin != _origin(DEFAULT_HOST):
            raise HostedError(
                f"Refusing to classify {origin} as OpenAdapt-managed; the "
                f"recognized managed origin is {_origin(DEFAULT_HOST)}."
            )
        return DestinationPolicy(kind, origin, True, "recognized managed origin")
    if kind == "local":
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HostedError("A local destination must resolve to a loopback hostname")
        return DestinationPolicy(kind, origin, True, "loopback development endpoint")
    if parsed.scheme != "https":
        raise HostedError("Customer-managed artifact upload requires HTTPS")
    if origin not in _trusted_hosts(trusted_hosts):
        raise HostedError(
            f"Customer-managed destination {origin} is not in the exact-origin allowlist"
        )
    return DestinationPolicy(kind, origin, True, "explicit customer origin allowlist")


def _auto_approve_enabled(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return explicit
    configured = _hosted_config().get("auto_approve_sanitized", False)
    raw = os.environ.get(AUTO_APPROVE_ENV)
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(configured)


def _phi_mode(phi_mode: Optional[bool] = None) -> bool:
    """Resolve PHI mode: arg -> ``OPENADAPT_FLOW_PHI_MODE`` env -> ``SCRUB=on``.

    PHI mode never permits raw egress. A verified sanitized derivative may
    still upload under the destination trust policy."""
    if phi_mode is not None:
        return phi_mode
    env = os.environ.get(PHI_MODE_ENV)
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    from openadapt_flow import privacy

    return privacy.scrub_mode() == "on"


def _auth_headers(token: str) -> dict[str, str]:
    """The bearer header every outbound call carries (also acceptable to the
    API as ``x-ingest-token``)."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# one-click browser pairing
# ---------------------------------------------------------------------------


def parse_connect_uri(uri: str) -> dict[str, str]:
    """Parse the narrow ``openadapt://connect`` desktop deep-link contract.

    It accepts only the fixed connect action and known scalar fields. No path,
    fragment, duplicate/unknown query field, shell command, or browser URL can
    be smuggled through this protocol.
    """
    parsed = urlparse(str(uri).strip())
    if (
        parsed.scheme != "openadapt"
        or parsed.netloc != "connect"
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HostedError("Invalid OpenAdapt connect link")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    allowed = {"pairing", "host", "destination_kind"}
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise HostedError("Connect link contains unknown or duplicate fields")
    if set(query) < {"pairing", "host"}:
        raise HostedError("Connect link is missing pairing or host")
    destination_kind = query.get("destination_kind", [None])[0]
    if destination_kind not in (None, "openadapt-managed", "local"):
        raise HostedError("Connect link has an unsupported destination kind")
    result = {"pairing": query["pairing"][0], "host": query["host"][0]}
    if destination_kind:
        result["destination_kind"] = destination_kind
    return result


def _abort_pairing(
    host: str,
    pairing_id: str,
    token: str,
) -> str:
    """Best-effort rollback with deliberately narrow, fail-closed semantics."""
    try:
        response = httpx.post(
            f"{host}/api/local-bridge/pairings/abort",
            json={"pairing_id": pairing_id},
            headers=_auth_headers(token),
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return "indeterminate"
    if response.status_code == 200:
        try:
            if response.json().get("revoked") is True:
                return "revoked"
        except (AttributeError, TypeError, ValueError):
            pass
        return "indeterminate"
    if response.status_code == 401:
        return "invalid"
    if response.status_code == 409:
        return "conflict"
    return "indeterminate"


def _rollback_pairing(
    *,
    host: str,
    pairing_id: str,
    token: str,
    prior_token: Optional[str],
    staged: bool,
    canonical_may_be_new: bool,
) -> str:
    """Abort a failed claim and restore canonical state only when revocation is proven."""
    abort_result = _abort_pairing(host, pairing_id, token)
    if abort_result != "revoked":
        if not staged:
            return (
                "The previous connection was preserved, but Cloud rollback could "
                "not be proven and no recovery credential could be retained. "
                "Inspect Cloud settings before retrying."
            )
        if canonical_may_be_new:
            return (
                "Cloud rollback could not be proven; the new credential and its "
                "keychain recovery copy were retained. Retry confirmation or inspect "
                "the connection in Cloud settings before changing credentials."
            )
        return (
            "The previous connection was preserved. Cloud rollback could not be "
            "proven, so the keychain recovery copy was retained; inspect Cloud "
            "settings before retrying."
        )

    restored = True
    if canonical_may_be_new:
        if prior_token is None:
            restored = _delete_keyring_token(host)
        else:
            restored = _store_keyring_token(host, prior_token)
    if not restored:
        return (
            "Cloud revoked the new credential, but the previous canonical "
            "credential could not be restored. The keychain recovery copy was "
            "retained; repair the keychain before retrying."
        )

    staging_deleted = not staged or _delete_staged_keyring_token(host, pairing_id)
    if not staging_deleted:
        return (
            "Cloud revoked the new credential and the previous connection was "
            "restored, but keychain staging cleanup is still pending."
        )
    return "Cloud revoked the new credential and preserved the previous connection."


def _confirm_pairing(
    *,
    host: str,
    pairing_id: str,
    token: str,
) -> tuple[bool, str]:
    """Confirm once, retrying the exact idempotent request after ambiguity."""
    confirmation_url = f"{host}/api/local-bridge/pairings/confirm"
    last_detail = "transport failure"
    for _attempt in range(2):
        try:
            confirmation = httpx.post(
                confirmation_url,
                json={"pairing_id": pairing_id},
                headers=_auth_headers(token),
                timeout=_API_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            last_detail = "transport failure"
            continue
        try:
            confirmed = confirmation.json().get("connected") is True
        except (AttributeError, TypeError, ValueError):
            confirmed = False
        if 200 <= confirmation.status_code < 300 and confirmed:
            return True, "confirmed"
        last_detail = f"status {confirmation.status_code}"
        if confirmation.status_code < 500:
            break
    return False, last_detail


def connect(
    pairing_secret: str,
    host: Optional[str] = None,
    *,
    device_name: Optional[str] = None,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Claim a five-minute browser pairing without clobbering a working token.

    The one-time secret authorizes only ``POST /api/local-bridge/pairings/claim``.
    The previous canonical token is snapshotted before the claim. The returned
    token is staged under a pairing-specific keyring account, validated from
    memory, promoted to canonical, and then confirmed. Rollback restores the
    prior token only after Cloud proves the unconfirmed token was revoked.
    """
    secret = str(pairing_secret).strip()
    if not PAIRING_SECRET_RE.fullmatch(secret):
        raise HostedError("Pairing code is malformed")
    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    if not _keyring_available():
        raise HostedError(
            "Secure pairing needs an OS keychain, but none is available. Install "
            "'openadapt-flow[hosted]' and unlock the keychain, or use the manual "
            "`openadapt-flow login` token flow."
        )
    snapshot_ok, prior_token = _snapshot_keyring_token(resolved_host)
    if not snapshot_ok:
        raise HostedError(
            "Secure pairing could not read the current OS-keychain credential. "
            "Unlock or repair the keychain before retrying."
        )

    clean_device = re.sub(r"[\x00-\x1f\x7f]", "", device_name or socket.gethostname())
    clean_device = clean_device.strip()[:80] or "this computer"
    claim_url = f"{resolved_host}/api/local-bridge/pairings/claim"
    try:
        response = httpx.post(
            claim_url,
            json={"pairing_secret": secret, "device_name": clean_device},
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HostedError(f"Could not reach {resolved_host}: {exc}") from exc
    if response.status_code == 410:
        raise HostedError("Pairing code expired, was cancelled, or was already used")
    if not 200 <= response.status_code < 300:
        raise HostedError(
            f"Pairing failed ({response.status_code}) against {claim_url}"
        )
    try:
        body = response.json()
        token = str(body["ingest_token"])
        pairing_id = str(UUID(str(body["pairing_id"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise HostedError(
            "Pairing response did not contain a credential and pairing id"
        ) from exc
    if not re.fullmatch(r"oai_ingest_[A-Za-z0-9_-]{32,}", token):
        raise HostedError("Pairing response contained a malformed credential")

    if not _store_staged_keyring_token(resolved_host, pairing_id, token):
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=False,
            canonical_may_be_new=False,
        )
        raise HostedError(
            "The pairing was claimed, but the OS keychain refused its protected "
            f"staging copy. No plaintext copy was written. {rollback}"
        )

    # A second authenticated request prevents a successful claim response from
    # being mistaken for a usable connection.
    validation_url = f"{resolved_host}/api/needs-attention/count"
    try:
        validation = httpx.get(
            validation_url,
            headers=_auth_headers(token),
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=True,
            canonical_may_be_new=False,
        )
        raise HostedError(
            "The paired credential could not be validated because Cloud was "
            f"unreachable. {rollback}"
        ) from exc
    if validation.status_code == 401:
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=True,
            canonical_may_be_new=False,
        )
        raise HostedError(f"Cloud rejected the paired credential. {rollback}")
    if not 200 <= validation.status_code < 300:
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=True,
            canonical_may_be_new=False,
        )
        raise HostedError(
            "Cloud could not validate the paired credential "
            f"(status {validation.status_code}). {rollback}"
        )

    if not _store_keyring_token(resolved_host, token):
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=True,
            canonical_may_be_new=True,
        )
        raise HostedError(
            "The paired credential validated, but the OS keychain refused to "
            f"promote it to the canonical account. {rollback}"
        )

    confirmed, confirmation_detail = _confirm_pairing(
        host=resolved_host,
        pairing_id=pairing_id,
        token=token,
    )
    if not confirmed:
        rollback = _rollback_pairing(
            host=resolved_host,
            pairing_id=pairing_id,
            token=token,
            prior_token=prior_token,
            staged=True,
            canonical_may_be_new=True,
        )
        raise HostedError(
            "Cloud did not definitively confirm the new connection "
            f"({confirmation_detail}). {rollback}"
        )

    _delete_staged_keyring_token(resolved_host, pairing_id)
    _update_hosted_config({"host": resolved_host})
    _remove_hosted_config_key("token")
    return {
        "host": resolved_host,
        "paired": True,
        "token_storage": "keyring",
        "device_name": clean_device,
        "settings_url": f"{resolved_host}/dashboard/settings/ingest",
    }


# ---------------------------------------------------------------------------
# recording / bundle discovery + zipping
# ---------------------------------------------------------------------------


def _is_recording_dir(path: Path) -> bool:
    return (path / "meta.json").is_file() and (path / "events.jsonl").is_file()


def _is_bundle_dir(path: Path) -> bool:
    return (path / "workflow.json").is_file() or (path / "workflow.json.enc").is_file()


def find_latest_recording(base: Optional[Path] = None) -> Path:
    """Return the most-recently-modified recording directory.

    Searches the immediate children of a few conventional roots (``base`` or
    the CWD, plus ``recordings/`` and ``runs/`` under it). A recording dir has
    ``meta.json`` + ``events.jsonl``. Raises :class:`HostedError` when none is
    found — the caller must then pass an explicit PATH.
    """
    root = Path(base) if base else Path.cwd()
    roots = [root, root / "recordings", root / "runs"]
    candidates: list[Path] = []
    for r in roots:
        if not r.is_dir():
            continue
        if _is_recording_dir(r):
            candidates.append(r)
        for child in r.iterdir():
            if child.is_dir() and _is_recording_dir(child):
                candidates.append(child)
    if not candidates:
        raise HostedError(
            f"No recording directory found under {root} "
            "(looked for meta.json + events.jsonl). Pass an explicit PATH."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _zip_dir(src: Path) -> Path:
    """Zip a directory's CONTENTS (files at the archive root) to a temp
    ``.zip`` and return its path. The caller is responsible for deleting it."""
    if not src.is_dir():
        raise HostedError(f"Not a directory: {src}")
    tmp = tempfile.mkdtemp(prefix="openadapt-flow-push-")
    base = Path(tmp) / src.name
    archive = shutil.make_archive(str(base), "zip", root_dir=str(src))
    return Path(archive)


def _assert_upload_tree_safe(src: Path) -> None:
    """Refuse filesystem indirection that could escape an upload directory."""
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise HostedError(
                f"Refusing to upload {src}: symlink {path.relative_to(src)} "
                "could reference data outside the reviewed artifact tree."
            )


#: Recording artifacts scrubbed before a cloud upload. Frames are image-redacted;
#: structured/text artifacts are run through the text scrubber (whole-file — a
#: best-effort de-identification that preserves surrounding JSON syntax).
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_TEXT_SUFFIXES = frozenset(
    {".json", ".jsonl", ".txt", ".md", ".csv", ".yaml", ".yml", ".toml"}
)


def _scrub_recording_tree(src: Path) -> Path:
    """Copy a recording directory to a temp location with every frame + text
    artifact PHI-scrubbed, and return the scrubbed COPY (the original is never
    mutated). The caller deletes the returned dir's parent.

    Fail-closed: raises :class:`HostedError` if no scrubbing provider is
    available (the caller must first check
    :func:`openadapt_flow.privacy.scrubbing_available`) or if any single frame
    cannot be redacted — an unredactable frame must abort the upload, never ship
    raw.
    """
    from openadapt_flow import privacy

    scrubber = privacy.get_scrubber()
    if scrubber is None:  # defensive; the caller gates on scrubbing_available()
        raise HostedError(
            "Refusing to upload a recording: no PHI scrubber is available. "
            "Install it with: pip install 'openadapt-flow[privacy]'."
        )
    tmp = Path(tempfile.mkdtemp(prefix="openadapt-flow-scrub-"))
    dest = tmp / src.name
    shutil.copytree(src, dest)
    try:
        for path in sorted(dest.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in _IMAGE_SUFFIXES:
                path.write_bytes(
                    privacy.scrub_image_bytes(path.read_bytes(), force=True)
                )
            elif suffix in _TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(scrubber.scrub_text(text), encoding="utf-8")
            else:
                raise HostedError(
                    "unsupported artifact cannot be PHI-scrubbed safely: "
                    f"{path.relative_to(dest)}. Database, video, audio, archive, "
                    "and unknown binary files must remain local."
                )
    except Exception as exc:  # noqa: BLE001 — fail closed: never ship raw PHI
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, HostedError):
            raise
        raise HostedError(
            f"Refusing to upload: PHI scrub of the recording failed ({exc}). "
            "The recording was NOT uploaded."
        ) from exc
    return dest


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def login(
    token: Optional[str] = None,
    host: Optional[str] = None,
    *,
    save: bool = True,
    allow_plaintext_token: bool = False,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Validate an ingest token against the hosted API and (optionally) record
    the host in ``~/.openadapt/config.toml`` and token in the OS keychain.

    Args:
        token: The ingest token (``oai_ingest_…``). Falls back to
            ``OPENADAPT_INGEST_TOKEN`` env, the OS keychain, then an existing
            plaintext ``config.toml`` token retained only for migration.
        host: Hosted base URL (default: ``config.toml`` host, else
            :data:`DEFAULT_HOST`).
        save: When True (default), persist the host and store the token in the
            OS keychain. Plaintext token fallback requires
            ``allow_plaintext_token=True``.

    Returns:
        ``{"host": <str>, "valid": True, "settings_url": <str>,
           "config_path": <str|None>}``.

    Raises:
        HostedError: the token is missing, invalid (401), or the API is
            unreachable.
    """
    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    resolved_token = resolve_token(token, host=resolved_host)
    # Validate with a cheap authenticated GET (resolves the token -> org).
    url = f"{resolved_host}/api/needs-attention/count"
    try:
        resp = httpx.get(
            url,
            headers=_auth_headers(resolved_token),
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HostedError(f"Could not reach {resolved_host}: {exc}") from exc
    if resp.status_code == 401:
        raise HostedError(
            "Ingest token was rejected (401). Mint a fresh token at "
            f"{resolved_host}/dashboard/settings/ingest."
        )
    if resp.status_code >= 400:
        raise HostedError(
            f"Token validation failed ({resp.status_code}) against {url}."
        )
    saved_to: Optional[str] = None
    storage: Optional[str] = None
    if save:
        if _store_keyring_token(resolved_host, resolved_token):
            _update_hosted_config({"host": resolved_host})
            _remove_hosted_config_key("token")
            saved_to = f"OS keychain ({KEYRING_SERVICE}/{_origin(resolved_host)})"
            storage = "keyring"
        elif allow_plaintext_token:
            path = _update_hosted_config(
                {"host": resolved_host, "token": resolved_token}
            )
            saved_to = str(path)
            storage = "plaintext-config"
        else:
            _update_hosted_config({"host": resolved_host})
            raise HostedError(
                "Token validated but was not stored: no usable OS keychain is "
                "available. Install 'openadapt-flow[hosted]', set the token via "
                f"{TOKEN_ENV}, use --no-save, or explicitly accept the mode-0600 "
                "plaintext fallback with --allow-plaintext-token."
            )
    return {
        "host": resolved_host,
        "valid": True,
        "settings_url": f"{resolved_host}/dashboard/settings/ingest",
        "config_path": saved_to,
        "token_storage": storage,
    }


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def push(
    path: Optional[Any] = None,
    *,
    kind: str = "recording",
    name: Optional[str] = None,
    workflow_id: Optional[str] = None,
    resolves_run_id: Optional[str] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
    deployment_kind: Optional[str] = None,
    phi_mode: Optional[bool] = None,
    attest_non_phi: bool = False,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
    sanitized_out: Optional[Any] = None,
    auto_approve: Optional[bool] = None,
    validation_attestation: Optional[Any] = None,
) -> dict[str, Any]:
    """Sanitize, approve, and upload an exact immutable artifact archive.

    Deployment lane and destination trust are separate.  ``byoc`` and
    ``regulated`` artifacts may upload after complete sanitization to either the
    recognized OpenAdapt-managed origin or an explicitly allowlisted
    customer-managed origin.  A raw artifact is never uploaded.  Human review
    is required by default; an administrator can enable policy approval only
    after every file type is covered and the second scrub pass is stable.

    A first call with a raw path normally returns ``pending_review`` and the
    durable derivative path.  Review and approve that derivative, then call
    ``push`` on the derivative.  Approval freezes a deterministic archive;
    upload sends those exact approved bytes and a
    ``openadapt.sanitization/v1`` manifest.
    """
    if kind not in ("recording", "bundle"):
        raise HostedError(f"--kind must be 'recording' or 'bundle', got {kind!r}")
    requested_kind = kind
    normalized_workflow_id: Optional[str] = None
    if workflow_id is not None:
        if requested_kind != "bundle":
            raise HostedError("--workflow-id is only valid with --kind bundle")
        try:
            normalized_workflow_id = str(UUID(str(workflow_id).strip()))
        except (ValueError, AttributeError) as exc:
            raise HostedError("--workflow-id must be a valid UUID") from exc
    normalized_resolves_run_id: Optional[str] = None
    if resolves_run_id is not None:
        if requested_kind != "bundle" or normalized_workflow_id is None:
            raise HostedError(
                "--resolves-run-id requires --kind bundle and --workflow-id"
            )
        try:
            normalized_resolves_run_id = str(UUID(str(resolves_run_id).strip()))
        except (ValueError, AttributeError) as exc:
            raise HostedError("--resolves-run-id must be a valid UUID") from exc
    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token, host=resolved_host)
    lane = resolve_deployment_kind(deployment_kind)
    if lane not in _DEPLOYMENT_LANES:
        raise HostedError(
            f"Unknown deployment lane {lane!r}; expected one of "
            f"{sorted(_DEPLOYMENT_LANES)}. Refusing to choose an egress policy."
        )
    destination = resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    if attest_non_phi:
        raise HostedError(
            "--attest-non-phi no longer bypasses sanitization. A declaration is "
            "not a verified derivative; run `sanitize`, review, and approve it."
        )

    src = Path(path) if path is not None else find_latest_recording()
    if not src.is_dir():
        raise HostedError(f"PATH is not a directory: {src}")
    _assert_upload_tree_safe(src)

    from openadapt_flow.sanitized_artifact import (
        MANIFEST_NAME,
        SanitizationError,
        approve_derivative,
        approved_archive_path,
        build_ingest_manifest,
        load_and_verify_derivative,
        load_valid_approval,
        sanitize_artifact,
        source_tree_sha256,
    )

    is_derivative = (src / MANIFEST_NAME).is_file()
    if not is_derivative:
        actual_kind = (
            "bundle" if kind == "bundle" and _is_bundle_dir(src) else "recording"
        )
        if kind == "bundle" and actual_kind != "bundle":
            raise HostedError(
                f"{src.name!r} is not a compiled bundle (no workflow.json/.enc). "
                "Pass --kind recording instead."
            )
        if sanitized_out is not None:
            derivative = Path(sanitized_out)
        else:
            root = config_path().parent / "sanitized"
            stamp = source_tree_sha256(src)[:12]
            derivative = root / f"artifact-{stamp}"
        try:
            if not derivative.exists():
                sanitize_artifact(src, derivative, kind=actual_kind)
            else:
                existing = load_and_verify_derivative(derivative)
                if existing.get("source_tree_sha256") != source_tree_sha256(src):
                    raise SanitizationError(
                        "Existing derivative does not match the current source tree"
                    )
            if _auto_approve_enabled(auto_approve):
                approve_derivative(
                    derivative,
                    source=src,
                    reviewer="policy:complete-type-coverage",
                    automatic=True,
                )
            else:
                return {
                    "uploaded": False,
                    "pending_review": True,
                    "sanitized_path": str(derivative),
                    "review_command": (
                        f"openadapt-flow review-sanitized {derivative} --original {src}"
                    ),
                    "destination_kind": destination.kind,
                    "deployment_kind": lane,
                    "phi_mode": _phi_mode(phi_mode),
                }
        except SanitizationError as exc:
            raise HostedError(f"Artifact sanitization failed: {exc}") from exc
        src = derivative

    try:
        local_manifest = load_and_verify_derivative(src)
        approval = load_valid_approval(src)
        ingest_manifest = build_ingest_manifest(src)
    except SanitizationError as exc:
        raise HostedError(f"Sanitized artifact is not uploadable: {exc}") from exc
    kind = str(local_manifest["kind"])
    if kind != requested_kind:
        raise HostedError(
            f"--kind {requested_kind!r} does not match the reviewed derivative's "
            f"manifest kind {kind!r}"
        )
    if int(ingest_manifest["findings"]["unresolved"]) != 0:
        raise HostedError("Sanitized artifact has unresolved findings")

    archive_path = approved_archive_path(src)
    data: dict[str, str] = {"kind": kind}
    if normalized_workflow_id is not None:
        data["workflow_id"] = normalized_workflow_id
    if normalized_resolves_run_id is not None:
        data["resolves_run_id"] = normalized_resolves_run_id
    if kind == "bundle":
        if validation_attestation is None:
            raise HostedError(
                "A runnable bundle needs a runtime-validation attestation. Run "
                "`openadapt-flow validate-hosted ...`, then pass its JSON with "
                "--validation-attestation. Privacy approval alone is not runtime "
                "validation."
            )
        from openadapt_flow.runtime_validation import (
            RuntimeValidationError,
            load_runtime_validation_attestation,
            verify_runtime_validation_attestation,
        )

        try:
            attestation = (
                validation_attestation
                if isinstance(validation_attestation, dict)
                else load_runtime_validation_attestation(Path(validation_attestation))
            )
            verify_runtime_validation_attestation(
                attestation,
                bundle_sha256=approval["approved_derivative_sha256"],
                token=resolved_token,
            )
        except RuntimeValidationError as exc:
            raise HostedError(f"Runtime validation is not uploadable: {exc}") from exc
        data["validation_attestation"] = json.dumps(
            attestation, sort_keys=True, separators=(",", ":")
        )
    if name:
        from openadapt_flow import privacy

        scrubber = privacy.get_scrubber()
        if scrubber is None:
            raise HostedError("Cannot sanitize the outbound workflow name")
        safe_name = scrubber.scrub_text(name)
        if scrubber.scrub_text(safe_name) != safe_name:
            raise HostedError("Workflow name still changes on a second scrub pass")
        data["name"] = safe_name
    data["sanitization_manifest"] = json.dumps(
        ingest_manifest, sort_keys=True, separators=(",", ":")
    )
    try:
        with archive_path.open("rb") as fh:
            resp = httpx.post(
                f"{resolved_host}/api/ingest",
                headers=_auth_headers(resolved_token),
                files={
                    "file": (
                        f"openadapt-sanitized-{ingest_manifest['artifact']['sha256'][:12]}.zip",
                        fh,
                        "application/zip",
                    )
                },
                data=data,
                timeout=_UPLOAD_TIMEOUT,
                follow_redirects=False,
            )
    except httpx.HTTPError as exc:
        raise HostedError(
            f"Upload to {resolved_host}/api/ingest failed: {exc}"
        ) from exc
    if resp.status_code == 401:
        raise HostedError("Ingest token was rejected (401).")
    if resp.status_code != 201:
        raise HostedError(
            f"Ingest returned {resp.status_code} (expected 201): {_body_snippet(resp)}"
        )
    payload = resp.json()
    ingest = payload.get("ingest", payload) if isinstance(payload, dict) else {}
    workflow_id = ingest.get("workflow_id")
    result = dict(ingest)
    result["uploaded"] = True
    result["sanitization"] = ingest_manifest
    result["approval"] = approval
    result["destination_kind"] = destination.kind
    if workflow_id:
        result["dashboard_url"] = f"{resolved_host}/dashboard/workflows/{workflow_id}"
    return result


# ---------------------------------------------------------------------------
# break report
# ---------------------------------------------------------------------------


def _body_snippet(resp: httpx.Response, limit: int = 300) -> str:
    try:
        return resp.text[:limit]
    except Exception:  # noqa: BLE001 — diagnostics only, never raise here
        return "<unreadable body>"


def _last_failed_rung(report: Any) -> Optional[str]:
    """The resolution rung of the last non-ok step, if any (diagnostic)."""
    for step in reversed(report.results):
        if not step.ok and step.resolution is not None:
            return step.resolution.rung
    return None


def _build_break_payload(
    report: Any,
    *,
    workflow_id: str,
    client_run_id: str,
) -> dict[str, Any]:
    """Assemble the schema-minimal ``break-summary/v2`` payload.

    Screenshots, DOM, field values, report bodies, and all free-text
    intent/reason/error fields are excluded. The stable run UUID links a later
    resumed success without exposing or hashing report contents.
    """
    halt = report.halt
    status = "halt" if halt is not None else "failed"
    resolver_rung = _last_failed_rung(report)
    if resolver_rung is not None and resolver_rung not in _RUN_SUMMARY_RUNGS:
        raise HostedError("report contains an unknown resolution rung")

    payload: dict[str, Any] = {
        "kind": "break_summary",
        "schema": BREAK_SUMMARY_SCHEMA,
        "workflow_id": _optional_uuid(workflow_id, field="workflow_id"),
        "bundle_content_digest": _hex64(
            report.bundle_content_digest,
            field="bundle_content_digest",
        ),
        "status": status,
        "client_run_id": _optional_uuid(client_run_id, field="client_run_id"),
        "started_at": _run_summary_timestamp(report.started_at),
        "metrics": {
            "steps": _bounded_int(len(report.results), field="metrics.steps"),
            "duration_s": round(
                _bounded_float(
                    report.total_ms / 1000.0,
                    field="metrics.duration_s",
                    maximum=_MAX_DURATION_S,
                ),
                3,
            ),
        },
        "phi_minimal": True,
    }
    if resolver_rung is not None:
        payload["resolver_rung"] = resolver_rung
    return payload


def report_break(
    run_dir: Any,
    *,
    workflow_id: str,
    host: Optional[str] = None,
    token: Optional[str] = None,
    deployment_kind: str = "cloud",
    org_id: Optional[str] = None,
    allow_local_fallback: bool = True,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Emit a PHI-free break diagnostic for a halted run to
    ``POST /api/runs/ingest-report`` (§3c).

    Reads ``run_dir/report.json`` (:class:`~openadapt_flow.ir.RunReport`) and,
    when it carries a halt (or failed), posts a schema-minimal descriptor so the break
    is triageable centrally. The recording NEVER leaves the machine — only the
    diagnostic (§8 item 3: halt is read from ``report.json``, not an exit code).

    Args:
        run_dir: The run directory holding ``report.json``.
        workflow_id: The hosted workflow id this run belongs to (returned by
            :func:`push` / the dashboard).
        host: Hosted base URL (default resolution).
        token: Ingest token (default resolution).
        deployment_kind: Backward-compatible caller hint; v2 provenance is
            derived by the server and this value never enters the payload.
        org_id: Backward-compatible caller hint; token ownership is canonical
            server-side and this value never enters the payload.
        allow_local_fallback: When the server rejects the payload as a PHI
            boundary violation (422) even after a harder scrub — or the
            scrubber is unavailable under a fail-closed policy — return a
            ``local_only`` result instead of raising.

    Returns:
        On success: the server response (``run_id`` / ``halt_id`` / ``status`` /
        ``teach_url``) plus ``{"emitted": True}``. When there is nothing to
        report (a successful run): ``{"emitted": False, "reason": …}``. On a
        PHI-boundary fallback: ``{"emitted": False, "local_only": True, …}``.

    Raises:
        HostedError: missing report, auth failure, or a non-2xx/422 response
            (422 is handled by local fallback).
    """
    from openadapt_flow.ir import RunReport

    run_path = Path(run_dir)
    report_path = run_path / "report.json"
    if not report_path.is_file():
        raise HostedError(f"No report.json in {run_path} — nothing to report.")
    try:
        report_size = report_path.stat().st_size
    except OSError as exc:
        raise HostedError("Could not inspect report.json") from exc
    if report_size > _MAX_RUN_REPORT_BYTES:
        raise HostedError(
            f"report.json exceeds the {_MAX_RUN_REPORT_BYTES}-byte reporting limit"
        )
    try:
        report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostedError("report.json is unreadable or invalid") from exc

    if report.halt is None and report.success:
        return {"emitted": False, "reason": "run succeeded; no halt to report"}

    # Launch contract: auto-upload only the schema-minimal descriptor. Even
    # scrubbed free text needs a separately reviewed, hash-bound sanitization
    # artifact before the control plane can trust it.
    payload = _build_break_payload(
        report,
        workflow_id=workflow_id,
        client_run_id=_client_run_id(run_path),
    )
    if (
        len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        > _MAX_RUN_SUMMARY_BYTES
    ):
        raise HostedError("break summary exceeds the outbound size limit")

    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    resolved_token = resolve_token(token, host=resolved_host)
    url = f"{resolved_host}/api/runs/ingest-report"

    resp = _post_report(url, resolved_token, payload)

    if resp.status_code == 422:
        if allow_local_fallback:
            return {
                "emitted": False,
                "local_only": True,
                "reason": "server rejected payload as PHI boundary (422)",
                "detail": _body_snippet(resp),
            }
        raise HostedError(
            f"ingest-report rejected as PHI boundary (422): {_body_snippet(resp)}"
        )

    if resp.status_code == 401:
        raise HostedError("Ingest token was rejected (401).")
    if resp.status_code not in (200, 202):
        raise HostedError(
            f"ingest-report returned {resp.status_code} (expected 202): "
            f"{_body_snippet(resp)}"
        )
    result = _parse_report_response(
        resp,
        expected_status=payload["status"],
        require_break_fields=True,
    )
    teach_url = result.get("teach_url")
    if teach_url and str(teach_url).startswith("/"):
        result["teach_url"] = f"{resolved_host}{teach_url}"
    result["emitted"] = True
    return result


def _post_report(url: str, token: str, payload: dict[str, Any]) -> httpx.Response:
    """POST a JSON break payload; wrap transport errors as :class:`HostedError`."""
    try:
        return httpx.post(
            url,
            headers=_auth_headers(token),
            json=payload,
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HostedError(f"POST {url} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# run summary report (SUCCESS rail — the metering/visibility counterpart of
# report_break; same endpoint, same paired ingest credential, same PHI
# boundary, opposite outcome)
# ---------------------------------------------------------------------------

#: Wire-schema tag for the success payload; version it if the shape changes.
RUN_SUMMARY_SCHEMA = "run-summary/v2"
BREAK_SUMMARY_SCHEMA = "break-summary/v2"

#: CLOSED substrate enum (red-team PHI-1): the payload's ``backend`` field may
#: only carry one of these fixed tokens, never a free-text hint. In particular
#: recorded window titles / ``backend_hints`` (meta.json) are PHI-bearing
#: (PMS titles embed patient names) and are NEVER read by this rail.
_BACKEND_ENUM = frozenset({"web", "windows", "macos", "linux", "rdp", "citrix"})
_RUN_SUMMARY_RUNGS = frozenset(
    {"structural", "template", "template_global", "ocr", "geometry", "grounder", "api"}
)
_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_FLOW_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:[-+][0-9A-Za-z.-]{1,32})?$"
)
_MAX_COUNTER = 1_000_000
_MAX_DURATION_S = 365 * 24 * 60 * 60
_MAX_MODEL_COST_USD = 1_000_000.0
_MAX_RUN_REPORT_BYTES = 8 * 1024 * 1024
_MAX_RUN_SUMMARY_BYTES = 16 * 1024

#: Sidecar file persisting this run's client-generated report UUID, so
#: re-reporting the SAME run is idempotent while DISTINCT runs never collide.
_RUN_REPORT_ID_FILE = ".report_run_id"


def _client_run_id(run_path: Path) -> str:
    """The stable, client-generated UUID for this run directory.

    The UUID is created with an exclusive, no-overwrite file operation beside
    ``report.json``. Existing symlinks/non-files and persistence failures are
    refused: silently minting a fresh id would turn a retry into a duplicate
    Cloud run, while following a symlink could overwrite an unrelated file.
    """
    import uuid

    id_path = run_path / _RUN_REPORT_ID_FILE

    def read_existing(metadata: os.stat_result) -> str:
        """Read one exact existing entry, allowing its creator to finish.

        ``O_EXCL`` publishes the directory entry before the winning writer can
        populate it.  A losing reporter therefore pins that entry's descriptor
        and retries only an empty/partial UUID prefix for a bounded interval.
        Any path replacement, symlink, non-regular entry, or complete malformed
        value remains an immediate refusal.
        """
        deadline = time.monotonic() + 1.0
        last_error: Optional[Exception] = None
        if not stat.S_ISREG(metadata.st_mode):
            raise HostedError("run report id must be a regular file (symlinks refused)")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        # Windows has no portable O_NOFOLLOW. On every platform, bind the
        # opened descriptor back to this exact directory entry before reading
        # any bytes; O_NOFOLLOW is an added symlink refusal where available.
        try:
            fd = os.open(id_path, flags)
        except FileNotFoundError as exc:
            raise HostedError(
                "run report id changed while it was being opened"
            ) from exc
        except OSError as exc:
            raise HostedError("run report id is unreadable or invalid") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise HostedError(
                    "run report id must be a regular file (symlinks refused)"
                )
            if not os.path.samestat(metadata, opened):
                raise HostedError("run report id changed while it was being opened")

            def require_same_entry(*, action: str) -> os.stat_result:
                try:
                    current = id_path.lstat()
                except FileNotFoundError as exc:
                    raise HostedError(f"run report id changed while {action}") from exc
                if not stat.S_ISREG(current.st_mode):
                    raise HostedError(
                        "run report id must be a regular file (symlinks refused)"
                    )
                if not os.path.samestat(opened, current):
                    raise HostedError(f"run report id changed while {action}")
                return current

            def is_canonical_prefix(raw: bytes) -> bool:
                """Whether ``raw`` can be a prefix of our canonical UUID."""
                uuid_part = raw[:36]
                hex_bytes = b"0123456789abcdef"
                dash_positions = {8, 13, 18, 23}
                if not all(
                    byte == ord("-") if index in dash_positions else byte in hex_bytes
                    for index, byte in enumerate(uuid_part)
                ):
                    return False
                # Text-mode writes use LF on POSIX and CRLF on Windows. A
                # trailing CR is the only valid byte beyond the UUID that can
                # still be an in-progress serialization.
                return len(raw) <= 36 or raw == uuid_part + b"\r"

            while True:
                require_same_entry(action="waiting for its writer")

                os.lseek(fd, 0, os.SEEK_SET)
                raw_bytes = os.read(fd, 128)
                # Bind the bytes back to the same live directory entry after
                # the read as well as before it. Otherwise a replacement
                # during os.read could return UUID A while the path persists B.
                require_same_entry(action="it was being read")
                size_after_read = os.fstat(fd).st_size
                if len(raw_bytes) == 128 or size_after_read >= 128:
                    raise HostedError("run report id is unreadable or invalid")
                try:
                    raw = raw_bytes.decode("utf-8")
                    parsed = str(UUID(raw.strip()))
                    if size_after_read != len(raw_bytes):
                        raise ValueError("run report id changed during read")
                    if raw not in {parsed, f"{parsed}\n", f"{parsed}\r\n"}:
                        raise ValueError("run report id is not canonical")
                    return parsed
                except (UnicodeError, ValueError) as exc:
                    last_error = exc
                    # A UUID written by this function is 36 ASCII bytes plus a
                    # newline. Only a shorter prefix can be an in-progress
                    # write; a complete malformed value is never retried.
                    if not is_canonical_prefix(raw_bytes):
                        raise HostedError(
                            "run report id is unreadable or invalid"
                        ) from exc
                if time.monotonic() >= deadline:
                    raise HostedError(
                        "run report id is unreadable or invalid"
                    ) from last_error
                time.sleep(0.005)
        except OSError as exc:
            raise HostedError("run report id is unreadable or invalid") from exc
        finally:
            os.close(fd)

    try:
        existing = id_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HostedError("run report id is unreadable or invalid") from exc
    else:
        return read_existing(existing)

    fresh = str(uuid.uuid4())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(id_path, flags, 0o600)
    except FileExistsError:
        try:
            existing = id_path.lstat()
        except FileNotFoundError as exc:
            raise HostedError(
                "run report id changed while another writer was creating it"
            ) from exc
        except OSError as exc:
            raise HostedError("run report id is unreadable or invalid") from exc
        return read_existing(existing)
    except OSError as exc:
        raise HostedError("could not persist the run report id") from exc
    try:
        created = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise HostedError("could not persist the run report id") from exc

    unowned_fd: Optional[int] = fd
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        unowned_fd = None  # ownership transferred to the file object
        with handle:
            handle.write(fresh + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            current = id_path.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise HostedError(
                    "run report id must be a regular file (symlinks refused)"
                )
            if not os.path.samestat(created, current):
                raise HostedError("run report id changed while it was being written")
        # POSIX requires the containing directory to be flushed as well before
        # the newly created name is durable across a crash. Windows does not
        # expose a portable directory fsync through Python; FlushFileBuffers on
        # the file handle above is the strongest supported local primitive.
        if os.name != "nt":
            directory_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                directory_flags |= os.O_DIRECTORY
            directory_fd = os.open(run_path, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except HostedError:
        raise
    except OSError as exc:
        # There is no portable conditional-unlink primitive. Leave this exact
        # failed-create entry behind so subsequent reports fail closed instead
        # of risking deletion of a path replacement between lstat and unlink.
        raise HostedError("could not persist the run report id") from exc
    finally:
        if unowned_fd is not None:
            os.close(unowned_fd)
    return fresh


def _identity_summary(report: Any) -> dict[str, int]:
    """Categorical identity-verdict COUNTS only.

    ``IdentityCheck.expected`` / ``observed`` hold live band text (names,
    DOBs — PHI by construction) and are deliberately never read here.
    """
    counts = {"verified": 0, "mismatch": 0, "abstain": 0, "unreadable": 0}
    for step in report.results:
        if step.identity is not None and step.identity.status in counts:
            counts[step.identity.status] += 1
    counts["applicable"] = int(report.identity_applicable_steps)
    counts["armed"] = int(report.identity_armed_steps)
    return counts


def _effect_summary(report: Any) -> dict[str, int]:
    """Return categorical effect-verdict counts only.

    ``StepResult.effect_contract_hashes`` intentionally remain local. They bind
    resolved record/value/idempotency fields for local authorization and audit,
    so even a SHA-256 representation can be dictionary-recoverable when an
    identifier has a small candidate space. Cloud needs coverage counts, not
    those per-record fingerprints.
    """
    verified = 0
    approved_unverified = 0
    contract_count = 0
    for step in report.results:
        if step.effect_verified is True:
            verified += 1
        if step.effect_approved_unverified:
            approved_unverified += 1
        contract_count += len(step.effect_contract_hashes)
    return {
        "verified": verified,
        "approved_unverified": approved_unverified,
        "contract_count": contract_count,
    }


def _bounded_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostedError(f"{field} must be an integer")
    if value < 0 or value > _MAX_COUNTER:
        raise HostedError(f"{field} is outside the accepted range")
    return value


def _bounded_float(value: Any, *, field: str, maximum: float) -> float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostedError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > maximum:
        raise HostedError(f"{field} is outside the accepted range")
    return parsed


def _run_summary_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise HostedError("started_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostedError("started_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HostedError("started_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_uuid(value: Optional[str], *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise HostedError(f"{field} must be a canonical UUID")
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HostedError(f"{field} must be a canonical UUID") from exc


def _parse_report_response(
    resp: httpx.Response,
    *,
    expected_status: str,
    require_break_fields: bool,
) -> dict[str, Any]:
    """Validate a successful local-report response before marking it emitted.

    Both report rails share the same run identity and duplicate semantics.
    Break responses additionally bind the halt and teach route to that run.
    """
    try:
        body = resp.json()
    except (UnicodeError, ValueError) as exc:
        raise HostedError("ingest-report returned malformed JSON") from exc
    if not isinstance(body, dict):
        raise HostedError("ingest-report returned an unexpected response shape")

    run_id = _optional_uuid(body.get("run_id"), field="response.run_id")
    if run_id is None:
        raise HostedError("response.run_id must be a canonical UUID")
    if body.get("status") != expected_status:
        raise HostedError(
            f"ingest-report returned status {body.get('status')!r}; "
            f"expected {expected_status!r}"
        )
    if "duplicate" in body and not isinstance(body["duplicate"], bool):
        raise HostedError("ingest-report duplicate must be a boolean")

    if require_break_fields:
        halt_id = _optional_uuid(body.get("halt_id"), field="response.halt_id")
        if halt_id is None:
            raise HostedError("response.halt_id must be a canonical UUID")
        teach_url = body.get("teach_url")
        if teach_url != f"/dashboard/runs/{run_id}/teach":
            raise HostedError(
                "ingest-report teach_url must be the relative route for response.run_id"
            )

    prior_halt_run_id = body.get("prior_halt_run_id")
    if prior_halt_run_id is not None:
        _optional_uuid(prior_halt_run_id, field="response.prior_halt_run_id")
    provenance = body.get("provenance")
    if provenance is not None and provenance != "locally_reported":
        raise HostedError("ingest-report returned an unexpected provenance")

    return dict(body)


def _hex64(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise HostedError(f"{field} must be a lowercase 64-hex SHA-256 digest")
    return value


def _build_run_summary_payload(
    report: Any,
    *,
    workflow_id: Optional[str],
    backend: Optional[str],
    client_run_id: str,
) -> dict[str, Any]:
    """Assemble the schema-minimal SUCCESS payload.

    PHI posture (mirrors ``_build_break_payload``, one-way evidence rules):
    every field is validated locally before the request is created. Screenshots,
    params, URLs, workflow names/digests, resolved effect-contract hashes,
    report hashes, free text, and identity expected/observed strings are never
    sent. ``client_run_id`` is the sole run-scoped idempotency key.
    """
    normalized_workflow_id = _optional_uuid(workflow_id, field="workflow_id")
    if backend is not None and backend not in _BACKEND_ENUM:
        raise HostedError("backend must be web, windows, macos, linux, rdp, or citrix")
    normalized_client_run_id = _optional_uuid(
        client_run_id,
        field="client_run_id",
    )
    if normalized_client_run_id is None:  # pragma: no cover - required argument
        raise HostedError("client_run_id must be a canonical UUID")
    flow_version = _flow_version()
    if not _FLOW_VERSION_RE.fullmatch(flow_version):
        raise HostedError("flow_version is not a safe release identifier")

    steps = _bounded_int(len(report.results), field="metrics.steps")
    steps_ok = _bounded_int(
        sum(1 for step in report.results if step.ok), field="metrics.steps_ok"
    )
    steps_skipped = _bounded_int(
        sum(1 for step in report.results if step.skipped),
        field="metrics.steps_skipped",
    )
    identity = _identity_summary(report)
    identity = {
        key: _bounded_int(value, field=f"metrics.identity.{key}")
        for key, value in identity.items()
    }
    effects = {
        key: _bounded_int(value, field=f"metrics.effects.{key}")
        for key, value in _effect_summary(report).items()
    }
    rung_counts: dict[str, int] = {}
    for rung, count in dict(report.rung_counts).items():
        if rung not in _RUN_SUMMARY_RUNGS:
            raise HostedError("report contains an unknown resolution rung")
        rung_counts[rung] = _bounded_int(count, field=f"metrics.rung_counts.{rung}")
    duration_s = round(
        _bounded_float(
            report.total_ms / 1000.0,
            field="metrics.duration_s",
            maximum=_MAX_DURATION_S,
        ),
        3,
    )
    model_cost = round(
        _bounded_float(
            report.est_model_cost_usd,
            field="metrics.est_model_cost_usd",
            maximum=_MAX_MODEL_COST_USD,
        ),
        6,
    )
    payload: dict[str, Any] = {
        "kind": "run_summary",
        "schema": RUN_SUMMARY_SCHEMA,
        "status": "success",
        "flow_version": flow_version,
        "started_at": _run_summary_timestamp(report.started_at),
        "client_run_id": normalized_client_run_id,
        "phi_minimal": True,
        "metrics": {
            "steps": steps,
            "steps_ok": steps_ok,
            "steps_skipped": steps_skipped,
            "duration_s": duration_s,
            "heal_count": _bounded_int(report.heal_count, field="metrics.heal_count"),
            "model_calls": _bounded_int(
                report.model_calls, field="metrics.model_calls"
            ),
            "est_model_cost_usd": model_cost,
            "rung_counts": rung_counts,
            "identity": identity,
            "effects": effects,
        },
        "bundle_content_digest": _hex64(
            report.bundle_content_digest,
            field="bundle_content_digest",
        ),
    }
    if backend is not None:
        payload["backend"] = backend
    if normalized_workflow_id:
        payload["workflow_id"] = normalized_workflow_id
    return payload


def _flow_version() -> str:
    from openadapt_flow import __version__

    return __version__


def report_run(
    run_dir: Any,
    *,
    workflow_id: Optional[str] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
    deployment_kind: str = "local",
    org_id: Optional[str] = None,
    backend: Optional[str] = None,
    allow_local_fallback: bool = True,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Emit a PHI-free SUCCESS summary of a completed run to
    ``POST /api/runs/ingest-report`` (the local visibility rail; L0 in the
    multi-substrate design).

    Reads ``run_dir/report.json`` and, when the run SUCCEEDED, posts the
    schema-minimal summary so the control plane can show a locally reported
    completion for locally executed workflows. Halted/failed runs are
    ``report_break``'s
    contract — this function refuses them (``emitted=False``) rather than
    blurring the two rails. The recording, screenshots, params, and all free
    text NEVER leave the machine; see ``_build_run_summary_payload``.

    Never auto-invoked: callers opt in per-run (``run --report``) or via the
    ``OPENADAPT_FLOW_REPORT_RUN`` config knob.

    Args:
        run_dir: The run directory holding ``report.json``.
        workflow_id: Hosted workflow id when known (from ``push``/dashboard).
            The exact pushed bundle content digest is always required as the
            execution binding; the id only avoids an extra server lookup.
        deployment_kind: Deprecated compatibility input retained from Flow
            1.18.0. The authenticated server derives provenance; this value is
            ignored and never enters the payload.
        org_id: Deprecated compatibility input retained from Flow 1.18.0.
            Token ownership is canonical server-side; this value is ignored
            and never enters the payload.
        backend: The backend/substrate kind this run executed on (``web`` /
            ``windows`` / ``macos`` / ``linux`` / ``rdp``), when known.
        allow_local_fallback: On a server-side PHI-boundary rejection (422),
            return a ``local_only`` result instead of raising.

    Returns:
        On success: the server response (``run_id`` / ``status`` /
        ``duplicate``) plus ``{"emitted": True}``. When the run did not
        succeed: ``{"emitted": False, "reason": …}``. On a PHI-boundary
        fallback: ``{"emitted": False, "local_only": True, …}``.

    Raises:
        HostedError: missing report, missing workflow binding, auth failure,
            or a non-2xx/422 response.
    """
    from openadapt_flow.ir import RunReport

    run_path = Path(run_dir)
    report_path = run_path / "report.json"
    if not report_path.is_file():
        raise HostedError(f"No report.json in {run_path} — nothing to report.")
    try:
        report_size = report_path.stat().st_size
    except OSError as exc:
        raise HostedError("Could not inspect report.json") from exc
    if report_size > _MAX_RUN_REPORT_BYTES:
        raise HostedError(
            f"report.json exceeds the {_MAX_RUN_REPORT_BYTES}-byte reporting limit"
        )
    try:
        report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostedError("report.json is unreadable or invalid") from exc

    if not report.success:
        return {
            "emitted": False,
            "reason": ("run did not succeed; the halt/failure rail is `report-break`"),
        }
    if not report.bundle_content_digest:
        raise HostedError(
            "No exact bundle binding: run a sealed bundle whose report.json "
            "carries bundle_content_digest."
        )

    # Build and validate the complete outbound payload before resolving a
    # credential or creating a request. Server-side 4xx handling is defense in
    # depth, not the client privacy boundary.
    payload = _build_run_summary_payload(
        report,
        workflow_id=workflow_id,
        backend=backend,
        client_run_id=_client_run_id(run_path),
    )
    if (
        len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        > _MAX_RUN_SUMMARY_BYTES
    ):
        raise HostedError("run summary exceeds the outbound size limit")

    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    resolved_token = resolve_token(token, host=resolved_host)
    url = f"{resolved_host}/api/runs/ingest-report"

    resp = _post_report(url, resolved_token, payload)

    if resp.status_code == 422:
        if allow_local_fallback:
            return {
                "emitted": False,
                "local_only": True,
                "reason": "server rejected payload as PHI boundary (422)",
                "detail": _body_snippet(resp),
            }
        raise HostedError(
            f"ingest-report rejected as PHI boundary (422): {_body_snippet(resp)}"
        )
    if resp.status_code == 401:
        raise HostedError("Ingest token was rejected (401).")
    if resp.status_code not in (200, 202):
        raise HostedError(
            f"ingest-report returned {resp.status_code} (expected 202): "
            f"{_body_snippet(resp)}"
        )
    # Retained solely for the public 1.18.0 keyword/CLI contract. Authenticated
    # server identity is authoritative and neither value may egress.
    del deployment_kind, org_id
    result = _parse_report_response(
        resp,
        expected_status="success",
        require_break_fields=False,
    )
    result["emitted"] = True
    return result
