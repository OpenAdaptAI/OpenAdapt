"""The durable-run controller: the replayer's Tier-3 hook (RFC §5).

``DurableRun`` is the object the ``Replayer`` drives to make a run durable. It
owns the :class:`~.checkpoint.CheckpointStore` and turns each ``StepResult``
into the right durable artifact:

- a VERIFIED step (``result.ok``) -> a :class:`~.checkpoint.RunCheckpoint`
  written under ``run_dir/checkpoints/`` (the resume point advances);
- a HALTED step (``not result.ok``) -> a
  :class:`~.checkpoint.PendingEscalation` written to
  ``run_dir/pending_escalation.json`` that captures WHY the run paused, the
  proposed operator options, and the last verified checkpoint to resume from.

The replayer's coupling to this module is intentionally TINY, so the Phase-2
state-machine interpreter (which rewrites ``replayer.py`` heavily) can keep it
across the rebase. The touch-points are:

1. ``Replayer.__init__`` accepts ``durable: bool = False`` and stores it.
2. ``Replayer.run`` accepts ``resume_from: Optional[int] = None``; when
   durability is on it constructs one ``DurableRun`` and, per step, calls
   :meth:`DurableRun.record` right after the result is appended.
3. A resume skips already-verified steps and pre-loads their results via
   :func:`resumed_step_results`.

Nothing here makes a model call or touches the backend/vision -- durability is
pure bookkeeping over the ``StepResult`` the replayer already produces.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from openadapt_flow.bundle_validation import BundleIntegrityError
from openadapt_flow.ir import (
    ProgramExceptionEvidence,
    ProgramTransitionEvidence,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.approval import StateDiverged
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    ProgramCheckpoint,
    bundle_version,
)

if TYPE_CHECKING:
    from openadapt_flow.runtime.durable.attended import TransitionObservation

HUMAN_REQUIRED_MARKERS: tuple[str, ...] = (
    "captcha",
    "verify you are human",
    "verify that you are human",
    "i'm not a robot",
    "i am not a robot",
    "unusual traffic",
    "press and hold",
    "multi-factor",
    "multifactor",
    "two-factor",
    "one-time passcode",
    "one-time code",
    "enter the verification code",
    "enter verification code",
    "code sent to",
    "session expired",
    "sign in again",
    "log in again",
)
_HUMAN_REQUIRED_TOKEN_RE = re.compile(r"(?<![a-z0-9])(?:2fa|mfa)(?![a-z0-9])")


def looks_like_human_required(*texts: Optional[str]) -> bool:
    """Recognize a visible human-presence/authentication interruption.

    The result is only a halt classification.  It never triggers a solver,
    retry, click, key entry, or model call.
    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(marker in lowered for marker in HUMAN_REQUIRED_MARKERS):
            return True
        if _HUMAN_REQUIRED_TOKEN_RE.search(lowered):
            return True
    return False


