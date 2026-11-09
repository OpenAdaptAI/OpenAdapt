"""Immutable completion seal for a stopped native capture."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ARTIFACT_MANIFEST_FILENAME = "capture-artifact-manifest.json"
CAPTURE_TERMINAL_FILENAME = "capture-terminal.json"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "openadapt.capture-artifact-manifest/v1"
CAPTURE_TERMINAL_SCHEMA_VERSION = "openadapt.capture-terminal/v2"
_MANIFEST_DOMAIN = b"openadapt.capture-artifact-manifest.v1\0"
_TERMINAL_DOMAIN = b"openadapt.capture-terminal.v2\0"
_EXCLUDED_ARTIFACTS = {
    ARTIFACT_MANIFEST_FILENAME,
    CAPTURE_TERMINAL_FILENAME,
}


class CaptureSealError(RuntimeError):
    """A capture seal or one of its inventoried artifacts is invalid."""


class ArtifactRecord(BaseModel):
    """One exact regular file in a stopped capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _safe_relative_path(self) -> "ArtifactRecord":
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("artifact paths must be safe POSIX-relative paths")
        if path.as_posix() in _EXCLUDED_ARTIFACTS:
            raise ValueError("seal metadata cannot inventory itself")
        return self


class CaptureArtifactManifest(BaseModel):
    """Canonical inventory of every stopped capture artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.capture-artifact-manifest/v1"]
    artifacts: tuple[ArtifactRecord, ...]

    @model_validator(mode="after")
    def _ordered_unique_paths(self) -> "CaptureArtifactManifest":
        paths = [artifact.path for artifact in self.artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique and sorted")
        if "recording.db" not in paths:
            raise ValueError("the artifact manifest must inventory recording.db")
        return self


class CaptureEventCounts(BaseModel):
    """Committed event counts at the completion boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: int = Field(ge=0)
    screen: int = Field(ge=0)
    window: int = Field(ge=0)
    browser: int = Field(ge=0)
    video: int = Field(ge=0)


