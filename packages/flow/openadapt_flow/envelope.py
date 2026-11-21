"""Who can open an encrypted recording, bundle or run: its recipients.

Every sealed set (:mod:`openadapt_flow.sealed_set`) has its own random data
key (DEK). The files are encrypted under the DEK, and the DEK is *wrapped*
for each recipient in the set's header. Format version 3 wraps it with age
(https://age-encryption.org, through ``pyrage``, X25519 recipients):

* **The device recipient** (the default). An age identity Flow creates on
  this computer on first use and keeps in the OS keychain (macOS Keychain,
  Windows Credential Manager with DPAPI, Linux Secret Service), or in a 0600
  file with a warning where no keychain works (:mod:`openadapt_flow.at_rest`).
  It lets the computer that recorded a run replay and view it.
* **Organization recipients** (optional). Public age recipients (``age1...``)
  from ``OPENADAPT_ORG_RECIPIENTS`` or a policy file
  (``OPENADAPT_POLICY_FILE``, default ``$OPENADAPT_HOME/policy.json``, key
  ``"recipients"``) that IT or MDM can place. The organization's reviewers
  hold the private key in their own KMS, HSM or age plugin; Flow never sees
  it. A reviewer opens a set with ``OPENADAPT_AGE_IDENTITY_FILE``.
* **Share recipients**, added later by :func:`add_recipients`, which rewraps
  the same DEK without re-encrypting any file.

A configured passphrase (``OPENADAPT_BUNDLE_KEY`` or
``OPENADAPT_RECORDING_KEY``) is a legacy recipient: with it, and no
organization recipient, Flow writes the version 2 header it always wrote.

:func:`shred` deletes a set's wrapped keys (and the durable checkpoints that
another store encrypted inside it). With them gone the ciphertext can't be
opened by anyone: this is the crypto-shredding retention primitive.

OpenAdapt Cloud never holds a key, a wrapped key or recovery material.

A device-written header also carries ``device_mac``, an HMAC under a key
derived from the device identity, over the whole header. age has no sender
authentication, so without it anyone who knows the public device recipient
could write a set this computer would open. The device refuses a header that
names it but lacks a valid MAC.

The provider layer is deliberately thin: :func:`device_identity` and
:func:`org_recipients` are the only places that decide who the recipients
are.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Final, Optional, Sequence

ORG_RECIPIENTS_ENV: Final[str] = "OPENADAPT_ORG_RECIPIENTS"
POLICY_FILE_ENV: Final[str] = "OPENADAPT_POLICY_FILE"
IDENTITY_FILE_ENV: Final[str] = "OPENADAPT_AGE_IDENTITY_FILE"
_WRAP_SCHEMA: Final[str] = "openadapt.flow.wrapped-dek/v3"
_MAC_LABEL: Final[bytes] = b"openadapt-flow/sealed-set/device-mac/v3"


class EnvelopeError(ValueError):
    """A recipient is malformed, or no recipient this computer holds opens a set."""


def _pyrage() -> Any:
    try:
        import pyrage
    except ImportError as exc:  # pragma: no cover - a core dependency
        raise EnvelopeError(
            "pyrage is not installed, so Flow can't open or write version 3 "
            "encrypted sets. Reinstall openadapt-flow."
        ) from exc
    return pyrage


# -- recipients ----------------------------------------------------------------


def parse_recipient(text: str) -> Any:
    """An age X25519 recipient, or :class:`EnvelopeError`."""
    pyrage = _pyrage()
    try:
        return pyrage.x25519.Recipient.from_str(text.strip())
    except Exception as exc:  # noqa: BLE001 - pyrage raises several types
        raise EnvelopeError(
            f"{text.strip()[:12]}... is not an age recipient (age1...)"
        ) from exc


def _home() -> Path:
    root = os.environ.get("OPENADAPT_HOME")
    return Path(root) if root else Path.home() / ".openadapt"


def policy_path() -> Path:
    configured = os.environ.get(POLICY_FILE_ENV)
    return Path(configured) if configured else _home() / "policy.json"


def org_recipients() -> list[str]:
    """The organization's public recipients, from the environment and the
    policy file. A malformed entry raises: an org recipient that is silently
    skipped is evidence the organization can't read later."""
    found: list[str] = []
    raw = os.environ.get(ORG_RECIPIENTS_ENV) or ""
    found += [item for item in raw.replace(",", " ").split() if item]
    path = policy_path()
    if path.is_file():
        try:
            policy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EnvelopeError(f"{path} is not a readable policy file") from exc
        listed = policy.get("recipients", []) if isinstance(policy, dict) else None
        if not isinstance(listed, list) or not all(isinstance(r, str) for r in listed):
            raise EnvelopeError(f"{path}: 'recipients' must be a list of age1... keys")
        found += listed
    out: list[str] = []
    for item in found:
        text = str(parse_recipient(item))
        if text not in out:
            out.append(text)
    return out


