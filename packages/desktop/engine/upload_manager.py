"""Legacy customer-owned storage queue with bandwidth limiting.

All uploads go through a persistent queue (stored in index.db) that survives
app restarts. The upload pipeline:

    [User approves in review UI] -> Compress (tar.gz) -> Queue -> Upload Worker
                                                          |
                                                    Backend-specific:
                                                    - S3: multipart upload
                                                    - HF Hub: git lfs push
                                                    - R2: S3-compatible multipart
                                                    - Wormhole: P2P direct

Before any upload, the recording must pass check_egress_allowed() from review.py.
OpenAdapt hosted ingest does not use this queue. It uses Flow's stronger
inventory, sanitization, exact-hash approval, and immutable archive contract.

Bandwidth limiting uses a token bucket algorithm (configurable via
OPENADAPT_UPLOAD_BANDWIDTH_LIMIT).

See design doc Section 7 for backend details.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from engine.audit import AuditLogger
from engine.backends.protocol import StorageBackend, UploadResult
from engine.config import EngineConfig
from engine.db import IndexDB
from engine.review import (
    EgressArtifactError,
    EgressBlockedError,
    approved_egress_path,
    derivative_tree_sha256,
    load_derivative_approval,
    update_derivative_tree_digest,
)

# Durable/offline retry policy (spec section 5): jobs survive restarts (they
# live in the DB), retry with exponential backoff, and flush when connectivity
# returns. A permanent failure (missing capture/path) is NOT retried.
DEFAULT_MAX_ATTEMPTS = 6
_BACKOFF_BASE_S = 30
_BACKOFF_CAP_S = 3600
_FLOW_ONLY_BACKENDS = frozenset({"hosted_ingest"})
_FLOW_ONLY_ERROR = (
    "Direct hosted ingest is disabled. Use `openadapt-desktop push` so Flow can "
    "bind the upload to a reviewed, exact-hash sanitized artifact."
)
_PAUSED_STORAGE_BACKENDS = frozenset({"s3"})
_PAUSED_STORAGE_ERROR = (
    "Customer-storage upload is paused for this release. Its exit condition is a "
    "Flow-owned complete artifact inventory, image-capable sanitization, and an "
    "in-app exact-artifact review contract. Use governed hosted push or keep the "
    "recording local."
)
_APPROVED_ARCHIVE_RE = re.compile(
    r"^(?P<job>[a-f0-9]{32})-(?P<digest>[a-f0-9]{64})\.approved\.zip$"
)


@dataclass(frozen=True)
class _FrozenArtifact:
    """One queue-owned archive whose exact bytes passed local approval."""

    path: Path
    sha256: str


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff (capped) for the given attempt count."""
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** max(0, attempts - 1)))


