"""Planted-fault injectors for Medplum FHIR R4 silent-fail verification."""

from __future__ import annotations

import copy
from typing import Any

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_target",
    "duplicate_record",
    "operation_outcome_masked",
)


def apply_fault(
    bundle: dict[str, Any],
    fault_class: str,
    *,
    field_key: str = "birthDate",
) -> dict[str, Any]:
    """Mutate a FHIR R4 search Bundle to simulate a silent server-side persistence fault."""
    mutated = copy.deepcopy(bundle)
    entries = list(mutated.get("entry") or [])

    if fault_class == "dropped_write":
        mutated["entry"] = []
        mutated["total"] = 0
        return mutated

    if fault_class == "operation_outcome_masked":
        return {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "processing",
                    "diagnostics": "silent backend failure",
                }
            ],
        }

    if fault_class == "duplicate_record":
        if entries:
            dup = copy.deepcopy(entries[0])
            if isinstance(dup.get("resource"), dict):
                dup["resource"]["id"] = f"{dup['resource'].get('id', 'rec')}-dup"
            entries.append(dup)
            mutated["entry"] = entries
            mutated["total"] = len(entries)
        return mutated

    if fault_class == "truncated_value":
        if entries and isinstance(entries[0].get("resource"), dict):
            res = entries[0]["resource"]
            val = res.get(field_key)
            if isinstance(val, str) and len(val) > 2:
                res[field_key] = val[: len(val) // 2]
            else:
                res[field_key] = ""
        return mutated

    if fault_class == "wrong_target":
        if entries and isinstance(entries[0].get("resource"), dict):
            entries[0]["resource"]["id"] = "pat-synth-002"
            entries[0]["resource"][field_key] = "1900-01-01"
        return mutated

    raise ValueError(f"Unsupported Medplum fault class: {fault_class!r}")
