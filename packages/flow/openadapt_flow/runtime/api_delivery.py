"""Per-request delivery authority for the built-in API actuators."""

from __future__ import annotations

from typing import Any, Callable

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.runtime.actuators.api import ApiActuator
from openadapt_flow.runtime.actuators.web_form import WebFormActuator


def execute_api_request(
    replayer: Any,
    actuator: Any,
    binding: Any,
    prepared: Any,
    params: Any,
    result: Any,
    workflow: Any,
    step: Any,
) -> Any:
    """Guard and acknowledge each actual write, after reads and preparation.

    A multi-request form gets a separate one-use permit for each transport
    call. An exception after a request leaves its permit unacknowledged and
    prevents either another request or a verified managed outcome.
    """
    from openadapt_flow.runtime.authorization import runtime_params_for_gui

    inputs = runtime_params_for_gui(params)
    managed = replayer.managed_dispatch_binding is not None
    if managed and type(actuator) not in (WebFormActuator, ApiActuator):
        result.safety_halt = True
        result.failure_category = "governed_refusal"
        raise StructuralResolutionRefused(
            "managed API delivery requires an exact built-in per-request actuator"
        )
    calls = 0

    def deliver(call: Callable[[], Any]) -> Any:
        nonlocal calls
        if calls:
            refusal = replayer._delivery_authorization_refusal(
                workflow, params, step, result, api_delivery=True
            )
            if refusal is not None:
                raise StructuralResolutionRefused(refusal)
        # This may read the live qualification environment. Sample the
        # receiver clock only after it finishes, inside the shared boundary.
        replayer._require_qualification_environment_current()
        calls += 1
        return replayer._deliver_backend_call(result, call)

    if isinstance(actuator, WebFormActuator):
        return actuator.send(binding, prepared, delivery_boundary=deliver)
    if isinstance(actuator, ApiActuator):
        return actuator.actuate(
            binding, inputs, delivery_boundary=deliver, strict_delivery=managed
        )
    # Existing deployment adapters remain usable for local execution. Their
    # whole call is the declared delivery boundary.
    return deliver(
        lambda: (
            actuator.send(binding, prepared)
            if prepared is not None
            else actuator.actuate(binding, inputs)
        )
    )


def managed_delivery_unacknowledged(replayer: Any) -> bool:
    """A record read-back cannot replace a missing managed delivery receipt."""
    if replayer.managed_dispatch_binding is None:
        return False
    initial = getattr(replayer, "_durable_initial_delivery_guard", None)
    continuation = getattr(replayer, "_durable_continuation_guard", None)
    return (
        getattr(initial, "pending_remote_permit", None) is not None
        or getattr(continuation, "_pending_remote_permit", None) is not None
    )


def record_span_write_ahead(
    replayer: Any,
    durable_run: Any,
    workflow: Any,
    folded: Any,
    start_index: int,
    end_index: int,
    params: Any,
    surface: Any,
    observations: Any,
) -> None:
    """Retain folded progress and the applicable local/managed write-ahead."""
    from openadapt_flow.ir import StepResult

    save = workflow.steps[end_index]
    # What a resume reads if the process stops while the span's
    # requests are going out: the folded steps are done, and the
    # save is paused as delivery-uncertain. A resume then starts at
    # the save and only reconciles it; it never sends a write of
    # the sequence again. The save's own outcome replaces this
    # pause, and the run's completion consumes it.
    replayer._web_form_folded_checkpointed = True
    for offset, item in enumerate(folded):
        durable_run.record(
            start_index + offset,
            workflow.steps[start_index + offset],
            item,
            dict(params),
            workflow=workflow,
            active_surface=surface,
            surface_observations=observations,
        )
    if replayer.managed_dispatch_binding is not None:
        # The protected pending Cloud permit is the managed
        # write-ahead. A synthetic operator pause here would
        # revoke the live initial owner before it could ACK.
        return
    provisional = StepResult(
        step_id=save.id,
        intent=save.intent,
        ok=False,
        safety_halt=True,
        failure_category="safety_halt",
        actuation="api",
        delivery_attempted=True,
        surface=surface,
        risk=save.risk,
        risk_explanation=save.risk_explanation,
        risk_review_required=save.risk_review_required,
        error=(
            f"Step '{save.id}' ({save.intent}): the API step began "
            "sending its requests and the run stopped before it "
            "finished; they may have written. Resume never sends "
            "them again; reconcile by reading the records back"
        ),
    )
    durable_run.record(
        end_index,
        save,
        provisional,
        dict(params),
        workflow=workflow,
        active_surface=surface,
        surface_observations=observations,
    )
    replayer._web_form_write_ahead_pause = durable_run.pause_digest()
