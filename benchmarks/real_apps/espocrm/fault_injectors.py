"""Planted-fault injectors for EspoCRM Brokerage AMS verification."""

from __future__ import annotations

import copy
from typing import Any

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_target",
    "duplicate_record",
    "stage_reverted",
)


def apply_fault(
    payload: dict[str, Any],
    fault_class: str,
    *,
    field_key: str = "billingAddressCity",
) -> dict[str, Any]:
    """Mutate an EspoCRM ``{"list": [...]}`` response to inject a silent failure."""
    mutated = copy.deepcopy(payload)
    rows = list(mutated.get("list") or [])
    if fault_class == "dropped_write":
        mutated["list"] = []
        mutated["total"] = 0
        return mutated
    if fault_class == "duplicate_record":
        if rows:
            dup = copy.deepcopy(rows[0])
            dup["id"] = f"{dup.get('id', 'rec')}-dup"
            rows.append(dup)
            mutated["list"] = rows
            mutated["total"] = len(rows)
        return mutated
    if fault_class == "truncated_value":
        if rows and isinstance(rows[0].get(field_key), str):
            val = rows[0][field_key]
            rows[0][field_key] = val[: max(1, len(val) // 2)]
        return mutated
    if fault_class == "wrong_target":
        if rows:
            rows[0]["name"] = "Northstar Home Brokerage East"
        return mutated
    if fault_class == "stage_reverted":
        if rows:
            rows[0]["stage"] = "Prospecting"
        return mutated
    raise ValueError(f"Unsupported EspoCRM fault class: {fault_class!r}")
