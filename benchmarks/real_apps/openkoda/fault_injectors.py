"""Planted-fault injectors for Openkoda P&C Insurance verification."""

from __future__ import annotations

import copy
from typing import Any

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_target",
    "duplicate_record",
    "stale_deductible",
)


def apply_fault(
    records: list[dict[str, Any]],
    fault_class: str,
    *,
    field_key: str = "insuredName",
) -> list[dict[str, Any]]:
    """Mutate persisted Openkoda records to inject a deterministic silent failure."""
    mutated = copy.deepcopy(records)
    if fault_class == "dropped_write":
        return []
    if fault_class == "duplicate_record":
        if mutated:
            mutated.append(copy.deepcopy(mutated[0]))
        return mutated
    if fault_class == "truncated_value":
        if mutated and isinstance(mutated[0].get(field_key), str):
            val = mutated[0][field_key]
            mutated[0][field_key] = val[: max(1, len(val) // 2)]
        return mutated
    if fault_class == "wrong_target":
        if mutated:
            mutated[0]["policyNumber"] = "POL-HO3-2026-999"
        return mutated
    if fault_class == "stale_deductible":
        if mutated:
            mutated[0]["deductible"] = int(mutated[0].get("deductible", 1000)) + 1500
        return mutated
    raise ValueError(f"Unsupported Openkoda fault class: {fault_class!r}")
