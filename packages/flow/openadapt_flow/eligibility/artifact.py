"""Atomic practice-local eligibility evidence artifacts.

Each logical check is one atomically promoted transaction directory containing
the exact raw response, the exact practice-facing normalized record, and a
hash manifest.  A CSV is a derived index, never the source of truth.  Repeating
the same ``operation_id`` with identical content is idempotent; reusing it with
different content fails loud.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal, Mapping, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from openadapt_flow.eligibility.client import (
    ApplicationMode,
    EligibilityRequest,
    EligibilityResult,
    eligibility_request_sha256,
    parse_271,
)
from openadapt_flow.runtime.effects.document_hash import DocumentHashVerifier
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectVerdict,
    ValueExpr,
    Verdict,
)

RESULTS_CSV = "eligibility_results.csv"
TRANSACTIONS_DIR = "transactions"
BOUNDARY_FILE = "boundary.json"
_ENCRYPTED_SUFFIX = ".enc"
_AES_PREFIX = b"OAE1"

_CSV_COLUMNS = [
    "committed_at",
    "application_mode",
    "http_status",
    "payer",
    "payer_id",
    "member_id",
    "date_of_service",
    "network_code",
    "coverage_level_code",
    "time_qualifier_code",
    "procedure_code",
    "status",
    "plan_name",
    "copay",
    "coinsurance_percent",
    "deductible_total",
    "deductible_remaining",
    "out_of_pocket_total",
    "out_of_pocket_remaining",
    "service_type_codes",
    "source",
    "raw_271_sha256",
    "operation_id",
    "request_sha256",
]


class ArtifactEncryption(str, Enum):
    PLATFORM_VOLUME = "platform_volume"
    APPLICATION_AES256_GCM = "application_aes256_gcm"


class PracticeArtifactPolicy(BaseModel):
    """Explicit PHI storage, encryption, retention, and egress boundary."""

    boundary_id: str
    application_mode: ApplicationMode
    encryption: ArtifactEncryption
    retention_days: int = Field(ge=1, le=3650)
    egress: Literal["none"] = "none"
    phi_storage_allowed: bool = True
    volume_encryption_attested: bool = False
    encryption_key_env: Optional[str] = None

    @field_validator("boundary_id", "encryption_key_env")
    @classmethod
    def _safe_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (
            not value
            or len(value) > 128
            or any(
                ch
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                for ch in value
            )
        ):
            raise ValueError("boundary identifiers must be non-PHI operational names")
        return value

    @model_validator(mode="after")
    def _encryption_contract(self) -> "PracticeArtifactPolicy":
        if not self.phi_storage_allowed:
            raise ValueError(
                "eligibility artifacts require an explicit PHI storage boundary"
            )
        if self.encryption is ArtifactEncryption.PLATFORM_VOLUME:
            if not self.volume_encryption_attested:
                raise ValueError("platform-volume mode requires encryption attestation")
            if self.encryption_key_env is not None:
                raise ValueError("platform-volume mode does not use an application key")
        elif not self.encryption_key_env:
            raise ValueError(
                "application encryption requires an environment key reference"
            )
        return self


class EligibilityArtifact(BaseModel):
    artifact_dir: str
    transaction_dir: str
    results_csv: str
    raw_271_file: str
    normalized_file: str
    raw_271_sha256: str
    normalized_sha256: str
    application_mode: ApplicationMode
    committed_at: str
    created: bool
    effects: list[Effect] = Field(default_factory=list)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ensure_secure_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("artifact root must be a regular directory, not a link")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError(
                "existing artifact directories must already be owner-only"
            )
    else:
        path.mkdir(parents=True, mode=0o700)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise PermissionError("artifact root is not owner-only")


def _require_secure_existing_dir(path: Path, *, label: str) -> None:
    """Refuse a committed directory whose type or owner-only mode drifted."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} is not a regular directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"{label} directory must remain owner-only")


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short protected artifact write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("artifact is not a regular file")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise PermissionError("artifact files must remain owner-only")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _policy_payload(policy: PracticeArtifactPolicy) -> bytes:
    return _canonical(
        {
            "schema_version": 2,
            "boundary_id": policy.boundary_id,
            "application_mode": policy.application_mode.value,
            "encryption": policy.encryption.value,
            "retention_days": policy.retention_days,
            "egress": policy.egress,
            "phi_storage_allowed": policy.phi_storage_allowed,
        }
    )


