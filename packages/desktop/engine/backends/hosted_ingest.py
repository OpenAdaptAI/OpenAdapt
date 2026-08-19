"""Deprecated direct hosted-ingest adapter.

The former adapter accepted any ZIP file and sent it to ``POST /api/ingest``.
It could not prove that Flow had inventoried, sanitized, reviewed, and frozen
the exact bytes. The adapter remains importable for protocol compatibility,
but it fails closed. Use :func:`engine.hosted.push`, which delegates to Flow's
approved sanitized-derivative contract.
"""

from __future__ import annotations

from pathlib import Path

from engine.auth.store import DEFAULT_HOST, auth_header
from engine.backends.protocol import UploadRecord, UploadResult

INGEST_PATH = "/api/ingest"


class HostedIngestBackend:
    """Uploads recordings/bundles to the hosted ``/api/ingest`` endpoint.

    Args:
        host: Hosted base URL. Defaults to the shared ``DEFAULT_HOST``.
        timeout: HTTP timeout in seconds for the multipart POST.
    """

    name: str = "hosted_ingest"
    supports_delete: bool = False
    supports_list: bool = False

    def __init__(self, host: str = DEFAULT_HOST, timeout: float = 120.0) -> None:
        self.host = host.rstrip("/")
        self._timeout = timeout

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Refuse the obsolete direct upload path without making a request.

        Args:
            archive_path: Path to the ``.zip`` (recording dir or bundle dir).
            metadata: May carry ``kind`` ("recording"|"bundle") and ``name``.

        Returns:
            A failed result that identifies the governed replacement path.
        """
        return UploadResult(
            success=False,
            error=(
                "Direct hosted ingest is disabled. Use `openadapt-desktop push` so Flow "
                "can inventory, sanitize, review, approve, and freeze the exact artifact."
            ),
        )

    def delete(self, recording_id: str) -> bool:
        """Not supported -- deletion is managed in the dashboard."""
        raise NotImplementedError("Hosted ingest does not support delete from the client.")

    def list_uploads(self) -> list[UploadRecord]:
        """Not supported -- listing is managed in the dashboard."""
        raise NotImplementedError("Hosted ingest does not support listing from the client.")

    def verify_credentials(self) -> bool:
        """True when a bearer token is resolvable from the auth store/env."""
        return "Authorization" in auth_header(self.host)

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Hosted ingest has no per-upload storage cost surfaced to the client."""
        return None
