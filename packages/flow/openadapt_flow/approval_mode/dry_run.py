"""Non-authorizing Check-it gate: observe checks, never permit app input.

Evidence uses the existing redacted/sealed approval DTO. Input values and
form contents are never added to this block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openadapt_flow.approval_mode.gate import (
    ApprovalGate,
    GateVerdict,
    SaveContext,
    StepContext,
)
from openadapt_flow.approval_mode.model import (
    ApprovalModeItemEvidence,
    CheckOutcome,
    ItemDecisionKind,
)
from openadapt_flow.approval_mode.post import ItemDecision

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Step, StepResult


class DryRunGate(ApprovalGate):
    """Refuse every delivery, including reversible steps and interstitials."""

    stops_before_first_save = True
    stops_before_any_input = True

    def __init__(self) -> None:
        # No approval store, signer registry, key, token or granting authority.
        super().__init__("not_gated", pinned_approver_keys={})

    def _reset_item_state(self) -> None:
        super()._reset_item_state()
        self._observed_preconditions: dict[
            int, tuple[StepResult, tuple[CheckOutcome, ...]]
        ] = {}

    def before_step(self, ctx: StepContext) -> GateVerdict:
        # Resolution/identity/guard checks can run. The final delivery hook
        # always refuses, including when the workflow calls an input reversible.
        return GateVerdict(proceed=True, decision=None, checks=())

    def record_preconditions(self, step: Step, result: StepResult) -> None:
        """Called only where the runtime actually passed the step's gates."""
        checks = [
            CheckOutcome(
                code="dry_run", passed=True, detail="step_gates_passed", step_id=step.id
            )
        ]
        if result.starting_state_settled is not None:
            checks.append(
                CheckOutcome(
                    code="dry_run",
                    passed=result.starting_state_settled is True,
                    detail="starting_state_settled",
                    step_id=step.id,
                )
            )
        if step.guard is not None:
            checks.append(
                CheckOutcome(
                    code="dry_run", passed=True, detail="guard_passed", step_id=step.id
                )
            )
        # SCROLL evaluates this predicate in its input loop, after this hook.
        if step.wait_until is not None and step.action.value != "scroll":
            checks.append(
                CheckOutcome(
                    code="dry_run",
                    passed=True,
                    detail="wait_until_passed",
                    step_id=step.id,
                )
            )
        self._observed_preconditions[id(result)] = (result, tuple(checks))

    def before_save(self, ctx: SaveContext) -> GateVerdict:
        """The last-point hook also refuses all unproved prefix inputs."""
        observed = self._observed_preconditions.get(id(ctx.result))
        checks = (
            list(observed[1])
            if observed is not None and observed[0] is ctx.result
            else []
        )
        identity, failure = self._check_right_record(ctx)
        if ctx.result.identity is None or ctx.result.identity.status != "verified":
            identity = CheckOutcome(
                code="right_record",
                passed=False,
                detail="current_identity_not_verified",
                step_id=ctx.step.id,
            )
        checks.append(identity)
        checks.append(
            CheckOutcome(
                code="dry_run",
                passed=all(
                    result.delivery_attempted is False
                    for result in [*ctx.prior_results, ctx.result]
                ),
                detail="no_input_delivered",
                step_id=ctx.step.id,
            )
        )
        # Only a declared final save on a prefilled form earns a reached-boundary
        # result. An unknown prefix input still stops, but is a refused dry-run.
        reached = bool(
            observed is not None
            and all(check.passed for check in checks)
            and failure is None
            and self._is_save(ctx)
            and ctx.step.effects
            and ctx.workflow.steps
            and ctx.workflow.steps[-1].id == ctx.step.id
        )
        checks.append(
            CheckOutcome(
                code="dry_run",
                passed=reached,
                detail="stopped_before_delivery"
                if reached
                else "input_not_proven_effect_free",
                step_id=ctx.step.id,
            )
        )
        self._pre_checks = checks
        self._gated_step_id = ctx.step.id
        self._decision = ItemDecision(
            kind=ItemDecisionKind.STOP_BEFORE_SAVING,
            reason="dry_run" if reached else "dry_run_refused",
            needs_person=True,
            counts_toward_problems=False,
            switch_off=None,
        )
        return GateVerdict(proceed=False, decision=self._decision, checks=tuple(checks))

    def finish_item(self, report: RunReport) -> ApprovalModeItemEvidence:
        return ApprovalModeItemEvidence(
            mode=self.mode,
            decision=ItemDecisionKind.STOP_BEFORE_SAVING,
            decision_reason=self._decision.reason
            if self._decision is not None
            else "dry_run_refused",
            needs_person=True,
            counts_toward_problems=False,
            gated_step_id=self._gated_step_id,
            pre_checks=list(self._pre_checks),
        )
