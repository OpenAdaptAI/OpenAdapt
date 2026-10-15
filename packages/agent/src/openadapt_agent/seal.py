"""Verify a Cloud-issued Seal before ever reporting ``sealed``.

The agent reports ``sealed: true`` / ``proof: "sealed"`` only when the run's
report carries an OpenAdapt Cloud Execute receipt (``execute_receipt``) whose
``seal_signature`` (``openadapt.seal-signature/v1``) verifies against a
**pinned** issuer key registry, the receipt is for this run, and its outcome
is verified. Everything else is ``sealed: false``; a verified local run is
still done with ``proof: "local"``.

The registry is the JSON from ``GET https://app.openadapt.ai/api/public/seal-keys``
saved locally and named by ``OPENADAPT_SEAL_KEYS_PATH`` (or inline in
``OPENADAPT_SEAL_KEYS_JSON``). It is never fetched at verification time, so a
network attacker cannot substitute keys. Byte-compatible with
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


def load_pinned_registry(env: Optional[Mapping[str, str]] = None) -> list[dict]:
    """The pinned registry, or ``[]`` (which makes every Seal unverifiable)."""
    env = os.environ if env is None else env
    try:
        path = env.get("OPENADAPT_SEAL_KEYS_PATH", "").strip()
        if path:
            return parse_registry(json.loads(Path(path).read_text(encoding="utf-8")))
        inline = env.get("OPENADAPT_SEAL_KEYS_JSON", "").strip()
        if inline:
            return parse_registry(json.loads(inline))
    except (OSError, ValueError):
        return []
    return []


def verify_receipt_seal(receipt: Any, registry: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Return the verifying key id, or ``None`` for any failure."""
    if not isinstance(receipt, Mapping):
        return None
    sig = receipt.get("seal_signature")
    if not isinstance(sig, Mapping) or set(sig) != set(_FIELDS):
        return None
    if (
        sig["schema"] != SEAL_SIGNATURE_SCHEMA
        or sig["issuer"] != "openadapt-cloud"
        or sig["algorithm"] != "ed25519"
        or not isinstance(sig["key_id"], str) or not _KEY_ID.match(sig["key_id"])
        or not isinstance(sig["receipt_sha256"], str) or not _SHA.match(sig["receipt_sha256"])
        or not isinstance(sig["issued_at"], str) or not _ISO.match(sig["issued_at"])
        or not isinstance(sig["signature"], str)
    ):
        return None
    if sig["receipt_sha256"] != receipt_sha256(receipt):
        return None
    if sig["run_id"] != receipt.get("execution_id"):
        return None
    key = next((k for k in registry if k.get("key_id") == sig["key_id"]), None)
    if key is None:
        return None
    if key["status"] == "revoked" and (not key.get("revoked_at") or sig["issued_at"] >= key["revoked_at"]):
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        payload = _DOMAIN + _canonical({k: v for k, v in sig.items() if k != "signature"}).encode("utf-8")
        Ed25519PublicKey.from_public_bytes(key["raw"]).verify(base64.b64decode(sig["signature"], validate=True), payload)
    except (ImportError, InvalidSignature, ValueError):
        return None
    return str(key["key_id"])


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
    if receipt.get("execution_id") != run_id or receipt.get("outcome") not in _SEALABLE:
        return False
    registry = load_pinned_registry() if registry is None else registry
    return verify_receipt_seal(receipt, registry) is not None
