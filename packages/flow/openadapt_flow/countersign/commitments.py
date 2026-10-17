"""Salted field commitments and selective disclosure.

A receipt that leaves the customer must not carry an MRN, a date of birth, or
any other identifying value, and a bare SHA-256 of such a value is no better:
an MRN has a few million plausible values, so hashing each one finds it in
seconds. A salted commitment fixes that. Each field gets its own 256-bit
random salt, and only the commitment

    sha256("openadapt.field-commitment/v1\\0" || canonical({label, name, salt, value}))

leaves the machine. Without the salt, guessing the value means guessing 256
random bits as well. The salts and values stay in the local evidence vault.

To settle a dispute, the holder discloses chosen fields: each disclosure gives
the label, name, value, and salt, and anyone can recompute the commitment.
Undisclosed fields stay hidden, because every field has an independent salt.

Labels are opaque (``f-`` plus random hex) so that field names stay out of the
commitment block. The field name is inside the commitment and is revealed
only when that field is disclosed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "COMMITMENTS_SCHEMA",
    "DISCLOSURE_SCHEMA",
    "CommitmentError",
    "DisclosureCheck",
    "FieldSecret",
    "build_disclosure",
    "commit_fields",
    "commitments_sha256",
    "field_commitment",
    "verify_disclosure",
]

COMMITMENTS_SCHEMA = "openadapt.field-commitments/v1"
DISCLOSURE_SCHEMA = "openadapt.field-disclosure/v1"
COMMITMENT_ALGORITHM = "sha256-salted-256"
SALT_BYTES = 32
_DOMAIN = b"openadapt.field-commitment/v1\x00"
_LABEL = re.compile(r"^f-[0-9a-f]{8,32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_FIELDS = 64
_MAX_VALUE_CHARS = 4096


class CommitmentError(ValueError):
    """The commitment block or a disclosure is malformed."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def field_commitment(label: str, name: str, value: str, salt: bytes) -> str:
    """The commitment to one field. Every input is a string; the salt is 32 bytes."""
    if len(salt) != SALT_BYTES:
        raise CommitmentError("a field salt is 32 bytes")
    payload = _DOMAIN + _canonical(
        {"label": label, "name": name, "salt": _b64(salt), "value": value}
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FieldSecret:
    """What the vault keeps for one field. Never leaves the customer by default."""

    label: str
    name: str
    value: str
    salt: str  # base64, 32 bytes

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "name": self.name,
            "value": self.value,
            "salt": self.salt,
        }