class UploadManager:
    """Manages a durable upload queue and dispatches to storage backends.

    The queue is persisted in ``index.db`` (survives restarts). Transient
    failures (e.g. network blips) are retried with exponential backoff and the
    manager exposes an ``offline`` signal for the tray's OFFLINE/SYNCING states.

    Args:
        config: Engine configuration.
        backends: List of active storage backend instances.
        db: Index database for persistent queue.
        audit: Audit logger for upload events.
        max_attempts: Attempts before a transient failure becomes permanent.
    """

    def __init__(
        self,
        config: EngineConfig,
        backends: list[StorageBackend],
        db: IndexDB,
        audit: AuditLogger,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.config = config
        self.backends = {b.name: b for b in backends}
        self._db = db
        self._audit = audit
        self.max_attempts = max_attempts
        # True after a transient (retriable) failure this cycle; feeds tray state.
        self.offline = False

    def enqueue(self, capture_id: str, backend_name: str) -> str:
        """Add a capture to the upload queue.

        The capture must have a reviewed sanitized derivative.

        Args:
            capture_id: ID of the capture to upload.
            backend_name: Name of the target storage backend.

        Returns:
            Upload job ID.

        Raises:
            EgressBlockedError: If the capture hasn't been reviewed.
            ValueError: If the backend is not available.
        """
        if backend_name in _FLOW_ONLY_BACKENDS:
            raise ValueError(_FLOW_ONLY_ERROR)
        if backend_name in _PAUSED_STORAGE_BACKENDS:
            raise ValueError(_PAUSED_STORAGE_ERROR)

        if backend_name not in self.backends:
            raise ValueError(f"Backend not available: {backend_name}")

        job_id = uuid.uuid4().hex
        artifact_path = approved_egress_path(capture_id, self._db)
        frozen = self._freeze_artifact(
            job_id,
            artifact_path,
            approved_tree_sha256=load_derivative_approval(artifact_path),
        )
        try:
            self._db.insert_upload_job(
                job_id,
                capture_id,
                backend_name,
                archive_path=str(frozen.path),
            )
        except Exception:
            frozen.path.unlink(missing_ok=True)
            raise
        return job_id

    def _upload_frozen(
        self,
        artifact: _FrozenArtifact,
        backend_name: str,
        metadata: dict,
    ) -> UploadResult:
        """Send only an artifact returned by the queue's hash verifier."""
        if backend_name in _FLOW_ONLY_BACKENDS:
            return UploadResult(success=False, error=_FLOW_ONLY_ERROR)
        if backend_name in _PAUSED_STORAGE_BACKENDS:
            return UploadResult(success=False, error=_PAUSED_STORAGE_ERROR)
        backend = self.backends[backend_name]
        size_bytes = artifact.path.stat().st_size
        dest = f"{backend_name}://{metadata.get('capture_id', 'unknown')}"

        self._audit.log_upload_start(backend_name, dest, size_bytes)

        try:
            result = backend.upload(artifact.path, metadata)
        except Exception as e:
            self._audit.log_upload_failed(backend_name, dest, str(e))
            return UploadResult(success=False, error=str(e))

        if result.success:
            self._audit.log_upload_complete(backend_name, result.remote_url, result.bytes_sent)
        else:
            self._audit.log_upload_failed(backend_name, dest, result.error)

        return result

    def _approved_archive_root(self) -> Path:
        """Return the private queue archive directory."""

        root = self.config.data_dir / "approved_uploads"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise EgressArtifactError("The approved upload directory cannot be a symlink.")
        os.chmod(root, 0o700)
        if hasattr(os, "getuid") and root.stat().st_uid != os.getuid():
            raise EgressArtifactError("The approved upload directory has the wrong owner.")
        return root.resolve(strict=True)

    @staticmethod
    def _stream_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _freeze_artifact(
        self,
        job_id: str,
        source: Path,
        *,
        approved_tree_sha256: str,
    ) -> _FrozenArtifact:
        """Freeze only bytes that reproduce the review-time tree digest."""

        root = self._approved_archive_root()
        temporary = root / f".{job_id}.tmp"
        try:
            frozen_tree = hashlib.sha256()
            members = [source, *sorted(source.rglob("*"))]
            with zipfile.ZipFile(
                temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for member in members:
                    if member.is_symlink():
                        raise EgressArtifactError(
                            "The sanitized derivative contains a symlink."
                        )
                    relative = (
                        "." if member == source else member.relative_to(source).as_posix()
                    )
                    if relative == "review_status.json":
                        continue
                    if not member.is_file():
                        if member.is_dir():
                            update_derivative_tree_digest(
                                frozen_tree,
                                relative_path=relative,
                                member_type="directory",
                            )
                            continue
                        raise EgressArtifactError(
                            "The sanitized derivative has an unsupported member type."
                        )
                    stat = os.stat(member, follow_symlinks=False)
                    if stat.st_nlink != 1:
                        raise EgressArtifactError(
                            "The sanitized derivative contains a hard-linked file."
                        )
                    info = zipfile.ZipInfo(relative)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    file_digest = hashlib.sha256()
                    with member.open("rb") as input_file, archive.open(info, "w") as output_file:
                        while chunk := input_file.read(1024 * 1024):
                            file_digest.update(chunk)
                            output_file.write(chunk)
                    update_derivative_tree_digest(
                        frozen_tree,
                        relative_path=relative,
                        member_type="file",
                        file_sha256=file_digest.hexdigest(),
                    )
            if (
                frozen_tree.hexdigest() != approved_tree_sha256
                or derivative_tree_sha256(source) != approved_tree_sha256
            ):
                raise EgressArtifactError(
                    "The sanitized derivative does not match the exact reviewed bytes."
                )
            archive_digest = self._stream_sha256(temporary)
            destination = root / f"{job_id}-{archive_digest}.approved.zip"
            temporary.replace(destination)
            os.chmod(destination, 0o600)
            return _FrozenArtifact(path=destination, sha256=archive_digest)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _load_frozen_artifact(self, job: dict) -> _FrozenArtifact:
        """Verify a persisted queue archive before any network call."""

        value = str(job.get("archive_path") or "").strip()
        candidate = Path(value)
        if not value or candidate.is_symlink() or not candidate.is_file():
            raise EgressArtifactError("The approved queue archive is unavailable or unsafe.")
        root = self._approved_archive_root()
        path = candidate.resolve(strict=True)
        if path.parent != root:
            raise EgressArtifactError("The queue archive is outside the approved upload directory.")
        match = _APPROVED_ARCHIVE_RE.fullmatch(path.name)
        if match is None or match.group("job") != job["job_id"]:
            raise EgressArtifactError("The queue archive identity is invalid.")
        expected = match.group("digest")
        actual = self._stream_sha256(path)
        if actual != expected:
            raise EgressArtifactError("The approved queue archive changed after enqueue.")
        return _FrozenArtifact(path=path, sha256=actual)

    def get_queue_status(self) -> list[dict]:
        """Get the current state of the upload queue.

        Returns:
            List of pending and in-progress upload jobs.
        """
        return self._db.get_pending_jobs()

    def get_active_backends(self) -> list[str]:
        """Get names of currently active storage backends.

        Returns:
            List of backend names.
        """
        return list(self.backends.keys())

    def process_queue(self) -> list[dict]:
        """Process due uploads in the durable queue, retrying transient failures.

        Jobs whose retry backoff has not yet elapsed are skipped. A missing
        capture/path is a permanent failure (no retry); a backend/network error
        is transient -- the job returns to ``pending`` with exponential backoff
        until ``max_attempts`` is exhausted, then becomes ``failed``.

        Returns:
            List of result dicts for each job attempted this cycle.
        """
        self._db.recover_interrupted_upload_jobs()
        due = self._db.get_due_jobs()
        results = []
        self.offline = False

        for job in due:
            job_id = job["job_id"]
            capture_id = job["capture_id"]
            backend_name = job["backend_name"]

            self._db.update_upload_job(job_id, status="in_progress")

            if backend_name in _FLOW_ONLY_BACKENDS:
                self._db.update_upload_job(job_id, status="failed", error=_FLOW_ONLY_ERROR)
                self._cleanup_job_archive(job)
                results.append(
                    self._result(job_id, capture_id, backend_name, False, "", _FLOW_ONLY_ERROR)
                )
                continue
            if backend_name in _PAUSED_STORAGE_BACKENDS:
                self._db.update_upload_job(
                    job_id, status="failed", error=_PAUSED_STORAGE_ERROR
                )
                self._cleanup_job_archive(job)
                results.append(
                    self._result(
                        job_id,
                        capture_id,
                        backend_name,
                        False,
                        "",
                        _PAUSED_STORAGE_ERROR,
                    )
                )
                continue

            capture = self._db.get_capture(capture_id)
            if not capture:
                self._db.update_upload_job(
                    job_id, status="failed", error=f"Capture {capture_id} not found"
                )
                self._cleanup_job_archive(job)
                results.append(
                    self._result(
                        job_id,
                        capture_id,
                        backend_name,
                        False,
                        "",
                        f"Capture {capture_id} not found",
                    )
                )
                continue

            try:
                approved_egress_path(capture_id, self._db)
            except (ValueError, EgressBlockedError, EgressArtifactError) as exc:
                self._db.update_upload_job(job_id, status="failed", error=str(exc))
                self._cleanup_job_archive(job)
                results.append(self._result(job_id, capture_id, backend_name, False, "", str(exc)))
                continue

            metadata = {
                "capture_id": capture_id,
                "started_at": capture.get("started_at", ""),
                "duration_secs": capture.get("duration_secs", 0),
                "event_count": capture.get("event_count", 0),
            }

            try:
                frozen = self._load_frozen_artifact(job)
                result = self._upload_frozen(frozen, backend_name, metadata)
            except (OSError, ValueError, EgressArtifactError) as exc:
                self._db.update_upload_job(job_id, status="failed", error=str(exc))
                self._cleanup_job_archive(job)
                results.append(self._result(job_id, capture_id, backend_name, False, "", str(exc)))
                continue

            if result.success:
                self._db.update_upload_job(
                    job_id,
                    status="completed",
                    remote_url=result.remote_url,
                    bytes_sent=result.bytes_sent,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self._cleanup_job_archive(job)
            else:
                terminal = self._schedule_retry(job, result.error)
                if terminal:
                    self._cleanup_job_archive(job)

            results.append(
                self._result(
                    job_id,
                    capture_id,
                    backend_name,
                    result.success,
                    result.remote_url if result.success else "",
                    result.error if not result.success else "",
                )
            )

        return results

    def _cleanup_job_archive(self, job: dict) -> None:
        """Remove only the queue-owned archive for a terminal job."""

        value = str(job.get("archive_path") or "").strip()
        if not value:
            return
        candidate = Path(value)
        try:
            root = self._approved_archive_root()
            if (
                not candidate.is_symlink()
                and candidate.parent.resolve(strict=True) == root
                and candidate.name.startswith(f"{job['job_id']}-")
            ):
                candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove terminal queue archive for {jid}", jid=job["job_id"])

    def _schedule_retry(self, job: dict, error: str) -> bool:
        """Requeue a transiently-failed job with backoff, or fail it permanently."""
        attempts = (job.get("attempts") or 0) + 1
        self.offline = True
        if attempts >= self.max_attempts:
            self._db.update_upload_job(
                job["job_id"], status="failed", attempts=attempts, error=error
            )
            logger.warning(
                "Upload job {jid} permanently failed after {n} attempts",
                jid=job["job_id"],
                n=attempts,
            )
            return True
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(attempts))
        self._db.update_upload_job(
            job["job_id"],
            status="pending",
            attempts=attempts,
            next_retry_at=next_retry.isoformat(),
            error=error,
        )
        logger.info(
            "Upload job {jid} deferred (attempt {n}); retry at {t}",
            jid=job["job_id"],
            n=attempts,
            t=next_retry.isoformat(),
        )
        return False

    @staticmethod
    def _result(
        job_id: str,
        capture_id: str,
        backend: str,
        success: bool,
        remote_url: str,
        error: str,
    ) -> dict:
        return {
            "job_id": job_id,
            "capture_id": capture_id,
            "backend": backend,
            "success": success,
            "remote_url": remote_url,
            "error": error,
        }
