"""Emit workflow outputs as L1 acquisition artifacts.

Layered clinical-data platforms (an L2 standardization layer over L1
acquisition)
ingest acquisition output through a deliberately thin contract: an on-disk
file under a resolved extraction directory, addressed by a canonical
``{file_number}_{date}_{doctype}`` filename, plus a manifest row carrying
metadata and provenance. This module writes that contract, so a compiled
workflow's outputs (downloaded documents, captured screens, extracted text)
can feed such a layer directly.

The manifest is a CSV sidecar (``manifest.csv``) appended atomically per
artifact; each row also gets a JSON provenance envelope next to the payload
(``<artifact>.provenance.json``) with a sha256 checksum, in the spirit of the
``L1Artifact`` envelope design (source_type, session, tool, captured_at).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openadapt_flow import __version__

MANIFEST_NAME = "manifest.csv"
MANIFEST_FIELDS = [
    "filename",
    "file_number",
    "date",
    "doctype",
    "source_type",
    "session_id",
    "sha256",
    "captured_at",
    "tool_name",
    "tool_version",
]

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(value: str) -> str:
    """Sanitize a metadata field for use in a filename."""
    cleaned = _SAFE.sub("-", value.strip()).strip("-")
    if not cleaned:
        raise ValueError(f"value {value!r} has no filename-safe characters")
    return cleaned


@dataclass(frozen=True)
class L1ArtifactRef:
    """Reference to an emitted artifact: payload path + manifest row."""

    path: Path
    provenance_path: Path
    manifest_path: Path
    row: dict[str, str]


def emit_l1_artifact(
    payload: Path | str,
    extraction_dir: Path | str,
    *,
    file_number: str,
    date: str,
    doctype: str,
    source_type: str = "workflow_replay",
    session_id: Optional[str] = None,
    tool_name: str = "openadapt-flow",
) -> L1ArtifactRef:
    """Copy ``payload`` into ``extraction_dir`` under the canonical
    ``{file_number}_{date}_{doctype}`` name and append a manifest row.

    Args:
        payload: Existing file produced by a workflow run (PDF, PNG, text…).
        extraction_dir: The resolved extraction root the L2 layer watches.
        file_number: Chart/file identifier (sanitized into the filename).
        date: Acquisition date, ISO ``YYYY-MM-DD``.
        doctype: Document-type hint (e.g. ``referral``, ``opnote``).
        source_type: Provenance tag for how the payload was acquired.
        session_id: Optional id grouping artifacts from one run.
        tool_name: Provenance tool name.

    Returns:
        L1ArtifactRef with the emitted path and the manifest row written.

    Raises:
        FileNotFoundError: payload does not exist.
        ValueError: date is not ISO format or a field is not sanitizable.
        FileExistsError: an artifact with the same canonical name exists with
            DIFFERENT content (identical re-emits are idempotent no-ops).
    """
    src = Path(payload)
    if not src.is_file():
        raise FileNotFoundError(src)
    datetime.strptime(date, "%Y-%m-%d")  # validate; raises ValueError

    out_root = Path(extraction_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    stem = f"{_slug(file_number)}_{date}_{_slug(doctype)}"
    dest = out_root / f"{stem}{src.suffix.lower()}"
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    if dest.exists():
        existing = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing != digest:
            raise FileExistsError(
                f"{dest} exists with different content (sha256 mismatch)"
            )
    else:
        shutil.copy2(src, dest)

    captured_at = datetime.now(timezone.utc).isoformat()
    row = {
        "filename": dest.name,
        "file_number": file_number,
        "date": date,
        "doctype": doctype,
        "source_type": source_type,
        "session_id": session_id or "",
        "sha256": digest,
        "captured_at": captured_at,
        "tool_name": tool_name,
        "tool_version": __version__,
    }

    provenance_path = dest.with_suffix(dest.suffix + ".provenance.json")
    provenance_path.write_text(json.dumps(row, indent=2))

    manifest_path = out_root / MANIFEST_NAME
    write_header = not manifest_path.exists()
    with manifest_path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    return L1ArtifactRef(
        path=dest,
        provenance_path=provenance_path,
        manifest_path=manifest_path,
        row=row,
    )
