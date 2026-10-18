"""Turn reads into interchange field values, grade them, and seal exchanges.

PROTOTYPE glue shared by the two workflows. Two parts are stand-ins:

- Grades follow ``openadapt_flow.records.schema.grade_for`` (the reading
  step's rule). When that module is installed it is used; otherwise the same
  rule is applied here.
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

try:  # the reading step's record schema, once merged
    from openadapt_flow.records.schema import grade_for as _records_grade_for
except ImportError:  # pragma: no cover - until the records schema is on main
    _records_grade_for = None

V = "openadapt.records-interchange/v0"
EXECUTOR = {"kind": "executor", "id": "openadapt-flow@demo-runner"}
DOC_SIDE = {"kind": "system", "id": "intake@demo-site"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def grade(reads: list[dict[str, Any]], agreement: str) -> str:
    if _records_grade_for is not None:
        from openadapt_flow.records.schema import ChannelRead

        channel = {"ocr": "ocr", "clipboard": "clipboard", "export": "export"}
        return _records_grade_for(
            [
                ChannelRead(
                    channel=channel[r["channel"]],
                    raw=str(r["value"]),
                    session=r["session"],
                )
                for r in reads
            ],
            agreement,
        )
    if agreement != "agree" or len(reads) < 2:
        return "unconfirmed"
    if any(r["session"] == "independent" for r in reads):
        return "verified"
    if any(r["session"] == "reopened" for r in reads):
        return "committed_record_readback"
    return "screen_readback"


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
    export: Optional[str] = None,
) -> dict[str, Any]:
    """One field read through two (or three) channels, with its evidence."""
    reads: list[dict[str, Any]] = [
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
    if export is not None:
        reads.append(
            {
                "channel": "export",
                "value": export,
                "session": "independent",
                "observed_at": now(),
            }
        )
    values = [r["value"] for r in reads]
    agree = (
        all(v is not None for v in values) and len({norm(str(v)) for v in values}) == 1
    )
    agreement = "agree" if agree else "disagree"
    g = grade(reads, agreement)
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