def _bind_policy(root: Path, policy: PracticeArtifactPolicy) -> None:
    expected = _policy_payload(policy)
    marker = root / BOUNDARY_FILE
    if marker.exists() or marker.is_symlink():
        if _read_regular(marker) != expected:
            raise ValueError("artifact root is bound to a different PHI policy")
        return
    _write_exclusive(marker, expected)
    _fsync_dir(root)


def _key(
    policy: PracticeArtifactPolicy, env: Optional[Mapping[str, str]]
) -> Optional[bytes]:
    if policy.encryption is ArtifactEncryption.PLATFORM_VOLUME:
        return None
    source = os.environ if env is None else env
    encoded = source.get(policy.encryption_key_env or "", "")
    try:
        key = base64.urlsafe_b64decode(encoded.encode())
    except Exception as exc:  # noqa: BLE001 - error is deliberately secret-free
        raise ValueError("application encryption key is not valid base64") from exc
    if len(key) != 32:
        raise ValueError("application encryption key must decode to 32 bytes")
    return key


def _protect(payload: bytes, *, key: Optional[bytes], aad: bytes) -> bytes:
    if key is None:
        return payload
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return _AES_PREFIX + nonce + AESGCM(key).encrypt(nonce, payload, aad)


def _unprotect(payload: bytes, *, key: Optional[bytes], aad: bytes) -> bytes:
    if key is None:
        return payload
    if len(payload) < 16 or payload[:4] != _AES_PREFIX:
        raise ValueError("encrypted artifact envelope is malformed")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(payload[4:16], payload[16:], aad)


@contextmanager
def _artifact_lock(root: Path) -> Iterator[None]:
    lock = root / ".eligibility-write.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BlockingIOError(
            "another eligibility artifact writer holds the lock"
        ) from exc
    try:
        yield
    finally:
        lock.rmdir()


def _transaction_id(policy: PracticeArtifactPolicy, operation_id: str) -> str:
    return hashlib.sha256(f"{policy.boundary_id}:{operation_id}".encode()).hexdigest()[
        :24
    ]


def _normalized_payload(
    result: EligibilityResult,
    *,
    request: EligibilityRequest,
    committed_at: str,
) -> dict[str, object]:
    selection = request.benefit_selection
    return {
        "schema_version": 3,
        "operation_id": result.operation_id,
        "committed_at": committed_at,
        "application_mode": result.application_mode.value
        if result.application_mode is not None
        else "",
        "http_status": result.http_status,
        "payer": result.payer_name or "",
        "payer_id": result.payer_id,
        "member_id": request.member_id or "",
        "date_of_service": request.date_of_service,
        "network_code": selection.network_code or "",
        "coverage_level_code": selection.coverage_level_code or "",
        "time_qualifier_code": selection.time_qualifier_code or "",
        "procedure_code": selection.procedure_code or "",
        "status": result.status.value,
        "plan_name": result.plan_name or "",
        "plan_begin": result.plan_begin or "",
        "plan_end": result.plan_end or "",
        "coverage_by_service": {
            k: v.value for k, v in result.coverage_by_service.items()
        },
        "benefits": [benefit.model_dump(mode="json") for benefit in result.benefits],
        "copay": result.copay or "",
        "coinsurance_percent": result.coinsurance_percent or "",
        "deductible_total": result.deductible_total or "",
        "deductible_remaining": result.deductible_remaining or "",
        "out_of_pocket_total": result.out_of_pocket_total or "",
        "out_of_pocket_remaining": result.out_of_pocket_remaining or "",
        "service_type_codes": list(result.service_type_codes),
        "source": result.source,
        "raw_271_sha256": result.raw_271_sha256,
        "request_sha256": result.request_sha256,
        "response_subject_sha256": result.response_subject_sha256,
    }


