"""Shared exact-shape checks for one retained action result.

The production outcome classifier and the durable checkpoint boundary must
agree about which action evidence the runtime can emit.  This module contains
only path/action shape checks.  It does not depend on a qualification project,
read a customer artifact, or decide whether a business effect succeeded.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Optional

from openadapt_flow.ir import ActionKind, Step
from openadapt_flow.runtime.authorization import RuntimeParamScalar, runtime_param_text

AUTOMATED_GUI_ACTUATIONS = frozenset(
    {"uia", "dom", "guarded_coordinate", "guarded_keyboard", "remote_guarded"}
)
HUMAN_ATTENDED_ACTUATIONS = frozenset({"human_attended", "human_attended_skip"})


_DELIVERY_OPERATIONS: dict[tuple[str, ActionKind], set[tuple[str, bool]]] = {
    ("uia", ActionKind.CLICK): {
        ("uia_invoke", True),
        ("uia_focus", True),
        ("uia_toggle", True),
        ("uia_select", True),
        ("atspi_invoke", True),
        ("atspi_focus", True),
        ("atspi_toggle", True),
        ("atspi_select", True),
    },
    ("dom", ActionKind.CLICK): {
        ("dom_click", False),
        ("physical_click", False),
    },
    ("guarded_coordinate", ActionKind.CLICK): {
        ("guarded_coordinate_click", False),
        ("physical_click", False),
    },
    ("dom", ActionKind.DOUBLE_CLICK): {
        ("dom_double_click", False),
        ("physical_double_click", False),
    },
    ("guarded_coordinate", ActionKind.DOUBLE_CLICK): {
        ("guarded_coordinate_double_click", False),
        ("physical_double_click", False),
    },
    ("guarded_coordinate", ActionKind.RIGHT_CLICK): {
        ("guarded_coordinate_right_click", False),
        ("physical_right_click", False),
    },
    ("remote_guarded", ActionKind.CLICK): {
        ("rdp_click", False),
        ("remote_click", False),
    },
    ("remote_guarded", ActionKind.DOUBLE_CLICK): {
        ("rdp_double_click", False),
        ("remote_double_click", False),
    },
    ("remote_guarded", ActionKind.RIGHT_CLICK): {
        ("rdp_right_click", False),
        ("remote_right_click", False),
    },
    ("dom", ActionKind.DRAG): {("guarded_dom_drag", False)},
    ("guarded_coordinate", ActionKind.DRAG): {
        ("guarded_coordinate_drag", False),
        ("physical_drag", False),
    },
    ("remote_guarded", ActionKind.DRAG): {
        ("rdp_drag", False),
        ("remote_drag", False),
    },
    ("guarded_keyboard", ActionKind.TYPE): {
        ("guarded_dom_type", False),
        ("guarded_atspi_type", True),
        ("physical_type_text", False),
    },
    ("remote_guarded", ActionKind.SELECT_OPTION): {
        ("rdp_select_option", False),
        ("remote_select_option", False),
    },
    ("guarded_keyboard", ActionKind.SELECT_OPTION): {
        ("guarded_select_option", False),
    },
    ("guarded_keyboard", ActionKind.KEY): {
        ("guarded_dom_key", False),
        ("guarded_atspi_key", False),
        ("physical_press", False),
    },
    ("guarded_keyboard", ActionKind.HOTKEY): {
        ("guarded_dom_key", False),
        ("guarded_atspi_key", False),
        ("physical_press", False),
    },
}


def _resolution_shape_error(resolution: Any, anchor: Any) -> Optional[str]:
    if resolution is None:
        return "GUI action lacks its required target resolution"
    if anchor is None:
        return "GUI action claims a resolution for an anchorless step"
    x, y = resolution.point
    if (
        not isinstance(x, int)
        or not isinstance(y, int)
        or not 0.0 < resolution.confidence <= 1.0
        or resolution.elapsed_ms < 0.0
    ):
        return "GUI target resolution has values the runtime cannot emit"
    if resolution.rung == "structural":
        handle = resolution.structural_handle
        if (
            anchor.structural is None
            or handle is None
            or resolution.visual_evidence is not None
            or handle.candidate_count != 1
            or handle.point != resolution.point
            or abs(handle.confidence - resolution.confidence) > 1e-9
        ):
            return "structural resolution does not bind one exact target"
        return None
    if resolution.structural_handle is not None:
        return "visual resolution contains a structural target handle"
    visual = resolution.visual_evidence
    if resolution.rung in {"template", "template_global"} and anchor.template is None:
        return "template resolution is not supported by the compiled anchor"
    if resolution.rung == "ocr" and anchor.ocr_text is None:
        return "OCR resolution is not supported by the compiled anchor"
    if resolution.rung == "geometry" and not anchor.landmarks:
        return "geometry resolution is not supported by retained landmarks"
    if resolution.rung not in {
        "template",
        "template_global",
        "ocr",
        "geometry",
        "grounder",
    }:
        return "GUI action uses an unknown resolution rung"
    # Ordinary execution retains the typed runtime result. Qualification fault
    # campaigns additionally retain the exact frame/template evaluator inputs.
    # Do not require that optional campaign inventory on a normal production
    # report; when present, the frozen model has already validated its shape.
    if visual is None:
        return None
    return None


def _resolution_fingerprint(
    step: Step, resolution: Any, *, endpoint: bool = False
) -> Optional[str]:
    if resolution is None:
        return None
    anchor = step.drag_end_anchor if endpoint else step.anchor
    if anchor is None:
        return None
    if resolution.rung == "structural":
        if anchor.structural is None or resolution.structural_handle is None:
            return None
        from openadapt_flow.runtime.resolver import structural_resolution_fingerprint

        return structural_resolution_fingerprint(
            anchor.structural,
            resolution.structural_handle,
        )
    visual = resolution.visual_evidence
    if visual is None:
        return None
    from openadapt_flow.runtime.resolver import visual_resolution_point_fingerprint

    return visual_resolution_point_fingerprint(visual.frame_sha256, resolution.point)


def _delivery_receipt_error(
    step: Step,
    result: Any,
    *,
    params: Mapping[str, RuntimeParamScalar],
) -> Optional[str]:
    receipt = result.delivery_receipt
    if receipt is None:
        return "GUI action lacks its exact delivery receipt"
    allowed = _DELIVERY_OPERATIONS.get((result.actuation, step.action), set())
    if (receipt.operation, receipt.native) not in allowed:
        return "delivery receipt operation conflicts with the compiled action"

    source_fingerprint = _resolution_fingerprint(step, result.resolution)
    if (
        step.action is not ActionKind.DRAG
        and result.actuation in {"uia", "dom"}
        and result.resolution is not None
        and result.resolution.rung == "structural"
    ):
        handle = result.resolution.structural_handle
        if (
            handle is None
            or handle.target_fingerprint is None
            or receipt.target_fingerprint != handle.target_fingerprint
        ):
            return "delivery receipt does not bind the resolved structural target"
    if result.actuation == "remote_guarded" and step.action is not ActionKind.DRAG:
        if receipt.target_fingerprint is None or (
            source_fingerprint is not None
            and receipt.target_fingerprint != source_fingerprint
        ):
            return "remote delivery receipt does not bind the resolved target"

    if step.action is ActionKind.DRAG:
        if (
            source_fingerprint is None
            or receipt.target_fingerprint != source_fingerprint
        ):
            return "delivery receipt does not bind the resolved drag source"
        destination = _resolution_fingerprint(
            step,
            result.drag_end_resolution,
            endpoint=True,
        )
        if receipt.destination_fingerprint is None or (
            destination is not None and receipt.destination_fingerprint != destination
        ):
            return "delivery receipt does not bind the resolved drag destination"
    elif receipt.destination_fingerprint is not None:
        return "non-drag delivery receipt contains a destination fingerprint"

    if step.action is ActionKind.SELECT_OPTION:
        selected_value = params.get(step.param) if step.param is not None else step.text
        selected = (
            runtime_param_text(selected_value) if selected_value is not None else None
        )
        if selected is None or step.selection_commit_key is None:
            return "selection delivery receipt lacks its compiled input contract"
        if (
            receipt.selection_value_sha256
            != hashlib.sha256(selected.encode("utf-8")).hexdigest()
            or receipt.selection_commit_key != step.selection_commit_key
        ):
            return "selection delivery receipt differs from the compiled input"
    elif (
        receipt.selection_value_sha256 is not None
        or receipt.selection_commit_key is not None
    ):
        return "non-selection delivery receipt contains selection metadata"
    return None


def nonvacuous_identity_error(check: Any) -> Optional[str]:
    """Return why a verified identity row contains no retained proof."""

    if check is None or check.status != "verified":
        return "action lacks verified identity evidence"
    if check.mode == "signal_quorum":
        evidence = list(check.signal_evidence)
        signals = [item.signal for item in evidence]
        if (
            not evidence
            or len(signals) != len(set(signals))
            or any(item.verdict != "verified" for item in evidence)
            or check.quorum_required is None
            or check.quorum_verified != len(evidence)
            or len(evidence) < check.quorum_required
            or check.coverage != 1.0
            or check.expected
            or check.observed
            or check.param is not None
            or check.pixel_evidence is not None
        ):
            return "identity quorum evidence is empty or internally inconsistent"
        return None
    if (
        check.signal_evidence
        or check.quorum_required is not None
        or check.quorum_verified is not None
    ):
        return "canonical identity evidence contains an incompatible quorum payload"
    if check.mode == "pixel":
        return (
            None if check.pixel_evidence is not None else "pixel identity lacks proof"
        )
    if check.pixel_evidence is not None:
        return "non-pixel identity contains incompatible pixel proof"
    if check.mode in {"structured", "context", "param"} and check.observed:
        return None
    return "verified identity evidence is only a status label"


def action_evidence_error(
    step: Step,
    result: Any,
    *,
    params: Mapping[str, RuntimeParamScalar] | None = None,
    identity_required: bool = False,
    strict_production: bool = True,
) -> Optional[str]:
    """Validate the action-evidence shape shared by outcome and durability."""

    scoped_params = params or {}
    if result.skipped:
        if (
            not result.ok
            or result.delivery_attempted is not False
            or result.delivery_receipt is not None
            or result.resolution is not None
            or result.drag_end_resolution is not None
            or result.input_verified is not None
            or result.starting_state_settled not in {None, True}
            or result.fresh_actuation_events
            or result.actuation not in {None, "human_attended_skip"}
        ):
            return "skipped action contains delivery evidence"
        return None

    actuation = result.actuation
    uncertainty = getattr(result, "delivery_uncertainty", None)
    if uncertainty is not None:
        if (
            actuation is not None
            or result.delivery_attempted is not True
            or result.delivery_receipt is not None
            or result.starting_state_settled is not True
            or any(not event.retried for event in result.fresh_actuation_events)
        ):
            return "uncertain GUI delivery contains an impossible action shape"
        if step.anchor is not None:
            error = _resolution_shape_error(result.resolution, step.anchor)
            if error is not None:
                return error
        elif result.resolution is not None:
            return "anchorless uncertain delivery contains target resolution"
        if result.drag_end_resolution is not None:
            return "uncertain delivery contains an unbound drag destination"
    elif actuation == "api":
        if (
            result.resolution is not None
            or result.drag_end_resolution is not None
            or result.delivery_receipt is not None
            or result.starting_state_settled is not None
            or result.input_verified is not None
            or result.input_retried
            or result.postconditions_ok is not None
            or result.fresh_actuation_events
        ):
            return "API action contains GUI-only action evidence"
        if result.delivery_attempted is not True:
            return "successful API action lacks its delivery boundary"
    elif actuation in HUMAN_ATTENDED_ACTUATIONS:
        if actuation == "human_attended_skip":
            return "human-attended skip must use the skipped result shape"
        if (
            result.delivery_attempted is not False
            or result.delivery_receipt is not None
        ):
            return "human-attended completion invents engine delivery evidence"
        if (
            result.resolution is not None
            or result.drag_end_resolution is not None
            or result.starting_state_settled is not None
            or result.input_verified is not None
            or result.input_retried
            or result.fresh_actuation_events
        ):
            return "human-attended completion contains engine-only action evidence"
    elif actuation in AUTOMATED_GUI_ACTUATIONS:
        if strict_production and result.starting_state_settled is not True:
            return "GUI action lacks the exact settled-state observation"
        if result.starting_state_settled not in {None, True}:
            return "GUI action contains an invalid settled-state observation"
        if strict_production and result.delivery_attempted is not True:
            return "GUI action lacks its delivery boundary"
        if result.delivery_attempted not in {None, True}:
            return "GUI action contains an invalid delivery boundary"
        if any(not event.retried for event in result.fresh_actuation_events):
            return "successful GUI action retains a terminal fresh-frame mismatch"
        if [event.attempt for event in result.fresh_actuation_events] != list(
            range(1, len(result.fresh_actuation_events) + 1)
        ):
            return "fresh-frame retry evidence is not contiguous"
        if step.anchor is not None:
            if strict_production or result.resolution is not None:
                error = _resolution_shape_error(result.resolution, step.anchor)
                if error is not None:
                    return error
        elif result.resolution is not None:
            return "anchorless GUI action contains target-resolution evidence"
        if step.action is ActionKind.DRAG:
            if strict_production or result.drag_end_resolution is not None:
                error = _resolution_shape_error(
                    result.drag_end_resolution,
                    step.drag_end_anchor,
                )
                if error is not None:
                    return f"drag endpoint: {error}"
        elif result.drag_end_resolution is not None:
            return "non-drag action contains a drag endpoint resolution"
        if step.action in {ActionKind.TYPE, ActionKind.SELECT_OPTION}:
            if strict_production and result.input_verified is not True:
                return "GUI input action lacks exact readback verification"
            if result.input_verified not in {None, True}:
                return "GUI input action contains failed readback verification"
        elif result.input_verified is not None:
            return "non-input GUI action contains input readback evidence"
        if strict_production or result.delivery_receipt is not None:
            receipt_error = _delivery_receipt_error(step, result, params=scoped_params)
            if receipt_error is not None:
                return receipt_error
    elif step.action is ActionKind.WAIT and actuation is None:
        if result.delivery_attempted not in {None, False}:
            return "wait action claims an input-delivery boundary"
    elif strict_production:
        return "production action lacks a closed runtime actuation path"

    if identity_required:
        return nonvacuous_identity_error(result.identity)
    # A step without an armed identity contract may continue when the optional
    # identity ladder cannot decide.  That preserves ordinary non-entity work
    # on substrates that cannot observe an identity band.  A positive mismatch
    # is different: it is affirmative evidence for a different entity and must
    # always reject the action, even when the step did not require identity.
    # Required identity remains above: ``nonvacuous_identity_error`` requires
    # a fully retained, verified identity proof.
    if result.identity is not None and result.identity.status == "mismatch":
        return "successful action retains an identity mismatch verdict"
    return None
