"""Replay preparation checks, including the non-authorizing no-input path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Optional

from openadapt_flow.ir import ActionKind, StateKind

if TYPE_CHECKING:
    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.ir import RunReport, Step, StepResult, Workflow
    from openadapt_flow.runtime.authorization import RuntimeParamScalar
    from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint
    from openadapt_flow.runtime.effects.effect import Effect
    from openadapt_flow.runtime.replayer import Replayer


def sealed_run_unsupported(self: Replayer, workflow: Workflow) -> Optional[str]:
    """Name a run feature whose run-directory state is not sealed yet."""
    if self.approval_gate is not None:
        from openadapt_flow.approval_mode.dry_run import DryRunGate
        from openadapt_flow.approval_mode.model import ApprovalMode

        gate = self.approval_gate
        # This exact non-authorizing gate writes no approval/request/state
        # files: its closed evidence belongs only to the sealed run report.
        # No subclass or caller-declared capability can make an attended
        # gate's plaintext stores eligible for a sealed run.
        if (
            type(gate) is not DryRunGate
            or gate.mode is not ApprovalMode.NOT_GATED
            or gate.approval_store is not None
            or gate.log is not None
            or gate.approver is not None
            or gate.record is not None
        ):
            return "an approval gate (attended approvals)"
    if self._attended_draft_by is not None:
        return "an attended draft"
    program = workflow.program
    if program is not None:
        graphs = [program, *(getattr(workflow, "subflows", None) or {}).values()]
        for graph in graphs:
            for state in getattr(graph, "states", {}).values():
                if getattr(state, "kind", None) == StateKind.BUSINESS_DECISION:
                    return "a business-decision state"
    return None


def actuator_for(self: Replayer, binding: Any) -> Optional[Any]:
    """The actuator that runs ``binding``, or None when none can."""

    if getattr(self.approval_gate, "stops_before_any_input", False) is True:
        return None
    if binding is None:
        return None
    if binding.kind != "web_form":
        return self.api_actuator
    if self.web_form_actuator is not None:
        return self.web_form_actuator
    # In a multi-surface run the request goes through the active
    # surface's session, which on a resume may not be self.backend yet.
    router = getattr(self, "_surface_router", None)
    backend = router.active_backend if router is not None else self.backend
    page = getattr(backend, "page", None)
    if page is None:
        return None
    from openadapt_flow.runtime.actuators.web_form import (
        PlaywrightSessionTransport,
        WebFormActuator,
    )

    try:
        return WebFormActuator(PlaywrightSessionTransport(page))
    except Exception:  # noqa: BLE001 - no live session: the GUI runs
        return None


def record_dry_run_preconditions(
    self: Replayer, step: Step, result: StepResult
) -> None:
    if self.approval_gate is None:
        return
    from openadapt_flow.approval_mode.dry_run import DryRunGate

    if isinstance(self.approval_gate, DryRunGate):
        self.approval_gate.record_preconditions(step, result)


def disable_api_for_dry_run(
    self: Replayer, approval_gate: Optional[ApprovalGate]
) -> None:
    if getattr(approval_gate, "stops_before_any_input", False) is True:
        # Request preparation can execute page scripts. Never enter an API
        # span or construct its actuator in a zero-input Check-it run.
        self.api_steps = False
        self.api_steps_off_reason = (
            "Check-it dry-run does not prepare or send API steps"
        )


def validate_dry_run_entry(
    self: Replayer,
    workflow: Workflow,
    *,
    resume_from: Optional[int],
    resume_program: Optional[ProgramCheckpoint],
    worklists: Optional[dict[str, list[dict[str, str]]]],
) -> None:
    if getattr(self.approval_gate, "stops_before_any_input", False) is True:
        from openadapt_flow.ir import ActionKind

        if (
            self.durable
            or resume_from is not None
            or resume_program is not None
            or workflow.program is not None
            or workflow.subflows
            or worklists
            or workflow.surfaces
            or self.surface_backends
            or any(step.action is ActionKind.SWITCH_SURFACE for step in workflow.steps)
        ):
            raise ValueError(
                "Check-it dry-run requires a single-surface linear replay without durable continuation"
            )


def dry_run_preparation_refusal(
    self: Replayer,
    workflow: Workflow,
    params: Mapping[str, RuntimeParamScalar],
    step: Step,
    result: StepResult,
    error: Optional[str],
    resolved_effects: Optional[list[Effect]],
) -> tuple[Optional[str], Optional[list[Effect]]]:
    if (
        error is None
        and getattr(self.approval_gate, "stops_before_any_input", False) is True
        and (step.action not in (ActionKind.READ, ActionKind.WAIT) or step.effects)
    ):
        # A verifier may navigate a second UI session to snapshot a
        # record. Stop before that preparation as well as before input.
        # Only pure contract binding runs here; no request is prepared.
        if step.effects:
            resolved_effects = self._resolve_effects(step.effects, params)
            result.effect_contract_hashes.extend(
                effect.contract_hash() for effect in resolved_effects
            )
        error = self._delivery_authorization_refusal(workflow, params, step, result)
    return error, resolved_effects


def stamp_approval(self: Replayer, report: RunReport) -> None:
    """Stamp the approval gate's post-run decision on the report."""

    gate = self.approval_gate
    if gate is None:
        return
    try:
        report.approval = gate.finish_item(report)
    except Exception:  # noqa: BLE001 - an unknown decision goes to a person
        from openadapt_flow.approval_mode.model import (
            ApprovalModeItemEvidence,
            ItemDecisionKind,
        )

        report.approval = ApprovalModeItemEvidence(
            mode=gate.mode,
            decision=ItemDecisionKind.PROBLEM,
            decision_reason="gate_error",
            needs_person=True,
        )