def _semantic_result_payload(result: EligibilityResult) -> bytes:
    """Canonical result meaning, excluding only observation-time metadata."""
    return _canonical(
        result.model_dump(
            mode="json",
            exclude={"checked_at", "attempt_count", "raw_271", "raw_271_bytes"},
        )
    )


def _safe_csv(value: object) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _csv_payload(rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for row in rows:
        csv_row: dict[str, str] = {}
        for column in _CSV_COLUMNS:
            value = row.get(column, "")
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            csv_row[column] = _safe_csv(value)
        writer.writerow(csv_row)
    return stream.getvalue().encode()


def _atomic_replace(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("refusing to replace a symlinked artifact index")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_exclusive(temporary, payload)
    try:
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _effect(name: str, digest: str, probe: str) -> list[Effect]:
    match = {"name": ValueExpr(literal=name)}
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            probe=f"exactly one {probe}",
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="sha256",
            value=ValueExpr(literal=digest),
            probe=f"{probe} storage bytes match the committed digest",
        ),
    ]


def _artifact_from_manifest(
    root: Path, tx_dir: Path, manifest: dict[str, object], *, created: bool
) -> EligibilityArtifact:
    raw_name = str(manifest["raw_file"])
    normalized_name = str(manifest["normalized_file"])
    effects = _effect(raw_name, str(manifest["raw_storage_sha256"]), "raw 271")
    effects += _effect(
        normalized_name,
        str(manifest["normalized_storage_sha256"]),
        "normalized eligibility record",
    )
    return EligibilityArtifact(
        artifact_dir=str(root),
        transaction_dir=str(tx_dir),
        results_csv=str(root / str(manifest["results_index"])),
        raw_271_file=str(tx_dir / raw_name),
        normalized_file=str(tx_dir / normalized_name),
        raw_271_sha256=str(manifest["raw_plain_sha256"]),
        normalized_sha256=str(manifest["normalized_plain_sha256"]),
        application_mode=ApplicationMode(str(manifest["application_mode"])),
        committed_at=str(manifest["committed_at"]),
        created=created,
        effects=effects,
    )


def _utc_timestamp(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"eligibility transaction {field} is malformed") from exc
    return parsed


