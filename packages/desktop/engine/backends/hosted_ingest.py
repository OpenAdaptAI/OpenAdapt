"""HostedIngestBackend -- pushes a zipped recording/bundle to POST /api/ingest.

This is the cloud-lane egress sink: a :class:`~engine.backends.protocol.StorageBackend`
that uploads a ``.zip`` (a recording directory OR a compiled bundle) to the
hosted control plane as ``multipart/form-data`` with a bearer ingest token
resolved through :func:`engine.auth.store.auth_header`.

Contract (spec section 3b, cloud PR #19 ``docs/INGEST.md``):

    POST {host}/api/ingest
    Content-Type: multipart/form-data
    Authorization: Bearer <ingest token>
    fields: file (required .zip), kind ("recording"|"bundle"), name (optional)
    -> 201 { "ingest": { workflow_id, workflow_name, kind, compile{...}, auth } }
    errors: 401 auth · 400 bad request · 502 store/compile failure

The archive is expected to already be a ``.zip`` -- :mod:`engine.hosted` zips
the recording/bundle directory before enqueuing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from loguru import logger

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
        """POST a ``.zip`` archive to ``/api/ingest``.

        Args:
            archive_path: Path to the ``.zip`` (recording dir or bundle dir).
            metadata: May carry ``kind`` ("recording"|"bundle") and ``name``.

        Returns:
            UploadResult with the resulting workflow_id/dashboard URL on success.
        """
        headers = auth_header()
        if "Authorization" not in headers:
            return UploadResult(success=False, error="Not logged in (no ingest token).")

        if not archive_path.exists():
            return UploadResult(success=False, error=f"Archive not found: {archive_path}")

        kind = metadata.get("kind", "recording")
        data = {"kind": kind}
        name = metadata.get("name")
        if name:
            data["name"] = name

        url = f"{self.host}{INGEST_PATH}"
        try:
            with open(archive_path, "rb") as fh:
                files = {"file": (archive_path.name, fh, "application/zip")}
                resp = httpx.post(
                    url, headers=headers, data=data, files=files, timeout=self._timeout
                )
        except httpx.HTTPError as exc:
            return UploadResult(success=False, error=f"Ingest request failed: {exc}")

        if resp.status_code == 401:
            return UploadResult(success=False, error="Ingest token was rejected (401).")
        if resp.status_code >= 400:
            return UploadResult(
                success=False, error=f"Ingest failed ({resp.status_code}): {resp.text[:200]}"
            )

        try:
            body = resp.json()
        except ValueError:
            body = {}
        ingest = body.get("ingest", {})
        workflow_id = ingest.get("workflow_id", "")
        remote_url = f"{self.host}/dashboard/workflows/{workflow_id}" if workflow_id else ""
        logger.info("Pushed {kind} to hosted ingest: workflow {wid}", kind=kind, wid=workflow_id)
        return UploadResult(
            success=True,
            remote_url=remote_url,
            bytes_sent=archive_path.stat().st_size,
            metadata=ingest,
        )

    def delete(self, recording_id: str) -> bool:
        """Not supported -- deletion is managed in the dashboard."""
        raise NotImplementedError("Hosted ingest does not support delete from the client.")

    def list_uploads(self) -> list[UploadRecord]:
        """Not supported -- listing is managed in the dashboard."""
        raise NotImplementedError("Hosted ingest does not support listing from the client.")

    def verify_credentials(self) -> bool:
        """True when a bearer token is resolvable from the auth store/env."""
        return "Authorization" in auth_header()

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Hosted ingest has no per-upload storage cost surfaced to the client."""
        return None
