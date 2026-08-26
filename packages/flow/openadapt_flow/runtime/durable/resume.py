"""Resume a durably-paused run from its last verified checkpoint (RFC §5).

:func:`resume` is the Tier-3 resume entrypoint, and P0-5 makes it an
AUTHENTICATED APPROVAL workflow. Given a ``run_dir`` that holds a
:class:`~.checkpoint.PendingEscalation` (and the checkpoints written during the
original run), it:

1. ENFORCES an authenticated approval (RFC §5, P0-5): an
   :class:`~.approval.ApprovalRecord` (approver / timestamp / resolution /
   bundle version) must accompany the resume, the pause must not have expired
   (stale-pause window), and the approval must match the current bundle version.
   A caller without a valid approval record CANNOT resume;
2. reads the :class:`~.checkpoint.RunManifest` to recover the bundle and the
   run's parameter bindings (so the caller need only supply a live ``Replayer``);
3. REVALIDATES the live app is still in the checkpoint's expected state (and that
   the already-confirmed effects still hold) before continuing;
4. RESTORES the run from its last verified checkpoint and re-drives it from
   there onward -- NEVER from step 0.

For a Phase-2 PROGRAM run the resume point is not a step index but the whole
INTERPRETER STATE (:class:`~.program_checkpoint.ProgramCheckpoint`): the frame
stack, loop cursors, bound params, and completed effect keys are RESTORED so an
already-confirmed consequential write is never re-performed and a mid-loop pause
finishes the in-progress row and runs the remaining rows. For a linear run the
resume point is the last verified step index (unchanged from before).

Explicit non-goal (RFC §5): resume is DETERMINISTIC. It hands the remaining
workflow to the SAME deterministic replayer, never to a free-form agent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from openadapt_flow.ir import ExecutionTargetKind, RunReport, Step, Workflow
from openadapt_flow.policy import effects_for_actuation
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    BundleMismatch,
    StateDiverged,
    enforce_resume_authorization,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    TOP_GRAPH_ID,
    ProgramCheckpoint,
    bundle_version,
    history_hash,
)
from openadapt_flow.runtime.effects import Effect


def _resolved_step_effects(
    step: Step,
    *,
    params: dict[str, str],
    run_id: str,
    actuation: Optional[str],
) -> list[Effect]:
    """Resolve the exact path-specific effects declared by one retained step."""

    namespace = {**params, "__run_id__": run_id}
    return [
        effect.resolve(namespace) for effect in effects_for_actuation(step, actuation)
    ]


def _validate_retained_step_proof(
    *,
    step: Step,
    params: dict[str, str],
    run_id: str,
    skipped: bool,
    actuation: Optional[str],
    effect_verified: Optional[bool],
    effect_approved_unverified: bool,
    effect_contract_hashes: list[str],
    effect_evidence: list[Any],
    stored_effects: Optional[list[dict[str, Any]]],
    identity: Any,
    input_verified: Optional[bool],
    starting_state_settled: Optional[bool],
    delivery_attempted: Optional[bool],
    delivery_receipt: Any,
    resolution: Any,
    drag_end_resolution: Any,
    fresh_actuation_events: list[Any],
    postconditions_ok: Optional[bool],
    delivery_uncertainty: Any,
    governed_authorization_id: Optional[str],
    governed_approval_source: Optional[str],
    manifest: RunManifest,
    workflow: Workflow,
) -> list[Effect]:
    """Refuse a checkpoint that no longer proves its declared step semantics."""

    authorization = manifest.governed_authorization
    expected_authorization_id = (
        authorization.authorization_id if authorization is not None else None
    )
    expected_approval_source = (
        authorization.approval_source if authorization is not None else None
    )
    if (
        governed_authorization_id != expected_authorization_id
        or governed_approval_source != expected_approval_source
    ):
        raise StateDiverged(
            "retained checkpoint authorization evidence does not match the run"
        )
    identity_required = step.identity_armed or (
        authorization is not None and authorization.requires_verified_identity(step.id)
    )
    from openadapt_flow.action_evidence import action_evidence_error

    action_error = action_evidence_error(
        step,
        SimpleNamespace(
            ok=True,
            skipped=skipped,
            actuation=actuation,
            identity=identity,
            input_verified=input_verified,
            input_retried=False,
            starting_state_settled=starting_state_settled,
            delivery_attempted=delivery_attempted,
            delivery_receipt=delivery_receipt,
            resolution=resolution,
            drag_end_resolution=drag_end_resolution,
            fresh_actuation_events=fresh_actuation_events,
            delivery_uncertainty=delivery_uncertainty,
            postconditions_ok=postconditions_ok,
        ),
        params=params,
        identity_required=identity_required,
        strict_production=(
            authorization is not None
            and authorization.execution_profile in {"standard", "regulated"}
            and identity_required
        ),
    )
    if action_error is not None:
        raise StateDiverged(
            f"retained checkpoint action evidence is invalid: {action_error}"
        )
    if (
        not skipped
        and actuation != "api"
        and step.expect
        and postconditions_ok is not True
    ):
        raise StateDiverged(
            "retained checkpoint lacks the declared postcondition proof"
        )
    if delivery_uncertainty is not None and not (
        delivery_uncertainty.verification_attempted
        and delivery_uncertainty.effects_confirmed is True
        and delivery_uncertainty.resolved_by_contract
        and (not step.expect or delivery_uncertainty.postconditions_confirmed is True)
    ):
        raise StateDiverged(
            "a verified checkpoint cannot retain unresolved delivery uncertainty"
        )

    declared = (
        []
        if skipped
        else _resolved_step_effects(
            step,
            params=params,
            run_id=run_id,
            actuation=actuation,
        )
    )
    declared_keys = [effect.contract_hash() for effect in declared]
    declared_dumps = [effect.model_dump(mode="json") for effect in declared]
    evidence_keys = [item.effect_contract_hash for item in effect_evidence]
    if stored_effects is not None and stored_effects != declared_dumps:
        raise StateDiverged(
            "retained checkpoint effects do not match the declared workflow path"
        )
    if not declared:
        if (
            effect_verified is not None
            or effect_approved_unverified
            or effect_contract_hashes
            or effect_evidence
            or (stored_effects is not None and stored_effects)
        ):
            raise StateDiverged(
                "retained checkpoint invents effect proof for an effect-free step"
            )
        return []
    if effect_contract_hashes != declared_keys:
        raise StateDiverged(
            "retained checkpoint effect hashes do not match the workflow"
        )
    if effect_verified is True and not effect_approved_unverified:
        if evidence_keys != declared_keys or any(
            getattr(item, "final_verdict", None) != "confirmed"
            for item in effect_evidence
        ):
            raise StateDiverged(
                "retained checkpoint lacks complete effect verification evidence"
            )
        if authorization is not None and authorization.execution_profile is not None:
            from openadapt_flow.execution_profiles import required_effect_tier
            from openadapt_flow.verification import VerificationTier

            minimum = (
                authorization.minimum_effect_tier
                if authorization.minimum_effect_tier is not None
                else required_effect_tier(workflow, authorization.execution_profile)
            )
            if minimum is not None:
                try:
                    minimum_tier = VerificationTier(minimum)
                except (TypeError, ValueError) as exc:
                    raise StateDiverged(
                        "the execution profile has an invalid effect tier"
                    ) from exc
                for item in effect_evidence:
                    try:
                        actual_tier = VerificationTier(item.verification_tier)
                    except (TypeError, ValueError) as exc:
                        raise StateDiverged(
                            "retained effect evidence has no valid verification tier"
                        ) from exc
                    if not actual_tier.satisfies(minimum_tier):
                        raise StateDiverged(
                            "retained effect evidence is weaker than the "
                            "execution profile"
                        )
        return declared
    if effect_verified is None and effect_approved_unverified:
        if effect_evidence:
            raise StateDiverged(
                "approved-unverified checkpoint cannot carry verified evidence"
            )
        if (
            actuation == "api"
            or authorization is None
            or not authorization.approves_unverified_write(step)
        ):
            raise StateDiverged(
                "retained unverified effect lacks exact governed approval"
            )
        return []
    raise StateDiverged(
        "retained checkpoint does not prove its declared effect outcome"
    )


def resume_point(run_dir: Path | str, *, key: Optional[str] = None) -> int:
    """The step index a resume would continue from for a LINEAR ``run_dir``.

    The last verified checkpoint's ``next_step_index``; 0 when nothing has
    verified yet (a run that halted on its very first step resumes from 0). Not
    meaningful for a program run (whose resume point is an interpreter state);
    use :meth:`CheckpointStore.last_program_checkpoint` there. ``key`` decrypts
    encrypted checkpoints (see :class:`CheckpointStore`).
    """
    last = CheckpointStore(run_dir, key=key).last_checkpoint()
    return last.next_step_index if last is not None else 0


def _created_no_later(checkpoint_created_at: str, pause_created_at: str) -> bool:
    """Return whether one checkpoint predates its retained pause."""

    try:
        return datetime.fromisoformat(checkpoint_created_at) <= datetime.fromisoformat(
            pause_created_at
        )
    except (TypeError, ValueError):
        return False


def _created_later(checkpoint_created_at: str, pause_created_at: str) -> bool:
    """Return whether an attended checkpoint follows its retained pause."""

    try:
        return datetime.fromisoformat(checkpoint_created_at) > datetime.fromisoformat(
            pause_created_at
        )
    except (TypeError, ValueError):
        return False


def _linear_resume_checkpoint(
    *,
    run_dir: Path,
    checkpoints: list[RunCheckpoint],
    pending: PendingEscalation,
    manifest: RunManifest,
    workflow: Workflow,
) -> Optional[RunCheckpoint]:
    """Validate the exact linear history retained by one active pause."""

    continuing = pending.status == "continuing"
    attended = (
        bool(checkpoints)
        and pending.status in {"approved", "continuing"}
        and checkpoints[-1].step_index == pending.step_index
        and checkpoints[-1].step_id == pending.step_id
        and checkpoints[-1].actuation in {"human_attended", "human_attended_skip"}
    )
    expected_count = pending.resume_from_index + (1 if attended else 0)
    valid_count = (
        pending.resume_from_index <= len(checkpoints) <= len(workflow.steps)
        if continuing
        else len(checkpoints) == expected_count
    )
    if not valid_count:
        raise StateDiverged(
            "the linear checkpoint history does not match the durable pause cursor"
        )
    for index, checkpoint in enumerate(checkpoints):
        if (
            index >= len(workflow.steps)
            or checkpoint.schema_version != 2
            or checkpoint.run_id != manifest.run_id
            or checkpoint.workflow_name != workflow.name
            or not checkpoint.bundle_version
            or checkpoint.bundle_version != bundle_version(manifest.bundle_dir)
            or checkpoint.step_index != index
            or checkpoint.next_step_index != index + 1
            or checkpoint.step_id != workflow.steps[index].id
            or checkpoint.params != manifest.params
        ):
            raise StateDiverged(
                "the linear checkpoint history does not match the workflow"
            )
        is_post_pause = continuing and index >= pending.resume_from_index
        is_attended_tail = attended and index == len(checkpoints) - 1
        valid_time = (
            _created_later(checkpoint.created_at, pending.created_at)
            if is_post_pause or is_attended_tail
            else _created_no_later(checkpoint.created_at, pending.created_at)
        )
        if not valid_time:
            raise StateDiverged(
                "the linear checkpoint history changed after the durable pause"
            )
        from openadapt_flow.runtime.durable.attended import (
            validate_attended_checkpoint_identity,
        )

        validate_attended_checkpoint_identity(
            run_dir,
            checkpoint=checkpoint,
            step=workflow.steps[index],
            manifest=manifest,
            live_bundle_version=bundle_version(manifest.bundle_dir),
        )
        _validate_retained_step_proof(
            step=workflow.steps[index],
            params=checkpoint.params,
            run_id=manifest.run_id,
            skipped=checkpoint.skipped,
            actuation=checkpoint.actuation,
            effect_verified=checkpoint.effect_verified,
            effect_approved_unverified=checkpoint.effect_approved_unverified,
            effect_contract_hashes=checkpoint.effect_contract_hashes,
            effect_evidence=checkpoint.effect_evidence,
            stored_effects=None,
            identity=checkpoint.identity,
            input_verified=checkpoint.input_verified,
            starting_state_settled=checkpoint.starting_state_settled,
            delivery_attempted=checkpoint.delivery_attempted,
            delivery_receipt=checkpoint.delivery_receipt,
            resolution=checkpoint.resolution,
            drag_end_resolution=checkpoint.drag_end_resolution,
            fresh_actuation_events=list(checkpoint.fresh_actuation_events),
            postconditions_ok=checkpoint.postconditions_ok,
            delivery_uncertainty=checkpoint.delivery_uncertainty,
            governed_authorization_id=checkpoint.governed_authorization_id,
            governed_approval_source=checkpoint.governed_approval_source,
            manifest=manifest,
            workflow=workflow,
        )
        expected_postconditions = (
            []
            if checkpoint.skipped
            else [
                condition.model_copy(deep=True)
                for condition in workflow.steps[index].expect
            ]
        )
        if checkpoint.expected_postconditions != expected_postconditions:
            raise StateDiverged(
                "the linear checkpoint does not retain the exact declared "
                "postconditions"
            )
    prior = (
        checkpoints[pending.resume_from_index - 1]
        if pending.resume_from_index > 0
        else None
    )
    if pending.resume_from_step_id != (prior.step_id if prior is not None else None):
        raise StateDiverged(
            "the linear checkpoint cursor does not match the durable pause"
        )
    return checkpoints[-1] if checkpoints else None


def _program_resume_checkpoints(
    *,
    run_dir: Path,
    checkpoints: list[ProgramCheckpoint],
    pending: PendingEscalation,
    manifest: RunManifest,
    workflow: Workflow,
    live_bundle_version: str,
) -> tuple[list[ProgramCheckpoint], Optional[ProgramCheckpoint]]:
    """Validate and freeze the complete program history for one pause."""

    continuing = pending.status == "continuing"
    attended_tail = (
        bool(checkpoints)
        and pending.status in {"approved", "continuing"}
        and checkpoints[-1].attended_transition is not None
        and checkpoints[-1].seq == pending.program_checkpoint_seq + 1
        and checkpoints[-1].verified_state_id == (pending.state_id or pending.step_id)
    )
    expected_count = pending.program_checkpoint_seq + (1 if attended_tail else 0)
    valid_count = (
        len(checkpoints) >= pending.program_checkpoint_seq
        if continuing
        else len(checkpoints) == expected_count
    )
    if not valid_count:
        raise StateDiverged(
            "the program checkpoint history does not match the durable pause cursor"
        )

    def _boundary_prefix(history: list[str], delta: list[str]) -> list[str]:
        if len(delta) > len(history):
            raise StateDiverged(
                "the program transition delta exceeds its retained history"
            )
        prefix = history[: len(history) - len(delta)] if delta else list(history)
        if history != [*prefix, *delta]:
            raise StateDiverged("the program transition history is not append-only")
        return prefix

    def _validate_checkpoint_history(
        checkpoint: ProgramCheckpoint,
        parent_history: list[str],
    ) -> list[str]:
        history = list(checkpoint.transition_history)
        delta = list(checkpoint.transition_delta)
        prefix = _boundary_prefix(history, delta)
        if (
            not history
            or prefix != parent_history
            or checkpoint.transition_parent_hash != history_hash(parent_history)
            or checkpoint.transition_history_hash != history_hash(history)
            or (not delta and checkpoint.attended_transition is None)
        ):
            raise StateDiverged(
                "the program transition history chain is incomplete or changed"
            )
        return history

    def _validate_pause_history(parent_history: list[str]) -> list[str]:
        history = list(pending.program_history)
        delta = list(pending.program_history_delta)
        prefix = _boundary_prefix(history, delta)
        if (
            not history
            or prefix != parent_history
            or pending.program_parent_history_hash != history_hash(parent_history)
            or pending.program_history_hash != history_hash(history)
        ):
            raise StateDiverged(
                "the durable program pause history chain is incomplete or changed"
            )
        return history

    transition_history: list[str] = []
    pause_validated = False
    for position, checkpoint in enumerate(checkpoints, start=1):
        if position == pending.program_checkpoint_seq + 1:
            transition_history = _validate_pause_history(transition_history)
            pause_validated = True
        transition_history = _validate_checkpoint_history(
            checkpoint,
            transition_history,
        )
    if not pause_validated:
        _validate_pause_history(transition_history)

    for position, checkpoint in enumerate(checkpoints, start=1):
        is_post_pause = continuing and position > pending.program_checkpoint_seq
        is_attended_tail = attended_tail and position == len(checkpoints)
        if (
            checkpoint.schema_version != 2
            or checkpoint.seq != position
            or checkpoint.run_id != manifest.run_id
            or checkpoint.workflow_name != workflow.name
            or not checkpoint.bundle_version
            or checkpoint.bundle_version != live_bundle_version
            or not checkpoint.frames
            or checkpoint.frames[-1].state_id != checkpoint.verified_state_id
            or checkpoint.frames[-1].params != checkpoint.bound_params
        ):
            raise StateDiverged(
                "the program checkpoint history does not match the workflow"
            )
        leaf = checkpoint.frames[-1]
        graph = (
            workflow.program
            if leaf.graph_id == TOP_GRAPH_ID
            else workflow.subflows.get(leaf.graph_id)
        )
        state = graph.states.get(leaf.state_id) if graph is not None else None
        expected_step_id = (
            state.step.id
            if state is not None and state.step is not None
            else leaf.state_id
        )
        if (
            state is None
            or checkpoint.verified_state_id != state.id
            or checkpoint.step_id != expected_step_id
        ):
            raise StateDiverged(
                "the program checkpoint references a different workflow state"
            )
        valid_time = (
            _created_later(checkpoint.created_at, pending.created_at)
            if is_post_pause or is_attended_tail
            else _created_no_later(checkpoint.created_at, pending.created_at)
        )
        if not valid_time:
            raise StateDiverged(
                "the program checkpoint history changed after the durable pause"
            )
        if state.step is None:
            raise StateDiverged(
                "the program checkpoint has no declared action semantics"
            )
        verified_keys = list(checkpoint.new_effect_keys)
        unverified_keys = list(checkpoint.new_unverified_effect_keys)
        _validate_retained_step_proof(
            step=state.step,
            params=checkpoint.bound_params,
            run_id=manifest.run_id,
            skipped=checkpoint.skipped,
            actuation=checkpoint.actuation,
            effect_verified=(True if verified_keys else None),
            effect_approved_unverified=bool(unverified_keys),
            effect_contract_hashes=verified_keys or unverified_keys,
            effect_evidence=checkpoint.new_effect_evidence,
            stored_effects=(
                checkpoint.new_effects
                if verified_keys
                else checkpoint.new_unverified_effects
            ),
            identity=checkpoint.identity,
            input_verified=checkpoint.input_verified,
            starting_state_settled=checkpoint.starting_state_settled,
            delivery_attempted=checkpoint.delivery_attempted,
            delivery_receipt=checkpoint.delivery_receipt,
            resolution=checkpoint.resolution,
            drag_end_resolution=checkpoint.drag_end_resolution,
            fresh_actuation_events=list(checkpoint.fresh_actuation_events),
            postconditions_ok=checkpoint.postconditions_ok,
            delivery_uncertainty=checkpoint.delivery_uncertainty,
            governed_authorization_id=checkpoint.governed_authorization_id,
            governed_approval_source=checkpoint.governed_approval_source,
            manifest=manifest,
            workflow=workflow,
        )
        expected_texts = [
            condition.text
            for condition in state.step.expect
            if getattr(condition.kind, "value", condition.kind) == "text_present"
            and condition.text
        ]
        if not checkpoint.skipped and checkpoint.expected_texts != expected_texts:
            raise StateDiverged(
                "the program checkpoint screen-state proof does not match the "
                "declared postconditions"
            )
        expected_postconditions = (
            []
            if checkpoint.skipped
            else [condition.model_copy(deep=True) for condition in state.step.expect]
        )
        if checkpoint.expected_postconditions != expected_postconditions:
            raise StateDiverged(
                "the program checkpoint does not retain the exact declared "
                "postconditions"
            )
        effect_sets = (
            (checkpoint.new_effect_keys, checkpoint.new_effects),
            (
                checkpoint.new_unverified_effect_keys,
                checkpoint.new_unverified_effects,
            ),
        )
        for keys, effects in effect_sets:
            if len(keys) != len(effects):
                raise StateDiverged(
                    "the program checkpoint effect ledger is incomplete"
                )
            for key, effect_dump in zip(keys, effects):
                try:
                    effect = Effect.model_validate(effect_dump)
                except Exception as exc:
                    raise StateDiverged(
                        "the program checkpoint effect ledger is invalid"
                    ) from exc
                if effect.contract_hash() != key:
                    raise StateDiverged("the program checkpoint effect ledger changed")
        evidence_keys = sorted(
            evidence.effect_contract_hash for evidence in checkpoint.new_effect_evidence
        )
        if evidence_keys != sorted(checkpoint.new_effect_keys):
            raise StateDiverged(
                "the program checkpoint verification evidence is incomplete"
            )
        if checkpoint.attended_transition is not None:
            from openadapt_flow.runtime.durable.attended import (
                validate_attended_program_receipt,
            )

            validate_attended_program_receipt(
                run_dir,
                checkpoint=checkpoint,
                pending=(pending if is_attended_tail and not continuing else None),
                manifest=manifest,
                workflow=workflow,
                live_bundle_version=live_bundle_version,
                historical=continuing or not is_attended_tail,
            )

    normal_last = (
        checkpoints[pending.program_checkpoint_seq - 1]
        if pending.program_checkpoint_seq > 0
        else None
    )
    if pending.resume_from_step_id != (
        normal_last.verified_state_id if normal_last is not None else None
    ):
        raise StateDiverged(
            "the program checkpoint cursor does not match the durable pause"
        )
    frozen = [checkpoint.model_copy(deep=True) for checkpoint in checkpoints]
    return frozen, (frozen[-1] if frozen else None)


def _begin_continuation(
    store: CheckpointStore,
    pending: PendingEscalation,
    approved: ApprovalRecord,
) -> PendingEscalation:
    """Persist exact authority, then advance the exact pause to continuing."""

    if pending.status == "continuing":
        retained = store.read_approval()
        if retained != approved:
            raise ApprovalRequired(
                "the continuing durable run lacks its exact admitted approval"
            )
        return pending
    return store.commit_approval_transition(
        expected_pending=pending,
        approval=approved,
        target_status="continuing",
    )


def resume(
    run_dir: Path | str,
    replayer: Any,
    *,
    approval: Optional[ApprovalRecord] = None,
    bundle_dir: Optional[Path | str] = None,
    params: Optional[dict[str, str]] = None,
    worklists: Optional[dict[str, list[dict[str, str]]]] = None,
    save_healed_to: Optional[Path | str] = None,
    execution_target_kind: Optional[ExecutionTargetKind] = None,
    now: Optional[datetime] = None,
    key: Optional[str] = None,
) -> RunReport:
    """Resume one exact pause under the shared continuation fence."""

    from openadapt_flow import crypto as _crypto
    from openadapt_flow.runtime.durable.continuation import (
        ContinuationCoordinator,
        ContinuationGuard,
    )

    resolved_key = _crypto.resolve_key(key)
    coordinator = ContinuationCoordinator(run_dir, key=resolved_key)
    pending = coordinator.store.read_pending()
    if pending is None:
        raise ApprovalRequired("there is no active durable pause to resume")
    if pending.status == "rejected":
        from openadapt_flow.runtime.durable.approval import RunRejected

        raise RunRejected("the durable run was rejected and cannot be resumed")
    manifest = coordinator.store.read_manifest()
    if manifest is None:
        raise ApprovalRequired("the durable manifest is missing")
    if manifest.delivery_authority_kind == "cloud_runner":
        binding = getattr(replayer, "managed_dispatch_binding", None)
        if (
            binding is None
            or binding.run_id != manifest.remote_delivery_run_id
            or binding.authorization != manifest.governed_authorization
            or binding.binding_sha256 != manifest.managed_dispatch_binding_sha256
        ):
            raise ApprovalRequired(
                "managed resume requires the exact validated dispatch binding"
            )
    with coordinator.lease(operation="resume", now=now) as token:
        prior_guard = getattr(replayer, "_durable_continuation_guard", None)
        replayer._durable_continuation_guard = ContinuationGuard(coordinator, token)
        try:
            return _resume_under_lease(
                run_dir,
                replayer,
                approval=approval,
                bundle_dir=bundle_dir,
                params=params,
                worklists=worklists,
                save_healed_to=save_healed_to,
                execution_target_kind=execution_target_kind,
                now=now,
                key=resolved_key,
            )
        finally:
            replayer._durable_continuation_guard = prior_guard


def _resume_under_lease(
    run_dir: Path | str,
    replayer: Any,
    *,
    approval: Optional[ApprovalRecord] = None,
    bundle_dir: Optional[Path | str] = None,
    params: Optional[dict[str, str]] = None,
    worklists: Optional[dict[str, list[dict[str, str]]]] = None,
    save_healed_to: Optional[Path | str] = None,
    execution_target_kind: Optional[ExecutionTargetKind] = None,
    now: Optional[datetime] = None,
    key: Optional[str] = None,
) -> RunReport:
    """Resume a durably-paused run from its last verified checkpoint.

    Args:
        run_dir: The original run directory (holds the checkpoints, the
            manifest, the pending escalation, and any approval record).
        replayer: A live :class:`~openadapt_flow.runtime.replayer.Replayer`
            (its backend/vision cannot be serialized, so the caller provides a
            fresh one bound to the recovered system). Durability is force-
            enabled on it so the resumed leg keeps checkpointing.
        approval: The authenticated authorization to resume (P0-5). When omitted,
            an ``approval.json`` written by the ``approve`` command is used. If
            neither is present (or the record carries no approver), resume is
            REFUSED with :class:`~.approval.ApprovalRequired`.
        bundle_dir: Override the bundle recorded in the manifest (rarely
            needed); defaults to the manifest's ``bundle_dir``.
        params: Override the parameter bindings recorded in the manifest;
            defaults to the manifest's ``params`` so the resume re-binds
            identically.
        worklists: Override the worklists recorded in the manifest; defaults to
            the original frozen worklists so program loops resume identically.
        save_healed_to: Override the manifest's healed-bundle path.
        execution_target_kind: Resolved backend token for the resumed leg's
            report and substrate-aware runtime attestation.
        now: Injectable clock for the stale-pause check (defaults to UTC now).
        key: At-rest passphrase for an ENCRYPTED run (its checkpoints and/or its
            bundle). Resolved from ``key`` or ``OPENADAPT_BUNDLE_KEY``. Used to
            decrypt the durable checkpoints, load an encrypted bundle, and keep
            the resumed leg sealing new checkpoints. None => plaintext.

    Returns:
        The :class:`~openadapt_flow.ir.RunReport` for the resumed leg.

    Raises:
        FileNotFoundError: when ``run_dir`` has no manifest (it was not run
            durably) and no ``bundle_dir`` override is supplied.
        ResumeRefused: (``ApprovalRequired`` / ``PauseExpired`` /
            ``BundleMismatch`` / ``StateDiverged``) when the resume is not
            authorized, the pause expired, the bundle changed, or the live app
            diverged from the checkpoint's expected state.
    """
    from openadapt_flow import crypto as _crypto

    key = _crypto.resolve_key(key)
    run_dir = Path(run_dir)
    store = CheckpointStore(run_dir, key=key)
    manifest = store.read_manifest()
    pending = store.read_pending()

    if manifest is None:
        raise FileNotFoundError(
            f"Cannot resume {run_dir}: no durable manifest was found. A "
            "continuation must use the exact retained run context."
        )
    if manifest.schema_version != 2:
        raise StateDiverged(
            "this durable run predates exact run and pause binding; start a "
            "fresh version-2 run instead of migrating authorization evidence"
        )
    store.validate_namespace(manifest)
    if pending is None:
        raise ApprovalRequired(
            "the run has no active durable pause to approve and resume"
        )
    if pending.schema_version != 2:
        raise StateDiverged(
            "this durable pause predates exact run and pause binding; start a "
            "fresh version-2 run"
        )

    resolved_bundle = bundle_dir or manifest.bundle_dir
    resolved_bundle = Path(resolved_bundle)
    if params is not None and params != manifest.params:
        raise StateDiverged(
            "resume parameters differ from the exact retained run inputs"
        )
    if worklists is not None and worklists != manifest.worklists:
        raise StateDiverged(
            "resume worklists differ from the exact retained run inputs"
        )
    resolved_params = dict(manifest.params)
    resolved_worklists = {
        name: [dict(row) for row in rows] for name, rows in manifest.worklists.items()
    }
    resolved_healed = save_healed_to or manifest.save_healed_to

    live_bundle_version = bundle_version(resolved_bundle)

    # -- P0-5: enforce an authenticated approval before ANYTHING re-executes.
    # A pending escalation means a human was asked to authorize the resume; no
    # valid approval => refuse (never a silent proceed). A run_dir with no
    # pending escalation is not a paused run -- nothing to authorize.
    if (
        not manifest.run_id
        or pending.run_id != manifest.run_id
        or pending.workflow_name != manifest.workflow_name
        or (not pending.program and pending.params != manifest.params)
    ):
        raise StateDiverged(
            "the durable pause does not match the exact retained run manifest"
        )
    stored_approval = store.read_approval()
    approved: ApprovalRecord = enforce_resume_authorization(
        pending,
        approval if approval is not None else stored_approval,
        bundle_version=live_bundle_version,
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run_dir,
        now=now,
    )
    # Caller-owned approval objects remain mutable. All later admission and
    # callback boundaries consume only the exact record that passed this gate.
    approved = approved.model_copy(deep=True)
    from openadapt_flow.runtime.durable.continuation import (
        ContinuationCoordinator,
        current_continuation_token,
    )

    continuation_token = current_continuation_token()
    if continuation_token is None:
        raise StateDiverged("durable continuation lost its external approval authority")
    ContinuationCoordinator(run_dir, key=key).bind_approval(
        continuation_token, approved
    )
    resumed_screenshots_may_leave_box = bool(
        getattr(replayer, "_screenshots_may_leave_box", False)
    )
    if resumed_screenshots_may_leave_box and not manifest.screenshots_may_leave_box:
        continuation_guard = getattr(replayer, "_durable_continuation_guard", None)
        if continuation_guard is None:
            raise StateDiverged(
                "durable continuation lost its audit-evidence authority guard"
            )
        updated_manifest = manifest.model_copy(
            update={"screenshots_may_leave_box": True}
        )
        store.cas_manifest(store.model_digest(manifest), updated_manifest)
        # Commit the sticky privacy posture to the same continuation authority
        # before retained-state checks or resumed execution can call a model.
        continuation_guard.acknowledge_progress()
        manifest = updated_manifest
    if pending.delivery_uncertainty is not None:
        last_linear = store.last_checkpoint()
        last_program = store.last_program_checkpoint()
        reconciled_without_retry = (
            not pending.program
            and last_linear is not None
            and last_linear.step_index == pending.step_index
            and last_linear.next_step_index > pending.step_index
            and last_linear.actuation in {"human_attended", "human_attended_skip"}
        ) or (
            pending.program
            and last_program is not None
            and last_program.verified_state_id == (pending.state_id or pending.step_id)
            and last_program.attended_transition is not None
        )
        if not reconciled_without_retry and not approved.authorize_uncertain_retry:
            raise ApprovalRequired(
                "the paused step may already have actuated; ordinary resume "
                "cannot repeat it. Reconcile and independently verify the "
                "outcome through the attended completion path, or create a "
                "fresh approval that explicitly authorizes one "
                "uncertain-delivery retry"
            )

    workflow = Workflow.load(resolved_bundle, key=key)
    if workflow.name != manifest.workflow_name:
        raise StateDiverged(
            "the durable manifest names a different workflow than the bundle"
        )
    if manifest is not None and manifest.governed_authorization is not None:
        existing = getattr(replayer, "governed_authorization", None)
        if existing is not None and existing != manifest.governed_authorization:
            raise BundleMismatch(
                "resume Replayer carries a different governed authorization "
                "than the durable run manifest"
            )
        replayer.governed_authorization = manifest.governed_authorization
        replayer.governed_continuation = True
    # Resume never accepts a caller override for delivery authority.  The
    # retained manifest is the sole source for both customer-local and managed
    # Cloud continuation binding.
    replayer.delivery_authority_kind = manifest.delivery_authority_kind
    replayer.remote_delivery_run_id = manifest.remote_delivery_run_id
    # Keep the resumed leg sealing new checkpoints with the same key.
    replayer.checkpoint_key = key
    if workflow.program is not None:
        if not pending.program:
            raise StateDiverged(
                "a program workflow cannot resume from a linear durable pause"
            )
        if store.checkpoints():
            raise StateDiverged(
                "a program continuation contains linear checkpoint artifacts"
            )
        program_checkpoints, program_checkpoint = _program_resume_checkpoints(
            run_dir=run_dir,
            checkpoints=store.program_checkpoints(),
            pending=pending,
            manifest=manifest,
            workflow=workflow,
            live_bundle_version=live_bundle_version,
        )
        for retained_checkpoint in program_checkpoints:
            replayer.revalidate_program_checkpoint_effects(
                retained_checkpoint,
                list(retained_checkpoint.new_effects),
                workflow=workflow,
            )
        if program_checkpoint is not None:
            replayer.revalidate_program_checkpoint_state(
                program_checkpoint,
                bundle_dir=resolved_bundle,
            )
        if (
            bundle_version(resolved_bundle) != live_bundle_version
            or approved.bundle_version != live_bundle_version
        ):
            raise BundleMismatch(
                "the workflow bundle changed after resume approval or "
                "revalidation; refusing to continue"
            )
        pending = _begin_continuation(store, pending, approved)
        return _resume_program(
            store=store,
            replayer=replayer,
            workflow=workflow,
            checkpoint=program_checkpoint,
            checkpoints=program_checkpoints,
            bundle_dir=resolved_bundle,
            params=resolved_params,
            worklists=resolved_worklists,
            save_healed_to=resolved_healed,
            live_bundle_version=live_bundle_version,
            run_id=(manifest.run_id if manifest is not None else None),
            execution_target_kind=execution_target_kind,
            manifest=manifest,
            pending=pending,
            approval=approved,
            stored_approval=approved,
        )

    if pending.program:
        raise StateDiverged(
            "a linear workflow cannot resume from a program durable pause"
        )
    if store.program_checkpoints():
        raise StateDiverged(
            "a linear continuation contains program checkpoint artifacts"
        )
    # -- linear resume -------------------------------------------------------
    linear_checkpoints = store.checkpoints()
    last_linear = _linear_resume_checkpoint(
        run_dir=run_dir,
        checkpoints=linear_checkpoints,
        pending=pending,
        manifest=manifest,
        workflow=workflow,
    )
    for checkpoint in linear_checkpoints:
        if checkpoint.effect_verified is not True:
            continue
        step = workflow.steps[checkpoint.step_index]
        replayer.revalidate_retained_effects(
            _resolved_step_effects(
                step,
                params=checkpoint.params,
                run_id=manifest.run_id,
                actuation=checkpoint.actuation,
            ),
            workflow=workflow,
            step=step,
            actuation_path=("api" if checkpoint.actuation == "api" else "gui"),
            retained_evidence=list(checkpoint.effect_evidence),
        )
    if last_linear is not None and last_linear.next_step_index < len(workflow.steps):
        replayer.revalidate_linear_checkpoint_state(
            last_linear,
            bundle_dir=resolved_bundle,
        )
    if (
        bundle_version(resolved_bundle) != live_bundle_version
        or approved.bundle_version != live_bundle_version
    ):
        raise BundleMismatch(
            "the workflow bundle changed after resume approval or "
            "revalidation; refusing to continue"
        )
    pending = _begin_continuation(store, pending, approved)
    start_index = last_linear.next_step_index if last_linear is not None else 0
    from openadapt_flow.runtime.replayer import _DURABLE_RESUME_AUTHORITY

    effective_run_id = replayer._admit_durable_resume(
        _DURABLE_RESUME_AUTHORITY,
        mode="linear",
        workflow=workflow,
        run_dir=run_dir,
        bundle_dir=resolved_bundle,
        run_id=(manifest.run_id if manifest is not None else None),
        params=resolved_params,
        worklists=resolved_worklists,
        resume_from=start_index,
        resume_program=None,
        durable_context={
            "manifest": manifest.model_copy(deep=True),
            "pending": pending.model_copy(deep=True),
            "approval": approved.model_copy(deep=True),
            "linear_checkpoints": [
                checkpoint.model_copy(deep=True) for checkpoint in linear_checkpoints
            ],
            "program_checkpoints": [],
            "auxiliary_digest": replayer._durable_auxiliary_digest(run_dir, []),
        },
        authorizing_approval=approved.model_copy(deep=True),
    )
    replayer.durable = True
    return replayer.run(
        workflow,
        params=resolved_params,
        worklists=resolved_worklists,
        bundle_dir=resolved_bundle,
        run_dir=run_dir,
        save_healed_to=(Path(resolved_healed) if resolved_healed else None),
        resume_from=start_index,
        run_id=effective_run_id,
        idempotency_key=(manifest.idempotency_key if manifest is not None else None),
        execution_target_kind=execution_target_kind,
        prior_screenshots_may_leave_box=(
            manifest.screenshots_may_leave_box if manifest is not None else False
        ),
        prior_model_calls=(manifest.model_calls if manifest is not None else 0),
        prior_external_network_calls=(
            manifest.external_network_calls if manifest is not None else "unknown"
        ),
    )


def _resume_program(
    *,
    store: CheckpointStore,
    replayer: Any,
    workflow: Workflow,
    checkpoint: Optional[ProgramCheckpoint],
    checkpoints: list[ProgramCheckpoint],
    bundle_dir: Path,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    save_healed_to: Optional[Path | str],
    live_bundle_version: str,
    run_id: str,
    execution_target_kind: Optional[ExecutionTargetKind],
    manifest: RunManifest,
    pending: PendingEscalation,
    approval: ApprovalRecord,
    stored_approval: Optional[ApprovalRecord],
) -> RunReport:
    """Restore and continue a Phase-2 PROGRAM run from its interpreter checkpoint.

    Revalidates the bundle version and the live app state (and re-verifies the
    already-confirmed effects still hold) BEFORE re-driving, then hands the
    restored interpreter state to the replayer's program resume path. A program
    run that halted on its very FIRST state has no checkpoint (``checkpoint`` is
    None): there is nothing verified to restore, so it resumes from the top.
    """
    attended_checkpoint = (
        checkpoint is not None
        and checkpoint.attended_transition is not None
        and pending.status in {"approved", "continuing"}
        and checkpoint.seq == pending.program_checkpoint_seq + 1
        and checkpoint.verified_state_id == (pending.state_id or pending.step_id)
        and _created_later(checkpoint.created_at, pending.created_at)
    )
    if checkpoint is None:
        if (
            pending.program_checkpoint_seq != 0
            or pending.resume_from_step_id is not None
        ):
            raise StateDiverged(
                "the durable program pause lost its verified interpreter checkpoint"
            )
    else:
        continuing_checkpoint = (
            pending.status == "continuing"
            and checkpoint.seq >= pending.program_checkpoint_seq
            and (
                checkpoint.seq == pending.program_checkpoint_seq
                or _created_later(checkpoint.created_at, pending.created_at)
            )
        )
        if (
            checkpoint.workflow_name != workflow.name
            or (
                not attended_checkpoint
                and not continuing_checkpoint
                and (
                    checkpoint.seq != pending.program_checkpoint_seq
                    or pending.resume_from_step_id != checkpoint.verified_state_id
                    or not _created_no_later(checkpoint.created_at, pending.created_at)
                )
            )
            or checkpoint.run_id != run_id
        ):
            raise StateDiverged(
                "the program checkpoint cursor changed after the durable pause"
            )
        if checkpoint.attended_transition is not None:
            if manifest is None:
                raise BundleMismatch(
                    "the attended interpreter transition has no run manifest"
                )
            from openadapt_flow.runtime.durable.attended import (
                validate_attended_program_receipt,
            )

            validate_attended_program_receipt(
                store.run_dir,
                checkpoint=checkpoint,
                pending=(pending if pending.status == "approved" else None),
                manifest=manifest,
                workflow=workflow,
                live_bundle_version=live_bundle_version,
                historical=pending.status == "continuing",
            )
        if (
            checkpoint.bundle_version
            and checkpoint.bundle_version != live_bundle_version
        ):
            raise BundleMismatch(
                "the interpreter checkpoint was captured against bundle version "
                f"{checkpoint.bundle_version!r} but the bundle being resumed is "
                f"{live_bundle_version!r} — the program changed; re-run"
            )
    from openadapt_flow.runtime.replayer import _DURABLE_RESUME_AUTHORITY

    effective_run_id = replayer._admit_durable_resume(
        _DURABLE_RESUME_AUTHORITY,
        mode="program",
        workflow=workflow,
        run_dir=store.run_dir,
        bundle_dir=bundle_dir,
        run_id=run_id,
        params=params,
        worklists=worklists,
        resume_from=None,
        resume_program=checkpoint,
        durable_context={
            "manifest": manifest.model_copy(deep=True),
            "pending": pending.model_copy(deep=True),
            "approval": (
                stored_approval.model_copy(deep=True)
                if stored_approval is not None
                else None
            ),
            "linear_checkpoints": [],
            "program_checkpoints": [item.model_copy(deep=True) for item in checkpoints],
            "auxiliary_digest": replayer._durable_auxiliary_digest(
                store.run_dir, checkpoints
            ),
        },
        authorizing_approval=approval.model_copy(deep=True),
    )
    replayer.durable = True
    return replayer.run(
        workflow,
        params=params,
        worklists=worklists,
        bundle_dir=bundle_dir,
        run_dir=store.run_dir,
        save_healed_to=(Path(save_healed_to) if save_healed_to else None),
        resume_program=checkpoint,
        run_id=effective_run_id,
        idempotency_key=manifest.idempotency_key,
        execution_target_kind=execution_target_kind,
        prior_screenshots_may_leave_box=(
            manifest.screenshots_may_leave_box if manifest is not None else False
        ),
        prior_model_calls=(manifest.model_calls if manifest is not None else 0),
        prior_external_network_calls=(
            manifest.external_network_calls if manifest is not None else "unknown"
        ),
    )