class CaptureTerminal(BaseModel):
    """Strict immutable proof that the recorder completed and sealed output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.capture-terminal/v2"]
    state: Literal["COMPLETE"]
    reason_code: Literal["normal_stop"]
    source_capture_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(min_length=20)
    ended_at: str = Field(min_length=20)
    event_counts: CaptureEventCounts
    last_source_ordinal: int | None = Field(default=None, ge=1)
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_manifest_size_bytes: int = Field(gt=0)
    terminal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_terminal_digest(self) -> "CaptureTerminal":
        payload = self.model_dump(mode="json", exclude={"terminal_sha256"})
        if self.terminal_sha256 != _terminal_sha256(payload):
            raise ValueError("capture terminal digest is invalid")
        return self


def _canonical_json_bytes(payload: object, *, newline: bool) -> bytes:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _manifest_sha256(raw_manifest: bytes) -> str:
    return hashlib.sha256(_MANIFEST_DOMAIN + raw_manifest).hexdigest()


def _terminal_sha256(payload: object) -> str:
    return hashlib.sha256(
        _TERMINAL_DOMAIN + _canonical_json_bytes(payload, newline=False)
    ).hexdigest()


def _utc_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def source_capture_session_sha256(
    *, session_id: str, process_started_at: float, capture_started_at: float
) -> str:
    """Derive a privacy-safe identity for one recorder process session."""
    payload = {
        "capture_started_at": capture_started_at,
        "process_started_at": process_started_at,
        "session_id": session_id,
    }
    return hashlib.sha256(
        b"openadapt.capture-source-session.v1\0" + _canonical_json_bytes(payload, newline=False)
    ).hexdigest()


def _safe_artifact_path(capture_dir: Path, relative_path: str) -> Path:
    record = ArtifactRecord(path=relative_path, size_bytes=0, sha256="0" * 64)
    candidate = capture_dir.joinpath(*PurePosixPath(record.path).parts)
    try:
        candidate.relative_to(capture_dir)
        candidate.resolve(strict=True).relative_to(capture_dir)
    except (OSError, ValueError) as exc:
        raise CaptureSealError("an artifact path escapes the capture directory") from exc
    return candidate


def _open_stable_regular_file(path: Path) -> tuple[int, os.stat_result]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise CaptureSealError(f"capture artifact is not a regular file: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
    ) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise CaptureSealError(f"capture artifact changed before reading: {path.name}")
    return fd, before


def _open_relative_regular_file(
    root: Path,
    relative_path: str,
) -> tuple[int, os.stat_result]:
    """Open one artifact without following any intermediate path component."""
    record = ArtifactRecord(path=relative_path, size_bytes=0, sha256="0" * 64)
    parts = PurePosixPath(record.path).parts
    supports_descriptor_walk = (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )
    if not supports_descriptor_walk:
        return _open_stable_regular_file(_safe_artifact_path(root, record.path))

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(root, directory_flags)
    opened_directories = [directory_fd]
    try:
        try:
            for part in parts[:-1]:
                directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                opened_directories.append(directory_fd)
            before = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise CaptureSealError(
                    f"capture artifact is not a regular file: {record.path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(parts[-1], flags, dir_fd=directory_fd)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                os.close(fd)
                raise CaptureSealError(
                    f"capture artifact changed before reading: {record.path}"
                )
            return fd, before
        except OSError as exc:
            raise CaptureSealError(
                f"capture artifact path changed before reading: {record.path}"
            ) from exc
    finally:
        for opened_directory in reversed(opened_directories):
            os.close(opened_directory)


def _assert_relative_identity(
    root: Path,
    relative_path: str,
    expected: os.stat_result,
) -> None:
    fd, current = _open_relative_regular_file(root, relative_path)
    os.close(fd)
    if (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
    ) != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise CaptureSealError(
            f"capture artifact changed while reading: {relative_path}"
        )


def _assert_stable_file(path: Path, before: os.stat_result, after: os.stat_result) -> None:
    current = path.lstat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or identity != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise CaptureSealError(f"capture artifact changed while reading: {path.name}")


def _hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash one stable regular file without following a symbolic link."""
    fd, before = _open_stable_regular_file(path)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CaptureSealError(f"capture artifact changed before hashing: {path.name}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    _assert_stable_file(path, before, after)
    if size != before.st_size:
        raise CaptureSealError(f"capture artifact size changed while hashing: {path.name}")
    return size, digest.hexdigest()


def _hash_relative_regular_file(root: Path, relative_path: str) -> tuple[int, str]:
    fd, before = _open_relative_regular_file(root, relative_path)
    try:
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CaptureSealError(f"capture artifact changed while reading: {relative_path}")
    _assert_relative_identity(root, relative_path, before)
    if size != before.st_size:
        raise CaptureSealError(f"capture artifact size changed while hashing: {relative_path}")
    return size, digest.hexdigest()


def _read_regular_file(path: Path) -> bytes:
    """Read one stable regular file through the descriptor that was verified."""
    try:
        fd, before = _open_stable_regular_file(path)
    except FileNotFoundError as exc:
        raise CaptureSealError(f"capture artifact is missing: {path.name}") from exc
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    _assert_stable_file(path, before, after)
    if size != before.st_size:
        raise CaptureSealError(f"capture artifact size changed while reading: {path.name}")
    return b"".join(chunks)


def _copy_verified_regular_file(
    source_root: Path,
    relative_path: str,
    destination: Path,
    expected: ArtifactRecord,
) -> None:
    """Copy one exact source file without following a replacement symlink."""
    source_fd, before = _open_relative_regular_file(source_root, relative_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        destination_fd = os.open(destination, flags, 0o600)
    except BaseException:
        os.close(source_fd)
        raise
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
        finally:
            os.close(destination_fd)
            os.close(source_fd)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        destination.unlink(missing_ok=True)
        raise CaptureSealError(
            f"capture artifact changed during snapshot: {relative_path}"
        )
    _assert_relative_identity(source_root, relative_path, before)
    if (size, digest.hexdigest()) != (expected.size_bytes, expected.sha256):
        destination.unlink(missing_ok=True)
        raise CaptureSealError(f"capture artifact changed during snapshot: {expected.path}")


def build_artifact_manifest(capture_dir: str | os.PathLike[str]) -> CaptureArtifactManifest:
    """Inventory all stopped regular files except mutable and seal metadata."""
    root = Path(capture_dir).resolve()
    artifacts: list[ArtifactRecord] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in _EXCLUDED_ARTIFACTS:
            continue
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise CaptureSealError(f"capture artifact is not a regular file: {relative}")
        size, digest = _hash_relative_regular_file(root, relative)
        artifacts.append(ArtifactRecord(path=relative, size_bytes=size, sha256=digest))
    return CaptureArtifactManifest(
        schema_version=ARTIFACT_MANIFEST_SCHEMA_VERSION,
        artifacts=tuple(artifacts),
    )


def _write_new_atomic(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise CaptureSealError(f"refusing to replace existing capture seal: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seal_capture(
    capture_dir: str | os.PathLike[str],
    *,
    session_id: str,
    process_started_at: float,
    capture_started_at: float,
    capture_ended_at: float,
    event_counts: dict[str, int],
    last_source_ordinal: int | None,
) -> CaptureTerminal:
    """Write the manifest and terminal after all capture writers have stopped."""
    root = Path(capture_dir).resolve()
    manifest = build_artifact_manifest(root)
    manifest_raw = _canonical_json_bytes(manifest.model_dump(mode="json"), newline=True)
    manifest_digest = _manifest_sha256(manifest_raw)
    _write_new_atomic(root / ARTIFACT_MANIFEST_FILENAME, manifest_raw)
    payload = {
        "schema_version": CAPTURE_TERMINAL_SCHEMA_VERSION,
        "state": "COMPLETE",
        "reason_code": "normal_stop",
        "source_capture_session_sha256": source_capture_session_sha256(
            session_id=session_id,
            process_started_at=process_started_at,
            capture_started_at=capture_started_at,
        ),
        "started_at": _utc_timestamp(capture_started_at),
        "ended_at": _utc_timestamp(capture_ended_at),
        "event_counts": event_counts,
        "last_source_ordinal": last_source_ordinal,
        "artifact_manifest_sha256": manifest_digest,
        "artifact_manifest_size_bytes": len(manifest_raw),
    }
    payload["terminal_sha256"] = _terminal_sha256(payload)
    terminal = CaptureTerminal.model_validate(payload)
    terminal_raw = _canonical_json_bytes(terminal.model_dump(mode="json"), newline=True)
    _write_new_atomic(root / CAPTURE_TERMINAL_FILENAME, terminal_raw)
    verified_terminal, _ = verify_capture_artifacts(root)
    return verified_terminal


def verify_capture_artifacts(
    capture_dir: str | os.PathLike[str],
) -> tuple[CaptureTerminal, CaptureArtifactManifest]:
    """Verify canonical seal bytes and every inventoried artifact."""
    root = Path(capture_dir).resolve()
    terminal_path = root / CAPTURE_TERMINAL_FILENAME
    manifest_path = root / ARTIFACT_MANIFEST_FILENAME
    terminal_raw = _read_regular_file(terminal_path)
    manifest_raw = _read_regular_file(manifest_path)
    try:
        terminal = CaptureTerminal.model_validate_json(terminal_raw)
        manifest = CaptureArtifactManifest.model_validate_json(manifest_raw)
    except Exception as exc:
        raise CaptureSealError("capture seal is malformed") from exc
    if terminal_raw != _canonical_json_bytes(terminal.model_dump(mode="json"), newline=True):
        raise CaptureSealError("capture terminal bytes are not canonical")
    if manifest_raw != _canonical_json_bytes(manifest.model_dump(mode="json"), newline=True):
        raise CaptureSealError("artifact manifest bytes are not canonical")
    if terminal.artifact_manifest_size_bytes != len(manifest_raw):
        raise CaptureSealError("artifact manifest size differs from the terminal")
    if terminal.artifact_manifest_sha256 != _manifest_sha256(manifest_raw):
        raise CaptureSealError("artifact manifest digest differs from the terminal")
    expected_paths = {artifact.path for artifact in manifest.artifacts}
    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative in _EXCLUDED_ARTIFACTS:
            continue
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise CaptureSealError(f"capture artifact is not a regular file: {relative}")
        actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise CaptureSealError("capture artifacts differ from the sealed inventory")
    for artifact in manifest.artifacts:
        size, digest = _hash_relative_regular_file(root, artifact.path)
        if (size, digest) != (artifact.size_bytes, artifact.sha256):
            raise CaptureSealError(f"capture artifact differs from its seal: {artifact.path}")
    return terminal, manifest


def copy_verified_capture(
    capture_dir: str | os.PathLike[str],
) -> tuple[tempfile.TemporaryDirectory[str], Path, CaptureTerminal]:
    """Copy a verified capture into a private, stable consumer snapshot."""
    terminal, manifest = verify_capture_artifacts(capture_dir)
    source = Path(capture_dir).resolve()
    temporary = tempfile.TemporaryDirectory(prefix="openadapt-capture-verified-")
    destination = Path(temporary.name)
    try:
        for artifact in manifest.artifacts:
            target_path = destination.joinpath(*PurePosixPath(artifact.path).parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _copy_verified_regular_file(source, artifact.path, target_path, artifact)
            size, digest = _hash_regular_file(target_path)
            if (size, digest) != (artifact.size_bytes, artifact.sha256):
                raise CaptureSealError(f"capture artifact changed during snapshot: {artifact.path}")
        _write_new_atomic(
            destination / ARTIFACT_MANIFEST_FILENAME,
            _canonical_json_bytes(manifest.model_dump(mode="json"), newline=True),
        )
        _write_new_atomic(
            destination / CAPTURE_TERMINAL_FILENAME,
            _canonical_json_bytes(terminal.model_dump(mode="json"), newline=True),
        )
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, destination, terminal


def copy_verified_database(
    capture_dir: str | os.PathLike[str],
) -> tuple[
    tempfile.TemporaryDirectory[str],
    Path,
    CaptureTerminal,
    CaptureArtifactManifest,
]:
    """Copy only the sealed database for bounded-space semantic validation."""
    terminal, manifest = verify_capture_artifacts(capture_dir)
    database_record = next(
        artifact for artifact in manifest.artifacts if artifact.path == "recording.db"
    )
    source = Path(capture_dir).resolve()
    temporary = tempfile.TemporaryDirectory(prefix="openadapt-capture-database-")
    database_path = Path(temporary.name) / "recording.db"
    try:
        _copy_verified_regular_file(
            source,
            database_record.path,
            database_path,
            database_record,
        )
        size, digest = _hash_regular_file(database_path)
        if (size, digest) != (database_record.size_bytes, database_record.sha256):
            raise CaptureSealError("capture database changed during snapshot")
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, database_path, terminal, manifest