def classify_halt(step: Optional[Step], result: StepResult) -> tuple[str, list[str]]:
    """Categorize a halt and propose operator options.

    Maps the replayer's halt reason (``result.error`` plus the
    ``result.effect_results`` audit lines and the identity verdict) to a coarse
    machine ``category`` and a list of human-facing ``proposed_options``. The
    options are advisory next actions for the operator reviewing the pause;
    "approve and resume from the last verified checkpoint" and "abort" are
    always offered, alongside cause-specific guidance derived from the halt
    reason / compensation escalation.

    Returns ``(category, proposed_options)``.
    """
    error = result.error or ""
    lower = error.lower()
    effect_lines = result.effect_results or []
    effect_blob = " ".join(effect_lines).lower()

    resume = (
        "Approve and RESUME from the last verified checkpoint (re-runs only "
        "this step onward; already-confirmed steps are not repeated)"
    )
    abort = "Abort the run and discard the pending escalation"

    if result.delivery_uncertainty is not None:
        return "delivery_uncertain", [
            "Reconcile the live application and independently verify whether "
            "the effect landed; the attended Continue path can checkpoint the "
            "verified outcome without repeating the action",
            "If reconciliation proves a retry is necessary, record a fresh "
            "approval explicitly authorizing one uncertain-delivery retry",
            abort,
        ]

    # CAPTCHA, MFA, and re-authentication are human-presence requirements.
    # OpenAdapt only halts and leaves the live application to the operator.
    if looks_like_human_required(error):
        return "human_required", [
            "Complete the challenge or authentication in the LIVE application "
            "yourself; OpenAdapt never answers, solves, or retries it",
            resume,
            abort,
        ]

    # System-of-record effect halts (the richest signal -- effect_verified is
    # explicitly False and the verdict lines carry the cause).
    if result.effect_verified is False or "system of record" in lower:
        if "operator confirmation" in effect_blob or "placeholder" in lower:
            return "placeholder_effect", [
                "Complete the system-of-record binding the compiler flagged as "
                "app-specific (endpoint / record selector / idempotency key) "
                "and clear the effect's needs_operator_confirmation flag",
                resume,
                abort,
            ]
        if "no effectverifier" in effect_blob or "no effectverifier" in lower:
            return "effect_unverifiable", [
                "Configure an EffectVerifier bound to this deployment's system "
                "of record, then re-run",
                abort,
            ]
        if "escalat" in lower or "escalat" in effect_blob:
            return "effect_escalated", [
                "Inspect the system of record and correct it (the automatic "
                "compensation could not safely undo the fault)",
                resume,
                abort,
            ]
        if "indeterminate" in lower or "indeterminate" in effect_blob:
            return "effect_indeterminate", [
                "Restore reachability of the system of record and confirm "
                "whether the write landed",
                resume,
                abort,
            ]
        if "refuted" in lower or "refuted" in effect_blob:
            return "effect_refuted", [
                "Investigate the system of record: the screen showed success "
                "but the record is missing/duplicated/wrong; correct it",
                resume,
                abort,
            ]
        return "effect_refuted", [
            "Investigate the system-of-record effect that could not be "
            "confirmed and correct the record",
            resume,
            abort,
        ]

    # Phase-1 guard / wait_until precondition halts.
    if (
        "precondition" in lower
        or "guard" in lower
        or "wait_until" in lower
        or ("readiness" in lower)
    ):
        return "unmet_guard", [
            "Satisfy the step's precondition (bring the app to the expected "
            "state), then resume",
            resume,
            abort,
        ]

    # Disambiguation (which entity / which of several matches).
    if "disambigu" in lower or "ambiguous" in lower:
        return "disambiguation", [
            "Choose the intended target/entity for this step, then resume",
            resume,
            abort,
        ]

    # Pre-click identity gate (wrong-entity refusal or unreadable/abstain).
    if result.identity is not None and (
        "identity" in lower or "refusing to act" in lower
    ):
        return "identity", [
            "Confirm the resolved target is the intended entity (the identity "
            "band could not be certified), then resume",
            resume,
            abort,
        ]

    if "postconditions failed" in lower or "semantic drift" in lower:
        return "postcondition", [
            "Verify the app reached the expected screen state; re-run this step "
            "once the drift is understood",
            resume,
            abort,
        ]

    if "could not resolve" in lower or "all resolution rungs failed" in lower:
        return "resolution", [
            "The target could not be located on screen; confirm the app view "
            "matches the recording, then resume",
            resume,
            abort,
        ]

    return "halt", [resume, abort]


