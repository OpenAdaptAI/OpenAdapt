"""This tenant's policy for patches that arrive from the fleet.

A patch never promotes itself. After import it is a ``fleet_patch``
candidate, and :func:`process_imported_patch` takes it as far as this
tenant's :class:`FleetPromotionPolicy` allows:

* **auto-promote** only when ALL of these hold: the policy turns
  auto-promotion on and covers the patch's application family; the
  signature chain verified at import; the patch touches no identity-evidence
  anchor; the contract invariants hold; and the canary on this tenant's own
  qualification cases is green (every healthy case ``VERIFIED`` with zero
  model calls, every fault case refused, and at least the policy's minimum
  number of each). Promotion then runs the existing lifecycle: policy review,
  policy approval bound to the exact bundle digests and the policy digest,
  staging by hash, and the bounded live canary window, which still
  auto-reverts on any regression;
* **needs a person** in every other case that is not blocked. The canary has
  already run and its results are on the candidate for the reviewer;
* **blocked** when the canary failed or the contract check rejected the
  candidate. Approval refuses a candidate whose campaigns did not pass, by a
  person or by policy.

The policy is a local file approved by a named person. Its SHA-256 is
recorded on every approval it grants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.fleet.canary import (
    QualificationCase,
    TenantCanaryRunner,
    canary_patch,
)
from openadapt_flow.repair.candidate import RepairCandidate
from openadapt_flow.repair.lifecycle import (
    PolicyPreApproval,
    RepairLifecycleError,
    RepairStore,
)


class FleetPromotionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.promotion-policy/v1"] = (
        "openadapt.fleet.promotion-policy/v1"
    )
    #: The person who approved this policy. Recorded on every approval.
    approved_by: str = Field(min_length=1, max_length=200)
    approved_at: str = Field(min_length=10, max_length=64)
    auto_promote: bool = False
    app_families: tuple[str, ...] = ()
    min_healthy_cases: int = Field(default=3, ge=1, le=1000)
    min_fault_cases: int = Field(default=1, ge=1, le=1000)
    canary_max_runs: int = Field(default=5, ge=1, le=1000)

    def policy_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def grant(self) -> PolicyPreApproval:
        return PolicyPreApproval(
            policy_sha256=self.policy_sha256(), approved_by=self.approved_by
        )

    @classmethod
    def load(cls, path: Path | str) -> "FleetPromotionPolicy":
        return cls.model_validate_json(Path(path).read_text())


Outcome = Literal["auto_promoted", "needs_person", "blocked"]


@dataclass
class PromotionDecision:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    candidate: RepairCandidate | None = None


def _pre_canary_reasons(
    policy: FleetPromotionPolicy, candidate: RepairCandidate
) -> list[str]:
    """Why the policy cannot stand in for a person, before any canary."""
    reasons: list[str] = []
    fleet = candidate.fleet_patch
    if not policy.auto_promote:
        reasons.append("this tenant's policy does not allow auto-promotion")
    if candidate.source != "fleet_patch" or fleet is None:
        reasons.append("not an imported fleet patch")
        return reasons
    if fleet.identity_evidence_changed or any(
        change.identity for change in candidate.binding_changes
    ):
        reasons.append("the patch touches identity evidence")
    if candidate.contract_weakenings:
        reasons.append("the patch weakens the safety contract")
    return reasons


def _family_reason(policy: FleetPromotionPolicy, app_family: str) -> list[str]:
    if app_family not in policy.app_families:
        return [f"the policy does not cover application family {app_family}"]
    return []


def _canary_reasons(
    policy: FleetPromotionPolicy, candidate: RepairCandidate
) -> tuple[list[str], list[str]]:
    """(blocking reasons, reasons that only need a person)."""
    blocking: list[str] = []
    person: list[str] = []
    replay, fault = candidate.replay_campaign, candidate.fault_campaign
    if replay is None or fault is None:
        blocking.append("the canary did not run")
        return blocking, person
    if not replay.passed:
        blocking.append(f"canary: {replay.summary()}")
    if not fault.passed:
        blocking.append(f"canary: {fault.summary()}")
    if replay.total < policy.min_healthy_cases:
        person.append(
            f"only {replay.total} healthy case(s); policy needs {policy.min_healthy_cases}"
        )
    if fault.total < policy.min_fault_cases:
        person.append(
            f"only {fault.total} fault case(s); policy needs {policy.min_fault_cases}"
        )
    return blocking, person


def process_imported_patch(
    store: RepairStore,
    candidate_id: str,
    *,
    policy: FleetPromotionPolicy,
    runner: TenantCanaryRunner,
    cases: Sequence[QualificationCase],
    work_dir: Path,
) -> PromotionDecision:
    """Run the canary automatically, then promote only if the policy allows."""
    candidate = store.load_candidate(candidate_id)
    if candidate.state == "rejected":
        return PromotionDecision(
            "blocked", [candidate.rejection_reason or "rejected"], candidate
        )
    if candidate.state != "candidate":
        raise RepairLifecycleError(
            f"candidate {candidate_id} is already {candidate.state!r}"
        )
    person = _pre_canary_reasons(policy, candidate)
    if candidate.fleet_patch is not None:
        person += _family_reason(policy, candidate.fleet_patch.app_family)
    grant = policy.grant()
    if not person:
        candidate = store.review_under_policy(candidate_id, grant)

    candidate = canary_patch(store, candidate_id, runner, cases, work_dir)
    blocking, person_after = _canary_reasons(policy, candidate)
    if blocking:
        return PromotionDecision("blocked", blocking, candidate)
    person += person_after
    if person:
        return PromotionDecision("needs_person", person, candidate)

    candidate = store.approve_under_policy(candidate_id, grant)
    candidate = store.stage(candidate_id, grant.actor)
    candidate = store.start_canary(
        candidate_id, grant.actor, max_runs=policy.canary_max_runs
    )
    return PromotionDecision(
        "auto_promoted",
        [
            "pre-approved by policy "
            f"{grant.policy_sha256[:16]} (approved by {policy.approved_by}); "
            f"live canary window of {policy.canary_max_runs} runs started"
        ],
        candidate,
    )


__all__ = [
    "FleetPromotionPolicy",
    "PromotionDecision",
    "process_imported_patch",
]
