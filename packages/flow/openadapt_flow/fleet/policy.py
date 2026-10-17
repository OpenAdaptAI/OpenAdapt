"""This tenant's signed policy for patches that arrive from the fleet.

A patch never promotes itself. After import it is a ``fleet_patch``
candidate, and :func:`process_imported_patch` takes it as far as this
tenant's policy allows:

* **auto-promote** only when ALL of these hold:

  - the policy file is signed by a person this tenant's role registry lists
    as a policy approver, who is not also a workflow author, and records
    that person's name and title and an attestation that the organisation's
    security official authorised it (:func:`verify_policy`);
  - the policy turns auto-promotion on and covers the patch's application
    family;
  - a fresh, verified revocation list shows no certificate in the patch's
    chain as revoked, and no verified kill switch has turned auto-promotion
    off. This is checked before the policy review and again right before
    the policy approval. Stale, unreachable, or unverifiable status fails
    closed;
  - the patch touches no identity-evidence anchor and the contract
    invariants hold;
  - the canary on this tenant's own qualification cases is green (every
    healthy case ``VERIFIED`` with zero model calls, every fault case
    refused, and at least the policy's minimum number of each).

  Promotion then runs the existing lifecycle: policy review, policy approval
  bound to the exact bundle digests and the policy digest, staging by hash,
  and the bounded live canary window, which still auto-reverts on any
  regression;
* **needs a person** in every other case that is not blocked. The canary has
  already run and its results are on the candidate for the reviewer;
* **blocked** when the canary failed, the contract check rejected the
  candidate, or a verified kill switch rolled the patch back.

:func:`apply_fleet_kill_switches` rolls back or rejects every local
candidate made from a patch a verified kill switch names.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.fleet.canary import (
    QualificationCase,
    TenantCanaryRunner,
    canary_patch,
)
from openadapt_flow.fleet.signing import (
    FleetTrust,
    KeyCustody,
    PatchSignatureError,
    key_id,
    sign_document,
    verify_document,
)
from openadapt_flow.fleet.status import (
    FleetStatus,
    FleetStatusSource,
    read_fleet_status,
)
from openadapt_flow.repair.candidate import Actor, RepairCandidate
from openadapt_flow.repair.lifecycle import (
    PolicyPreApproval,
    RepairLifecycleError,
    RepairStore,
)

POLICY_DOMAIN: Final[bytes] = b"openadapt.fleet.promotion-policy/v2\x00"
POLICY_FILE: Final[str] = "promotion-policy.json"
ROLES_FILE: Final[str] = "roles.json"
#: The exact attestation a policy approver signs.
SECURITY_ATTESTATION: Final[str] = (
    "Our organisation's security official authorised this fleet promotion "
    "policy, including its auto_promote setting."
)
KILL_SWITCH_ACTOR: Final[Actor] = Actor(kind="automation", id="fleet-kill-switch")
_PERSON_ID = r"^[a-z0-9][a-z0-9._-]{1,63}$"


class PolicyVerificationError(ValueError):
    """The policy file is not signed by a permitted approver."""


class PolicyApprover(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    person_id: str = Field(pattern=_PERSON_ID)
    name: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)


class SecurityOfficialAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    official_name: str = Field(min_length=1, max_length=200)
    official_title: str = Field(min_length=1, max_length=200)
    statement: Literal[
        "Our organisation's security official authorised this fleet promotion "
        "policy, including its auto_promote setting."
    ] = SECURITY_ATTESTATION  # type: ignore[assignment]
    authorised_at: str = Field(min_length=10, max_length=64)


class FleetPromotionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.promotion-policy/v2"] = (
        "openadapt.fleet.promotion-policy/v2"
    )
    approver: PolicyApprover
    security_official: SecurityOfficialAttestation
    approved_at: str = Field(min_length=10, max_length=64)
    auto_promote: bool = False
    app_families: tuple[str, ...] = ()
    min_healthy_cases: int = Field(default=3, ge=1, le=1000)
    min_fault_cases: int = Field(default=1, ge=1, le=1000)
    canary_max_runs: int = Field(default=5, ge=1, le=1000)
    #: Oldest revocation list this tenant accepts.
    max_status_age_hours: int = Field(default=24, ge=1, le=24)

    @model_validator(mode="after")
    def _separate(self) -> "FleetPromotionPolicy":
        if self.security_official.official_name.strip().lower() == (
            self.approver.name.strip().lower()
        ):
            raise ValueError(
                "the security official who authorised the policy is not its approver"
            )
        return self

    @property
    def approved_by(self) -> str:
        return f"{self.approver.name} ({self.approver.title})"

    def policy_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def grant(self) -> PolicyPreApproval:
        return PolicyPreApproval(
            policy_sha256=self.policy_sha256(), approved_by=self.approved_by
        )


class SignedPromotionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: FleetPromotionPolicy
    signer_key_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    signature: str = Field(min_length=1, max_length=128)

    @classmethod
    def load(cls, path: Path | str) -> "SignedPromotionPolicy":
        return cls.model_validate_json(Path(path).read_text())

    def save(self, path: Path | str) -> None:
        _atomic_write(Path(path), self.model_dump_json(indent=2) + "\n")


def sign_policy(policy: FleetPromotionPolicy, key: KeyCustody) -> SignedPromotionPolicy:
    return SignedPromotionPolicy(
        policy=policy,
        signer_key_id=key_id(key.public_key_b64()),
        signature=sign_document(key, POLICY_DOMAIN, policy),
    )


class RoleRegistry(BaseModel):
    """Who may author workflows and who may approve fleet policy, locally.

    Kept by this tenant's admin. One person cannot hold both roles.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.roles/v1"] = "openadapt.fleet.roles/v1"
    workflow_authors: tuple[str, ...] = ()
    #: person id -> base64 Ed25519 public key that person signs policy with.
    policy_approvers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _separate(self) -> "RoleRegistry":
        both = set(self.workflow_authors) & set(self.policy_approvers)
        if both:
            raise ValueError(
                "a workflow author cannot also approve fleet policy: "
                + ", ".join(sorted(both))
            )
        return self

    @classmethod
    def load(cls, path: Path | str) -> "RoleRegistry":
        file = Path(path)
        if not file.is_file():
            return cls()
        return cls.model_validate_json(file.read_text())

    def save(self, path: Path | str) -> None:
        _atomic_write(Path(path), self.model_dump_json(indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_policy(
    signed: SignedPromotionPolicy, roles: RoleRegistry
) -> FleetPromotionPolicy:
    """Return the policy if a permitted approver signed it; raise otherwise."""
    policy = signed.policy
    person = policy.approver.person_id
    if person in roles.workflow_authors:
        raise PolicyVerificationError(
            "the policy approver is also a workflow author on this tenant"
        )
    public = roles.policy_approvers.get(person)
    if public is None:
        raise PolicyVerificationError(
            f"{person} is not a policy approver in this tenant's role registry"
        )
    if key_id(public) != signed.signer_key_id:
        raise PolicyVerificationError("the policy was signed with another key")
    try:
        verify_document(public, POLICY_DOMAIN, policy, signed.signature, "policy")
    except PatchSignatureError as exc:
        raise PolicyVerificationError(str(exc)) from exc
    return policy


Outcome = Literal["auto_promoted", "needs_person", "blocked"]


@dataclass
class PromotionDecision:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    candidate: RepairCandidate | None = None
    status: Optional[FleetStatus] = None


def _pre_canary_reasons(
    policy: Optional[FleetPromotionPolicy], candidate: RepairCandidate
) -> list[str]:
    """Why the policy cannot stand in for a person, before any canary."""
    reasons: list[str] = []
    fleet = candidate.fleet_patch
    if policy is not None and not policy.auto_promote:
        reasons.append("this tenant's policy does not allow auto-promotion")
    if candidate.source != "fleet_patch" or fleet is None:
        reasons.append("not an imported fleet patch")
        return reasons
    if policy is not None and fleet.app_family not in policy.app_families:
        reasons.append(
            f"the policy does not cover application family {fleet.app_family}"
        )
    if fleet.identity_evidence_changed or any(
        change.identity for change in candidate.binding_changes
    ):
        reasons.append("the patch touches identity evidence")
    if candidate.contract_weakenings:
        reasons.append("the patch weakens the safety contract")
    return reasons


def _status_reasons(status: FleetStatus, candidate: RepairCandidate) -> list[str]:
    """Why fleet status stops auto-promotion. Any reason means a person decides."""
    reasons = [f"fleet status: {p}" for p in status.problems]
    if status.auto_promotion_disabled:
        reasons.append("a fleet kill switch has turned auto-promotion off")
    fleet = candidate.fleet_patch
    if fleet is not None:
        revoked = status.revoked_key_ids
        if fleet.intermediate_key_id in revoked:
            reasons.append("the patch's intermediate certificate is revoked")
        if (
            fleet.signer_key_id in revoked
            or fleet.signer_serial in status.revoked_serials
        ):
            reasons.append("the patch's distribution certificate is revoked")
    return reasons


def _canary_reasons(
    min_healthy: int, min_fault: int, candidate: RepairCandidate
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
    if replay.total < min_healthy:
        person.append(
            f"only {replay.total} healthy case(s); policy needs {min_healthy}"
        )
    if fault.total < min_fault:
        person.append(f"only {fault.total} fault case(s); policy needs {min_fault}")
    return blocking, person


def _read_status(
    source: FleetStatusSource,
    trust: FleetTrust,
    policy: Optional[FleetPromotionPolicy],
    now: Optional[datetime],
) -> FleetStatus:
    hours = policy.max_status_age_hours if policy is not None else 24
    return read_fleet_status(source, trust, now=now, max_age=timedelta(hours=hours))


def process_imported_patch(
    store: RepairStore,
    candidate_id: str,
    *,
    policy: Optional[SignedPromotionPolicy],
    roles: RoleRegistry,
    trust: FleetTrust,
    status_source: FleetStatusSource,
    runner: TenantCanaryRunner,
    cases: Sequence[QualificationCase],
    work_dir: Path,
    now: Optional[datetime] = None,
) -> PromotionDecision:
    """Run the canary automatically, then promote only if everything allows it."""
    candidate = store.load_candidate(candidate_id)
    if candidate.state == "rejected":
        return PromotionDecision(
            "blocked", [candidate.rejection_reason or "rejected"], candidate
        )
    if candidate.state != "candidate":
        raise RepairLifecycleError(
            f"candidate {candidate_id} is already {candidate.state!r}"
        )

    person: list[str] = []
    verified: Optional[FleetPromotionPolicy] = None
    if policy is None:
        person.append("this tenant has no signed promotion policy")
    else:
        try:
            verified = verify_policy(policy, roles)
        except PolicyVerificationError as exc:
            person.append(f"the promotion policy does not verify: {exc}")
    person += _pre_canary_reasons(verified, candidate)

    status = _read_status(status_source, trust, verified, now)
    fleet = candidate.fleet_patch
    if fleet is not None and fleet.patch_id in status.rollback_patch_ids:
        candidate = store.reject(
            candidate_id,
            KILL_SWITCH_ACTOR,
            "a fleet kill switch rolled this patch back",
        )
        return PromotionDecision(
            "blocked", ["a fleet kill switch rolled this patch back"], candidate, status
        )
    person += _status_reasons(status, candidate)

    grant = verified.grant() if verified is not None else None
    if not person and grant is not None:
        candidate = store.review_under_policy(candidate_id, grant)

    candidate = canary_patch(store, candidate_id, runner, cases, work_dir)
    min_healthy = verified.min_healthy_cases if verified is not None else 3
    min_fault = verified.min_fault_cases if verified is not None else 1
    blocking, person_after = _canary_reasons(min_healthy, min_fault, candidate)
    if blocking:
        return PromotionDecision("blocked", blocking, candidate, status)
    person += person_after
    if not person:
        # Fresh check right before the approval: the canary takes time.
        status = _read_status(status_source, trust, verified, now)
        person += _status_reasons(status, candidate)
        if candidate.fleet_patch is not None and (
            candidate.fleet_patch.patch_id in status.rollback_patch_ids
        ):
            person.append("a fleet kill switch rolled this patch back")
    if person or grant is None or verified is None:
        return PromotionDecision("needs_person", person, candidate, status)

    candidate = store.approve_under_policy(candidate_id, grant)
    candidate = store.stage(candidate_id, grant.actor)
    candidate = store.start_canary(
        candidate_id, grant.actor, max_runs=verified.canary_max_runs
    )
    return PromotionDecision(
        "auto_promoted",
        [
            f"pre-approved by policy {grant.policy_sha256[:16]} (approved by "
            f"{verified.approved_by}, authorised by security official "
            f"{verified.security_official.official_name}); revocation list "
            f"#{status.revocation_sequence} fresh; live canary window of "
            f"{verified.canary_max_runs} runs started"
        ],
        candidate,
        status,
    )


def apply_fleet_kill_switches(store: RepairStore, status: FleetStatus) -> list[str]:
    """Roll back or reject local candidates made from rolled-back patches.

    Returns one line per candidate changed. Rollback ids come only from
    verified kill-switch entries, so this runs even when the revocation list
    is stale.
    """
    changed: list[str] = []
    for candidate in store.list_candidates():
        fleet = candidate.fleet_patch
        if fleet is None or fleet.patch_id not in status.rollback_patch_ids:
            continue
        if candidate.state in ("canary", "active"):
            store.rollback(KILL_SWITCH_ACTOR, candidate.candidate_id)
            changed.append(f"{candidate.candidate_id}: rolled back")
        elif candidate.state not in ("rejected", "rolled_back"):
            store.reject(
                candidate.candidate_id,
                KILL_SWITCH_ACTOR,
                "a fleet kill switch rolled this patch back",
            )
            changed.append(f"{candidate.candidate_id}: rejected")
    return changed


__all__ = [
    "FleetPromotionPolicy",
    "KILL_SWITCH_ACTOR",
    "POLICY_FILE",
    "PolicyApprover",
    "PolicyVerificationError",
    "PromotionDecision",
    "ROLES_FILE",
    "RoleRegistry",
    "SECURITY_ATTESTATION",
    "SecurityOfficialAttestation",
    "SignedPromotionPolicy",
    "apply_fleet_kill_switches",
    "process_imported_patch",
    "sign_policy",
    "verify_policy",
]
