"""Zero-PHI Ed25519 Merkle cohort receipt aggregating worklist and audit samples.

Aggregates a batch worklist and an ``audit_sample_v1`` manifest into a signed,
zero-PHI cohort manifest. Item identifiers are SHA-256 hashed before entering
the manifest, leaf digests are folded into a domain-separated SHA-256 Merkle
tree, and the canonical payload is signed with the customer's Ed25519 key.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from openadapt.audit_sample import AUDIT_SAMPLE_SCHEMA, load_run_records
from openadapt.trust_init import (
    KeyStoreBackend,
    compute_public_key_fingerprint,
    load_private_key,
    load_public_key,
)

COHORT_RECEIPT_SCHEMA = "openadapt.cohort-receipt/v1"

_VERIFIED_STATUSES = frozenset({"verified", "confirmed", "committed", "passed", "ok"})
_HALTED_STATUSES = frozenset(
    {
        "halted",
        "halted_for_human",
        "needs_human",
        "escalated",
        "paused",
        "pending_human",
        "stopped",
    }
)
_DROPPED_STATUSES = frozenset(
    {"dropped", "silently_dropped", "missing", "unrecorded", "skipped"}
)

_AGREE_VERDICTS = frozenset(
    {"agree", "agreed", "match", "matched", "pass", "passed", "ok", "verified", "confirmed"}
)
_DISAGREE_VERDICTS = frozenset(
    {"disagree", "disagreed", "mismatch", "fail", "failed", "refuted", "error"}
)


class CohortReceiptError(ValueError):
    """Raised when cohort receipt inputs or verification arguments are invalid."""


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying a signed cohort receipt manifest."""

    ok: bool
    reason: str
    merkle_root: str
    key_id: str
    leaf_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "merkle_root": self.merkle_root,
            "key_id": self.key_id,
            "leaf_count": self.leaf_count,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_merkle_root(leaf_digests: Sequence[str]) -> str:
    """Compute a domain-separated binary SHA-256 Merkle root over hex digests."""
    if not leaf_digests:
        return _sha256_hex(b"")
    level: list[bytes] = [
        hashlib.sha256(b"\x00" + digest.encode("ascii")).digest()
        for digest in leaf_digests
    ]
    while len(level) > 1:
        next_level: list[bytes] = []
        for idx in range(0, len(level), 2):
            left = level[idx]
            right = level[idx + 1] if idx + 1 < len(level) else left
            next_level.append(hashlib.sha256(b"\x01" + left + right).digest())
        level = next_level
    return level[0].hex()


