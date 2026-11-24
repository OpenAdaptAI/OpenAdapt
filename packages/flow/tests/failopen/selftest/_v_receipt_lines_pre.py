# vendored from 9b6956be37dd:packages/flow/openadapt_flow/receipt_record.py, lines 57-118 and 233-236, for the harness self-test only
# ruff: noqa: F401, F841, I001
"""receipt_record's line judge and tally before they moved onto the kernel,
for the differential test (test_receipt_lines_migration.py).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrappers around them
are not."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


# vendored: 9b6956be37dd:packages/flow/openadapt_flow/receipt_record.py 57-118
# fmt: off
def _line(
    index: int, result: Mapping[str, Any], ledger: Mapping[str, Any] | None
) -> dict[str, Any]:
    from openadapt_flow.check_level import CheckLevel, from_verification_tier

    evidence = result.get("effect_evidence") or []
    declared = Counter(result.get("effect_contract_hashes") or [])
    ledger_effects = ledger["effects"] if ledger is not None else []
    # A declared effect can lack a bound hash when the run stopped before the
    # reader ran. Keep the attempted write and withhold its verification.
    ledger_matches = (
        ledger is None
        or Counter(effect.get("contract_hash") for effect in ledger_effects) == declared
    )
    observed = Counter(item["effect_contract_hash"] for item in evidence)
    check = "not_checked"
    strength = "same_screen" if result.get("postconditions_ok") is True else "none"
    # A negative aggregate flag covers both a contradiction and an unavailable
    # reader. Only complete, contract-bound read evidence distinguishes them.
    if declared and ledger_matches and declared == observed:
        verdicts = {item["final_verdict"] for item in evidence}
        levels = [
            from_verification_tier(item["verification_tier"])
            if type(item.get("verification_tier")) is int
            else CheckLevel.UNCHECKED
            for item in evidence
        ]
        if verdicts <= {"confirmed", "refuted"} and min(levels) > CheckLevel.UNCHECKED:
            if "refuted" in verdicts:
                check = "refuted"
            elif (
                verdicts == {"confirmed"}
                and result.get("effect_verified") is True
                and all(item.get("observed_effect") == "present" for item in evidence)
                and (ledger is None or ledger.get("status") == "confirmed")
            ):
                check = "confirmed"
            if check != "not_checked":
                strength = (
                    "read_back"
                    if min(levels) >= CheckLevel.SYSTEM_OF_RECORD
                    else "same_screen"
                )
    not_checked: list[str] = []
    if result.get("effect_approved_unverified") is True:
        not_checked.append("approved_unverified")
    if not declared and not ledger_effects:
        not_checked.append("effect_not_declared")
    if not result.get("effect_field_checks"):
        not_checked.append("field_values")
    if result.get("postconditions_ok") is None:
        not_checked.append("postconditions")
    return {
        "step": index,
        "target_kind": "api" if result.get("actuation") == "api" else "gui",
        "operation": "irreversible_write"
        if result.get("risk") == "irreversible"
        else "write",
        "check": check,
        "strength": strength,
        "not_checked": not_checked,
    }
# fmt: on


def tally(lines: list[dict[str, Any]]) -> dict[str, int]:
    # vendored: 9b6956be37dd:packages/flow/openadapt_flow/receipt_record.py 233-236
    # fmt: off
    tally = {
        k: sum(1 for line in lines if line["check"] == k)
        for k in ("confirmed", "refuted", "not_checked")
    }
    # fmt: on
    return tally


def unverified(lines: list[dict[str, Any]]) -> bool:
    """The old rule (line 240, not vendored): any refuted or not-checked
    line kept a VERIFIED run from VERIFIED."""

    counts = tally(lines)
    return bool(counts["refuted"] or counts["not_checked"])
