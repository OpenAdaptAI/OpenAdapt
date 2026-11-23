"""Planted-fault injectors for WooCommerce order & refund silent-fail verification."""

from __future__ import annotations

import copy
from typing import Any

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_target",
    "duplicate_record",
    "silent_refund_mismatch",
)


def apply_fault(
    state: dict[str, Any],
    fault_class: str,
) -> dict[str, Any]:
    """Mutate a ``{"order": {...}, "refunds": [...]}`` state dict to inject a silent fault."""
    mutated = copy.deepcopy(state)
    order = mutated.get("order")
    refunds = list(mutated.get("refunds") or [])

    if fault_class == "dropped_write":
        mutated["order"] = {}
        mutated["refunds"] = []
        return mutated
    if fault_class == "truncated_value":
        if isinstance(order, dict) and isinstance(order.get("status"), str):
            order["status"] = order["status"][:3]
        return mutated
    if fault_class == "wrong_target":
        if isinstance(order, dict):
            order["id"] = int(order.get("id", 1001)) + 1
        return mutated
    if fault_class == "duplicate_record":
        if refunds:
            dup = copy.deepcopy(refunds[0])
            dup["id"] = int(dup.get("id", 5001)) + 1
            refunds.append(dup)
            mutated["refunds"] = refunds
        return mutated
    if fault_class == "silent_refund_mismatch":
        if refunds:
            refunds[0]["amount"] = "0.00"
            mutated["refunds"] = refunds
        return mutated
    raise ValueError(f"Unsupported WooCommerce fault class: {fault_class!r}")
