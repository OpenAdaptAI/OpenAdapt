"""Signed, resumable retention of an explicitly bound sealed run.

Preparation is an explicit local attestation, not an independent timestamp.
Its current signed time starts the policy interval. Raw payload unlinking is
not a claim of media sanitization, SSD erasure, or regulatory compliance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Sequence

from openadapt_types.seal import SealPublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from openadapt_flow import crypto, sealed_set
from openadapt_flow.offline_seal import (
    LocalSealV1,
    loads_strict,
    seal_sha256,
    sign_subject,
    verify_local_seal,
)
from openadapt_flow.receipt import RunReceipt
from openadapt_flow.retention import (
    RetentionError,
    RetentionPlan,
    RetentionPolicy,
    RetentionTarget,
    _iso,
    _root,
    _snapshot,
    _time,
    _tree,
    plan_retention,
)
from openadapt_flow.seal_keys import SealSigner
from openadapt_flow.sealed_maintenance import mutation_lock, rename_claim

_DIGEST = r"^[a-f0-9]{64}$"
_AAD = b"openadapt-flow/retention-authorization/v1"
_CLAIM_PREFIX = ".openadapt-retention-"


class RetentionReceipt(BaseModel):
    """Closed shareable projection: no paths, raw ids or recorded content."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["openadapt.retention-receipt/v1"] = (
        "openadapt.retention-receipt/v1"
    )
    run: RunReceipt
    runtime_id_sha256: StrictStr = Field(pattern=_DIGEST)
    report_sha256: StrictStr = Field(pattern=_DIGEST)
    source_seal_sha256: StrictStr = Field(pattern=_DIGEST)
    source_seal_file_sha256: StrictStr = Field(pattern=_DIGEST)
    header_sha256: StrictStr = Field(pattern=_DIGEST)
    set_digest: StrictStr = Field(pattern=_DIGEST)
    authorization_sha256: StrictStr = Field(pattern=_DIGEST)
    raw_days: StrictInt = Field(ge=0)
    audit_days: StrictInt = Field(ge=0)
    prepared_at: StrictStr
    eligible_at: StrictStr
    audit_until: StrictStr
    raw_files: StrictInt = Field(ge=0)
    raw_bytes: StrictInt = Field(ge=0)

    def canonical_json(self) -> bytes:
        return _json(self.model_dump(mode="json"))