def commit_fields(
    fields: Mapping[str, str],
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> tuple[dict[str, Any], list[FieldSecret]]:
    """Commit to each field with its own salt.

    Returns the public commitment block and the secrets the vault must keep.
    Losing the secrets loses the ability to disclose; it never weakens the
    commitments.
    """
    if not fields:
        raise CommitmentError("nothing to commit")
    if len(fields) > _MAX_FIELDS:
        raise CommitmentError(f"at most {_MAX_FIELDS} fields")
    commitments: dict[str, str] = {}
    kept: list[FieldSecret] = []
    for name, value in fields.items():
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise CommitmentError("field names and values are non-empty strings")
        if len(value) > _MAX_VALUE_CHARS:
            raise CommitmentError("a field value is too long to commit")
        while True:
            label = "f-" + random_bytes(4).hex()
            if label not in commitments:
                break
        salt = random_bytes(SALT_BYTES)
        if len(salt) != SALT_BYTES:
            raise CommitmentError("the salt source returned the wrong length")
        commitments[label] = field_commitment(label, name, value, salt)
        kept.append(FieldSecret(label=label, name=name, value=value, salt=_b64(salt)))
    block = {
        "schema": COMMITMENTS_SCHEMA,
        "algorithm": COMMITMENT_ALGORITHM,
        "fields": dict(sorted(commitments.items())),
    }
    return block, kept


def _check_block(block: Any) -> Mapping[str, str]:
    if (
        not isinstance(block, Mapping)
        or block.get("schema") != COMMITMENTS_SCHEMA
        or block.get("algorithm") != COMMITMENT_ALGORITHM
        or set(block) != {"schema", "algorithm", "fields"}
    ):
        raise CommitmentError("not an openadapt.field-commitments/v1 block")
    fields = block.get("fields")
    if not isinstance(fields, Mapping) or not fields or len(fields) > _MAX_FIELDS:
        raise CommitmentError("the commitment block has no fields")
    for label, digest in fields.items():
        if not isinstance(label, str) or not _LABEL.match(label):
            raise CommitmentError("a commitment label is not opaque")
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            raise CommitmentError("a commitment is not a sha256 digest")
    return fields


def commitments_sha256(block: Mapping[str, Any]) -> str:
    """Digest of the canonical commitment block, as bound by a countersign statement."""
    _check_block(block)
    return "sha256:" + hashlib.sha256(_canonical(dict(block))).hexdigest()


def build_disclosure(
    block: Mapping[str, Any],
    kept: Sequence[FieldSecret],
    labels: Sequence[str],
    *,
    receipt_sha256: str,
) -> dict[str, Any]:
    """A disclosure bundle revealing exactly ``labels``. Values are plaintext.

    The bundle is as sensitive as the values it reveals. Hand it only to the
    party who is entitled to those values.
    """
    fields = _check_block(block)
    by_label = {s.label: s for s in kept}
    chosen = []
    for label in labels:
        if label not in fields or label not in by_label:
            raise CommitmentError(f"no committed field labelled {label}")
        secret = by_label[label]
        salt = base64.b64decode(secret.salt, validate=True)
        if field_commitment(label, secret.name, secret.value, salt) != fields[label]:
            raise CommitmentError(f"the vault entry for {label} does not match")
        chosen.append(secret.as_dict())
    if not chosen:
        raise CommitmentError("choose at least one field to disclose")
    return {
        "schema": DISCLOSURE_SCHEMA,
        "receipt_sha256": receipt_sha256,
        "commitments_sha256": commitments_sha256(block),
        "fields": chosen,
    }


@dataclass(frozen=True)
class DisclosedField:
    label: str
    name: Optional[str]
    ok: bool
    reason: str


@dataclass(frozen=True)
class DisclosureCheck:
    ok: bool
    reason: str
    fields: list[DisclosedField] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "fields": [f.__dict__ for f in self.fields],
        }


def verify_disclosure(
    disclosure: Any,
    block: Any,
    *,
    receipt_sha256: Optional[str] = None,
) -> DisclosureCheck:
    """Check every disclosed field against the commitment block. Fail closed."""
    try:
        fields = _check_block(block)
        block_digest = commitments_sha256(block)
    except CommitmentError:
        return DisclosureCheck(False, "no_commitments")
    if (
        not isinstance(disclosure, Mapping)
        or disclosure.get("schema") != DISCLOSURE_SCHEMA
    ):
        return DisclosureCheck(False, "malformed")
    if disclosure.get("commitments_sha256") != block_digest:
        return DisclosureCheck(False, "commitments_mismatch")
    if (
        receipt_sha256 is not None
        and disclosure.get("receipt_sha256") != receipt_sha256
    ):
        return DisclosureCheck(False, "receipt_mismatch")
    entries = disclosure.get("fields")
    if not isinstance(entries, list) or not entries:
        return DisclosureCheck(False, "malformed")
    results: list[DisclosedField] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            results.append(DisclosedField("?", None, False, "malformed"))
            continue
        label, name, value, salt_b64 = (
            entry.get("label"),
            entry.get("name"),
            entry.get("value"),
            entry.get("salt"),
        )
        if not all(isinstance(x, str) for x in (label, name, value, salt_b64)):
            results.append(DisclosedField(str(label), None, False, "malformed"))
            continue
        assert isinstance(label, str) and isinstance(name, str)
        assert isinstance(value, str) and isinstance(salt_b64, str)
        if label in seen:
            results.append(DisclosedField(label, name, False, "duplicate"))
            continue
        seen.add(label)
        if label not in fields:
            results.append(DisclosedField(label, name, False, "not_committed"))
            continue
        try:
            salt = base64.b64decode(salt_b64, validate=True)
            recomputed = field_commitment(label, name, value, salt)
        except (ValueError, CommitmentError):
            results.append(DisclosedField(label, name, False, "malformed"))
            continue
        if recomputed != fields[label]:
            results.append(DisclosedField(label, name, False, "value_mismatch"))
        else:
            results.append(DisclosedField(label, name, True, "matches"))
    ok = all(r.ok for r in results)
    return DisclosureCheck(ok, "matches" if ok else "mismatch", results)
