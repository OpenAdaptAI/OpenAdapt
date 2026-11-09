"""Automatic repairs on the console and the phone portal: list, revert.

Each repair a signed policy applied without a person
(:mod:`openadapt_flow.repair.standard_change`) is a row the decision portal can
show. Reverting one is a person's action on the attended console only, like
answering a save: the phone echoes the row's digest, so it reverts exactly
the repair it showed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore
from openadapt_flow.repair.standard_change import Ledger, revert

PORTAL_ROUTE = "decision_portal"


class StandardChangeRevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StandardChangeRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _entry_sha256(row: dict[str, Any]) -> str:
    applied = {k: v for k, v in row.items() if k != "reverted"}
    return hashlib.sha256(
        json.dumps(applied, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def list_standard_changes(store_root: Path) -> list[dict[str, Any]]:
    """Every automatic repair, newest first, with what a phone card needs."""

    store = RepairStore(store_root)
    pointer = store.active_pointer()
    in_use = pointer.candidate_id if pointer is not None else None
    rows = []
    for row in reversed(Ledger(store_root).applied()):
        reverted = row.get("reverted")
        state: str
        try:
            state = store.load_candidate(row["candidate_id"]).state
        except RepairLifecycleError:
            state = "missing"
        # Only the repair in use, or one that never went live, can be
        # reverted: reverting an older one would drop a newer repair.
        revertable = not reverted and (
            state in ("approved", "staged")
            or (state in ("canary", "active") and in_use == row["candidate_id"])
        )
        rows.append(
            {
                "candidate_id": row["candidate_id"],
                "applied_at": row.get("at"),
                "workflow": row.get("workflow"),
                "step_id": row.get("step_id"),
                "change_class": row.get("change_class"),
                "detail": row.get("detail", ""),
                "policy_id": row.get("policy_id"),
                "signed_by": row.get("signed_by"),
                "test_record": row.get("test_record"),
                "preflight_outcome": row.get("preflight_outcome"),
                "reverted_by": reverted.get("reverted_by") if reverted else None,
                "reverted_at": reverted.get("at") if reverted else None,
                "state": state,
                "actions": ["revert"] if revertable else [],
                "entry_sha256": _entry_sha256(row),
            }
        )
    return rows


def revert_from_portal(
    store_root: Path, candidate_id: str, payload: StandardChangeRevertRequest
) -> dict[str, Any]:
    """A person reverts one automatic repair from the phone. Single use."""

    row = Ledger(store_root).find(candidate_id)
    if row is None:
        raise StandardChangeRefused("unknown", "no such automatic repair")
    if not hmac.compare_digest(payload.entry_sha256, _entry_sha256(row)):
        raise StandardChangeRefused("changed", "this repair changed since it was shown")
    try:
        candidate = revert(
            RepairStore(store_root),
            candidate_id,
            by=f"{PORTAL_ROUTE}:person",
            route=PORTAL_ROUTE,
        )
    except RepairLifecycleError as exc:
        raise StandardChangeRefused("refused", str(exc)) from exc
    return {
        "candidate_id": candidate.candidate_id,
        "state": candidate.state,
        "active_digest": candidate.prior_content_digest,
    }


__all__ = [
    "StandardChangeRefused",
    "StandardChangeRevertRequest",
    "list_standard_changes",
    "revert_from_portal",
]
