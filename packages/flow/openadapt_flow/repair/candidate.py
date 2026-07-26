"""The :class:`RepairCandidate` artifact and its supporting records.

A repair candidate is the unit the governed promotion lifecycle rules on. It
describes ONE proposed bundle revision (prior bundle -> proposed bundle,
identified by exact whole-bundle content digests) together with:

- WHAT changed: per-step, per-field binding changes. Values are stored as
  SHA-256 digests, never raw, so the record is privacy-safe by construction;
  a reviewer renders the full diff live from the two local bundles.
- WHY the prior binding failed: privacy-safe failure fingerprints (structured
  labels plus a digest of the evidence, never raw observations) and evidence
  references (path + hash, never content).
- WHAT evidence supports the new binding: campaign results, supporting
  evidence references, and the approval record.

A candidate NEVER auto-activates. Its ``state`` only advances through the
transitions enforced by :class:`openadapt_flow.repair.lifecycle.RepairStore`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.qualification import EvidenceRef

CANDIDATE_SCHEMA = "openadapt.repair-candidate/v1"

#: The explicit repair promotion lifecycle (roadmap Section 9). ``rejected``
#: and ``rolled_back`` are the refusal / recovery states.
RepairState = Literal[
    "candidate",
    "reviewed",
    "replay_passed",
    "fault_passed",
    "approved",
    "staged",
    "canary",
    "active",
    "rejected",
    "rolled_back",
]

RepairSource = Literal["heal", "teach", "manual", "model_suggestion"]

ActorKind = Literal["human", "automation", "model"]

_SHA256 = r"^[a-f0-9]{64}$"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def value_digest(value: Any) -> Optional[str]:
    """Privacy-safe digest of an arbitrary bound value (None stays None)."""
    if value is None:
        return None
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_hex(encoded.encode())


def candidate_id_for(prior_digest: str, proposed_digest: str) -> str:
    """Deterministic candidate id for one (prior, proposed) bundle pair."""
    return sha256_hex(f"{prior_digest}:{proposed_digest}".encode())[:16]


class Actor(BaseModel):
    """Who is driving a lifecycle transition.

    ``kind == "model"`` is refused for EVERY transition (a runtime model
    suggestion must never actuate a repair or promote itself). ``automation``
    may run campaigns and mechanical steps but never review or approve.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ActorKind
    id: str = Field(min_length=1, max_length=256)


class FailureFingerprint(BaseModel):
    """Privacy-safe fingerprint of one observed failure.

    Carries only bounded structural labels plus a digest of the evidence.
    Raw observations (frames, OCR text, identity bands) never enter this
    record, so it is safe to sync to Desktop / runners / Cloud.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=256)
    failure_class: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(pattern=_SHA256)

    @classmethod
    def from_evidence(
        cls, step_id: str, failure_class: str, evidence: str
    ) -> "FailureFingerprint":
        digest = sha256_hex(f"{step_id}\x00{failure_class}\x00{evidence}".encode())
        return cls(step_id=step_id, failure_class=failure_class, fingerprint=digest)


class BindingChange(BaseModel):
    """One changed anchor/binding field on one step (digests, never values)."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=256)
    field: str = Field(min_length=1, max_length=64)
    identity: bool = Field(description="True when this field carries identity evidence")
    old_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    new_sha256: Optional[str] = Field(default=None, pattern=_SHA256)


class ContractWeakening(BaseModel):
    """One detected weakening of the prior bundle's safety contract."""

    model_config = ConfigDict(extra="forbid")

    dimension: Literal["identity", "effect", "risk", "environment", "policy"]
    detail: str = Field(min_length=1, max_length=512)


class CampaignCaseResult(BaseModel):
    """One case verdict inside a replay or fault campaign."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    passed: bool
    detail: str = Field(default="", max_length=512)


class CampaignResult(BaseModel):
    """Recorded outcome of one campaign run against a candidate."""

    model_config = ConfigDict(extra="forbid")

    campaign: Literal["replay", "fault"]
    passed: bool
    total: int = Field(ge=0)
    failed: int = Field(ge=0)
    cases: list[CampaignCaseResult] = Field(default_factory=list)
    completed_at: str = Field(default_factory=_now)

    def summary(self) -> str:
        verdict = "PASSED" if self.passed else "FAILED"
        return (
            f"{self.campaign} campaign {verdict}: "
            f"{self.total - self.failed}/{self.total} cases passed"
        )


class ApprovalRecord(BaseModel):
    """The explicit human approval that authorizes promotion."""

    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(min_length=1, max_length=256)
    approved_at: str = Field(default_factory=_now)
    non_interactive: bool = False
    #: The exact hashes the approval binds to; a later bundle byte change
    #: invalidates the approval (checked at stage / canary time).
    prior_content_digest: str = Field(pattern=_SHA256)
    proposed_content_digest: str = Field(pattern=_SHA256)


class CanaryConfig(BaseModel):
    """Bounded first-N-runs window for a newly activated bundle."""

    model_config = ConfigDict(extra="forbid")

    max_runs: int = Field(default=5, ge=1, le=1000)


class CanaryMetrics(BaseModel):
    """Per-run verification tallies while the candidate is in canary."""

    model_config = ConfigDict(extra="forbid")

    runs_completed: int = Field(default=0, ge=0)
    verified_runs: int = Field(default=0, ge=0)
    silent_incorrect: int = Field(default=0, ge=0)
    verification_regressions: int = Field(default=0, ge=0)
    halted: bool = False
    halt_reason: Optional[str] = Field(default=None, max_length=512)
    last_run_at: Optional[str] = None


class CanaryRunObservation(BaseModel):
    """What one canary run showed, distilled to the two halt triggers."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=256)
    verified: bool
    silent_incorrect: bool
    detail: str = Field(default="", max_length=512)


