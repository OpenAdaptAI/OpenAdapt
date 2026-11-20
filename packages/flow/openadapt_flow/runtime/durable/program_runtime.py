"""Program delivery cursor capture and verified checkpoint persistence.

Replayer retains the entry methods so subclass and interruption seams continue
through the same shared program path. Durable models remain in program_checkpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Optional, cast

from openadapt_flow.ir import PostconditionKind
from openadapt_flow.runtime.durable.first_local_delivery import FirstLocalDeliveryHeld
from openadapt_flow.runtime.durable.first_local_runtime import (
    FirstLocalDeliveryStep,
    mark_first_local_delivery_held,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    ProgramCheckpoint,
    ProgramDeliveryCursor,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    history_hash as _history_hash,
)

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, State, StepResult
    from openadapt_flow.runtime.authorization import RuntimeParamScalar
    from openadapt_flow.runtime.replayer import Replayer


def capture_program_delivery_cursor(
    self: Replayer,
    state: State,
    params: Mapping[str, RuntimeParamScalar],
    report: RunReport,
) -> ProgramDeliveryCursor:
    """Freeze exactly the interpreter boundary used by pause and checkpoint."""
    delta = list(report.visited_states[self._program_history_boundary_index :])
    history = [*self._program_durable_history, *delta]
    return ProgramDeliveryCursor.capture(
        run_id=self._run_id,
        workflow_name=cast(Any, self._program_durable).workflow_name,
        bundle_version=self._bundle_version,
        step_id=state.step.id if state.step else "",
        state_id=self._current_state_id,
        params=dict(params),
        program_frames=list(self._frame_stack),
        program_checkpoint_seq=self._program_seq,
        program_history_hash=_history_hash(history),
        program_parent_history_hash=self._program_history_parent_hash,
        program_history_delta=delta,
        program_history=history,
        program_transition_evidence_delta=report.program_transition_evidence[
            self._program_transition_evidence_checkpoint_len :
        ],
        business_decision_evidence_delta=report.business_decision_evidence[
            self._business_decision_evidence_checkpoint_len :
        ],
        program_exception_evidence_delta=report.program_exception_evidence[
            self._program_exception_evidence_checkpoint_len :
        ],
    )


def record_program_checkpoint(
    self: Replayer,
    state: State,
    result: StepResult,
    params: Mapping[str, RuntimeParamScalar],
    report: RunReport,
    *,
    first_local_step: Optional[FirstLocalDeliveryStep] = None,
) -> None:
    """Persist a verified-state interpreter checkpoint (Tier-3, program mode).

    Captures the whole interpreter state -- the frame stack (subflow / loop
    nesting), each loop's cursor, the bound params, the effects CONFIRMED at
    this state (both their hashes, for the idempotency ledger, and their
    resolved contracts, for the resume-time re-verification), the expected
    on-screen text (for the resume-time state revalidation), and the bundle
    version -- so an approved resume RESTORES the interpreter from here."""
    from openadapt_flow.runtime.replayer import _ProgramHalt

    durable = self._program_durable
    if durable is None:
        return
    if first_local_step is not None and first_local_step.program_cursor is not None:
        try:
            if (
                first_local_step.current_program_cursor is None
                or first_local_step.current_program_cursor()
                != first_local_step.program_cursor
            ):
                raise FirstLocalDeliveryHeld(
                    "the pre-callback program cursor changed before checkpoint"
                )
        except (FirstLocalDeliveryHeld, ValueError, TypeError) as exc:
            mark_first_local_delivery_held(
                result,
                "first_local_delivery_held: the exact program cursor changed",
            )
            raise _ProgramHalt(
                "halt",
                "first_local_delivery_held: the exact program cursor changed",
                safety=True,
            ) from exc
    step = state.step
    if step is None:
        raise _ProgramHalt(
            "halt",
            "verified action checkpoint has no compiled step",
            safety=True,
        )
    from openadapt_flow.action_evidence import action_evidence_error

    profile = (
        self.governed_authorization.execution_profile
        if self.governed_authorization is not None
        else None
    )
    evidence_error = action_evidence_error(
        step,
        result,
        params=params,
        identity_required=(
            step.identity_armed
            or (
                self.governed_authorization is not None
                and self.governed_authorization.requires_verified_identity(step.id)
            )
        ),
        strict_production=(
            profile in {"standard", "regulated"}
            and (
                step.identity_armed
                or (
                    self.governed_authorization is not None
                    and self.governed_authorization.requires_verified_identity(step.id)
                )
            )
        ),
    )
    if evidence_error is not None:
        if first_local_step is not None and first_local_step.pause_digest:
            reason = "first_local_delivery_held: the program action lacks valid checkpoint evidence"
            mark_first_local_delivery_held(result, reason)
            raise _ProgramHalt("halt", reason, safety=True)
        raise _ProgramHalt(
            "halt",
            "refusing a durable checkpoint with invalid action evidence: "
            f"{evidence_error}",
            safety=True,
        )
    new_keys = (
        list(result.effect_contract_hashes) if result.effect_verified is True else []
    )
    new_unverified_keys = (
        list(result.effect_contract_hashes) if result.effect_approved_unverified else []
    )
    from openadapt_flow.policy import effects_for_actuation

    declared_effects = effects_for_actuation(step, result.actuation)
    resolved_effects = (
        [
            e.model_dump(mode="json")
            for e in self._resolve_effects(declared_effects, params)
        ]
        if declared_effects
        else []
    )
    # Extend the live ledger so a later state in the SAME leg (and the next
    # checkpoint's union) sees these as already-confirmed.
    self._completed_effect_keys.extend(new_keys)
    if new_keys:
        self._completed_effect_evidence.extend(result.effect_evidence)
    self._completed_unverified_effect_keys.extend(new_unverified_keys)
    expected = [
        pc.text
        for pc in step.expect
        if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text
    ]
    self._program_seq += 1
    report_history = list(report.visited_states)
    transition_delta = report_history[self._program_history_boundary_index :]
    transition_history = [*self._program_durable_history, *transition_delta]
    checkpoint = ProgramCheckpoint(
        run_id=self._run_id,
        workflow_name=durable.workflow_name,
        seq=self._program_seq,
        verified_state_id=state.id,
        intent=step.intent if step is not None else "",
        frames=[self._frame_to_model(f) for f in self._frame_stack],
        bound_params=dict(params),
        new_effect_keys=new_keys,
        new_effects=resolved_effects if new_keys else [],
        new_effect_evidence=(list(result.effect_evidence) if new_keys else []),
        new_unverified_effect_keys=new_unverified_keys,
        new_unverified_effects=(resolved_effects if new_unverified_keys else []),
        step_id=step.id,
        identity=result.identity,
        input_verified=result.input_verified,
        starting_state_settled=result.starting_state_settled,
        delivery_attempted=result.delivery_attempted,
        delivery_receipt=result.delivery_receipt,
        drag_end_resolution=result.drag_end_resolution,
        fresh_actuation_events=list(result.fresh_actuation_events),
        before_png=result.before_png,
        postconditions_ok=result.postconditions_ok,
        skipped=result.skipped,
        actuation=result.actuation,
        delivery_uncertainty=result.delivery_uncertainty,
        resolution=result.resolution,
        drift_oracle_calls=result.drift_oracle_calls,
        heal=result.heal,
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
        expected_texts=expected,
        expected_postconditions=(
            [condition.model_copy(deep=True) for condition in step.expect]
            if not result.skipped
            else []
        ),
        transition_history_hash=_history_hash(transition_history),
        visited_states_delta=transition_delta,
        program_transition_evidence_delta=report.program_transition_evidence[
            self._program_transition_evidence_checkpoint_len :
        ],
        business_decision_evidence_delta=report.business_decision_evidence[
            self._business_decision_evidence_checkpoint_len :
        ],
        program_exception_evidence_delta=report.program_exception_evidence[
            self._program_exception_evidence_checkpoint_len :
        ],
        transition_parent_hash=self._program_history_parent_hash,
        transition_delta=transition_delta,
        transition_history=transition_history,
        bundle_version=self._bundle_version,
    )
    pause_digest = first_local_step.pause_digest if first_local_step is not None else ""
    try:
        if pause_digest:
            durable.record_program_checkpoint(
                checkpoint, first_local_pause_digest=pause_digest
            )
            durable.consume_pause(pause_digest)
            if durable.store.read_pending() is not None:
                raise FirstLocalDeliveryHeld(
                    "another program pause is retained after the checkpoint"
                )
            self._sync_durable_audit(durable, report)
        else:
            durable.record_program_checkpoint(checkpoint)
    except FirstLocalDeliveryHeld as exc:
        mark_first_local_delivery_held(result, str(exc))
        raise _ProgramHalt("halt", str(exc), safety=True) from exc
    self._program_checkpoint_history_len = len(report_history)
    self._program_transition_evidence_checkpoint_len = len(
        report.program_transition_evidence
    )
    self._business_decision_evidence_checkpoint_len = len(
        report.business_decision_evidence
    )
    self._program_exception_evidence_checkpoint_len = len(
        report.program_exception_evidence
    )
    self._program_history_boundary_index = len(report_history)
    self._program_durable_history = transition_history
    self._program_history_parent_hash = checkpoint.transition_history_hash
    if self._durable_continuation_guard is not None:
        self._durable_continuation_guard.acknowledge_progress()
