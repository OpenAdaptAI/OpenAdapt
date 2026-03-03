"""Network audit logging -- append-only JSONL log of all outbound network activity.

Every outbound network request is logged to an append-only JSONL file at
~/.openadapt/audit.jsonl. This enables enterprise IT to:
    - grep the audit log for unexpected destinations
    - set firewall rules allowing only their S3 endpoint
    - use a network proxy to independently verify traffic
    - run OPENADAPT_STORAGE_MODE=air-gapped and confirm zero outbound traffic

Log format (from design doc Section 7.9):
    {"ts":"2026-03-02T10:00:01Z","event":"startup","storage_mode":"enterprise","backends":["s3"],"excluded":["hf","r2","ipfs"]}
    {"ts":"2026-03-02T10:05:00Z","event":"upload_start","backend":"s3","dest":"s3://bucket/rec.tar.zst","size_mb":142}
    {"ts":"2026-03-02T10:05:32Z","event":"upload_complete","backend":"s3","dest":"s3://bucket/rec.tar.zst","bytes_sent":148897280}

All significant actions are logged:
    - Recording started/stopped
    - Upload initiated/completed/failed (with destination URL)
    - Settings changed
    - Data deleted (local or cloud)
    - Update installed
    - Permissions granted/denied
    - Every outbound network request (destination, size, response code)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    """Append-only audit logger for network and significant events.

    Args:
        log_path: Path to the audit.jsonl file.
        enabled: Whether audit logging is active.
    """

    def __init__(self, log_path: Path, enabled: bool = True) -> None:
        self.log_path = log_path
        self.enabled = enabled

    def log(self, event: str, **data: Any) -> None:
        """Write an audit log entry.

        Args:
            event: Event type (e.g., "startup", "upload_start", "settings_changed").
            **data: Additional event data as keyword arguments.
        """
        if not self.enabled:
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }

        # TODO: Ensure log_path parent directory exists
        # TODO: Append JSONL entry atomically
        line = json.dumps(entry)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")

    def log_startup(self, storage_mode: str, active_backends: list[str]) -> None:
        """Log engine startup with active configuration.

        Args:
            storage_mode: Current storage mode (air-gapped, enterprise, community, full).
            active_backends: List of active backend names.
        """
        all_backends = {"s3", "r2", "hf", "wormhole", "federated"}
        excluded = sorted(all_backends - set(active_backends))
        self.log(
            "startup",
            storage_mode=storage_mode,
            backends=active_backends,
            excluded=excluded,
        )

    def log_upload_start(self, backend: str, dest: str, size_bytes: int) -> None:
        """Log the start of an upload operation.

        Args:
            backend: Storage backend name.
            dest: Destination URI (e.g., "s3://bucket/key").
            size_bytes: Size of the data being uploaded.
        """
        self.log(
            "upload_start",
            backend=backend,
            dest=dest,
            size_mb=round(size_bytes / (1024 * 1024), 1),
        )

    def log_upload_complete(self, backend: str, dest: str, bytes_sent: int) -> None:
        """Log the completion of an upload operation.

        Args:
            backend: Storage backend name.
            dest: Destination URI.
            bytes_sent: Total bytes sent.
        """
        self.log(
            "upload_complete",
            backend=backend,
            dest=dest,
            bytes_sent=bytes_sent,
        )

    def log_upload_failed(self, backend: str, dest: str, error: str) -> None:
        """Log a failed upload operation.

        Args:
            backend: Storage backend name.
            dest: Destination URI.
            error: Error message.
        """
        self.log(
            "upload_failed",
            backend=backend,
            dest=dest,
            error=error,
        )
