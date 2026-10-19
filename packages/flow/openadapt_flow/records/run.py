"""Collect READ step results from a run and point them at the run's Seal.

:func:`field_reads_from_report` lists the field reads a run's READ steps
produced. :func:`record_from_report` groups them into a
:class:`~openadapt_flow.records.schema.RecordRead` for one schema.
:func:`seal_field_reads` fills each requested Seal pointer from the run's
signed receipt (``receipt.json``): its sha256 and path.

Today the pointer is the existing run receipt, which covers the run, not
each value. When offline Seals with per-field commitments are available,
``SealRef.field_commitment`` carries the commitment for each value; until
then it stays unset, so a pointer never claims more than the receipt proves.
Only fields whose READ step asked for a Seal (``ReadProof.seal``) get one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.records.schema import FieldRead, RecordRead, RecordSchema, SealRef

__all__ = [
    "field_reads_from_report",
    "record_from_report",
    "seal_field_reads",
    "seal_record",
    "write_sealed_reads",
]


def field_reads_from_report(report: Any) -> list[FieldRead]:
    """Every field read in ``report`` (a RunReport), in step order."""

    return [r.field_read for r in report.results if r.field_read is not None]


def _seal_from_receipt(receipt_json: Path, prior: SealRef) -> SealRef:
    digest = hashlib.sha256(receipt_json.read_bytes()).hexdigest()
    return prior.model_copy(
        update={"receipt_sha256": digest, "receipt_path": str(receipt_json)}
    )


def seal_field_reads(
    reads: list[FieldRead], receipt_json: Path | str
) -> list[FieldRead]:
    """Fill the Seal pointer of every read that asked for one."""

    path = Path(receipt_json)
    out = []
    for read in reads:
        if read.seal is None:
            out.append(read)
        else:
            out.append(
                read.model_copy(update={"seal": _seal_from_receipt(path, read.seal)})
            )
    return out


def record_from_report(
    report: Any,
    schema: RecordSchema,
    *,
    run_id: Optional[str] = None,
) -> RecordRead:
    """The record ``report`` read for ``schema``; its key is the identity values."""

    by_field = {r.field: r for r in field_reads_from_report(report)}
    fields = tuple(by_field[s.name] for s in schema.fields if s.name in by_field)
    key = "|".join(
        str(by_field[n].value) if n in by_field else "" for n in schema.identity_fields
    )
    seal_run = next((f.seal.run_id for f in fields if f.seal is not None), None)
    record = RecordRead(
        schema_name=schema.name,
        schema_version=schema.version,
        record_key=key,
        run_id=run_id or seal_run or "unknown",
        read_at=getattr(report, "started_at", None)
        or datetime.now(timezone.utc).isoformat(),
        fields=fields,
    )
    record.check_against(schema)
    return record


def seal_record(record: RecordRead, receipt_json: Path | str) -> RecordRead:
    """``record`` with each requested Seal pointer filled from the receipt."""

    return record.model_copy(
        update={"fields": tuple(seal_field_reads(list(record.fields), receipt_json))}
    )


def write_sealed_reads(report: Any, receipt_json: Path | str) -> Optional[Path]:
    """Write ``records.json`` next to the receipt when the run read any fields.

    Returns the path, or None when the run had no READ steps.
    """

    reads = field_reads_from_report(report)
    if not reads:
        return None
    sealed = seal_field_reads(reads, receipt_json)
    out = Path(receipt_json).parent / "records.json"
    import json

    out.write_text(
        json.dumps([r.model_dump(mode="json") for r in sealed], indent=2) + "\n",
        encoding="utf-8",
    )
    return out
