# vendored from adef5a431254:packages/flow/openadapt_flow/receipt_record.py, lines 62-100, for the harness self-test only (re-indented)
# vendored from adef5a431254:packages/flow/openadapt_flow/receipt_record.py, lines 115-134, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S1 post-fix: every write is judged (adef5a431).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional

# Stub of openadapt_types.receipt at adef5a431 (MAX_LINES and receipt_outcome).
OUTCOME_FROM_RUN = {"VERIFIED": "VERIFIED", "HALTED": "STOPPED", "FAILED": "FAILED"}
r = SimpleNamespace(
    MAX_LINES=256,
    receipt_outcome=lambda o: (OUTCOME_FROM_RUN.get(o, "UNKNOWN"), str(o).lower()),
)


# vendored: adef5a431254:packages/flow/openadapt_flow/receipt_record.py 62-100
# fmt: off
def _is_write(result: Mapping[str, Any]) -> bool:
    return (
        result.get("effect_verified") is not None
        or result.get("effect_approved_unverified") is True
        or result.get("risk") == "irreversible"
        or bool(result.get("effect_contract_hashes"))
    )


def _line(index: int, result: Mapping[str, Any]) -> dict[str, Any]:
    effect = result.get("effect_verified")
    if effect is True:
        check, strength = "confirmed", "read_back"
    elif effect is False:
        check, strength = "refuted", "read_back"
    else:
        check = "not_checked"
        strength = "same_screen" if result.get("postconditions_ok") is True else "none"
    not_checked: list[str] = []
    if effect is None:
        not_checked.append(
            "approved_unverified"
            if result.get("effect_approved_unverified") is True
            else "effect_not_declared"
        )
    if effect is True and not result.get("effect_field_checks"):
        not_checked.append("field_values")
    if result.get("postconditions_ok") is None:
        not_checked.append("postconditions")
    return {
        "step": index,
        "target_kind": "api" if result.get("actuation") == "api" else "gui",
        "operation": (
            "irreversible_write" if result.get("risk") == "irreversible" else "write"
        ),
        "check": check,
        "strength": strength,
        "not_checked": not_checked,
    }
# fmt: on


def outcome_of(report: Mapping[str, Any]) -> Optional[str]:
    # vendored: adef5a431254:packages/flow/openadapt_flow/receipt_record.py 115-134
    # fmt: off
    results = report.get("results")
    results = (
        [x for x in results if isinstance(x, Mapping)]
        if isinstance(results, list)
        else []
    )
    every_line = [_line(i, x) for i, x in enumerate(results) if _is_write(x)]
    lines = every_line[: r.MAX_LINES]
    tally = {
        k: sum(1 for line in lines if line["check"] == k)
        for k in ("confirmed", "refuted", "not_checked")
    }
    outcome, reason = r.receipt_outcome(report.get("execution_outcome"))
    # Judge every write, not only the ones that fit: a refuted write past
    # the line limit still refuses VERIFIED, and so does any write the
    # receipt can't show.
    if outcome == "VERIFIED" and any(x["check"] == "refuted" for x in every_line):
        outcome, reason = "UNKNOWN", "refuted_write"
    elif outcome == "VERIFIED" and len(every_line) > r.MAX_LINES:
        outcome, reason = "UNKNOWN", "too_many_writes"
    # fmt: on
    return outcome
