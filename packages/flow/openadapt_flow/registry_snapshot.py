"""One immutable, publisher-verified index occurrence for every consumer."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openadapt_flow import registry, registry_attest


def json_object(payload: bytes, label: str) -> dict[str, Any]:
    """Decode one unambiguous JSON object; signed bytes retain their identity."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise registry.RegistryError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise registry.RegistryError(f"{label} contains a non-finite JSON number")

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise registry.RegistryError(f"{label} contains a non-finite JSON number")
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=number,
        )
    except (ValueError, UnicodeError) as exc:
        raise registry.RegistryError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise registry.RegistryError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True)
class RegistrySnapshot:
    """Verified bytes, never a caller-mutable projection of an index.

    Entries and subjects are materialized afresh. This is transport provenance,
    not an execution, certification or instance-qualification capability.
    """

    index_url: str
    index_bytes: bytes
    attestation_bytes: Optional[bytes]
    attested_by: Optional[str]
    subject_items: tuple[tuple[str, str], ...]
    serial_key: Optional[str]

    @property
    def data(self) -> dict[str, Any]:
        return json_object(self.index_bytes, "index")

    @property
    def entries(self) -> list[registry.Entry]:
        return registry.validate_index(self.data)

    @property
    def subjects(self) -> dict[str, str]:
        return dict(self.subject_items)

    @property
    def index_sha256(self) -> str:
        return hashlib.sha256(self.index_bytes).hexdigest()

    def require_current(self, *, now: Optional[datetime] = None) -> None:
        if self.attested_by is None:
            return
        data = self.data
        problem = registry.index_freshness_problem(data, now=now)
        if problem is None and self.serial_key is not None:
            problem = registry.serial_problem(self.serial_key, data["serial"])
        if problem is not None:
            raise registry.RegistryError(problem)


def attestation_bytes(index_url: str) -> Optional[bytes]:
    location = registry._join(index_url, registry_attest.ATTESTATION_NAME)
    local = not registry._remote(index_url)
    if local and not Path(location).exists():
        return None
    try:
        return registry._read(
            location,
            limit=registry.MAX_ATTESTATION_BYTES,
            local_root=Path(index_url).resolve().parent if local else None,
        )
    except (registry.RegistryError, OSError) as exc:
        raise registry.RegistryError("the index's attestation cannot be read") from exc


def verify_snapshot(
    index_bytes: bytes,
    proof_bytes: Optional[bytes],
    *,
    index_url: str,
    allow_unsigned: bool = False,
    allow_test_key: bool = False,
    identity: Any = None,
    trusted_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> RegistrySnapshot:
    remote = registry._remote(index_url)
    if remote and not index_url.startswith("https://"):
        raise registry.RegistryError("plain http is refused; use https")
    for flag, used in (
        ("--allow-unsigned", allow_unsigned),
        ("--allow-test-key", allow_test_key),
    ):
        if used and remote:
            raise registry.RegistryError(
                f"{flag} applies only to an index on the local disk"
            )
    if len(index_bytes) > registry.MAX_FILE_BYTES:
        raise registry.RegistryError("index exceeds its byte limit")
    data = json_object(index_bytes, "index")
    entries = registry.validate_index(data)
    for entry in entries:
        for digest in entry.files.values():
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise registry.RegistryError("an indexed file has an invalid sha256")
    attested_by = None
    subjects: dict[str, str] = {}
    serial_key = None
    if proof_bytes is None:
        if remote or not allow_unsigned:
            raise registry.RegistryError("the index has no attestation")
    else:
        if len(proof_bytes) > registry.MAX_ATTESTATION_BYTES:
            raise registry.RegistryError("attestation exceeds its byte limit")
        proof = json_object(proof_bytes, "attestation")
        pinned = identity or registry_attest.REGISTRY_IDENTITY
        try:
            attested_by, subjects = registry_attest.verify(
                proof,
                allow_test_key=allow_test_key and not remote,
                identity=pinned,
                trusted_root=trusted_root,
            )
        except registry_attest.AttestationError as exc:
            raise registry.RegistryError(str(exc)) from exc
        if (
            subjects.get(registry_attest.INDEX_SUBJECT)
            != hashlib.sha256(index_bytes).hexdigest()
        ):
            raise registry.RegistryError("the attestation does not name this index")
        serial_key = registry.identity_key(pinned, attested_by)
    snapshot = RegistrySnapshot(
        index_url,
        index_bytes,
        proof_bytes,
        attested_by,
        tuple(sorted(subjects.items())),
        serial_key,
    )
    snapshot.require_current(now=now)
    if serial_key is not None:
        # A listing has seen this publisher occurrence too; changing consumer
        # or mirror cannot reset the publisher's high-water mark.
        registry.record_serial(serial_key, data["serial"])
    return snapshot


def load_snapshot(
    index_url: str = registry.DEFAULT_INDEX_URL,
    *,
    allow_unsigned: bool = False,
    allow_test_key: bool = False,
    identity: Any = None,
    trusted_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> RegistrySnapshot:
    # Reject local-authoring flags before even fetching a remote index.
    if registry._remote(index_url) and (allow_unsigned or allow_test_key):
        flag = "--allow-unsigned" if allow_unsigned else "--allow-test-key"
        raise registry.RegistryError(
            f"{flag} applies only to an index on the local disk"
        )
    return verify_snapshot(
        registry._read(index_url),
        attestation_bytes(index_url),
        index_url=index_url,
        allow_unsigned=allow_unsigned,
        allow_test_key=allow_test_key,
        identity=identity,
        trusted_root=trusted_root,
        now=now,
    )
