"""Verify a Cloud-issued Seal before ever reporting ``sealed``.

The agent reports ``sealed: true`` / ``proof: "sealed"`` only when the run's
report carries an OpenAdapt Cloud Execute receipt (``execute_receipt``) whose
``seal_signature`` (``openadapt.seal-signature/v1``) verifies against a
**pinned** issuer key registry, the receipt is for this run, and its outcome
is verified. Everything else is ``sealed: false``; a verified local run is
still done with ``proof: "local"``.

The registry is the JSON from ``GET https://app.openadapt.ai/api/public/seal-keys``
pinned by a person with ``openadapt-agent seal keys pin`` (trust on first use,
explicit confirmation) to ``~/.openadapt/seal-keys.json``, or named by
``OPENADAPT_SEAL_KEYS_PATH`` (or inline in ``OPENADAPT_SEAL_KEYS_JSON``). It is
never fetched at verification time, so a network attacker cannot substitute
keys. A Cloud-connected run's receipt names the Cloud execution; Flow records
that id as ``cloud_execution_id`` when it attaches the receipt. Byte-compatible with
``openadapt_types.seal`` and Cloud ``src/lib/sealSignature.ts``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SEAL_SIGNATURE_SCHEMA = "openadapt.seal-signature/v1"
_DOMAIN = (SEAL_SIGNATURE_SCHEMA + "\0").encode("utf-8")
_FIELDS = (
    "schema", "issuer", "algorithm", "key_id", "receipt_sha256", "run_id", "issued_at", "signature",
)
_SHA = re.compile(r"^[a-f0-9]{64}$")
_KEY_ID = re.compile(r"^seal-ed25519-[a-f0-9]{16}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SEALABLE = {"verified", "rolled_back_verified"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    body = {k: v for k, v in receipt.items() if k != "seal_signature"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _key_id(raw: bytes) -> str:
    return "seal-ed25519-" + hashlib.sha256(raw).hexdigest()[:16]


def parse_registry(document: Any) -> list[dict]:
    entries = document.get("keys") if isinstance(document, Mapping) else document
    if not isinstance(entries, list):
        raise ValueError("seal key registry must be a list or {keys: [...]}")
    keys = []
    for entry in entries:
        raw = base64.b64decode(str(entry.get("public_key", "")), validate=True)
        if len(raw) != 32 or entry.get("key_id") != _key_id(raw):
            raise ValueError("seal key entry is invalid")
        status = entry.get("status")
        if status not in ("active", "revoked"):
            raise ValueError("seal key status is invalid")
        revoked_at = entry.get("revoked_at")
        if status == "revoked" and not (isinstance(revoked_at, str) and _ISO.match(revoked_at)):
            raise ValueError("revoked seal key needs revoked_at")
        keys.append({"key_id": entry["key_id"], "raw": raw, "status": status, "revoked_at": revoked_at})
    return keys


def default_keys_path() -> Path:
    """Where ``openadapt-agent seal keys pin`` stores the pinned registry."""
    return Path.home() / ".openadapt" / "seal-keys.json"


def pinned_keys_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    named = env.get("OPENADAPT_SEAL_KEYS_PATH", "").strip()
    return Path(named).expanduser() if named else default_keys_path()


def load_pinned_registry(env: Optional[Mapping[str, str]] = None) -> list[dict]:
    """The pinned registry, or ``[]`` (which makes every Seal unverifiable)."""
    env = os.environ if env is None else env
    try:
        path = env.get("OPENADAPT_SEAL_KEYS_PATH", "").strip()
        if path:
            return parse_registry(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))
        inline = env.get("OPENADAPT_SEAL_KEYS_JSON", "").strip()
        if inline:
            return parse_registry(json.loads(inline))
        default = default_keys_path()
        if default.is_file():
            return parse_registry(json.loads(default.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    return []


def explain_receipt_seal(
    receipt: Any, registry: Sequence[Mapping[str, Any]]
) -> tuple[Optional[str], str]:
    """Return ``(key_id, "verified")`` or ``(None, closed refusal reason)``."""
    if not isinstance(receipt, Mapping):
        return None, "malformed"
    sig = receipt.get("seal_signature")
    if sig is None:
        return None, "unsigned"
    if not isinstance(sig, Mapping) or set(sig) != set(_FIELDS):
        return None, "malformed"
    if (
        sig["schema"] != SEAL_SIGNATURE_SCHEMA
        or sig["issuer"] != "openadapt-cloud"
        or sig["algorithm"] != "ed25519"
        or not isinstance(sig["key_id"], str) or not _KEY_ID.match(sig["key_id"])
        or not isinstance(sig["receipt_sha256"], str) or not _SHA.match(sig["receipt_sha256"])
        or not isinstance(sig["issued_at"], str) or not _ISO.match(sig["issued_at"])
        or not isinstance(sig["signature"], str)
    ):
        return None, "malformed"
    if sig["receipt_sha256"] != receipt_sha256(receipt):
        return None, "receipt_mismatch"
    if sig["run_id"] != receipt.get("execution_id"):
        return None, "receipt_mismatch"
    if not registry:
        return None, "no_pinned_keys"
    key = next((k for k in registry if k.get("key_id") == sig["key_id"]), None)
    if key is None:
        return None, "unknown_key"
    if key["status"] == "revoked" and (not key.get("revoked_at") or sig["issued_at"] >= key["revoked_at"]):
        return None, "revoked_key"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        payload = _DOMAIN + _canonical({k: v for k, v in sig.items() if k != "signature"}).encode("utf-8")
        Ed25519PublicKey.from_public_bytes(key["raw"]).verify(base64.b64decode(sig["signature"], validate=True), payload)
    except (ImportError, InvalidSignature, ValueError):
        return None, "bad_signature"
    return str(key["key_id"]), "verified"


def verify_receipt_seal(receipt: Any, registry: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Return the verifying key id, or ``None`` for any failure."""
    return explain_receipt_seal(receipt, registry)[0]


