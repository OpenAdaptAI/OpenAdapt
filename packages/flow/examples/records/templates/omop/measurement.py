"""OMOP CDM v5.4 template: exported record rows -> ``measurement`` rows.

A template, not part of OpenAdapt's core. It unpivots one exported record row
(from ``openadapt_flow.records.export.export_rows``) into one OMOP
``measurement`` row per mapped field. Concept ids come from the caller's own
vocabulary mapping; this file ships none. Keep the provenance columns: they
carry the read grade and Seal for each value into ``measurement_source_value``
neighbours your ETL can keep or drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

MEASUREMENT_COLUMNS = (
    "measurement_id",
    "person_id",
    "measurement_concept_id",
    "measurement_date",
    "measurement_type_concept_id",
    "value_as_number",
    "value_source_value",
    "unit_source_value",
    "measurement_source_value",
    "openadapt_grade",
    "openadapt_receipt_sha256",
)


@dataclass(frozen=True)
class FieldConcept:
    """How one record field maps to an OMOP measurement."""

    measurement_concept_id: int
    source_value: str
    unit_source_value: Optional[str] = None


def _number(value: Any) -> Optional[str]:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def to_measurements(
    rows: Iterable[Mapping[str, Any]],
    *,
    person_id: Mapping[str, int],
    date_field: str,
    concepts: Mapping[str, FieldConcept],
    type_concept_id: int,
    first_id: int = 1,
) -> list[dict[str, Any]]:
    """One measurement row per mapped field per record row.

    ``person_id`` maps ``_record_key`` to the OMOP person id (your ETL's
    identity mapping). Rows with no person id are skipped.
    """

    out: list[dict[str, Any]] = []
    next_id = first_id
    for row in rows:
        pid = person_id.get(row["_record_key"])
        if pid is None:
            continue
        for field_name, concept in concepts.items():
            value = row.get(field_name)
            if value is None:
                continue
            out.append(
                {
                    "measurement_id": next_id,
                    "person_id": pid,
                    "measurement_concept_id": concept.measurement_concept_id,
                    "measurement_date": row[date_field],
                    "measurement_type_concept_id": type_concept_id,
                    "value_as_number": _number(value),
                    "value_source_value": str(value),
                    "unit_source_value": concept.unit_source_value,
                    "measurement_source_value": concept.source_value,
                    "openadapt_grade": row.get("_grade"),
                    "openadapt_receipt_sha256": row.get("_receipt_sha256"),
                }
            )
            next_id += 1
    return out