def _load_manifest(
    tx_dir: Path,
    policy: Optional[PracticeArtifactPolicy] = None,
    *,
    transaction_id: Optional[str] = None,
) -> dict[str, object]:
    raw = json.loads(_read_regular(tx_dir / "manifest.json"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 3:
        raise ValueError("eligibility transaction manifest is malformed")
    tx_id = transaction_id or tx_dir.name.removeprefix("tx_")
    suffix = (
        _ENCRYPTED_SUFFIX
        if policy is not None
        and policy.encryption is ArtifactEncryption.APPLICATION_AES256_GCM
        else ""
    )
    expected = {
        "raw_file": f"raw_271_{tx_id}.json{suffix}",
        "normalized_file": f"result_{tx_id}.json{suffix}",
        "results_index": RESULTS_CSV + suffix,
    }
    if not re.fullmatch(r"[0-9a-f]{24}", tx_id):
        raise ValueError("eligibility transaction directory name is malformed")
    if policy is not None and raw.get("boundary_id") != policy.boundary_id:
        raise ValueError("eligibility transaction escaped its PHI boundary")
    if (
        policy is not None
        and raw.get("application_mode") != policy.application_mode.value
    ):
        raise ValueError("eligibility transaction application mode is malformed")
    if not isinstance(raw.get("http_status"), int) or not (
        200 <= int(raw["http_status"]) < 300
    ):
        raise ValueError("eligibility transaction HTTP status is malformed")
    committed_at = _utc_timestamp(raw.get("committed_at", ""), field="commit time")
    retention_expires_at = _utc_timestamp(
        raw.get("retention_expires_at", ""), field="retention expiry"
    )
    if retention_expires_at <= committed_at:
        raise ValueError("eligibility transaction retention interval is malformed")
    if policy is not None and retention_expires_at != (
        committed_at + timedelta(days=policy.retention_days)
    ):
        raise ValueError(
            "eligibility transaction retention expiry does not match its bound policy"
        )
    for field, value in expected.items():
        if raw.get(field) != value:
            raise ValueError(f"eligibility transaction {field} is malformed")
    for field in (
        "raw_plain_sha256",
        "raw_storage_sha256",
        "normalized_plain_sha256",
        "normalized_storage_sha256",
        "request_sha256",
        "response_subject_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(raw.get(field, ""))):
            raise ValueError(f"eligibility transaction {field} is malformed")
    if raw.get("egress") != "none":
        raise ValueError("eligibility transaction egress policy is malformed")
    return raw


def _verified_transaction_row(
    tx_dir: Path,
    policy: PracticeArtifactPolicy,
    key: Optional[bytes],
    *,
    transaction_id: Optional[str] = None,
    expected_manifest: Optional[dict[str, object]] = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Re-read and verify every byte that defines one consumable result."""
    _require_secure_existing_dir(tx_dir, label="transaction")
    manifest = _load_manifest(tx_dir, policy, transaction_id=transaction_id)
    if expected_manifest is not None and manifest != expected_manifest:
        raise ValueError("staged eligibility manifest does not match its contract")
    final_tx_name = f"tx_{transaction_id}" if transaction_id else tx_dir.name
    raw_name = str(manifest["raw_file"])
    raw_stored = _read_regular(tx_dir / raw_name)
    if _sha(raw_stored) != manifest["raw_storage_sha256"]:
        raise ValueError("raw artifact fails its committed storage digest")
    raw_aad = f"{policy.boundary_id}:{final_tx_name}:{raw_name}".encode()
    raw_plain = _unprotect(raw_stored, key=key, aad=raw_aad)
    if _sha(raw_plain) != manifest["raw_plain_sha256"]:
        raise ValueError("raw artifact fails its committed plaintext digest")

    normalized_name = str(manifest["normalized_file"])
    normalized_stored = _read_regular(tx_dir / normalized_name)
    if _sha(normalized_stored) != manifest["normalized_storage_sha256"]:
        raise ValueError("normalized artifact fails its committed storage digest")
    normalized_aad = f"{policy.boundary_id}:{final_tx_name}:{normalized_name}".encode()
    normalized_plain = _unprotect(normalized_stored, key=key, aad=normalized_aad)
    if _sha(normalized_plain) != manifest["normalized_plain_sha256"]:
        raise ValueError("normalized artifact fails its committed digest")
    parsed = json.loads(normalized_plain)
    if not isinstance(parsed, dict):
        raise ValueError("normalized artifact is not an object")
    expected_fields = {
        "operation_id": manifest.get("operation_id"),
        "request_sha256": manifest.get("request_sha256"),
        "response_subject_sha256": manifest.get("response_subject_sha256"),
        "application_mode": manifest.get("application_mode"),
        "http_status": manifest.get("http_status"),
        "committed_at": manifest.get("committed_at"),
        "raw_271_sha256": manifest.get("raw_plain_sha256"),
    }
    for field, expected in expected_fields.items():
        if parsed.get(field) != expected:
            raise ValueError(f"normalized artifact {field} does not match its manifest")
    return manifest, parsed


def _index_material(
    root: Path,
    policy: PracticeArtifactPolicy,
    key: Optional[bytes],
    *,
    now: datetime,
    excluded: frozenset[Path] = frozenset(),
    staged: Optional[tuple[Path, str, dict[str, object]]] = None,
) -> tuple[str, bytes, bytes]:
    rows: list[dict[str, object]] = []
    transactions = root / TRANSACTIONS_DIR
    for tx_dir in sorted(transactions.iterdir()):
        if tx_dir in excluded or tx_dir.name.startswith("."):
            continue
        manifest, parsed = _verified_transaction_row(tx_dir, policy, key)
        expires = _utc_timestamp(
            manifest["retention_expires_at"], field="retention expiry"
        )
        if expires <= now:
            raise ValueError(
                "expired eligibility transaction is not consumable; purge it first"
            )
        rows.append(parsed)
    if staged is not None:
        stage, transaction_id, expected_manifest = staged
        manifest, parsed = _verified_transaction_row(
            stage,
            policy,
            key,
            transaction_id=transaction_id,
            expected_manifest=expected_manifest,
        )
        expires = _utc_timestamp(
            manifest["retention_expires_at"], field="retention expiry"
        )
        if expires <= now:
            raise ValueError("new eligibility transaction is already expired")
        rows.append(parsed)
    plain_csv = _csv_payload(rows)
    name = RESULTS_CSV + (_ENCRYPTED_SUFFIX if key is not None else "")
    payload = _protect(
        plain_csv,
        key=key,
        aad=f"{policy.boundary_id}:index:{name}".encode(),
    )
    return name, plain_csv, payload


def _stage_verified_index(
    root: Path,
    policy: PracticeArtifactPolicy,
    key: Optional[bytes],
    *,
    name: str,
    plain_csv: bytes,
    payload: bytes,
) -> Path:
    target = root / name
    if target.is_symlink():
        raise ValueError("refusing to replace a symlinked artifact index")
    if target.exists():
        _read_regular(target)
    temporary = root / f".{name}.{uuid4().hex}.tmp"
    try:
        _write_exclusive(temporary, payload)
        staged = _read_regular(temporary)
        if staged != payload:
            raise ValueError("staged eligibility index bytes changed during write")
        observed_plain = _unprotect(
            staged,
            key=key,
            aad=f"{policy.boundary_id}:index:{name}".encode(),
        )
        if observed_plain != plain_csv:
            raise ValueError("staged eligibility index fails plaintext verification")
        return temporary
    except Exception:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        raise


def _promote_staged_index(temporary: Path, target: Path) -> None:
    if target.is_symlink():
        raise ValueError("refusing to replace a symlinked artifact index")
    os.replace(temporary, target)
    _fsync_dir(target.parent)


def _restore_index(target: Path, previous: Optional[bytes]) -> None:
    """Restore the pre-transaction index after a failed two-path promotion."""
    if previous is None:
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                raise ValueError("cannot roll back a symlinked artifact index")
            target.unlink()
            _fsync_dir(target.parent)
        return
    _atomic_replace(target, previous)


def _rebuild_index(
    root: Path,
    policy: PracticeArtifactPolicy,
    key: Optional[bytes],
    *,
    now: Optional[datetime] = None,
) -> str:
    effective_now = _effective_now(now)
    name, plain_csv, payload = _index_material(root, policy, key, now=effective_now)
    index_target = root / name
    temporary = _stage_verified_index(
        root,
        policy,
        key,
        name=name,
        plain_csv=plain_csv,
        payload=payload,
    )
    try:
        _promote_staged_index(temporary, index_target)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return name


def _effective_now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("eligibility retention time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _purge_expired_locked(
    root: Path,
    policy: PracticeArtifactPolicy,
    key: Optional[bytes],
    *,
    now: datetime,
) -> list[str]:
    """Hide expired transactions, publish a verified index, then delete them."""
    transactions = root / TRANSACTIONS_DIR
    expired: list[tuple[Path, str]] = []
    for tx_dir in sorted(transactions.iterdir()):
        if tx_dir.name.startswith("."):
            continue
        _require_secure_existing_dir(tx_dir, label="transaction")
        manifest = _load_manifest(tx_dir, policy)
        expires = _utc_timestamp(
            manifest["retention_expires_at"], field="retention expiry"
        )
        if expires <= now:
            expired.append((tx_dir, str(manifest["operation_id"])))
    if not expired:
        return []

    excluded = frozenset(path for path, _operation_id in expired)
    name, plain_csv, payload = _index_material(
        root, policy, key, now=now, excluded=excluded
    )
    index_target = root / name
    previous_index = (
        _read_regular(index_target)
        if index_target.exists() or index_target.is_symlink()
        else None
    )
    temporary = _stage_verified_index(
        root,
        policy,
        key,
        name=name,
        plain_csv=plain_csv,
        payload=payload,
    )
    quarantined: list[tuple[Path, Path]] = []
    try:
        for original, _operation_id in expired:
            quarantine = transactions / f".expired-{original.name}-{uuid4().hex}"
            os.rename(original, quarantine)
            quarantined.append((original, quarantine))
        _fsync_dir(transactions)
        _promote_staged_index(temporary, index_target)
    except Exception:
        index_changed = not temporary.exists()
        for original, quarantine in reversed(quarantined):
            if quarantine.exists() and not quarantine.is_symlink():
                os.rename(quarantine, original)
        _fsync_dir(transactions)
        if index_changed:
            _restore_index(index_target, previous_index)
        raise
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()

    cleanup_errors: list[str] = []
    for _original, quarantine in quarantined:
        try:
            _require_secure_existing_dir(quarantine, label="expired transaction")
            shutil.rmtree(quarantine)
        except OSError as exc:
            cleanup_errors.append(type(exc).__name__)
    _fsync_dir(transactions)
    if cleanup_errors:
        raise RuntimeError(
            "expired eligibility data was deactivated but secure deletion failed: "
            + ",".join(cleanup_errors)
        )
    return [operation_id for _path, operation_id in expired]


def purge_expired_eligibility_artifacts(
    artifact_dir: Union[str, Path],
    *,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> list[str]:
    """Purge expired PHI transactions and atomically remove them from the index.

    A malformed manifest, insecure path, missing encryption key, or failed
    index verification denies the purge before any transaction is hidden.
    """
    root = Path(artifact_dir)
    if not root.exists() and not root.is_symlink():
        return []
    _ensure_secure_dir(root)
    _bind_policy(root, policy)
    key = _key(policy, env)
    transactions = root / TRANSACTIONS_DIR
    _ensure_secure_dir(transactions)
    effective_now = _effective_now(now)
    with _artifact_lock(root):
        return _purge_expired_locked(root, policy, key, now=effective_now)


def write_eligibility_artifacts(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    request: EligibilityRequest,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]] = None,
) -> EligibilityArtifact:
    """Atomically promote a PHI-bearing raw+normalized transaction."""
    if result.raw_271_bytes is None or result.raw_271_sha256 is None:
        raise ValueError(
            "a raw response is required for a consumable eligibility artifact"
        )
    if result.application_mode is None or result.http_status is None:
        raise ValueError("eligibility result lacks its response boundary metadata")
    if not result.is_answer:
        raise ValueError(
            "only an exact unambiguous eligibility answer may be promoted as consumable"
        )
    request_sha256 = eligibility_request_sha256(request)
    if (
        result.operation_id != request.operation_id
        or result.payer_id != request.payer_id
        or result.service_type_codes != request.service_type_codes
        or result.request_sha256 != request_sha256
    ):
        raise ValueError("eligibility result is not bound to the supplied request")
    if _sha(result.raw_271_bytes) != result.raw_271_sha256:
        raise ValueError("raw eligibility bytes do not match the wire digest")
    if result.application_mode is not policy.application_mode:
        raise ValueError(
            "eligibility result application mode does not match the artifact policy"
        )
    reparsed = parse_271(
        request,
        result.raw_271_bytes,
        http_status=result.http_status,
        expected_mode=policy.application_mode,
    )
    if not reparsed.is_answer:
        raise ValueError("raw eligibility response is not a consumable answer")
    if _semantic_result_payload(reparsed) != _semantic_result_payload(result):
        raise ValueError(
            "normalized eligibility result does not match the exact raw response"
        )
    root = Path(artifact_dir)
    _ensure_secure_dir(root)
    _bind_policy(root, policy)
    key = _key(policy, env)
    effective_now = _effective_now(None)
    transactions = root / TRANSACTIONS_DIR
    _ensure_secure_dir(transactions)
    tx_id = _transaction_id(policy, result.operation_id)
    tx_dir = transactions / f"tx_{tx_id}"
    raw_plain = result.raw_271_bytes
    suffix = _ENCRYPTED_SUFFIX if key is not None else ""
    raw_name = f"raw_271_{tx_id}.json{suffix}"
    normalized_name = f"result_{tx_id}.json{suffix}"

    with _artifact_lock(root):
        _purge_expired_locked(root, policy, key, now=effective_now)
        if tx_dir.exists() or tx_dir.is_symlink():
            _require_secure_existing_dir(tx_dir, label="idempotency target")
            manifest = _load_manifest(tx_dir, policy)
            normalized_plain = _canonical(
                _normalized_payload(
                    reparsed,
                    request=request,
                    committed_at=str(manifest["committed_at"]),
                )
            )
            if (
                manifest.get("operation_id") != result.operation_id
                or manifest.get("request_sha256") != request_sha256
                or manifest.get("raw_plain_sha256") != _sha(raw_plain)
                or manifest.get("normalized_plain_sha256") != _sha(normalized_plain)
            ):
                raise FileExistsError(
                    "operation_id was already committed with different content"
                )
            _rebuild_index(root, policy, key, now=effective_now)
            return _artifact_from_manifest(root, tx_dir, manifest, created=False)

        stage = transactions / f".staging-{uuid4().hex}"
        stage.mkdir(mode=0o700)
        staged_index: Optional[Path] = None
        index_target: Optional[Path] = None
        previous_index: Optional[bytes] = None
        promotion_started = False
        tx_promoted = False
        try:
            committed_at = effective_now.strftime("%Y-%m-%dT%H:%M:%SZ")
            normalized_plain = _canonical(
                _normalized_payload(
                    reparsed, request=request, committed_at=committed_at
                )
            )
            raw_aad = f"{policy.boundary_id}:{tx_dir.name}:{raw_name}".encode()
            normalized_aad = (
                f"{policy.boundary_id}:{tx_dir.name}:{normalized_name}".encode()
            )
            raw_stored = _protect(raw_plain, key=key, aad=raw_aad)
            normalized_stored = _protect(normalized_plain, key=key, aad=normalized_aad)
            _write_exclusive(stage / raw_name, raw_stored)
            _write_exclusive(stage / normalized_name, normalized_stored)
            expiry = (effective_now + timedelta(days=policy.retention_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            index_name = RESULTS_CSV + (_ENCRYPTED_SUFFIX if key is not None else "")
            committed_manifest: dict[str, object] = {
                "schema_version": 3,
                "operation_id": result.operation_id,
                "request_sha256": request_sha256,
                "response_subject_sha256": result.response_subject_sha256,
                "boundary_id": policy.boundary_id,
                "application_mode": policy.application_mode.value,
                "http_status": reparsed.http_status,
                "committed_at": committed_at,
                "retention_expires_at": expiry,
                "egress": "none",
                "raw_file": raw_name,
                "raw_plain_sha256": _sha(raw_plain),
                "raw_storage_sha256": _sha(raw_stored),
                "normalized_file": normalized_name,
                "normalized_plain_sha256": _sha(normalized_plain),
                "normalized_storage_sha256": _sha(normalized_stored),
                "results_index": index_name,
            }
            _write_exclusive(stage / "manifest.json", _canonical(committed_manifest))
            _fsync_dir(stage)

            _manifest, staged_row = _verified_transaction_row(
                stage,
                policy,
                key,
                transaction_id=tx_id,
                expected_manifest=committed_manifest,
            )
            if _canonical(staged_row) != normalized_plain:
                raise ValueError(
                    "staged normalized eligibility record changed during verification"
                )
            name, plain_csv, index_payload = _index_material(
                root,
                policy,
                key,
                now=effective_now,
                staged=(stage, tx_id, committed_manifest),
            )
            if name != index_name:
                raise ValueError("staged eligibility index name changed")
            staged_index = _stage_verified_index(
                root,
                policy,
                key,
                name=name,
                plain_csv=plain_csv,
                payload=index_payload,
            )
            index_target = root / name
            if index_target.exists() or index_target.is_symlink():
                previous_index = _read_regular(index_target)

            promotion_started = True
            os.rename(stage, tx_dir)
            tx_promoted = True
            _fsync_dir(transactions)
            _promote_staged_index(staged_index, index_target)
        except Exception:
            index_changed = staged_index is not None and not staged_index.exists()
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            if tx_promoted and tx_dir.exists() and not tx_dir.is_symlink():
                _require_secure_existing_dir(tx_dir, label="failed transaction")
                shutil.rmtree(tx_dir)
                _fsync_dir(transactions)
            if promotion_started and index_changed and index_target is not None:
                _restore_index(index_target, previous_index)
            raise
        finally:
            if staged_index is not None and staged_index.exists():
                if staged_index.is_symlink():
                    raise ValueError("staged eligibility index became a symlink")
                staged_index.unlink()
        return _artifact_from_manifest(root, tx_dir, committed_manifest, created=True)


def _rollback_created_artifact(
    artifact: EligibilityArtifact,
    *,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]],
) -> None:
    """Remove a newly promoted transaction if independent verification fails."""
    root = Path(artifact.artifact_dir)
    _ensure_secure_dir(root)
    _bind_policy(root, policy)
    key = _key(policy, env)
    transactions = root / TRANSACTIONS_DIR
    _require_secure_existing_dir(transactions, label="transactions")
    tx_dir = Path(artifact.transaction_dir)
    if tx_dir.parent != transactions or not re.fullmatch(
        r"tx_[0-9a-f]{24}", tx_dir.name
    ):
        raise ValueError("refusing to roll back an unexpected transaction path")
    effective_now = _effective_now(None)
    with _artifact_lock(root):
        if tx_dir.exists() or tx_dir.is_symlink():
            _require_secure_existing_dir(tx_dir, label="failed transaction")
            shutil.rmtree(tx_dir)
            _fsync_dir(transactions)
        _purge_expired_locked(root, policy, key, now=effective_now)
        _rebuild_index(root, policy, key, now=effective_now)


def write_and_verify(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    request: EligibilityRequest,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[EligibilityArtifact, list[EffectVerdict]]:
    root = Path(artifact_dir)
    artifact = write_eligibility_artifacts(
        result,
        root,
        request=request,
        policy=policy,
        env=env,
    )
    try:
        # Re-open without following links before consulting the independent
        # generic verifier. A newly created transaction is rolled back if any
        # verification step fails, so a failed result never remains consumable.
        _read_regular(Path(artifact.raw_271_file))
        _read_regular(Path(artifact.normalized_file))
        verifier = DocumentHashVerifier(root, glob=f"{TRANSACTIONS_DIR}/tx_*/*")
        before = verifier.capture_pre_state()
        verdicts = [verifier.verify(effect, before) for effect in artifact.effects]
        if not all_confirmed(verdicts):
            raise RuntimeError(
                "eligibility artifact effect verification did not confirm"
            )
        return artifact, verdicts
    except Exception:
        if artifact.created:
            _rollback_created_artifact(artifact, policy=policy, env=env)
        raise


def all_confirmed(verdicts: list[EffectVerdict]) -> bool:
    return bool(verdicts) and all(v.verdict is Verdict.CONFIRMED for v in verdicts)
