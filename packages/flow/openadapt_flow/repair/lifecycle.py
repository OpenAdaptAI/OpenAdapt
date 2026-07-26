"""The repair promotion state machine, store, canary, and rollback.

The :class:`RepairStore` owns every lifecycle transition:

    candidate -> reviewed -> replay_passed -> fault_passed -> approved
              -> staged -> canary -> active

with ``rejected`` reachable from every pre-staged state and ``rolled_back``
from ``canary`` / ``active``. Nothing else is a legal move; a candidate can
never skip a gate, and a candidate NEVER advances as a side effect of being
written to disk.

Hard rules enforced here:

- **No model self-promotion.** Every transition asserts the actor is not a
  model (:func:`assert_not_model_actor`). Review and approval additionally
  require a HUMAN actor. A runtime model suggestion can therefore never
  actuate a repair or promote itself.
- **Approval binds exact hashes.** The approval record carries the prior and
  proposed whole-bundle content digests; staging and activation re-verify
  them byte-for-byte, so what runs is exactly what was approved (and Desktop
  / runner / Cloud sync can key on the same digests deterministically).
- **Activation is atomic.** The active pointer (``ACTIVE.json``) is replaced
  with ``os.replace`` after the staged bundle re-verifies; it retains lineage
  (prior digest + bundle) so rollback is one command.
- **Canary is bounded and self-halting.** The first ``max_runs`` runs after
  activation are per-run verified; any silent-incorrect or verification
  regression automatically reverts the pointer to the prior bundle and drops
  the candidate back to ``staged``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.repair.candidate import (
    Actor,
    ApprovalRecord,
    CampaignResult,
    CanaryRunObservation,
    RepairCandidate,
    RepairState,
    TransitionRecord,
)
from openadapt_flow.repair.invariants import enforce_contract_invariants

_ACTIVE_NAME = "ACTIVE.json"
_CANDIDATES_DIR = "candidates"
_BUNDLES_DIR = "bundles"


class RepairLifecycleError(ValueError):
    """An illegal lifecycle transition or unmet gate; the move is refused."""


class ModelActuationError(RepairLifecycleError):
    """A model actor attempted a lifecycle transition. Hard refusal.

    A runtime model suggestion must never actuate a repair directly or
    promote itself; it may only produce a candidate for humans to review.
    """


def assert_not_model_actor(actor: Actor, operation: str) -> None:
    """Refuse ANY lifecycle operation driven by a model actor."""
    if actor.kind == "model":
        raise ModelActuationError(
            f"refused: a model actor may never perform {operation!r}. Model "
            "suggestions can only create candidates; every lifecycle "
            "transition requires a human or supervised automation actor, and "
            "review/approval require a human."
        )


def _require_human(actor: Actor, operation: str) -> None:
    assert_not_model_actor(actor, operation)
    if actor.kind != "human":
        raise RepairLifecycleError(
            f"refused: {operation} requires a human actor with an explicit "
            f"identity (got actor kind {actor.kind!r})"
        )


#: Legal transitions. Anything absent here is refused.
_ALLOWED: dict[RepairState, tuple[RepairState, ...]] = {
    "candidate": ("reviewed", "rejected"),
    "reviewed": ("replay_passed", "rejected"),
    "replay_passed": ("fault_passed", "rejected"),
    "fault_passed": ("approved", "rejected"),
    "approved": ("staged", "rejected"),
    "staged": ("canary", "rejected"),
    "canary": ("active", "staged", "rolled_back"),
    "active": ("rolled_back",),
    "rejected": (),
    "rolled_back": (),
}


class ActivePointer(BaseModel):
    """The atomic active-bundle pointer, with lineage for rollback."""

    model_config = ConfigDict(extra="forbid")

    workflow_name: str
    candidate_id: str
    active_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    active_bundle: str = Field(description="store-relative bundle path")
    prior_digest: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    prior_bundle: Optional[str] = None
    mode: Literal["canary", "active"]
    activated_at: str
    note: str = ""


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def observation_from_report(report: RunReport) -> CanaryRunObservation:
    """Distill a run report into the canary's two halt triggers (fail-safe).

    ``verified`` is true only for an explicitly VERIFIED outcome (or, for a
    report predating the outcome taxonomy, a fully successful run).
    ``silent_incorrect`` is true when the report shows a business effect that
    completed without verification or conflicts with the system of record.
    """
    if report.execution_outcome is not None:
        verified = report.execution_outcome == "VERIFIED"
    else:
        verified = bool(report.success)

    silent = False
    detail = ""
    if report.transaction_outcome in (
        "COMPLETED_UNVERIFIED",
        "RECONCILIATION_REQUIRED",
    ):
        silent = True
        detail = f"transaction outcome {report.transaction_outcome}"
    elif report.execution_outcome == "COMPLETED_UNVERIFIED":
        silent = True
        detail = "execution outcome COMPLETED_UNVERIFIED"
    else:
        for result in report.results:
            for evidence in result.effect_evidence:
                if evidence.observed_effect == "conflicting":
                    silent = True
                    detail = (
                        f"step {result.step_id!r} observed a conflicting "
                        "business effect"
                    )
                    break
            if silent:
                break
    return CanaryRunObservation(
        run_id=report.started_at or "unknown-run",
        verified=verified,
        silent_incorrect=silent,
        detail=detail,
    )


class RepairStore:
    """Filesystem store for repair candidates, staged bundles, and the pointer.

    Layout under ``root``::

        candidates/<candidate_id>.json
        bundles/<content_digest>/...   (immutable staged copies)
        ACTIVE.json                    (atomic active-bundle pointer)
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- paths ---------------------------------------------------------------

    def candidate_path(self, candidate_id: str) -> Path:
        return self.root / _CANDIDATES_DIR / f"{candidate_id}.json"

    def bundle_path(self, content_digest: str) -> Path:
        return self.root / _BUNDLES_DIR / content_digest

    @property
    def active_path(self) -> Path:
        return self.root / _ACTIVE_NAME

    # -- persistence ---------------------------------------------------------

    def save_candidate(self, candidate: RepairCandidate) -> Path:
        path = self.candidate_path(candidate.candidate_id)
        _write_atomic(path, candidate.model_dump_json(indent=2))
        return path

    def load_candidate(self, candidate_id: str) -> RepairCandidate:
        path = self.candidate_path(candidate_id)
        if not path.is_file():
            raise RepairLifecycleError(
                f"no repair candidate {candidate_id!r} in store {self.root}"
            )
        return RepairCandidate.model_validate_json(path.read_text())

    def list_candidates(self) -> list[RepairCandidate]:
        directory = self.root / _CANDIDATES_DIR
        if not directory.is_dir():
            return []
        candidates = [
            RepairCandidate.model_validate_json(path.read_text())
            for path in sorted(directory.glob("*.json"))
        ]
        return candidates

    def add_candidate(self, candidate: RepairCandidate) -> RepairCandidate:
        """Admit a candidate record into the store (idempotent by id)."""
        existing = self.candidate_path(candidate.candidate_id)
        if existing.is_file():
            return self.load_candidate(candidate.candidate_id)
        self.save_candidate(candidate)
        return candidate

    def active_pointer(self) -> Optional[ActivePointer]:
        if not self.active_path.is_file():
            return None
        return ActivePointer.model_validate_json(self.active_path.read_text())

    # -- transitions ---------------------------------------------------------

    def _transition(
        self,
        candidate: RepairCandidate,
        to_state: RepairState,
        actor: Actor,
        note: str = "",
    ) -> RepairCandidate:
        assert_not_model_actor(actor, f"transition to {to_state}")
        allowed = _ALLOWED.get(candidate.state, ())
        if to_state not in allowed:
            raise RepairLifecycleError(
                f"illegal transition {candidate.state} -> {to_state} for "
                f"candidate {candidate.candidate_id} (allowed: "
                f"{', '.join(allowed) if allowed else 'none'})"
            )
        candidate.history.append(
            TransitionRecord(
                from_state=candidate.state,
                to_state=to_state,
                actor_kind=actor.kind,
                actor_id=actor.id,
                note=note,
            )
        )
        candidate.state = to_state
        self.save_candidate(candidate)
        return candidate

    def review(self, candidate_id: str, actor: Actor) -> RepairCandidate:
        """A human reviewed the candidate's diff (candidate -> reviewed)."""
        _require_human(actor, "repair review")
        candidate = self.load_candidate(candidate_id)
        candidate.reviewed_by = actor.id
        return self._transition(candidate, "reviewed", actor, note="diff reviewed")

    def reject(self, candidate_id: str, actor: Actor, reason: str) -> RepairCandidate:
        candidate = self.load_candidate(candidate_id)
        candidate.rejection_reason = reason
        return self._transition(candidate, "rejected", actor, note=reason)

    def record_campaign(
        self, candidate_id: str, result: CampaignResult, actor: Actor
    ) -> RepairCandidate:
        """Record a campaign result; advance only when the campaign PASSED.

        A failed campaign is recorded on the candidate (auditable evidence)
        and the state does NOT advance; approval will refuse.
        """
        assert_not_model_actor(actor, "record campaign result")
        candidate = self.load_candidate(candidate_id)
        if result.campaign == "replay":
            candidate.replay_campaign = result
            if result.passed and candidate.state == "reviewed":
                return self._transition(
                    candidate, "replay_passed", actor, note=result.summary()
                )
        else:
            candidate.fault_campaign = result
            if result.passed and candidate.state == "replay_passed":
                return self._transition(
                    candidate, "fault_passed", actor, note=result.summary()
                )
        self.save_candidate(candidate)
        return candidate

    def _reverify_invariants(self, candidate: RepairCandidate) -> None:
        """Defense in depth: re-diff the two bundles from disk, fail closed."""
        prior_path = candidate.prior_bundle_path
        proposed_path = candidate.proposed_bundle_path
        if not prior_path or not proposed_path:
            raise RepairLifecycleError(
                "refused (fail closed): candidate carries no bundle paths to "
                "re-verify the contract invariants against"
            )
        prior = Path(prior_path)
        proposed = Path(proposed_path)
        if not prior.is_dir() or not proposed.is_dir():
            raise RepairLifecycleError(
                "refused (fail closed): candidate bundle path(s) are missing; "
                "cannot re-verify contract invariants"
            )
        prior_wf = Workflow.load(prior)
        proposed_wf = Workflow.load(proposed)
        self._require_digest(prior_wf, candidate.prior_content_digest, "prior")
        self._require_digest(proposed_wf, candidate.proposed_content_digest, "proposed")
        enforce_contract_invariants(prior_wf, proposed_wf)

    @staticmethod
    def _require_digest(workflow: Workflow, expected: str, label: str) -> None:
        actual = workflow.manifest.content_digest if workflow.manifest else ""
        if actual != expected:
            raise RepairLifecycleError(
                f"refused: the {label} bundle's content digest changed since "
                f"the candidate was created (expected {expected[:16]}..., "
                f"found {actual[:16] if actual else '<none>'}...)"
            )

    def approve(
        self,
        candidate_id: str,
        actor: Actor,
        *,
        non_interactive: bool = False,
    ) -> RepairCandidate:
        """Human approval (fault_passed -> approved). Binds exact hashes.

        Refuses unless BOTH campaigns passed, the actor is a human with an
        explicit identity, and the contract invariants still hold against the
        bundles on disk. ``non_interactive`` marks an automation-driven
        invocation that carried an explicit ``--approved-by`` identity.
        """
        _require_human(actor, "repair approval")
        candidate = self.load_candidate(candidate_id)
        if candidate.state != "fault_passed":
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} is in state "
                f"{candidate.state!r}; approval requires the full gate "
                "sequence (reviewed diff, replay campaign, fault campaign) "
                "to have passed first"
            )
        if not candidate.campaigns_passed():
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} has not passed both the "
                "replay and fault campaigns"
            )
        self._reverify_invariants(candidate)
        candidate.approval = ApprovalRecord(
            approved_by=actor.id,
            non_interactive=non_interactive,
            prior_content_digest=candidate.prior_content_digest,
            proposed_content_digest=candidate.proposed_content_digest,
        )
        return self._transition(
            candidate, "approved", actor, note=f"approved by {actor.id}"
        )

    # -- staging + activation ------------------------------------------------

    def _stage_bundle(self, source: Path, expected_digest: str) -> Path:
        """Copy a bundle into the store by digest and re-verify it byte-exact."""
        destination = self.bundle_path(expected_digest)
        if not destination.is_dir():
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{expected_digest[:12]}.stage-",
                    dir=destination.parent,
                )
            )
            try:
                shutil.copytree(source, staging, dirs_exist_ok=True)
                staged_wf = Workflow.load(staging)
                self._require_digest(staged_wf, expected_digest, "staged")
                try:
                    os.rename(staging, destination)
                except OSError:
                    # A concurrent stage of the same digest won the rename;
                    # both copies verified to the same content digest.
                    if not destination.is_dir():
                        raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        verify = Workflow.load(destination)
        self._require_digest(verify, expected_digest, "staged")
        return destination

    def stage(self, candidate_id: str, actor: Actor) -> RepairCandidate:
        """Stage BOTH bundles by hash (approved -> staged).

        The prior bundle is staged too, so rollback is always possible from
        store-local, digest-verified copies.
        """
        assert_not_model_actor(actor, "repair staging")
        candidate = self.load_candidate(candidate_id)
        if candidate.state != "approved":
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} is in state "
                f"{candidate.state!r}; staging requires an approved candidate"
            )
        if candidate.approval is None:
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} has no approval record"
            )
        if not candidate.prior_bundle_path or not candidate.proposed_bundle_path:
            raise RepairLifecycleError(
                "refused (fail closed): candidate carries no bundle paths to stage from"
            )
        self._stage_bundle(
            Path(candidate.prior_bundle_path), candidate.prior_content_digest
        )
        self._stage_bundle(
            Path(candidate.proposed_bundle_path),
            candidate.proposed_content_digest,
        )
        return self._transition(
            candidate, "staged", actor, note="both bundles staged by hash"
        )

    def _write_pointer(self, pointer: ActivePointer) -> None:
        _write_atomic(self.active_path, pointer.model_dump_json(indent=2))

    def start_canary(
        self, candidate_id: str, actor: Actor, *, max_runs: Optional[int] = None
    ) -> RepairCandidate:
        """Atomically activate the staged bundle in CANARY mode.

        The pointer swap happens only after the staged copy re-verifies to
        the approved digest. The candidate then runs a bounded first-N-runs
        window (:meth:`record_canary_run`) before full ``active``.
        """
        assert_not_model_actor(actor, "canary activation")
        candidate = self.load_candidate(candidate_id)
        if candidate.state != "staged":
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} is in state "
                f"{candidate.state!r}; canary requires a staged candidate"
            )
        if candidate.approval is None or (
            candidate.approval.proposed_content_digest
            != candidate.proposed_content_digest
        ):
            raise RepairLifecycleError(
                "refused: the approval record does not bind the exact "
                "proposed bundle hash"
            )
        staged = self.bundle_path(candidate.proposed_content_digest)
        staged_wf = Workflow.load(staged)
        self._require_digest(staged_wf, candidate.proposed_content_digest, "staged")
        if max_runs is not None:
            candidate.canary.max_runs = max_runs
        candidate.canary_metrics = candidate.canary_metrics.model_copy(
            update={
                "runs_completed": 0,
                "verified_runs": 0,
                "silent_incorrect": 0,
                "verification_regressions": 0,
                "halted": False,
                "halt_reason": None,
            }
        )
        pointer = ActivePointer(
            workflow_name=candidate.workflow_name,
            candidate_id=candidate.candidate_id,
            active_digest=candidate.proposed_content_digest,
            active_bundle=f"{_BUNDLES_DIR}/{candidate.proposed_content_digest}",
            prior_digest=candidate.prior_content_digest,
            prior_bundle=f"{_BUNDLES_DIR}/{candidate.prior_content_digest}",
            mode="canary",
            activated_at=_now(),
            note=f"canary window: first {candidate.canary.max_runs} runs",
        )
        self._write_pointer(pointer)
        return self._transition(
            candidate,
            "canary",
            actor,
            note=f"activated in canary mode (max_runs={candidate.canary.max_runs})",
        )

    def _revert_pointer(self, candidate: RepairCandidate, note: str) -> None:
        pointer = ActivePointer(
            workflow_name=candidate.workflow_name,
            candidate_id=candidate.candidate_id,
            active_digest=candidate.prior_content_digest,
            active_bundle=f"{_BUNDLES_DIR}/{candidate.prior_content_digest}",
            prior_digest=None,
            prior_bundle=None,
            mode="active",
            activated_at=_now(),
            note=note,
        )
        self._write_pointer(pointer)

    def record_canary_run(
        self, candidate_id: str, observation: CanaryRunObservation
    ) -> RepairCandidate:
        """Record one canary run; auto-halt on regression, promote when done.

        Any silent-incorrect or verification regression IMMEDIATELY reverts
        the active pointer to the prior bundle and drops the candidate back
        to ``staged``. After ``max_runs`` fully verified runs the candidate
        auto-completes to ``active`` (the human approval already happened; the
        completion is mechanical and recorded as automation).
        """
        automation = Actor(kind="automation", id="canary-monitor")
        candidate = self.load_candidate(candidate_id)
        if candidate.state != "canary":
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} is in state "
                f"{candidate.state!r}; canary runs can only be recorded in "
                "the canary state"
            )
        metrics = candidate.canary_metrics
        metrics.runs_completed += 1
        metrics.last_run_at = _now()
        regression = observation.silent_incorrect or not observation.verified
        if observation.silent_incorrect:
            metrics.silent_incorrect += 1
        if not observation.verified:
            metrics.verification_regressions += 1
        if observation.verified and not observation.silent_incorrect:
            metrics.verified_runs += 1

        if regression:
            reason = (
                "canary regression on run "
                f"{observation.run_id!r}: "
                + (
                    "silent-incorrect result"
                    if observation.silent_incorrect
                    else "verification regression"
                )
                + (f" ({observation.detail})" if observation.detail else "")
            )
            metrics.halted = True
            metrics.halt_reason = reason
            self._revert_pointer(candidate, note=reason)
            return self._transition(candidate, "staged", automation, note=reason)

        if metrics.verified_runs >= candidate.canary.max_runs:
            pointer = self.active_pointer()
            if pointer is None or pointer.candidate_id != candidate.candidate_id:
                raise RepairLifecycleError(
                    "refused: the active pointer no longer belongs to this "
                    "candidate; cannot complete the canary"
                )
            self._write_pointer(
                pointer.model_copy(
                    update={
                        "mode": "active",
                        "note": (
                            f"canary completed: {metrics.verified_runs} verified runs"
                        ),
                    }
                )
            )
            return self._transition(
                candidate,
                "active",
                automation,
                note=(
                    f"canary completed with {metrics.verified_runs} verified "
                    "runs; fully active"
                ),
            )

        self.save_candidate(candidate)
        return candidate

    def rollback(
        self, actor: Actor, candidate_id: Optional[str] = None
    ) -> RepairCandidate:
        """One-command rollback: restore the prior bundle hash as active."""
        assert_not_model_actor(actor, "repair rollback")
        if candidate_id is None:
            pointer = self.active_pointer()
            if pointer is None:
                raise RepairLifecycleError("refused: no active pointer to roll back")
            candidate_id = pointer.candidate_id
        candidate = self.load_candidate(candidate_id)
        if candidate.state not in ("canary", "active"):
            raise RepairLifecycleError(
                f"refused: candidate {candidate_id} is in state "
                f"{candidate.state!r}; rollback applies to canary or active "
                "candidates"
            )
        prior_staged = self.bundle_path(candidate.prior_content_digest)
        if prior_staged.is_dir():
            prior_wf = Workflow.load(prior_staged)
            self._require_digest(prior_wf, candidate.prior_content_digest, "prior")
        note = f"rolled back by {actor.id}: prior bundle restored"
        self._revert_pointer(candidate, note=note)
        return self._transition(candidate, "rolled_back", actor, note=note)
