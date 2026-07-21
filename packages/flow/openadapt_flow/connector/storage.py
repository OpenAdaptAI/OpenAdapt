"""Customer-owned storage — the BYOC PHI boundary on the DATA side.

For a BYOC job the PHI-bearing bytes (the compiled bundle IN, the run report
OUT) live in the CUSTOMER'S OWN storage, never ours. The control-plane job
descriptor carries only opaque relative KEYS (``storage.bundle_ref`` /
``storage.report_ref``); the Connector resolves them against the storage it is
configured with. Our control plane holds NO URL to these bytes and signs NO
access to them.

This module ships the two backends the core loop needs:

* :class:`LocalCustomerStorage` — REAL: refs resolve under a customer directory
  (ideally a full-disk-encrypted volume). The on-prem clinic posture.
* :class:`InMemoryCustomerStorage` — tests/dry-run: pretends the bundle is
  present and CAPTURES the report in memory, proving the report bytes never
  leave for the control plane, with zero infra.

S3 / Azure Blob backends are documented as production follow-ups (they need a
customer cloud to exercise); the operator reference agent in openadapt-cloud
(``connector/agent.py``) already carries them.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any, Optional, Protocol


class CustomerStorage(Protocol):
    """Read the bundle from / write the report to the customer's own store."""

    kind: str

    def fetch_bundle(self, ref: Optional[str], dest_dir: Path) -> Path:
        """Resolve ``ref`` to an unpacked bundle directory under ``dest_dir``."""
        ...

    def write_report(self, ref: Optional[str], report: dict[str, Any]) -> Optional[str]:
        """Persist the PHI-bearing report to the customer store; return its key."""
        ...


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip, refusing any entry that escapes ``dest`` (zip-slip)."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise RuntimeError(f"refusing unsafe archive path: {member!r}")
        zf.extractall(dest)


class LocalCustomerStorage:
    """REAL local-volume backend (the on-prem posture)."""

    kind = "local"

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def fetch_bundle(self, ref: Optional[str], dest_dir: Path) -> Path:
        if not ref:
            raise RuntimeError("byoc job has no storage.bundle_ref to read")
        src = self.root / ref
        if not src.exists():
            raise RuntimeError(f"bundle not found in customer storage: {src}")
        if src.is_dir():
            return src  # already an unpacked bundle directory
        _safe_extract(src, dest_dir)
        return dest_dir

    def write_report(self, ref: Optional[str], report: dict[str, Any]) -> Optional[str]:
        if not ref:
            return None
        dest = self.root / ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
        try:
            os.chmod(dest, 0o600)  # PHI-bearing — restrict at rest
        except OSError:  # pragma: no cover
            pass
        return str(dest)


class InMemoryCustomerStorage:
    """Tests/dry-run backend: a pre-staged bundle dir + an in-memory report sink.

    Proves the report bytes are written to the CUSTOMER side and never returned
    to the control plane, with zero infra.
    """

    kind = "memory"

    def __init__(self, bundle_dir: Optional[Path] = None) -> None:
        self.bundle_dir = bundle_dir
        self.written: dict[str, dict[str, Any]] = {}

    def fetch_bundle(self, ref: Optional[str], dest_dir: Path) -> Path:
        return self.bundle_dir if self.bundle_dir is not None else dest_dir

    def write_report(self, ref: Optional[str], report: dict[str, Any]) -> Optional[str]:
        self.written[ref or "?"] = report
        return ref


def build_storage(
    settings: Any, job_backend_hint: Optional[str] = None
) -> CustomerStorage:
    """Pick the customer-storage backend. The Connector's own config is
    authoritative; the job's ``storage.backend`` is only a hint; default local."""
    backend = (settings.storage_backend or job_backend_hint or "local").lower()
    if backend == "local":
        root = settings.storage_root
        if not root:
            raise RuntimeError(
                "byoc local storage: set storage_root (a full-disk-encrypted "
                "customer volume) — the PHI-bearing bundle/report live there"
            )
        return LocalCustomerStorage(root)
    raise RuntimeError(
        f"byoc storage_backend {backend!r} is not built into the engine connector "
        "(local only for now); s3/azure_blob are operator-reference backends — "
        "see openadapt-cloud connector/agent.py + deploy/byoc/README.md"
    )
