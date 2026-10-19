"""Turn reads into interchange field values, grade them, and seal exchanges.

PROTOTYPE glue shared by the two workflows. Two parts are stand-ins:

- Grades follow the reading step's rule (``openadapt_flow.records.schema.
  grade_for``), as the interchange checks apply it
  (``openadapt_flow.interchange.conformance.supported_grade``). Only reads
  with a value count; they must equal the value and come from two different
  (channel, session) pairs. An independent read gives ``system_of_record``;
  a read in the acting session plus a screen read of the reopened record
  gives ``reopened``; two channels give ``same_screen``; anything else is
  ``unchecked``. An export counts as independent only when it came from saved
  data (``source_kind`` ``committed_store``); an export from the live session
  is a read in the acting session. The grade is derived from the reads, never
  passed in.
- The Seal is an Ed25519 signature over the exchange checksum with a key that
  exists only in memory for this run. The offline Seal service replaces it;
  the exchange keeps the same ``seals`` entry shape.
"""

from __future__ import annotations

import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from x11 import Frame, OcrLine, norm

from openadapt_flow import __version__
from openadapt_flow.interchange import check, with_checksum
from openadapt_flow.interchange.conformance import supported_grade
from openadapt_flow.interchange.model import Read

V = "openadapt.records-interchange/v0"
EXECUTOR = {"kind": "executor", "id": "openadapt-flow@demo-runner"}
DOC_SIDE = {"kind": "system", "id": "intake@demo-site"}


def now() -> str:
    # Microseconds, so a read made after a decision has a later time.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def grade(reads: list[dict[str, Any]], value: Optional[str]) -> str:
    """Derive the grade from the reads of ``value``.

    A read that saw something other than ``value`` makes it ``unchecked``.
    """
    parsed = [r for r in reads if r["value"] is not None]
    if value is None or any(str(r["value"]).strip() != value.strip() for r in parsed):
        return "unchecked"
    return supported_grade(Read.model_validate(r) for r in reads)


def read_field(
    name: str,
    kind: str,
    *,
    expected: Optional[str],
    clipboard: Optional[str],
    ocr_line: Optional[OcrLine],
    frame: Frame,
    session: str,
    artifact_id: str,
    screen: str,
    exports: Optional[list[tuple[Optional[str], str]]] = None,
    acting: Optional[list[tuple[str, Optional[str], str]]] = None,
) -> dict[str, Any]:
    """One field read through two (or more) channels, with its evidence.

    ``clipboard`` and ``ocr_line`` were read in ``session``. ``acting`` lists
    ``(channel, value)`` reads of the same value made earlier in the acting
    session (for example from the record pane before the write), as
    ``(channel, value, observed_at)``.
    """
    reads: list[dict[str, Any]] = [
        {
            "channel": channel,
            "value": seen,
            "artifact_id": artifact_id,
            "session": "acting",
            "observed_at": at,
        }
        for channel, seen, at in acting or []
    ]
    reads += [
        {
            "channel": "clipboard",
            "value": clipboard,
            "artifact_id": artifact_id,
            "session": session,
            "observed_at": now(),
        }
    ]
    r_ocr: dict[str, Any] = {
        "channel": "ocr",
        "value": ocr_line.text if ocr_line else None,
        "artifact_id": artifact_id,
        "session": session,
        "observed_at": now(),
        "locator": {"screen": screen},
    }
    if ocr_line is not None:
        r_ocr["locator"]["region"] = {
            "bbox": list(ocr_line.bbox),
            "frame_sha256": frame.sha256,
            "crop_sha256": frame.crop_sha256(ocr_line.bbox),
        }
    reads.append(r_ocr)
    for value, source_kind in exports or []:
        committed = source_kind == "committed_store"
        reads.append(
            {
                "channel": "export",
                "value": value,
                "source_kind": source_kind,
                # A live-session export is made by the acting session.
                "session": "independent" if committed else "acting",
                "observed_at": now(),
            }
        )
    values = [r["value"] for r in reads]
    agree = (
        all(v is not None for v in values) and len({norm(str(v)) for v in values}) == 1
    )
    g = grade(reads, clipboard)
    matches = (
        clipboard is not None
        and expected is not None
        and norm(clipboard) == norm(expected)
    )
    field: dict[str, Any] = {
        "name": name,
        "type": kind,
        "value": clipboard,
        "confidence": round(
            min(1.0, (ocr_line.confidence if ocr_line else 0.0) + 0.05), 3
        )
        if agree
        else 0.4,
        "grade": g,
        "reads": reads,
    }
    # A value that differs from what was written means the write did not land;
    # that reason wins over a disagreement between channels.
    if expected is not None and not matches:
        field["needs_review"] = True
        field["review_reason"] = f"read {clipboard!r}, wrote {expected!r}"
    elif not agree:
        field["needs_review"] = True
        field["review_reason"] = f"channels disagree: {values}"
    return field


def seal(doc: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Checksum the exchange and attach a prototype Seal over the checksum."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    doc = with_checksum(doc)
    covers = doc["provenance"]["checksum"]
    key = Ed25519PrivateKey.generate()  # in memory only; never written
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    sig = key.sign(covers.encode())
    seals = run_dir / "seals"
    seals.mkdir(parents=True, exist_ok=True)
    rel = f"seals/{doc['exchange_id']}.seal.json"
    (run_dir / rel).write_text(
        json.dumps(
            {
                "kind": "openadapt.seal/prototype-ephemeral-ed25519",
                "covers": covers,
                "public_key": base64.b64encode(pub).decode(),
                "signature": base64.b64encode(sig).decode(),
                "signed_at": now(),
                "note": "PROTOTYPE: key lived in memory for one run; replaced by offline Seals",
            },
            indent=2,
        )
    )
    doc["seals"] = [
        {
            "kind": "openadapt.seal/prototype-ephemeral-ed25519",
            "covers": covers,
            "key_id": "ephemeral:" + pub.hex()[:16],
            "signature": base64.b64encode(sig).decode(),
            "uri": rel,
        }
    ]
    return doc


def provenance(producer: dict[str, str]) -> dict[str, Any]:
    return {
        "tool_name": "openadapt-flow/examples/legacy_records",
        "tool_version": __version__,
        "producer": producer,
        "captured_at": now(),
    }


def attach_payload(src: Path, run_dir: Path) -> dict[str, Any]:
    import hashlib

    dest = run_dir / "payloads" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    data = dest.read_bytes()
    mime = "application/pdf" if src.suffix == ".pdf" else "text/plain"
    return {
        "path": f"payloads/{src.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime": mime,
        "bytes": len(data),
    }


def write_exchange(doc: dict[str, Any], run_dir: Path) -> Path:
    """Write an exchange after checking it conforms. A failing check stops the run."""
    path = run_dir / f"{doc['exchange_id']}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    problems = check(doc, base_dir=run_dir)
    if problems:
        raise SystemExit("exchange does not conform: " + "; ".join(map(str, problems)))
    return path
