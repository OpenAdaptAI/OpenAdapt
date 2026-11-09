"""Plan retention from exact signed VERIFIED evidence without changing files.

The policy is supplied by the operator; durations are not a legal-compliance
preset. Planning uses explicit keys and pinned signing keys. It never loads a
local private key, signs a statement, or removes an artifact.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from openadapt_types.seal import SealPublicKey

from openadapt_flow import crypto, sealed_set
from openadapt_flow.ir import RunReport
from openadapt_flow.offline_seal import (
    LocalSealV1,
    loads_strict,
    seal_sha256,
    verify_local_seal,
)
from openadapt_flow.receipt import RunReceipt, build_receipt


class RetentionError(ValueError):
    """A closed refusal code; no recorded content or key material is included."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RetentionPolicy:
    raw_days: int
    audit_days: int

    def __post_init__(self) -> None:
        if any(type(n) is not int or n < 0 for n in (self.raw_days, self.audit_days)):
            raise RetentionError("invalid_retention_days")
        if self.audit_days < self.raw_days:
            raise RetentionError("audit_retention_shorter_than_raw")


@dataclass(frozen=True)
class RetentionTarget:
    relative_path: str
    sha256: str
    size: int
    device: int
    inode: int


@dataclass(frozen=True)
class RetentionPlan:
    source: Path
    source_device: int
    source_inode: int
    policy: RetentionPolicy
    eligible: bool
    reason: Literal["eligible", "retention_not_elapsed"]
    verified_at: str
    eligible_at: str
    audit_until: str
    header_sha256: str
    set_digest: str
    report_sha256: str
    source_seal_sha256: str
    source_seal_file_sha256: str
    receipt: RunReceipt
    targets: tuple[RetentionTarget, ...]
    source_seal_bytes: bytes = field(repr=False)

    def preview(self) -> dict[str, Any]:
        """Local operator preview; filenames are not an audit export."""
        return {
            "dry_run": True,
            "eligible": self.eligible,
            "reason": self.reason,
            "eligible_at": self.eligible_at,
            "audit_until": self.audit_until,
            "files": len(self.targets),
            "bytes": sum(t.size for t in self.targets),
            "would_delete": [t.relative_path for t in self.targets]
            if self.eligible
            else [],
            "report_sha256": self.report_sha256,
            "source_seal_sha256": self.source_seal_sha256,
            "set_digest": self.set_digest,
        }


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RetentionError("invalid_signed_time") from exc
    if result.utcoffset() is None:
        raise RetentionError("invalid_signed_time")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _root(path: Path | str) -> Path:
    base = Path(path).absolute()
    if ".." in base.parts:
        raise RetentionError("unsafe_source_path")
    try:
        for current in (*reversed(base.parents), base):
            if current.is_symlink():
                raise RetentionError("symlink_source_path")
        if not stat.S_ISDIR(base.lstat().st_mode):
            raise RetentionError("source_not_directory")
    except OSError as exc:
        raise RetentionError("source_unreadable") from exc
    return base


def _tree(root: Path) -> dict[str, os.stat_result]:
    """Refuse nested ownership boundaries and links rather than follow them."""
    files: dict[str, os.stat_result] = {}

    def unreadable(error: OSError) -> None:
        raise error

    try:
        for current, dirs, names in os.walk(
            root, followlinks=False, onerror=unreadable
        ):
            here = Path(current)
            for name in dirs:
                child = here / name
                if child.is_symlink() or not stat.S_ISDIR(child.lstat().st_mode):
                    raise RetentionError("unsafe_source_entry")
                if sealed_set.is_sealed(child):
                    raise RetentionError("nested_set_unbound")
            for name in names:
                path = here / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RetentionError("unsafe_source_entry")
                files[path.relative_to(root).as_posix()] = info
    except OSError as exc:
        raise RetentionError("source_unreadable") from exc
    return files


def _snapshot(root: Path, relative: str, prior: os.stat_result) -> RetentionTarget:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root / relative, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (prior.st_dev, prior.st_ino):
                raise RetentionError("source_changed")
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RetentionError("unsafe_source_entry")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise RetentionError("source_changed")
    except OSError as exc:
        raise RetentionError("source_unreadable") from exc
    return RetentionTarget(
        relative, digest.hexdigest(), before.st_size, before.st_dev, before.st_ino
    )


