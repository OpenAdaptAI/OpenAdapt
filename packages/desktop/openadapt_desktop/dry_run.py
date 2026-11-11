"""Local Check-it view of Flow's existing redacted/sealed run evidence.

No values or screen text are copied into a new persistent artifact. This view
is derived in memory from the ordinary report and never upgrades its outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openadapt_desktop.dispatch import EngineDispatcher


def dry_run_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """Refuse incomplete/ambiguous reports, even if the child exited cleanly."""
    from openadapt_desktop.flow_bridge import FlowBridge
    from openadapt_desktop.outcomes import consistent_transaction_outcome

    refused: dict[str, Any] = {"reached_boundary": False, "checks": []}
    try:
        from openadapt_flow.approval_mode.model import ApprovalModeItemEvidence

        block = ApprovalModeItemEvidence.model_validate(report.get("approval"))
    except (ImportError, ValueError, TypeError):
        return refused
    checks = [check.model_dump(mode="json") for check in block.pre_checks]
    refused["checks"] = checks
    outcome = FlowBridge.classify_outcome(1, report)
    results = report.get("results")
    if (
        not isinstance(results, list)
        or not results
        or any(
            not isinstance(result, dict) or result.get("delivery_attempted") is not False
            for result in results
        )
    ):
        return refused
    last = results[-1]
    reached = bool(
        outcome == "HALTED"
        and consistent_transaction_outcome(outcome, report.get("transaction_outcome"))
        == "HALTED_BEFORE_EFFECT"
        and block.decision.value == "stop_before_saving"
        and block.decision_reason == "dry_run"
        and checks
        and all(check["passed"] for check in checks)
        and block.gated_step_id == last.get("step_id")
        and last.get("safety_halt") is True
        and last.get("effect_contract_hashes")
        and any(check["code"] == "right_record" for check in checks)
        and {"step_gates_passed", "no_input_delivered", "stopped_before_delivery"}
        <= {check["detail"] for check in checks if check["code"] == "dry_run"}
        and all(check.get("step_id") == block.gated_step_id for check in checks)
    )
    return {"reached_boundary": reached, "checks": checks}


def check_prefilled_workflow(
    dispatcher: EngineDispatcher, params: dict[str, Any]
) -> dict[str, Any]:
    """Check a prefilled item through the no-input replay path."""
    from openadapt_desktop.work_items import WorkItemError, validate_inputs

    workflow_id = str(params.get("workflow_id") or "")
    if dispatcher._bundle_dir(workflow_id) is None:
        return dispatcher._pre_action_refusal("Unknown workflow")
    try:
        raw = params.get("inputs", {})
        if isinstance(raw, dict):
            raw = {**dispatcher._fixed_values(workflow_id), **raw}
        values = validate_inputs(raw, dispatcher._task_inputs(workflow_id))
    except WorkItemError as exc:
        return dispatcher._pre_action_refusal(str(exc))
    except Exception:
        return dispatcher._pre_action_refusal("Desktop could not read the task inputs")
    return dispatcher._replay_or_run(
        {
            "workflow_id": workflow_id,
            "target": params.get("target"),
            "deployment_config": params.get("deployment_config"),
            "_item_inputs": values,
        },
        run=False,
        stop_before_effect=True,
    )