class RetentionCapsule(BaseModel):
    """Public receipt/signature plus encrypted private deletion authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["openadapt.retention-capsule/v1"] = (
        "openadapt.retention-capsule/v1"
    )
    receipt: RetentionReceipt
    seal: LocalSealV1
    authorization: StrictStr

    def canonical_json(self) -> bytes:
        return _json(self.model_dump(mode="json", by_alias=True))


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: StrictStr
    sha256: StrictStr = Field(pattern=_DIGEST)
    size: StrictInt = Field(ge=0)
    device: StrictInt = Field(ge=0)
    inode: StrictInt = Field(ge=0)

    def snapshot(self) -> RetentionTarget:
        return RetentionTarget(**self.model_dump())


class _Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: StrictStr
    device: StrictInt = Field(ge=0)
    inode: StrictInt = Field(ge=0)
    targets: list[_Target]
    source_seal: StrictStr


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _clock(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise RetentionError("naive_current_time")
    # LocalSeal signs whole seconds. Rounding UP preserves the full policy
    # interval; truncation must not make eligibility up to a second early.
    current = now.astimezone(timezone.utc)
    return current.replace(microsecond=0) + (
        timedelta(seconds=1) if current.microsecond else timedelta()
    )


def prepare_retention(
    plan: RetentionPlan,
    registry: Sequence[SealPublicKey],
    *,
    key: str,
    protection_key: str,
    signer: SealSigner,
    now: datetime,
) -> RetentionCapsule:
    """Attest an exact frozen source now; never backdate age or write raw/capsule files."""
    if not isinstance(plan, RetentionPlan):
        raise RetentionError("invalid_retention_plan")
    if not signer or not isinstance(protection_key, str) or not protection_key:
        raise RetentionError("explicit_signer_and_protection_key_required")
    current = _clock(now)
    with mutation_lock(plan.source):
        if sealed_set.active_writer(plan.source) is not None:
            raise RetentionError("active_writer")
        checked = plan_retention(
            plan.source,
            plan.policy,
            plan.source_seal_bytes,
            registry,
            key=key,
            now=current,
        )
        if any(
            getattr(checked, name) != getattr(plan, name)
            for name in (
                "source_device",
                "source_inode",
                "header_sha256",
                "set_digest",
                "report_sha256",
                "source_seal_sha256",
                "source_seal_file_sha256",
                "receipt",
                "targets",
            )
        ):
            raise RetentionError("stale_retention_plan")
        if any(
            PurePosixPath(item.relative_path).name.startswith(_CLAIM_PREFIX)
            for item in checked.targets
        ):
            raise RetentionError("reserved_retention_path")
        authorization = _Authorization(
            source=str(checked.source),
            device=checked.source_device,
            inode=checked.source_inode,
            targets=[_Target(**vars(item)) for item in checked.targets],
            source_seal=base64.b64encode(checked.source_seal_bytes).decode("ascii"),
        )
        encrypted = crypto.encrypt_bytes(
            _json(authorization.model_dump()), protection_key, aad=_AAD
        )
        source_seal = LocalSealV1.model_validate(
            loads_strict(checked.source_seal_bytes)
        )
        try:
            due = current + timedelta(days=checked.policy.raw_days)
            audit = current + timedelta(days=checked.policy.audit_days)
        except OverflowError as exc:
            raise RetentionError("invalid_retention_days") from exc
        raw = [
            item for item in checked.targets if item.relative_path != sealed_set.HEADER
        ]
        receipt = RetentionReceipt(
            run=checked.receipt,
            runtime_id_sha256=hashlib.sha256(
                source_seal.run_id.encode("utf-8")
            ).hexdigest(),
            report_sha256=checked.report_sha256,
            source_seal_sha256=checked.source_seal_sha256,
            source_seal_file_sha256=checked.source_seal_file_sha256,
            header_sha256=checked.header_sha256,
            set_digest=checked.set_digest,
            authorization_sha256=hashlib.sha256(encrypted).hexdigest(),
            raw_days=checked.policy.raw_days,
            audit_days=checked.policy.audit_days,
            prepared_at=_iso(current),
            eligible_at=_iso(due),
            audit_until=_iso(audit),
            raw_files=len(raw),
            raw_bytes=sum(item.size for item in raw),
        )
        signed = sign_subject(
            "file",
            hashlib.sha256(receipt.canonical_json()).hexdigest(),
            "retention-v1",
            now=current,
            signer=signer,
        )
        return RetentionCapsule(
            receipt=receipt,
            seal=signed,
            authorization=base64.b64encode(encrypted).decode("ascii"),
        )


def _checked_capsule(
    raw: bytes, registry: Sequence[SealPublicKey], key: str, now: datetime
) -> tuple[RetentionCapsule, _Authorization, bool]:
    if not isinstance(key, str) or not key:
        raise RetentionError("explicit_protection_key_required")
    current = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.utcoffset() is not None
        else None
    )
    if current is None:
        raise RetentionError("naive_current_time")
    try:
        capsule = RetentionCapsule.model_validate(loads_strict(raw))
        receipt = capsule.receipt
        result = verify_local_seal(
            capsule.seal.wire(),
            hashlib.sha256(receipt.canonical_json()).hexdigest(),
            registry,
        )
        if (
            not result.ok
            or capsule.seal.subject_kind != "file"
            or capsule.seal.run_id != "retention-v1"
        ):
            raise RetentionError("retention_signature_invalid")
        policy = RetentionPolicy(receipt.raw_days, receipt.audit_days)
        prepared = _time(receipt.prepared_at)
        if prepared != _time(capsule.seal.issued_at) or prepared > current:
            raise RetentionError("retention_time_invalid")
        if _time(receipt.eligible_at) != prepared + timedelta(
            days=policy.raw_days
        ) or _time(receipt.audit_until) != prepared + timedelta(days=policy.audit_days):
            raise RetentionError("retention_policy_mismatch")
        encrypted = base64.b64decode(capsule.authorization, validate=True)
        if hashlib.sha256(encrypted).hexdigest() != receipt.authorization_sha256:
            raise RetentionError("retention_authorization_mismatch")
        authorization = _Authorization.model_validate(
            loads_strict(crypto.decrypt_bytes(encrypted, key, aad=_AAD))
        )
        source_bytes = base64.b64decode(authorization.source_seal, validate=True)
        source_seal = LocalSealV1.model_validate(loads_strict(source_bytes))
        original = verify_local_seal(
            source_seal.wire(), receipt.report_sha256, registry
        )
        if (
            not original.ok
            or source_seal.subject_kind != "run-report"
            or source_seal.outcome != "VERIFIED"
        ):
            raise RetentionError("original_seal_invalid")
        if (
            seal_sha256(source_seal.wire()) != receipt.source_seal_sha256
            or hashlib.sha256(source_bytes).hexdigest()
            != receipt.source_seal_file_sha256
            or hashlib.sha256(source_seal.run_id.encode("utf-8")).hexdigest()
            != receipt.runtime_id_sha256
        ):
            raise RetentionError("original_seal_mismatch")
        if _time(source_seal.issued_at) > prepared:
            raise RetentionError("retention_time_invalid")
        paths = [item.relative_path for item in authorization.targets]
        if len(set(paths)) != len(paths) or paths.count(sealed_set.HEADER) != 1:
            raise RetentionError("invalid_retention_targets")
        for relative in paths:
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or relative != path.as_posix()
                or relative in ("", ".")
                or "\\" in relative
                or path.name.startswith(_CLAIM_PREFIX)
            ):
                raise RetentionError("invalid_retention_targets")
        header = next(
            item
            for item in authorization.targets
            if item.relative_path == sealed_set.HEADER
        )
        targets = [
            item
            for item in authorization.targets
            if item.relative_path != sealed_set.HEADER
        ]
        if (
            header.sha256 != receipt.header_sha256
            or len(targets) != receipt.raw_files
            or sum(item.size for item in targets) != receipt.raw_bytes
        ):
            raise RetentionError("retention_inventory_mismatch")
        return capsule, authorization, current >= _time(receipt.eligible_at)
    except RetentionError:
        raise
    except (TypeError, ValueError, OverflowError, crypto.CryptoError) as exc:
        raise RetentionError("invalid_retention_capsule") from exc


def _claim(relative: str, digest: str, index: int) -> str:
    return str(PurePosixPath(relative).with_name(f"{_CLAIM_PREFIX}{digest}-{index}"))


def _source(
    root: Path, authorization: _Authorization, digest: str, *, resume: bool
) -> list[tuple[_Target, str, str]]:
    info = root.stat()
    if str(root) != authorization.source or (info.st_dev, info.st_ino) != (
        authorization.device,
        authorization.inode,
    ):
        raise RetentionError("source_replaced")
    disk = _tree(root)
    allowed: set[str] = {sealed_set.HEADER}
    remaining: list[tuple[_Target, str, str]] = []
    for index, item in enumerate(authorization.targets):
        relative = item.relative_path
        if relative == sealed_set.HEADER:
            if (
                relative not in disk
                or _snapshot(root, relative, disk[relative]) != item.snapshot()
            ):
                raise RetentionError("source_header_changed")
            continue
        claimed = _claim(relative, digest, index)
        allowed.update((relative, claimed))
        present = [name for name in (relative, claimed) if name in disk]
        if len(present) > 1 or (not present and not resume):
            raise RetentionError("source_inventory_mismatch")
        if present:
            checked = _snapshot(root, present[0], disk[present[0]])
            if (checked.sha256, checked.size, checked.device, checked.inode) != (
                item.sha256,
                item.size,
                item.device,
                item.inode,
            ):
                raise RetentionError("source_changed")
            if present[0] == claimed and not resume:
                raise RetentionError("unbound_retention_claim")
            remaining.append((item, present[0], claimed))
    if set(disk) - allowed:
        raise RetentionError("source_inventory_mismatch")
    return remaining


def _read_existing(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            raise RetentionError("unsafe_audit_capsule")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RetentionError("unsafe_audit_capsule")
        return stream.read()


def _persist(path: Path, data: bytes) -> None:
    """No overwrite; a complete signed capsule is durable before any claim."""
    import secrets

    parent = _root(path.parent)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".retention-capsule-{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            path.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _unlink_claim(root_fd: int, item: _Target, present: str, claimed: str) -> None:
    """Claim by rename before hashing/unlinking; a raced replacement is kept."""
    parts = PurePosixPath(present).parts
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            os.close(parent_fd)
            parent_fd = child
        name = parts[-1]
        claim_name = PurePosixPath(claimed).name
        if name != claim_name:
            # The exclusive per-set lock serializes Flow writers/retention.
            # Refuse an existing claim rather than replace any other file.
            try:
                os.stat(claim_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RetentionError("retention_claim_exists")
            rename_claim(parent_fd, name, claim_name)
            os.fsync(parent_fd)
        fd = os.open(claim_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            actual = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
            after = os.fstat(stream.fileno())
        expected = (item.device, item.inode, item.size)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size) != expected
            or (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
            != (before.st_mtime_ns, before.st_ctime_ns, before.st_size)
            or actual.hexdigest() != item.sha256
        ):
            raise RetentionError("source_changed_after_claim")
        current = os.stat(claim_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RetentionError("source_changed_after_claim")
        os.unlink(claim_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def apply_retention(
    run_dir: Path | str,
    capsule_bytes: bytes,
    audit_path: Path | str,
    registry: Sequence[SealPublicKey],
    *,
    protection_key: str,
    now: datetime,
    commit: bool = False,
) -> dict[str, Any]:
    """Default dry-run; commit preserves the identical capsule before unlinking.

    The original sealed header remains unchanged, so ordinary readers refuse
    the incomplete raw set. An interrupted operation resumes only when its
    exact signed capsule already exists durably at the explicit audit path.
    No audit artifact or source directory is deleted here. POSIX descriptor
    operations and Linux no-replace rename are required; unsupported platforms refuse commit.
    """
    if type(commit) is not bool:
        raise RetentionError("explicit_commit_boolean_required")
    capsule, authorization, eligible = _checked_capsule(
        capsule_bytes, registry, protection_key, now
    )
    root = _root(run_dir)
    audit = Path(audit_path).absolute()
    _root(audit.parent)
    if audit == root or root in audit.parents or ".." in audit.parts:
        raise RetentionError("audit_must_be_outside_source")
    if sealed_set.is_sealed_path(audit):
        raise RetentionError("audit_inside_sealed_set")
    existing = _read_existing(audit)
    if existing is not None and existing != capsule_bytes:
        raise RetentionError("audit_capsule_conflict")
    digest = capsule.receipt.authorization_sha256
    remaining = _source(root, authorization, digest, resume=existing is not None)
    preview = {
        "dry_run": not commit,
        "eligible": eligible,
        "reason": "eligible" if eligible else "retention_not_elapsed",
        "eligible_at": capsule.receipt.eligible_at,
        "audit_until": capsule.receipt.audit_until,
        "would_delete": [item.relative_path for item, _, _ in remaining]
        if eligible
        else [],
        "remaining_files": len(remaining),
        "retained_header_sha256": capsule.receipt.header_sha256,
        "receipt_sha256": hashlib.sha256(capsule.receipt.canonical_json()).hexdigest(),
        "media_sanitization_claimed": False,
    }
    if not commit:
        return preview
    if not eligible:
        raise RetentionError("retention_not_elapsed")
    if (
        os.name != "posix"
        or sys.platform != "linux"
        or os.open not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise RetentionError("descriptor_retention_unsupported")
    with mutation_lock(root):
        if sealed_set.active_writer(root) is not None:
            raise RetentionError("active_writer")
        existing = _read_existing(audit)
        if existing is not None and existing != capsule_bytes:
            raise RetentionError("audit_capsule_conflict")
        remaining = _source(root, authorization, digest, resume=existing is not None)
        if existing is None:
            _persist(audit, capsule_bytes)
        # Re-fsync even a previously published capsule: a prior interrupted
        # publication may have linked the file before its directory fsync.
        audit_fd = os.open(audit, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            audit_info = os.fstat(audit_fd)
            if not stat.S_ISREG(audit_info.st_mode) or audit_info.st_nlink != 1:
                raise RetentionError("unsafe_audit_capsule")
            with os.fdopen(os.dup(audit_fd), "rb") as stream:
                if stream.read() != capsule_bytes:
                    raise RetentionError("audit_capsule_conflict")
            os.fsync(audit_fd)
            audit_current = audit.lstat()
            if (audit_current.st_dev, audit_current.st_ino) != (
                audit_info.st_dev,
                audit_info.st_ino,
            ):
                raise RetentionError("audit_capsule_replaced")
        finally:
            os.close(audit_fd)
        parent_fd = os.open(audit.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(root_fd)
            if (info.st_dev, info.st_ino) != (
                authorization.device,
                authorization.inode,
            ):
                raise RetentionError("source_replaced")
            for item, present, claimed in remaining:
                _unlink_claim(root_fd, item, present, claimed)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        if _source(root, authorization, digest, resume=True):
            raise RetentionError("retention_incomplete")
        sealed_set._forget(root)
    return {
        **preview,
        "remaining_files": 0,
        "removed_files": len(remaining),
        "completed": True,
    }