class TransitionRecord(BaseModel):
    """One audited lifecycle transition."""

    model_config = ConfigDict(extra="forbid")

    from_state: RepairState
    to_state: RepairState
    at: str = Field(default_factory=_now)
    actor_kind: ActorKind
    actor_id: str = Field(min_length=1, max_length=256)
    note: str = Field(default="", max_length=512)


class RepairCandidate(BaseModel):
    """One proposed bundle revision moving through the governed lifecycle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.repair-candidate/v1"] = (
        "openadapt.repair-candidate/v1"
    )
    candidate_id: str = Field(min_length=8, max_length=64)
    created_at: str = Field(default_factory=_now)
    source: RepairSource

    workflow_name: str = Field(min_length=1, max_length=256)
    prior_content_digest: str = Field(pattern=_SHA256)
    proposed_content_digest: str = Field(pattern=_SHA256)
    #: Local path hints for rendering diffs and staging. Advisory only; the
    #: digests above are the authoritative identity for sync.
    prior_bundle_path: Optional[str] = None
    proposed_bundle_path: Optional[str] = None
    #: Compatibility scope: the qualification environment contract this
    #: repair claims to stay inside (when the bundle carries one).
    environment_contract_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    qualification_revision_prior: Optional[int] = Field(default=None, ge=1)
    qualification_revision_proposed: Optional[int] = Field(default=None, ge=1)

    binding_changes: list[BindingChange] = Field(default_factory=list)
    failure_evidence: list[EvidenceRef] = Field(default_factory=list)
    failure_fingerprints: list[FailureFingerprint] = Field(default_factory=list)
    supporting_evidence: list[EvidenceRef] = Field(default_factory=list)
    #: Run directory holding the heal frames the campaigns replay against.
    evidence_run_dir: Optional[str] = None
    contract_weakenings: list[ContractWeakening] = Field(default_factory=list)

    state: RepairState = "candidate"
    history: list[TransitionRecord] = Field(default_factory=list)
    reviewed_by: Optional[str] = Field(default=None, max_length=256)
    replay_campaign: Optional[CampaignResult] = None
    fault_campaign: Optional[CampaignResult] = None
    approval: Optional[ApprovalRecord] = None
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    canary_metrics: CanaryMetrics = Field(default_factory=CanaryMetrics)
    rejection_reason: Optional[str] = Field(default=None, max_length=2048)

    def record_sha256(self) -> str:
        """Digest of the full candidate record, for deterministic sync."""
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return sha256_hex(encoded)

    def campaigns_passed(self) -> bool:
        return bool(
            self.replay_campaign is not None
            and self.replay_campaign.passed
            and self.fault_campaign is not None
            and self.fault_campaign.passed
        )

    def diff_summary(self) -> str:
        """A short reviewable summary (digests only; render values from disk)."""
        lines = [
            f"RepairCandidate {self.candidate_id} [{self.state}] "
            f"source={self.source} workflow={self.workflow_name!r}",
            f"  prior    {self.prior_content_digest}",
            f"  proposed {self.proposed_content_digest}",
        ]
        if self.contract_weakenings:
            lines.append(
                "  contract weakenings (require a new qualification revision):"
            )
            for weakening in self.contract_weakenings:
                lines.append(f"    - [{weakening.dimension}] {weakening.detail}")
        if self.rejection_reason:
            lines.append(f"  REJECTED: {self.rejection_reason}")
        identity_changes = [c for c in self.binding_changes if c.identity]
        locator_changes = [c for c in self.binding_changes if not c.identity]
        if identity_changes:
            lines.append("  identity binding changes:")
            for change in identity_changes:
                lines.append(f"    - {change.step_id}.{change.field}")
        if locator_changes:
            lines.append("  locator binding changes:")
            for change in locator_changes:
                lines.append(f"    - {change.step_id}.{change.field}")
        for campaign in (self.replay_campaign, self.fault_campaign):
            if campaign is not None:
                lines.append(f"  {campaign.summary()}")
        if self.approval is not None:
            lines.append(f"  approved by {self.approval.approved_by}")
        return "\n".join(lines)
