# vendored from a23dfedadc61:packages/flow/openadapt_flow/txplan/engine.py, lines 1269-1291, for the harness self-test only (re-indented)
# vendored from a23dfedadc61:packages/flow/openadapt_flow/txplan/engine.py, lines 1331-1332, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S3 pre-fix: undo of a write with no undo counts as ROLLED_BACK (#2428).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from typing import Any


def undo_outcome(
    per_write: dict[str, str],
    leftover: list[str],
    own: dict[str, str],
    writes: list[Any],
    images: dict[str, Any],
) -> str:
    # vendored: a23dfedadc61:packages/flow/openadapt_flow/txplan/engine.py 1269-1291
    # fmt: off
    counted = [
        k
        for k, v in per_write.items()
        if v
        in (
            "undone",
            "undo_refuted",
            "frame_differs",
            "changed_since_apply",
            "undo_failed",
            "unresolved",
        )
    ]
    # Undo images name the rows a statement touched: those are the write's own.
    for w in writes:
        img = (images.get(w["key"]) or {}).get("rows") or {}
        for rid in [*img.get("before", {}), *img.get("added_pk", {})]:
            own.setdefault(rid, w["key"])
    collateral = [r for r in leftover if r not in own]
    for r in leftover:
        if r in own and per_write.get(own[r]) == "undone":
            per_write[own[r]] = "frame_differs"
    verified = [k for k in counted if per_write[k] == "undone" and not collateral]
    # fmt: on
    return undo_tail(per_write, counted, verified, collateral)


def undo_tail(
    per_write: dict[str, str],
    counted: list[str],
    verified: list[str],
    collateral: list[str],
) -> str:
    # vendored: a23dfedadc61:packages/flow/openadapt_flow/txplan/engine.py 1331-1332
    # fmt: off
    ok = len(verified) == len(counted) and not collateral
    outcome = "ROLLED_BACK" if ok else "RECONCILIATION_REQUIRED"
    # fmt: on
    return outcome