class DurableRun:
    """Per-run Tier-3 controller: writes checkpoints and pending escalations.

    Constructed once by the replayer when durability is enabled. It writes a
    :class:`~.checkpoint.RunManifest` up front (so a resume can reconstruct the
    run from ``run_dir`` alone) and then records one artifact per step.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        run_id: str,
        workflow_name: str,
        bundle_dir: Path | str,
        params: dict[str, str],
        worklists: dict[str, list[dict[str, str]]],
        idempotency_key: Optional[str] = None,
        save_healed_to: Optional[Path | str] = None,
        key: Optional[str] = None,
        governed_authorization: Optional[GovernedRunAuthorization] = None,
        delivery_authority_kind: Literal[
            "customer_local", "cloud_runner"
        ] = "customer_local",
        remote_delivery_run_id: Optional[str] = None,
        managed_dispatch_binding_sha256: Optional[str] = None,
        screenshots_may_leave_box: bool = False,
        model_calls: int = 0,
        external_network_calls: Literal["none", "observed", "unknown"] = "unknown",
        resume_existing: bool = False,
    ) -> None:
        # ``key`` (None by default) opts the durable artifacts into AES-256-GCM
        # encryption-at-rest; unset => plaintext, exactly as before.
        if not run_id:
            raise StateDiverged("a durable run requires a nonempty run identity")
        self.store = CheckpointStore(run_dir, key=key)
        from openadapt_flow.runtime.durable.authority import DurableAuthority

        self._authority = DurableAuthority(run_dir, self.store)
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.bundle_dir = Path(bundle_dir).resolve()
        self.bundle_version = bundle_version(self.bundle_dir)
        self.governed_authorization = governed_authorization
        if delivery_authority_kind == "cloud_runner" and not remote_delivery_run_id:
            raise StateDiverged("a managed durable run requires its Cloud run identity")
        if (
            delivery_authority_kind == "customer_local"
            and remote_delivery_run_id is not None
        ):
            raise StateDiverged("a local durable run cannot carry a Cloud run identity")
        existing = self.store.read_manifest()
        namespace_id = (
            existing.namespace_id
            if resume_existing and existing is not None
            else secrets.token_hex(16)
        )
        canonical_run_dir = (
            existing.canonical_run_dir
            if resume_existing and existing is not None
            else str(Path(run_dir).resolve())
        )
        manifest = RunManifest(
            run_id=run_id,
            namespace_id=namespace_id,
            canonical_run_dir=canonical_run_dir,
            workflow_name=workflow_name,
            bundle_dir=str(self.bundle_dir),
            params=dict(params),
            idempotency_key=idempotency_key,
            worklists=worklists,
            governed_authorization=governed_authorization,
            delivery_authority_kind=delivery_authority_kind,
            remote_delivery_run_id=remote_delivery_run_id,
            managed_dispatch_binding_sha256=managed_dispatch_binding_sha256,
            screenshots_may_leave_box=screenshots_may_leave_box,
            model_calls=model_calls,
            external_network_calls=external_network_calls,
            save_healed_to=(str(save_healed_to) if save_healed_to else None),
        )
        retained_without_manifest = (
            existing is None and self.store.has_durable_artifacts()
        )
        if retained_without_manifest:
            raise StateDiverged(
                "the run directory contains durable evidence without its run "
                "manifest; use a new run directory"
            )
        if resume_existing:
            if existing is None:
                raise StateDiverged(
                    "durable resume lost the exact retained run manifest"
                )
            if (
                existing.schema_version != 2
                or existing.run_id != run_id
                or not existing.namespace_id
                or existing.canonical_run_dir != str(Path(run_dir).resolve())
                or existing.workflow_name != workflow_name
                or Path(existing.bundle_dir).resolve() != self.bundle_dir
                or existing.params != params
                or existing.idempotency_key != idempotency_key
                or existing.worklists != worklists
                or existing.governed_authorization != governed_authorization
                or existing.delivery_authority_kind != delivery_authority_kind
                or existing.remote_delivery_run_id != remote_delivery_run_id
                or existing.managed_dispatch_binding_sha256
                != managed_dispatch_binding_sha256
            ):
                raise StateDiverged(
                    "durable resume does not match the exact version-2 retained "
                    "run manifest"
                )
            authority_record = self._authority.validate(existing)
            self._authority_digest = authority_record.progress_digest
            self._manifest = existing.model_copy(deep=True)
            self._manifest_digest = self.store.model_digest(existing)
            active_pause = self.store.read_pending()
            self._active_pause_digest = self.store.model_digest(active_pause)
            # Preserve the original manifest and its creation time. The resume
            # admission already bound its exact serialized form.
            return
        if existing is not None:
            raise StateDiverged(
                "the run directory already contains a durable run; use the "
                "authenticated resume API or choose a new run directory"
            )
        self.store.write_fresh_manifest(manifest)
        authority_record = self._authority.validate(manifest)
        self._authority_digest = authority_record.progress_digest
        self._manifest = manifest.model_copy(deep=True)
        self._manifest_digest = self.store.model_digest(manifest)
        self._active_pause_digest = self.store.model_digest(None)

    def _sync_authority(self) -> None:
        """Commit one trusted local mutation to the external monotonic record."""

        from openadapt_flow.runtime.durable.approval import approval_pause_digest
        from openadapt_flow.runtime.durable.continuation import (
            current_continuation_token,
        )

        pending = self.store.read_pending()
        token = current_continuation_token()
        if token is not None:
            # The continuation guard advances local evidence and the external
            # delivery fence together after the immutable checkpoint/pause is
            # durable. Advancing here could acknowledge only an audit-manifest
            # write before its action proof exists.
            return
        phase: Literal["active", "paused", "continuing"] = (
            "paused" if pending is not None else "active"
        )
        self._authority_digest = self._authority.advance(
            self._manifest,
            expected_progress_digest=self._authority_digest,
            phase=phase,
            pause_binding_sha256=(
                approval_pause_digest(pending) if pending is not None else ""
            ),
            attempt_id="",
            owner_nonce_sha256="",
        )

    @property
    def started_at(self) -> str:
        """Return the start of the complete logical run across resumed legs."""

        return self._manifest.created_at

    def update_audit_evidence(
        self,
        *,
        model_calls: int,
        external_network_calls: Literal["none", "observed", "unknown"],
    ) -> None:
        """Persist cumulative evidence shared by every leg of one run."""

        self.store.validate_namespace(self._manifest)
        updated = self._manifest.model_copy(
            update={
                "model_calls": model_calls,
                "external_network_calls": external_network_calls,
            }
        )
        self.store.cas_manifest(self._manifest_digest, updated)
        self._manifest = updated.model_copy(deep=True)
        self._manifest_digest = self.store.model_digest(updated)
        from openadapt_flow.runtime.durable.continuation import (
            current_continuation_token,
        )

        if current_continuation_token() is None:
            self._sync_authority()

    def record(
        self,
        step_index: int,
        step: Step,
        result: StepResult,
        params: dict[str, str],
        *,
        workflow: Optional[Workflow] = None,
        transition_observation: Optional["TransitionObservation"] = None,
    ) -> None:
        """Persist the durable artifact for one completed step.

        ``result.ok`` -> checkpoint (the resume point advances past this step).
        Otherwise -> a pending escalation capturing the pause. Idempotent: a
        resume that re-verifies a step overwrites its checkpoint rather than
        duplicating it.
        """
        if result.ok:
            from openadapt_flow.action_evidence import action_evidence_error

            profile = (
                self.governed_authorization.execution_profile
                if self.governed_authorization is not None
                else None
            )
            identity_required = bool(
                step.identity_armed
                or (
                    self.governed_authorization is not None
                    and self.governed_authorization.requires_verified_identity(step.id)
                )
            )
            evidence_error = action_evidence_error(
                step,
                result,
                params=params,
                identity_required=identity_required,
                strict_production=(
                    profile in {"standard", "regulated"} and identity_required
                ),
            )
            if evidence_error is not None:
                raise StateDiverged(
                    "refusing a durable checkpoint with invalid action evidence: "
                    f"{evidence_error}"
                )
            self.store.write_checkpoint(
                RunCheckpoint(
                    run_id=self.run_id,
                    workflow_name=self.workflow_name,
                    bundle_version=self.bundle_version,
                    step_index=step_index,
                    step_id=step.id,
                    intent=step.intent,
                    next_step_index=step_index + 1,
                    params=dict(params),
                    effect_verified=result.effect_verified,
                    effect_approved_unverified=result.effect_approved_unverified,
                    effect_contract_hashes=list(result.effect_contract_hashes),
                    effect_evidence=list(result.effect_evidence),
                    identity=result.identity,
                    input_verified=result.input_verified,
                    starting_state_settled=result.starting_state_settled,
                    delivery_attempted=result.delivery_attempted,
                    delivery_receipt=result.delivery_receipt,
                    drag_end_resolution=result.drag_end_resolution,
                    fresh_actuation_events=list(result.fresh_actuation_events),
                    before_png=result.before_png,
                    governed_authorization_id=(
                        self.governed_authorization.authorization_id
                        if self.governed_authorization is not None
                        else None
                    ),
                    governed_approval_source=(
                        self.governed_authorization.approval_source
                        if self.governed_authorization is not None
                        else None
                    ),
                    postconditions_ok=result.postconditions_ok,
                    expected_postconditions=(
                        [condition.model_copy(deep=True) for condition in step.expect]
                        if not result.skipped
                        else []
                    ),
                    skipped=result.skipped,
                    actuation=result.actuation,
                    delivery_uncertainty=result.delivery_uncertainty,
                    resolution=result.resolution,
                    drift_oracle_calls=result.drift_oracle_calls,
                    heal=result.heal,
                )
            )
            self._sync_authority()
            # Keep an approved/continuing pause as the crash-restart anchor.
            # Terminal completion or a later halt consumes/replaces it by CAS.
            return

        if result.failure_category == "continuation_preempted":
            # The shared continuation coordinator retained the exact approved
            # pause so the winning Reject request can make it terminal.
            return

        # HALT: durably pause instead of just dying. Resume from the last
        # verified checkpoint (0 when nothing verified yet).
        last = self.store.last_checkpoint()
        resume_from = last.next_step_index if last is not None else 0
        category, options = classify_halt(step, result)
        pending = PendingEscalation(
            run_id=self.run_id,
            workflow_name=self.workflow_name,
            step_index=step_index,
            step_id=step.id,
            intent=step.intent,
            category=category,
            reason=result.error or "",
            detail=list(result.effect_results or []),
            proposed_options=options,
            resume_from_index=resume_from,
            resume_from_step_id=(last.step_id if last is not None else None),
            params=dict(params),
            delivery_uncertainty=result.delivery_uncertainty,
        )
        self.store.cas_pending(self._active_pause_digest, pending)
        self._active_pause_digest = self.store.model_digest(pending)
        if workflow is not None:
            from openadapt_flow.runtime.durable.attended import (
                issue_attended_capability,
            )

            try:
                issue_attended_capability(
                    self.store.run_dir,
                    store=self.store,
                    pending=pending,
                    workflow=workflow,
                    result=result,
                    transition_observation=transition_observation,
                )
            except BundleIntegrityError:
                # The pause is still the safe terminal state for this leg. Do
                # not turn a detected bundle mutation into a runtime crash, and
                # do not mint new operator authority for the mutated bundle.
                pass
        self._sync_authority()

    # -- Phase-2 program (state-machine) durability --------------------------

    def record_program_checkpoint(self, checkpoint: ProgramCheckpoint) -> None:
        """Persist one verified-state interpreter checkpoint (Phase-2 program).

        Called by the program interpreter after each ``action`` state that
        VERIFIED (identity + effects + postconditions). The checkpoint captures
        the whole interpreter state (frame stack, loop cursors, bound params,
        completed effect keys) so a resume RESTORES the interpreter rather than
        translating to a step index. Idempotent per ``seq``."""
        if checkpoint.run_id != self.run_id:
            raise StateDiverged(
                "the program checkpoint does not match the active run identity"
            )
        self.store.write_program_checkpoint(checkpoint)
        self._sync_authority()
        # Keep the old pause as the restart anchor until terminal completion or
        # a replacement halt is durably committed.

    def record_program_halt(
        self,
        *,
        state_id: str,
        intent: str,
        result: StepResult,
        params: dict[str, str],
        workflow: Optional[Workflow] = None,
        transition_observation: Optional["TransitionObservation"] = None,
        program_frames: Optional[list[GraphFrame]] = None,
        program_checkpoint_seq: int = 0,
        program_history_hash: str = "",
        program_history_delta: Optional[list[str]] = None,
        program_transition_evidence_delta: Optional[
            list[ProgramTransitionEvidence]
        ] = None,
        program_exception_evidence_delta: Optional[
            list[ProgramExceptionEvidence]
        ] = None,
        program_parent_history_hash: str = "",
        program_history: Optional[list[str]] = None,
    ) -> None:
        """Persist a durable PROGRAM pause (the interpreter HALTED for a human).

        Mirrors :meth:`record` for the state machine: classify WHY it paused,
        propose operator options, and point the resume at the last verified
        interpreter checkpoint (``ProgramCheckpoint``, restored from ``run_dir``
        by :func:`~.resume.resume`). ``resume_from_index``/``resume_from_step_id``
        do NOT apply to a program run (the resume point is an interpreter state,
        not a step index), so they are left at their defaults; ``program=True``
        marks the pause as a state-machine pause."""
        if result.failure_category == "continuation_preempted":
            return
        last = self.store.last_program_checkpoint()
        category, options = classify_halt(None, result)
        pending = PendingEscalation(
            run_id=self.run_id,
            workflow_name=self.workflow_name,
            step_index=0,
            step_id=state_id,
            intent=intent,
            state_id=state_id,
            category=category,
            reason=result.error or "",
            detail=list(result.effect_results or []),
            proposed_options=options,
            resume_from_step_id=(last.verified_state_id if last is not None else None),
            params=dict(params),
            program=True,
            program_frames=list(program_frames or []),
            program_checkpoint_seq=program_checkpoint_seq,
            program_history_hash=program_history_hash,
            program_history_delta=list(program_history_delta or []),
            program_transition_evidence_delta=list(
                program_transition_evidence_delta or []
            ),
            program_exception_evidence_delta=list(
                program_exception_evidence_delta or []
            ),
            program_parent_history_hash=program_parent_history_hash,
            program_history=list(program_history or []),
            delivery_uncertainty=result.delivery_uncertainty,
        )
        self.store.cas_pending(self._active_pause_digest, pending)
        self._active_pause_digest = self.store.model_digest(pending)
        if workflow is not None:
            from openadapt_flow.runtime.durable.attended import (
                issue_attended_capability,
            )

            try:
                issue_attended_capability(
                    self.store.run_dir,
                    store=self.store,
                    pending=pending,
                    workflow=workflow,
                    result=result,
                    transition_observation=transition_observation,
                )
            except BundleIntegrityError:
                # Preserve the durable halt, but never issue attended mutation
                # authority against a bundle that no longer matches its seal.
                pass
        self._sync_authority()

    def complete(self) -> None:
        """Consume only the exact pause that this successful leg admitted."""

        current = self.store.read_pending()
        if current is None:
            if self._active_pause_digest != self.store.model_digest(None):
                raise StateDiverged(
                    "the durable pause disappeared before terminal commit"
                )
            return
        self.store.cas_pending(self._active_pause_digest, None)
        self._active_pause_digest = self.store.model_digest(None)


def resumed_step_results(
    run_dir: Path | str,
    workflow: Workflow,
    resume_from: int,
    *,
    key: Optional[str] = None,
    checkpoints: Optional[list[RunCheckpoint]] = None,
    run_id: Optional[str] = None,
) -> list[StepResult]:
    """Synthesize ``StepResult``s for the already-verified steps of a resume.

    A resume skips steps ``[0, resume_from)`` -- they were verified in the
    original run and must NOT re-execute (never re-perform a confirmed write).
    But the report's ``success`` accounting counts one result per step, so we
    reconstruct their results from the persisted checkpoints. A missing,
    reordered, or mismatched checkpoint refuses the resume. The runtime never
    invents verified success from the workflow definition.
    """
    from openadapt_flow.runtime.durable.approval import StateDiverged

    if resume_from < 0 or resume_from > len(workflow.steps):
        raise StateDiverged("the linear resume cursor is outside the workflow")
    if checkpoints is None:
        store = CheckpointStore(run_dir, key=key)
        manifest = store.read_manifest()
        if manifest is None or not manifest.run_id:
            raise StateDiverged(
                "the linear resume lacks exact verified checkpoint run identity"
            )
        checkpoints = store.checkpoints()
        run_id = manifest.run_id
    if not run_id:
        raise StateDiverged(
            "the linear resume lacks exact verified checkpoint run identity"
        )
    by_index = {c.step_index: c for c in checkpoints}
    results: list[StepResult] = []
    for index in range(resume_from):
        checkpoint = by_index.get(index)
        step = workflow.steps[index]
        if (
            checkpoint is None
            or checkpoint.run_id != run_id
            or checkpoint.workflow_name != workflow.name
            or checkpoint.step_index != index
            or checkpoint.next_step_index != index + 1
            or checkpoint.step_id != step.id
        ):
            raise StateDiverged(
                "the linear resume cursor lacks an exact verified checkpoint "
                f"for step {index}"
            )
        results.append(
            StepResult(
                step_id=step.id,
                intent=step.intent,
                ok=True,
                risk=step.risk,
                risk_explanation=step.risk_explanation,
                risk_review_required=step.risk_review_required,
                skipped=checkpoint.skipped,
                effect_verified=checkpoint.effect_verified,
                effect_approved_unverified=checkpoint.effect_approved_unverified,
                effect_contract_hashes=list(checkpoint.effect_contract_hashes),
                effect_evidence=list(checkpoint.effect_evidence),
                identity=checkpoint.identity,
                input_verified=checkpoint.input_verified,
                starting_state_settled=checkpoint.starting_state_settled,
                delivery_attempted=checkpoint.delivery_attempted,
                delivery_receipt=checkpoint.delivery_receipt,
                drag_end_resolution=checkpoint.drag_end_resolution,
                fresh_actuation_events=list(checkpoint.fresh_actuation_events),
                before_png=checkpoint.before_png,
                postconditions_ok=checkpoint.postconditions_ok,
                actuation=checkpoint.actuation,
                delivery_uncertainty=checkpoint.delivery_uncertainty,
                resolution=checkpoint.resolution,
                drift_oracle_calls=checkpoint.drift_oracle_calls,
                heal=checkpoint.heal,
                error=None,
            )
        )
    return results