def _normalize_status(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "silently_dropped"
    lower = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if lower in _VERIFIED_STATUSES:
        return "verified"
    if lower in _HALTED_STATUSES:
        return "halted_for_human"
    if lower in _DROPPED_STATUSES:
        return "silently_dropped"
    return lower


def _extract_item_id(row: dict[str, Any], index: int) -> str:
    for key in ("item_id", "run_id", "row_id", "task_id", "work_item_id", "id"):
        val = row.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()
    return f"item-{index}"


def _load_worklist(path: Path) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
    """Load a batch worklist from JSON or JSONL.

    Returns ``(items, pagination_complete, cohort_id)``.
    """
    if not path.is_file():
        raise CohortReceiptError(f"Worklist file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        items: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except ValueError as exc:
                raise CohortReceiptError(f"Malformed JSONL line in {path}: {exc}") from exc
            if isinstance(obj, dict):
                items.append(obj)
        return items, True, None

    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise CohortReceiptError(f"Malformed JSON in worklist {path}: {exc}") from exc

    if isinstance(doc, list):
        return [row for row in doc if isinstance(row, dict)], True, None

    if isinstance(doc, dict):
        pagination_complete = bool(doc.get("pagination_complete", True))
        cohort_id = doc.get("cohort_id") or doc.get("batch_id")
        for key in ("items", "worklist", "rows", "records", "runs"):
            nested = doc.get(key)
            if isinstance(nested, list):
                return (
                    [row for row in nested if isinstance(row, dict)],
                    pagination_complete,
                    str(cohort_id) if cohort_id is not None else None,
                )
        raise CohortReceiptError(
            f"Worklist JSON {path} must contain an 'items' list or be a JSON array."
        )

    raise CohortReceiptError(f"Unsupported worklist structure in {path}.")


def _compute_field_agreement_rate(audit_doc: dict[str, Any]) -> tuple[Optional[float], dict[str, Any]]:
    """Compute field agreement rate from an ``audit_sample_v1`` manifest."""
    if audit_doc.get("schema") != AUDIT_SAMPLE_SCHEMA:
        raise CohortReceiptError(
            f"Expected audit sample schema {AUDIT_SAMPLE_SCHEMA!r}, got {audit_doc.get('schema')!r}."
        )
    samples = audit_doc.get("samples")
    if not isinstance(samples, list):
        samples = []

    scores: list[float] = []
    reviewed_count = 0
    for entry in samples:
        if not isinstance(entry, dict):
            continue
        spot = entry.get("spot_check")
        if not isinstance(spot, dict):
            continue
        if isinstance(spot.get("field_agreement"), (int, float)) and not isinstance(
            spot.get("field_agreement"), bool
        ):
            val = float(spot["field_agreement"])
            if 0.0 <= val <= 1.0:
                scores.append(val)
                reviewed_count += 1
                continue
        verdict = spot.get("verdict")
        if isinstance(verdict, bool):
            scores.append(1.0 if verdict else 0.0)
            reviewed_count += 1
        elif isinstance(verdict, str) and verdict.strip():
            norm = verdict.strip().lower()
            if norm in _AGREE_VERDICTS:
                scores.append(1.0)
                reviewed_count += 1
            elif norm in _DISAGREE_VERDICTS:
                scores.append(0.0)
                reviewed_count += 1

    rate = round(sum(scores) / len(scores), 6) if scores else None
    meta = {
        "schema": AUDIT_SAMPLE_SCHEMA,
        "sampled": len(samples),
        "reviewed": reviewed_count,
        "manifest_digest": _sha256_hex(_canonical_bytes(audit_doc)),
    }
    return rate, meta


def _item_leaf_digest(item_id: str, status: str, receipt_digest: str) -> str:
    """Compute a zero-PHI SHA-256 leaf digest for one worklist item."""
    item_id_hash = _sha256_hex(item_id.encode("utf-8"))
    leaf_payload = {
        "item_id_hash": item_id_hash,
        "status": status,
        "receipt_digest": receipt_digest,
    }
    return _sha256_hex(_canonical_bytes(leaf_payload))


def build_cohort_receipt(
    worklist_path: Path,
    *,
    audit_sample_path: Optional[Path] = None,
    receipts_source: Optional[Path] = None,
    private_key: Optional[Ed25519PrivateKey] = None,
    key_dir: Optional[Path] = None,
    keystore: Optional[KeyStoreBackend] = None,
    cohort_id: Optional[str] = None,
    pagination_complete: Optional[bool] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build and Ed25519-sign a zero-PHI cohort receipt manifest."""
    items, file_pagination_complete, file_cohort_id = _load_worklist(worklist_path)
    resolved_pagination = (
        file_pagination_complete
        if pagination_complete is None
        else bool(pagination_complete)
    )

    observed_by_id: dict[str, tuple[str, str]] = {}
    if receipts_source is not None:
        for rec in load_run_records(receipts_source):
            observed_by_id[rec.run_id] = (_normalize_status(rec.status), rec.receipt_digest)

    verified = 0
    halted_for_human = 0
    silently_dropped = 0
    leaf_digests: list[str] = []

    for idx, row in enumerate(items):
        item_id = _extract_item_id(row, idx)
        raw_status = (
            row.get("status")
            or row.get("outcome")
            or row.get("verdict")
            or row.get("result")
        )
        raw_digest = (
            row.get("receipt_digest")
            or row.get("receipt_sha256")
            or row.get("digest")
            or ""
        )

        if item_id in observed_by_id:
            obs_status, obs_digest = observed_by_id[item_id]
            status = obs_status
            receipt_digest = str(raw_digest).strip() or obs_digest
        elif raw_status is not None:
            status = _normalize_status(raw_status)
            receipt_digest = str(raw_digest).strip()
        else:
            status = "silently_dropped"
            receipt_digest = ""

        if status == "verified":
            verified += 1
        elif status == "halted_for_human":
            halted_for_human += 1
        elif status == "silently_dropped":
            silently_dropped += 1
        else:
            halted_for_human += 1

        leaf_digests.append(_item_leaf_digest(item_id, status, receipt_digest))

    total_expected = len(items)
    audit_meta: Optional[dict[str, Any]] = None
    field_agreement_rate: Optional[float] = None
    if audit_sample_path is not None:
        if not audit_sample_path.is_file():
            raise CohortReceiptError(f"Audit sample file not found: {audit_sample_path}")
        try:
            audit_doc = json.loads(audit_sample_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CohortReceiptError(
                f"Malformed JSON in audit sample {audit_sample_path}: {exc}"
            ) from exc
        if not isinstance(audit_doc, dict):
            raise CohortReceiptError("Audit sample file must contain a JSON object.")
        field_agreement_rate, audit_meta = _compute_field_agreement_rate(audit_doc)

    merkle_root = compute_merkle_root(leaf_digests)
    signing_key = private_key or load_private_key(key_dir, keystore=keystore)
    public_key = signing_key.public_key()
    fingerprint, key_id = compute_public_key_fingerprint(public_key)
    raw_pub = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")

    resolved_cohort_id = cohort_id or file_cohort_id or f"cohort-{merkle_root[:12]}"
    payload: dict[str, Any] = {
        "schema": COHORT_RECEIPT_SCHEMA,
        "cohort_id": resolved_cohort_id,
        "generated_at": generated_at or _utc_now_iso(),
        "phi_free": True,
        "summary": {
            "total_expected": total_expected,
            "verified": verified,
            "halted_for_human": halted_for_human,
            "silently_dropped": silently_dropped,
            "pagination_complete": resolved_pagination,
            "field_agreement_rate": field_agreement_rate,
        },
        "merkle": {
            "hash_algorithm": "sha256",
            "leaf_count": len(leaf_digests),
            "root": merkle_root,
            "leaf_digests": leaf_digests,
        },
        "audit_sample": audit_meta,
    }

    sig_bytes = signing_key.sign(_canonical_bytes(payload))
    manifest = dict(payload)
    manifest["signature"] = {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "fingerprint_sha256": fingerprint,
        "public_key_b64": pub_b64,
        "signature_b64": base64.b64encode(sig_bytes).decode("ascii"),
    }
    return manifest


def _load_ed25519_public_key_from_pem(path: Path) -> Ed25519PublicKey:
    loaded = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(loaded, Ed25519PublicKey):
        raise CohortReceiptError(f"Key at {path} is not an Ed25519 public key.")
    return loaded


def verify_cohort_receipt(
    manifest: dict[str, Any],
    *,
    public_key: Optional[Ed25519PublicKey] = None,
    public_key_path: Optional[Path] = None,
    key_dir: Optional[Path] = None,
) -> VerificationResult:
    """Verify a signed cohort receipt's Merkle root, summary invariants, and Ed25519 signature."""
    if not isinstance(manifest, dict):
        return VerificationResult(False, "Manifest must be a JSON object.", "", "", 0)
    if manifest.get("schema") != COHORT_RECEIPT_SCHEMA:
        return VerificationResult(
            False,
            f"Unsupported schema {manifest.get('schema')!r}; expected {COHORT_RECEIPT_SCHEMA!r}.",
            "",
            "",
            0,
        )

    merkle = manifest.get("merkle")
    summary = manifest.get("summary")
    sig_block = manifest.get("signature")
    if not isinstance(merkle, dict) or not isinstance(summary, dict) or not isinstance(sig_block, dict):
        return VerificationResult(
            False, "Manifest missing required merkle, summary, or signature section.", "", "", 0
        )

    leaf_digests = merkle.get("leaf_digests")
    claimed_root = str(merkle.get("root") or "")
    claimed_count = merkle.get("leaf_count")
    if not isinstance(leaf_digests, list) or not all(isinstance(d, str) for d in leaf_digests):
        return VerificationResult(False, "Invalid merkle.leaf_digests list.", claimed_root, "", 0)
    if claimed_count != len(leaf_digests):
        return VerificationResult(
            False,
            f"merkle.leaf_count ({claimed_count}) does not match leaf_digests length ({len(leaf_digests)}).",
            claimed_root,
            "",
            len(leaf_digests),
        )

    recomputed_root = compute_merkle_root(leaf_digests)
    if recomputed_root != claimed_root:
        return VerificationResult(
            False,
            f"Merkle root mismatch: expected {claimed_root}, recomputed {recomputed_root}.",
            recomputed_root,
            str(sig_block.get("key_id") or ""),
            len(leaf_digests),
        )

    required_summary_keys = (
        "total_expected",
        "verified",
        "halted_for_human",
        "silently_dropped",
        "pagination_complete",
        "field_agreement_rate",
    )
    for key in required_summary_keys:
        if key not in summary:
            return VerificationResult(
                False,
                f"Summary missing required field {key!r}.",
                recomputed_root,
                str(sig_block.get("key_id") or ""),
                len(leaf_digests),
            )

    total_expected = summary["total_expected"]
    verified = summary["verified"]
    halted = summary["halted_for_human"]
    dropped = summary["silently_dropped"]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (total_expected, verified, halted, dropped)):
        return VerificationResult(
            False,
            "Summary counts must be non-negative integers.",
            recomputed_root,
            str(sig_block.get("key_id") or ""),
            len(leaf_digests),
        )
    if verified + halted + dropped != total_expected or total_expected != len(leaf_digests):
        return VerificationResult(
            False,
            "Summary counts do not reconcile with total_expected and Merkle leaf count.",
            recomputed_root,
            str(sig_block.get("key_id") or ""),
            len(leaf_digests),
        )

    agreement = summary["field_agreement_rate"]
    if agreement is not None:
        if (
            not isinstance(agreement, (int, float))
            or isinstance(agreement, bool)
            or not (0.0 <= float(agreement) <= 1.0)
        ):
            return VerificationResult(
                False,
                f"Invalid field_agreement_rate: {agreement!r}.",
                recomputed_root,
                str(sig_block.get("key_id") or ""),
                len(leaf_digests),
            )

    if sig_block.get("algorithm") != "Ed25519":
        return VerificationResult(
            False,
            f"Unsupported signature algorithm {sig_block.get('algorithm')!r}.",
            recomputed_root,
            str(sig_block.get("key_id") or ""),
            len(leaf_digests),
        )

    key_id = str(sig_block.get("key_id") or "")
    sig_b64 = sig_block.get("signature_b64")
    if not isinstance(sig_b64, str) or not sig_b64:
        return VerificationResult(False, "Missing signature_b64.", recomputed_root, key_id, len(leaf_digests))

    try:
        sig_bytes = base64.b64decode(sig_b64)
    except ValueError:
        return VerificationResult(False, "Malformed base64 signature.", recomputed_root, key_id, len(leaf_digests))

    verify_key: Optional[Ed25519PublicKey] = public_key
    if verify_key is None and public_key_path is not None:
        verify_key = _load_ed25519_public_key_from_pem(public_key_path)
    elif verify_key is None and key_dir is not None:
        verify_key = load_public_key(key_dir)
    elif verify_key is None:
        embedded_b64 = sig_block.get("public_key_b64")
        if not isinstance(embedded_b64, str) or not embedded_b64:
            return VerificationResult(
                False,
                "No public key supplied and manifest has no public_key_b64.",
                recomputed_root,
                key_id,
                len(leaf_digests),
            )
        try:
            verify_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(embedded_b64))
        except ValueError as exc:
            return VerificationResult(
                False,
                f"Malformed embedded public_key_b64: {exc}",
                recomputed_root,
                key_id,
                len(leaf_digests),
            )

    expected_fp, expected_kid = compute_public_key_fingerprint(verify_key)
    if key_id and key_id != expected_kid:
        return VerificationResult(
            False,
            f"Key ID mismatch: manifest signed by {key_id}, verifying key is {expected_kid}.",
            recomputed_root,
            key_id,
            len(leaf_digests),
        )
    if sig_block.get("fingerprint_sha256") and sig_block["fingerprint_sha256"] != expected_fp:
        return VerificationResult(
            False,
            "Public key fingerprint mismatch.",
            recomputed_root,
            key_id,
            len(leaf_digests),
        )

    payload = {k: v for k, v in manifest.items() if k != "signature"}
    try:
        verify_key.verify(sig_bytes, _canonical_bytes(payload))
    except InvalidSignature:
        return VerificationResult(
            False,
            "Ed25519 signature verification failed.",
            recomputed_root,
            expected_kid,
            len(leaf_digests),
        )

    return VerificationResult(
        True,
        "Merkle root and Ed25519 signature verified.",
        recomputed_root,
        expected_kid,
        len(leaf_digests),
    )
