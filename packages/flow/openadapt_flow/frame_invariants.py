"""Fixed checks over the authenticated receipt/search/final-state domain.

The shared targeted reader establishes authority and plan membership before
calling this pure judge. These DTOs and this judge cannot issue that authority.
Field matching, version binding and legitimate-other edits stay independent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from openadapt_flow.hard_invariants import HardInvariantVerdict, InvariantResult
from openadapt_flow.runtime.effects.frame import FrameVerdict

if TYPE_CHECKING:
    from openadapt_flow.runtime.effects.targeted import (
        SearchObservation,
        TargetedObservation,
    )

TARGETED_LAYER_SCHEMA = "openadapt.targeted-hard-invariants/v1"
OUTSIDE_INTERMEDIATE_HISTORY = "outside_receipt_intermediate_history"


def _digest(value: str, prefix: str = "sha256:") -> bool:
    return re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", value) is not None


def _instant(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _identity(value: str) -> bool:
    return re.fullmatch(r"[A-Z][A-Za-z0-9]*/[A-Za-z0-9.-]+", value) is not None


@dataclass(frozen=True)
class TargetedHardInvariantVerdict(HardInvariantVerdict):
    """A distinct guarantee from the optional global historical predicates.

    Only commitments, counts and fixed reason codes may enter a receipt.
    Raw identities, field values and actor names remain in memory.
    """

    receipt_sha256: Optional[str] = None
    target_id_macs: tuple[str, ...] = ()
    contract_sha256: Optional[str] = None
    schema_sha256: Optional[str] = None
    write_search_count: Optional[int] = None
    late_search_count: Optional[int] = None
    history_required: bool = True
    histories_covered: int = 0
    write_search_observed_at: Optional[str] = None
    late_search_observed_at: Optional[str] = None
    read_at: Optional[str] = None
    phase: Optional[str] = None

    def evidence(self) -> dict[str, object]:
        result = super().evidence()
        result["schema_version"] = TARGETED_LAYER_SCHEMA
        result["guarantee"] = {
            "receipt_sha256": self.receipt_sha256,
            "target_id_macs": list(self.target_id_macs),
            "contract_sha256": self.contract_sha256,
            "schema_sha256": self.schema_sha256,
            "write_search_count": self.write_search_count,
            "late_search_count": self.late_search_count
            if self.phase == "late"
            else None,
            "write_confirmation_count": self.late_search_count
            if self.phase == "write"
            else None,
            "phase": self.phase,
            "receipt_binding": "outer_issued_receipt"
            if self.phase == "write"
            else "original_issued_receipt"
            if self.phase == "late"
            else "UNKNOWN",
            "future_late_search": "owed" if self.phase == "write" else None,
            "history_required": self.history_required,
            "histories_covered": self.histories_covered,
            "write_search_observed_at": self.write_search_observed_at,
            "late_search_observed_at": self.late_search_observed_at
            if self.phase == "late"
            else None,
            "write_confirmation_observed_at": self.late_search_observed_at
            if self.phase == "write"
            else None,
            "read_at": self.read_at,
        }
        result["outside_guarantee"] = [
            {
                "name": OUTSIDE_INTERMEDIATE_HISTORY,
                "status": "UNKNOWN",
                "reason": "outside_declared_guarantee",
            },
            *(
                [
                    {
                        "name": "receipt_record_intermediate_history",
                        "status": "UNKNOWN",
                        "reason": "final_state_only_contract",
                    }
                ]
                if not self.history_required
                else []
            ),
        ]
        return result


def _search_valid(search: SearchObservation) -> bool:
    return (
        _digest(search.contract_sha256)
        and _digest(search.schema_sha256)
        and isinstance(search.candidate_count, int)
        and not isinstance(search.candidate_count, bool)
        and search.candidate_count >= len(search.matched_ids)
        and all(_identity(identity) for identity in search.matched_ids)
        and _instant(search.observed_at) is not None
    )


def judge_frame_invariants(
    frame: FrameVerdict,
    observation: Optional[TargetedObservation] = None,
) -> TargetedHardInvariantVerdict:
    """Name known failures and refuse missing in-scope targeted evidence.

    The producer, not a caller's completeness flag, authenticates receipt and
    plan membership. Wider outside-ID intermediate history is always named
    UNKNOWN and never downgrades an otherwise complete in-scope result.
    Write phase records only actual write-time observations; the issuing
    receipt will bind them, and the late search remains owed. A late phase
    requires the already-issued receipt digest. No caller-provided phase can
    replace the shared producer's authority at the public recheck boundary.
    No transient delete exception exists without a receipt-bound cleanup plan
    and final absence proof; actor equality alone cannot grant that exception.
    """
    outside = sum(not change.other_on_target for change in frame.collateral)
    removed = sum(
        change.observed_delete
        for change in frame.collateral
        if not change.other_on_target
    )
    scope = InvariantResult(
        "declared_scope",
        "refuted" if outside else "not_checked",
        "outside_scope_changes" if outside else "receipt_target_observation_missing",
        outside,
    )
    cardinality = InvariantResult(
        "at_most_one", "not_checked", "identifying_search_observation_missing"
    )
    deletes = InvariantResult(
        "no_undeclared_deletes",
        "refuted" if removed else "not_checked",
        "undeclared_deletes" if removed else "receipt_target_observation_missing",
        removed,
    )
    if observation is None:
        return TargetedHardInvariantVerdict((scope, cardinality, deletes))

    write, late = observation.write_search, observation.late_search
    searches_bound = (
        _search_valid(write)
        and _search_valid(late)
        and write.contract_sha256 == late.contract_sha256
        and write.schema_sha256 == late.schema_sha256
    )
    write_at, late_at, read_at = (
        _instant(write.observed_at),
        _instant(late.observed_at),
        _instant(observation.read_at),
    )
    ordered = (
        write_at is not None
        and late_at is not None
        and read_at is not None
        and write_at <= late_at <= read_at
    )
    targets = observation.targets
    phase = observation.phase
    receipt_bound = (
        phase == "late"
        and _digest(observation.receipt_sha256, "")
        or phase == "write"
        and observation.receipt_sha256 == ""
    )
    binding = (
        receipt_bound
        and bool(targets)
        and len({target.identity for target in targets}) == len(targets)
        and len({target.target_id_mac for target in targets}) == len(targets)
        and all(
            target.receipt_bound is True
            and _identity(target.identity)
            and _digest(target.target_id_mac, "hmac-sha256:")
            for target in targets
        )
        and searches_bound
        and ordered
        and all(target.identity in write.matched_ids for target in targets)
        and all(
            target.identity in late.matched_ids
            for target in targets
            if target.current_status == "present" and target.matches_receipt is True
        )
    )
    if not outside:
        scope = InvariantResult(
            "declared_scope",
            "held"
            if binding
            and frame.status == "held"
            and all(
                target.current_status in {"present", "absent"} for target in targets
            )
            else "not_checked",
            "receipt_targets_and_declared_scope_read"
            if binding
            and frame.status == "held"
            and all(
                target.current_status in {"present", "absent"} for target in targets
            )
            else "receipt_scope_or_final_state_incomplete",
            0,
        )

    known_counts = [
        len(search.matched_ids) for search in (write, late) if _search_valid(search)
    ]
    maximum = max(known_counts) if known_counts else None
    if binding and maximum is not None and maximum > 1:
        cardinality = InvariantResult(
            "at_most_one", "refuted", "duplicate_business_key", maximum
        )
    elif binding and write.complete is True and late.complete is True:
        cardinality = InvariantResult(
            "at_most_one",
            "held",
            "at_most_one_in_write_searches"
            if phase == "write"
            else "at_most_one_in_declared_searches",
            maximum,
        )
    else:
        cardinality = InvariantResult(
            "at_most_one",
            "not_checked",
            "identifying_search_incomplete_or_unbound",
            maximum,
        )

    histories = sum(
        target.history_status == "complete" and target.history_anchor_bound is True
        for target in targets
    )
    history_known = (
        binding
        and histories == len(targets)
        and all(
            version.author_trusted is True
            and (version.by_run is True or version.by_run is False)
            for target in targets
            for version in target.versions
            if version.operation == "delete"
        )
    )
    if binding:
        removed += sum(
            version.operation == "delete"
            and version.author_trusted is True
            and version.by_run is True
            for target in targets
            if target.history_anchor_bound is True
            for version in target.versions
        )
    if removed:
        deletes = InvariantResult(
            "no_undeclared_deletes", "refuted", "undeclared_deletes", removed
        )
    elif binding and (history_known or observation.history_required is False):
        deletes = InvariantResult(
            "no_undeclared_deletes",
            "held",
            "no_undeclared_deletes_in_receipt_history"
            if history_known
            else "no_observed_delete_final_state_only",
            0,
        )
    else:
        deletes = InvariantResult(
            "no_undeclared_deletes",
            "not_checked",
            "receipt_delete_history_incomplete_or_unbound",
            0,
        )
    return TargetedHardInvariantVerdict(
        (scope, cardinality, deletes),
        receipt_sha256=observation.receipt_sha256
        if _digest(observation.receipt_sha256, "")
        else None,
        target_id_macs=tuple(
            sorted(
                target.target_id_mac
                for target in targets
                if _digest(target.target_id_mac, "hmac-sha256:")
            )
        ),
        contract_sha256=write.contract_sha256
        if _digest(write.contract_sha256)
        else None,
        schema_sha256=write.schema_sha256 if _digest(write.schema_sha256) else None,
        write_search_count=len(write.matched_ids) if _search_valid(write) else None,
        late_search_count=len(late.matched_ids) if _search_valid(late) else None,
        history_required=observation.history_required,
        histories_covered=histories if binding else 0,
        write_search_observed_at=write.observed_at if write_at is not None else None,
        late_search_observed_at=late.observed_at if late_at is not None else None,
        read_at=observation.read_at if read_at is not None else None,
        phase=phase if phase in ("write", "late") else None,
    )