def plan_retention(
    run_dir: Path | str,
    policy: RetentionPolicy,
    source_report_seal: bytes,
    registry: Sequence[SealPublicKey],
    *,
    key: str,
    now: datetime,
) -> RetentionPlan:
    """Read a frozen encrypted run and return its exact default dry-run plan.

    A caller supplies an existing pinned report Seal. Unsigned legacy runs do
    not acquire an invented completion date. The signed issue time starts the
    retention interval conservatively. A run with reconciled/unindexed files
    is not a frozen source and refuses; normal runtime readers are unchanged.
    """
    if not isinstance(policy, RetentionPolicy):
        raise RetentionError("invalid_retention_policy")
    if not isinstance(key, str) or not key:
        raise RetentionError("explicit_decryption_key_required")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise RetentionError("naive_current_time")
    current = now.astimezone(timezone.utc)
    root = _root(run_dir)
    source_info = root.stat()
    disk = _tree(root)
    if sealed_set.HEADER not in disk:
        raise RetentionError("encrypted_run_required")
    header = _snapshot(root, sealed_set.HEADER, disk[sealed_set.HEADER])
    try:
        opened = sealed_set.SealedSet.open(root, key, expect="run")
        authenticated = sealed_set._open_index(root, opened._header, opened._key)
        # A live run may reconcile external checkpoints and locks. Retention
        # requires the finalized authenticated inventory, not that mutable view.
        if sealed_set._canonical(authenticated) != sealed_set._canonical(opened._files):
            raise RetentionError("run_inventory_not_finalized")
        expected = {
            sealed_set._physical(rel, opened.codec(rel)): entry["sha256"]
            for rel, entry in authenticated.items()
        }
        if set(disk) != {*expected, sealed_set.HEADER}:
            raise RetentionError("source_inventory_mismatch")
        opened.verify_all()
        report_bytes = opened.read_bytes("report.json")
    except RetentionError:
        raise
    except (OSError, ValueError, crypto.CryptoError) as exc:
        raise RetentionError("source_unreadable") from exc
    targets = tuple(_snapshot(root, rel, disk[rel]) for rel in sorted(expected))
    if any(t.sha256 != expected[t.relative_path] for t in targets):
        raise RetentionError("source_changed")
    if _snapshot(root, sealed_set.HEADER, disk[sealed_set.HEADER]) != header:
        raise RetentionError("source_changed")
    try:
        seal_raw = loads_strict(source_report_seal)
        source_seal = LocalSealV1.model_validate(seal_raw)
    except (TypeError, ValueError) as exc:
        raise RetentionError("invalid_source_seal") from exc
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    verified = verify_local_seal(source_seal.wire(), report_hash, registry)
    if not verified.ok:
        raise RetentionError("source_seal_" + str(verified.reason))
    if source_seal.subject_kind != "run-report" or source_seal.outcome != "VERIFIED":
        raise RetentionError("signed_verified_run_required")
    try:
        report_data = loads_strict(report_bytes)
        if not isinstance(report_data, dict):
            raise RetentionError("complete_verified_report_required")
        report = RunReport.model_validate(report_data)
        receipt = build_receipt(report)
    except (TypeError, ValueError) as exc:
        raise RetentionError("complete_verified_report_required") from exc
    # The runtime hashes its actual effect-contract identity into this
    # declared field. A Seal's directory label is not assumed equivalent.
    if report.run_id_sha256 is None:
        raise RetentionError("source_runtime_identity_missing")
    signed_identity_hash = hashlib.sha256(
        source_seal.run_id.encode("utf-8")
    ).hexdigest()
    if report.run_id_sha256 != signed_identity_hash:
        raise RetentionError("source_runtime_identity_mismatch")
    # A legacy raw value, if present, may not contradict the bound identity.
    # Raw identity is never required or added to a retained RunReport.
    if "run_id" in report_data and report_data["run_id"] != source_seal.run_id:
        raise RetentionError("source_run_id_mismatch")
    issued = _time(source_seal.issued_at)
    if issued > current or issued < _time(report.started_at):
        raise RetentionError("signed_time_outside_run")
    try:
        due = issued + timedelta(days=policy.raw_days)
        audit_until = issued + timedelta(days=policy.audit_days)
    except OverflowError as exc:
        raise RetentionError("invalid_retention_days") from exc
    eligible = current >= due
    return RetentionPlan(
        source=root,
        source_device=source_info.st_dev,
        source_inode=source_info.st_ino,
        policy=policy,
        eligible=eligible,
        reason="eligible" if eligible else "retention_not_elapsed",
        verified_at=_iso(issued),
        eligible_at=_iso(due),
        audit_until=_iso(audit_until),
        header_sha256=header.sha256,
        set_digest=opened.set_digest,
        report_sha256=report_hash,
        source_seal_sha256=seal_sha256(source_seal.wire()),
        source_seal_file_sha256=hashlib.sha256(source_report_seal).hexdigest(),
        receipt=receipt,
        targets=(*targets, header),
        source_seal_bytes=source_report_seal,
    )