def receipt_from_document(document: Any) -> Optional[Mapping[str, Any]]:
    """Find the Execute receipt in a receipt, run report, webhook, or wrapper."""
    if not isinstance(document, Mapping):
        return None
    if "seal_signature" in document or document.get("schema_version") == "openadapt.execute-evidence-receipt/v1":
        return document
    for key in ("execute_receipt", "receipt"):
        inner = document.get(key)
        if isinstance(inner, Mapping):
            return inner
    return None


def verify_document(document: Any, registry: Sequence[Mapping[str, Any]]) -> dict:
    """Plain verdict for ``openadapt-agent seal verify``. Offline; pinned keys only."""
    receipt = receipt_from_document(document)
    if receipt is None:
        return {"verified": False, "reason": "no_receipt"}
    key_id, reason = explain_receipt_seal(receipt, registry)
    verdict: dict = {
        "verified": key_id is not None,
        "reason": reason,
        "execution_id": receipt.get("execution_id"),
        "outcome": receipt.get("outcome"),
        "receipt_sha256": receipt_sha256(receipt),
    }
    if key_id is not None:
        verdict["key_id"] = key_id
        sig = receipt.get("seal_signature") or {}
        verdict["issued_at"] = sig.get("issued_at")
        if receipt.get("outcome") not in _SEALABLE:
            # A genuine signature over a non-verified outcome is not a Seal of success.
            verdict["verified_outcome"] = False
        else:
            verdict["verified_outcome"] = True
    return verdict


def run_is_sealed(
    report: Any,
    *,
    run_id: Optional[str],
    execution_outcome: Optional[str],
    registry: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bool:
    """True only for a verified run whose Cloud receipt Seal verifies."""
    if execution_outcome != "VERIFIED" or not run_id or not isinstance(report, Mapping):
        return False
    receipt = report.get("execute_receipt")
    if not isinstance(receipt, Mapping):
        return False
    # The receipt names either this run or the Cloud execution Flow recorded
    # when it attached the receipt to this run's report.
    bound_ids = {run_id}
    cloud_execution_id = report.get("cloud_execution_id")
    if isinstance(cloud_execution_id, str) and cloud_execution_id:
        bound_ids.add(cloud_execution_id)
    if receipt.get("execution_id") not in bound_ids or receipt.get("outcome") not in _SEALABLE:
        return False
    registry = load_pinned_registry() if registry is None else registry
    return verify_receipt_seal(receipt, registry) is not None