# -- identities ----------------------------------------------------------------


def device_identity(*, create: bool) -> Optional[str]:
    """This computer's age identity (``AGE-SECRET-KEY-1...``), created on first
    use when ``create``. None when there is none and ``create`` is False."""
    from openadapt_flow import at_rest

    return at_rest.device_secret(create=create)


def device_recipient(identity: str) -> str:
    return str(_pyrage().x25519.Identity.from_str(identity).to_public())


def _extra_identities() -> list[Any]:
    """Identities a reviewer configured (``OPENADAPT_AGE_IDENTITY_FILE``)."""
    path = os.environ.get(IDENTITY_FILE_ENV)
    if not path:
        return []
    pyrage = _pyrage()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EnvelopeError(f"{IDENTITY_FILE_ENV}: {path} can't be read") from exc
    out = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(pyrage.x25519.Identity.from_str(line))
            except Exception as exc:  # noqa: BLE001
                raise EnvelopeError(
                    f"{IDENTITY_FILE_ENV}: {path} holds a line that is not an age "
                    "identity"
                ) from exc
    return out


# -- wrapping ------------------------------------------------------------------


def _binding(header: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": _WRAP_SCHEMA,
        "kind": header["kind"],
        "data_key_id": header["data_key_id"],
    }


def wrap(dek: bytes, header: dict[str, Any], recipients: Sequence[str]) -> str:
    """The DEK sealed by age to ``recipients``, bound to the set's identity."""
    pyrage = _pyrage()
    payload = json.dumps(
        {**_binding(header), "dek": base64.b64encode(dek).decode("ascii")},
        sort_keys=True,
    ).encode("utf-8")
    parsed = [parse_recipient(r) for r in recipients]
    return base64.b64encode(pyrage.encrypt(payload, parsed)).decode("ascii")


def unwrap(header: dict[str, Any]) -> Optional[bytes]:
    """The DEK, through the device identity or a configured reviewer identity.
    None when no identity this computer holds is a recipient."""
    wrapped = header.get("age")
    if not isinstance(wrapped, str):
        return None
    identities = _extra_identities()
    device = device_identity(create=False)
    device_rcpt: Optional[str] = None
    pyrage = _pyrage()
    if device is not None:
        device_rcpt = device_recipient(device)
        if device_rcpt in (header.get("recipients") or []):
            check_device_mac(header, device)
        identities.insert(0, pyrage.x25519.Identity.from_str(device))
    if not identities:
        return None
    try:
        payload = pyrage.decrypt(base64.b64decode(wrapped, validate=True), identities)
    except Exception:  # noqa: BLE001 - not a recipient, or damaged
        return None
    try:
        body = json.loads(payload)
        dek = base64.b64decode(body["dek"], validate=True)
    except (ValueError, KeyError, TypeError) as exc:
        raise EnvelopeError("the wrapped data key is malformed") from exc
    if {k: body.get(k) for k in _binding(header)} != _binding(header) or len(dek) != 32:
        raise EnvelopeError("the wrapped data key belongs to another set")
    return dek


# -- the device MAC ------------------------------------------------------------


def _mac_key(identity: str) -> bytes:
    return hmac.new(identity.encode("ascii"), _MAC_LABEL, hashlib.sha256).digest()


def _mac_body(header: dict[str, Any]) -> bytes:
    body = {k: v for k, v in header.items() if k not in ("device_mac", "set_digest")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def device_mac(header: dict[str, Any], identity: str) -> str:
    return hmac.new(_mac_key(identity), _mac_body(header), hashlib.sha256).hexdigest()


def check_device_mac(header: dict[str, Any], identity: str) -> None:
    """Refuse a header that names this device but that it did not write."""
    got = header.get("device_mac")
    if not isinstance(got, str) or not hmac.compare_digest(
        got, device_mac(header, identity)
    ):
        raise EnvelopeError(
            "this encrypted set names this computer as a recipient, but its "
            "header was not written here (device MAC missing or wrong)"
        )


__all__ = [
    "EnvelopeError",
    "check_device_mac",
    "device_identity",
    "device_mac",
    "device_recipient",
    "org_recipients",
    "parse_recipient",
    "policy_path",
    "unwrap",
    "wrap",
]
