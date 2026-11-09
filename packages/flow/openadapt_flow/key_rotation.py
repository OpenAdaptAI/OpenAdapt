"""Explicit sealed-set DEK rewrapping; payload bytes are never re-encrypted.

Plans and coverage are local operator artifacts. Key ids are caller-assigned
labels authenticated by the envelope, not evidence of an external KMS. This
module never loads, generates, deletes, or retires a deployment key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openadapt_flow import crypto, sealed_set
from openadapt_flow.retention import RetentionTarget, _root, _snapshot, _tree
from openadapt_flow.sealed_maintenance import durable_replace, mutation_lock


class RotationError(ValueError):
    """A closed rotation refusal code, without key material."""


@dataclass(frozen=True)
class RewrapPlan:
    root: Path
    device: int
    inode: int
    previous_header_sha256: str
    replacement_header_sha256: str
    set_digest: str
    key_id: str
    files: tuple[RetentionTarget, ...]
    replacement: bytes = field(repr=False)

    def preview(self) -> dict[str, Any]:
        return {
            "dry_run": True,
            "would_rewrap": True,
            "payload_files": len(self.files),
            "payload_bytes": sum(item.size for item in self.files),
            "set_digest": self.set_digest,
            "previous_header_sha256": self.previous_header_sha256,
            "replacement_header_sha256": self.replacement_header_sha256,
            "key_id": self.key_id,
        }


def _explicit(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise RotationError("explicit_key_required")


def _verified(
    root: Path, key: str
) -> tuple[sealed_set.SealedSet, tuple[RetentionTarget, ...], RetentionTarget]:
    disk = _tree(root)
    if sealed_set.HEADER not in disk:
        raise RotationError("sealed_set_required")
    header = _snapshot(root, sealed_set.HEADER, disk[sealed_set.HEADER])
    opened = sealed_set.SealedSet.open(root, key)
    index = sealed_set._open_index(root, opened._header, opened._key)
    if sealed_set._canonical(index) != sealed_set._canonical(opened._files):
        raise RotationError("inventory_not_finalized")
    expected = {
        sealed_set._physical(rel, opened.codec(rel)): entry["sha256"]
        for rel, entry in index.items()
    }
    if set(disk) != {*expected, sealed_set.HEADER}:
        raise RotationError("inventory_mismatch")
    opened.verify_all()
    files = tuple(_snapshot(root, rel, disk[rel]) for rel in sorted(expected))
    if any(item.sha256 != expected[item.relative_path] for item in files):
        raise RotationError("source_changed")
    if _snapshot(root, sealed_set.HEADER, disk[sealed_set.HEADER]) != header:
        raise RotationError("source_changed")
    return opened, files, header


def plan_rewrap(
    root: Path | str, *, old_key: str, new_key: str, key_id: str
) -> RewrapPlan:
    """Read-only plan; authenticate every current payload before wrapping its DEK."""
    _explicit(old_key)
    _explicit(new_key)
    if (
        not isinstance(key_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key_id) is None
    ):
        raise RotationError("invalid_key_id")
    if old_key == new_key:
        raise RotationError("distinct_wrapping_key_required")
    base = _root(root)
    info = base.stat()
    opened, files, header = _verified(base, old_key)
    data_key = sealed_set._data_key(opened._header, old_key)
    replacement = dict(opened._header)
    if replacement["version"] == 1:
        if {"data_key_id", "wrapped_key", "key_id"}.intersection(replacement):
            raise RotationError("ambiguous_legacy_header")
        # Legacy salt plus the previous passphrase directly derives this DEK.
        # Never carry that recovery path into the current wrapping header.
        for name in sealed_set._LEGACY_DERIVATION_FIELDS:
            replacement.pop(name, None)
        replacement["data_key_id"] = secrets.token_hex(16)
    replacement.update(version=2, key_id=key_id)
    replacement["wrapped_key"] = base64.b64encode(
        crypto.encrypt_bytes(
            data_key, new_key, aad=sealed_set._wrapping_aad(replacement)
        )
    ).decode("ascii")
    # Prove the prepared envelope yields the identical key and authenticates the
    # original index. No plaintext data key is serialized or returned.
    if sealed_set._data_key(replacement, new_key) != data_key:
        raise RotationError("replacement_key_mismatch")
    verified = sealed_set._open_index(
        base, replacement, sealed_set._derive(replacement, new_key)
    )
    if sealed_set._canonical(verified) != sealed_set._canonical(opened._files):
        raise RotationError("replacement_index_mismatch")
    raw = json.dumps(replacement, indent=2, sort_keys=True).encode("utf-8")
    return RewrapPlan(
        base,
        info.st_dev,
        info.st_ino,
        header.sha256,
        hashlib.sha256(raw).hexdigest(),
        opened.set_digest,
        key_id,
        files,
        raw,
    )


def rewrap(
    plan: RewrapPlan, *, old_key: str, new_key: str, commit: bool = False
) -> dict[str, Any]:
    """Default dry-run. Explicit commit atomically publishes only the header.

    Each set commits independently. Interruption before replacement leaves the
    old header; interruption after replacement leaves the new header. Coverage
    must be checked for the full caller-supplied inventory before retiring any
    key elsewhere; this API supplies no retirement operation. Historical
    headers or previously recovered data keys still permit payload access.
    """
    _explicit(old_key)
    _explicit(new_key)
    if type(commit) is not bool:
        raise RotationError("explicit_commit_boolean_required")
    if not isinstance(plan, RewrapPlan):
        raise RotationError("invalid_rewrap_plan")

    def validate() -> None:
        base = _root(plan.root)
        info = base.stat()
        if (info.st_dev, info.st_ino) != (plan.device, plan.inode):
            raise RotationError("source_replaced")
        opened, files, header = _verified(base, old_key)
        if (
            header.sha256 != plan.previous_header_sha256
            or files != plan.files
            or opened.set_digest != plan.set_digest
        ):
            raise RotationError("stale_rewrap_plan")
        if (
            hashlib.sha256(plan.replacement).hexdigest()
            != plan.replacement_header_sha256
        ):
            raise RotationError("replacement_changed")
        replacement = json.loads(plan.replacement)
        if replacement.get("version") != 2 or replacement.get("key_id") != plan.key_id:
            raise RotationError("replacement_changed")
        new_data_key = sealed_set._data_key(replacement, new_key)
        if new_data_key != sealed_set._data_key(opened._header, old_key):
            raise RotationError("replacement_key_mismatch")
        mutable = {"version", "key_id", "wrapped_key"}
        if opened._header["version"] == 1:
            mutable.update(sealed_set._LEGACY_DERIVATION_FIELDS)
            mutable.add("data_key_id")
        if {
            name: value for name, value in replacement.items() if name not in mutable
        } != {
            name: value for name, value in opened._header.items() if name not in mutable
        }:
            raise RotationError("replacement_immutable_header_changed")
        if replacement.get("index") != opened._header.get("index"):
            raise RotationError("replacement_index_changed")
        new_index = sealed_set._open_index(
            base, replacement, crypto.SetKey(new_data_key)
        )
        if sealed_set._canonical(new_index) != sealed_set._canonical(opened._files):
            raise RotationError("replacement_index_mismatch")

    if not commit:
        validate()
        return plan.preview()
    with mutation_lock(plan.root):
        if sealed_set.active_writer(plan.root) is not None:
            raise RotationError("active_writer")
        validate()
        durable_replace(
            plan.root / sealed_set.HEADER,
            plan.replacement,
            expected_parent=(plan.device, plan.inode),
        )
        sealed_set._forget(plan.root)
        # Success is reported only after the on-disk header opens with the new
        # key and the complete payload inventory still authenticates.
        opened, files, header = _verified(plan.root, new_key)
        if (
            header.sha256 != plan.replacement_header_sha256
            or files != plan.files
            or opened.set_digest != plan.set_digest
        ):
            raise RotationError("published_rotation_mismatch")
    return {**plan.preview(), "dry_run": False, "rewrapped": True}


def key_coverage(roots: list[Path | str], *, keys: dict[str, str]) -> dict[str, Any]:
    """Authenticate only explicitly listed roots, never infer fleet-wide coverage.

    Missing keys, legacy/unlabelled sets, damaged sets and duplicate roots are
    reported as uncovered. Independently keyed external stores are also
    uncovered: their ciphertext hashes do not prove new-key decryptability.
    """
    entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        base = _root(root)
        if base in seen:
            raise RotationError("duplicate_coverage_root")
        seen.add(base)
        header_bytes = (base / sealed_set.HEADER).read_bytes()
        header = sealed_set.read_header(base)
        key_id = header.get("key_id") if header.get("version") == 2 else None
        entry: dict[str, Any] = {
            "root": str(base),
            "header_sha256": hashlib.sha256(header_bytes).hexdigest(),
            "key_id": key_id,
            "covered": False,
            "reason": "legacy_unlabelled" if key_id is None else "key_unavailable",
        }
        if isinstance(key_id, str) and keys.get(key_id):
            try:
                opened, _, snapshot = _verified(base, keys[key_id])
                if snapshot.sha256 != entry["header_sha256"]:
                    raise RotationError("source_changed")
                if any(opened.codec(rel) == "external" for rel in opened.files()):
                    entry["reason"] = "external_key_store_unverified"
                else:
                    entry.update(
                        covered=True,
                        reason="authenticated",
                        set_digest=opened.set_digest,
                    )
            except (OSError, ValueError, crypto.CryptoError):
                entry["reason"] = "authentication_failed"
        entries.append(entry)
    return {
        "scope": "explicit_roots_only",
        "complete": bool(entries) and all(e["covered"] for e in entries),
        "sets": entries,
        "key_retirement_authorized": False,
    }
