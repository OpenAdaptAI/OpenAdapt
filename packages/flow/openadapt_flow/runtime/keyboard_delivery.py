"""Bounded keyboard-only retries following a typed zero-input lease refusal.

The caller performs every target, identity, context and authorization check
again. Unknown failures and partial delivery never enter this retry path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, TypeVar

from openadapt_flow.backend import (
    KeyboardFrameChangedBeforeInput,
    NativeKeyboardLeaseBackend,
    StructuralResolutionRefused,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    FreshActuationEvent,
    Resolution,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.run_gate import is_consequential
from openadapt_flow.runtime.authorization import RuntimeParamScalar

if TYPE_CHECKING:
    from openadapt_flow.runtime.replayer import Replayer

T = TypeVar("T")


def deliver_with_revalidation(
    deliver: Callable[[], T],
    revalidate: Callable[[KeyboardFrameChangedBeforeInput], bool],
    record: Callable[[KeyboardFrameChangedBeforeInput, bool, int], None],
) -> T:
    for attempt in range(1, 4):
        try:
            return deliver()
        except KeyboardFrameChangedBeforeInput as exc:
            try:
                retry = attempt < 3 and revalidate(exc)
            except Exception:
                record(exc, False, attempt)
                raise
            record(exc, retry, attempt)
            if not retry:
                raise StructuralResolutionRefused(
                    "native keyboard exact-frame lease changed before input; bounded revalidation exhausted or refused"
                ) from exc
    raise AssertionError("bounded keyboard delivery loop escaped")


def native_keyboard_delivery(
    replay: "Replayer",
    *,
    step: Step,
    workflow: Workflow,
    params: Mapping[str, RuntimeParamScalar],
    bundle_dir: Path,
    run_dir: Path,
    result: StepResult,
    resolution: Resolution | None,
    frame: bytes,
    baseline: str | None,
    call: Callable[[str], ActionDeliveryReceipt],
) -> tuple[ActionDeliveryReceipt, Resolution | None, bytes]:
    """Retry only the refused keyboard phase, without a focusing click.

    Reversible transient edits can be re-observed. Effectful, API-bound,
    irreversible and otherwise consequential actions have no retry here.
    Every replacement frame must prove the same focused runtime element,
    record identity and context through the ordinary full revalidation path.
    """

    # A completed focusing gesture is preparation, not this keyboard action.
    # Enter the shared TYPE/KEY/HOTKEY phase without its positive receipt;
    # only the caller's actual keyboard acknowledgement may populate it.
    # Keep delivery_attempted: earlier preparation may already have had effect.
    result.delivery_receipt = None
    rejected_resolution, rejected_region = resolution, replay._active_delivery_region

    def revalidate(exc: KeyboardFrameChangedBeforeInput) -> bool:
        nonlocal resolution, frame, rejected_resolution, rejected_region
        rejected_resolution = replay._active_delivery_resolution
        rejected_region = replay._active_delivery_region
        if (
            step.anchor is None
            or resolution is None
            or step.effects
            or step.api_binding is not None
            or step.risk != "reversible"
            or is_consequential(step, workflow)
            or not isinstance(replay.backend, NativeKeyboardLeaseBackend)
        ):
            return False
        refusal = replay._fresh_actuation_authorization_refusal(workflow, params, step)
        if refusal is not None:
            return False
        fresh, region, png, error = replay._revalidate_consequential_actuation(
            step,
            resolution,
            None,
            frame,
            params,
            workflow,
            bundle_dir,
            result,
            run_dir=run_dir,
            arm_keyboard=True,
        )
        if error is not None or fresh is None:
            replay._cancel_guarded_keyboard()
            return False
        if (
            replay.backend.guarded_keyboard_target_fingerprint()
            != exc.target_fingerprint
        ):
            replay._cancel_guarded_keyboard()
            return False
        if step.action is ActionKind.TYPE and (
            baseline is None or replay._text_value_at(fresh.point) != baseline
        ):
            replay._cancel_guarded_keyboard()
            return False
        if region is not None:
            fresh = replay._retain_visual_resolution_evidence(
                workflow=workflow,
                anchor=step.anchor,
                resolution=fresh,
                matched_region=region,
                frame_png=png,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                allow_target_ocr=False,
            )
        resolution, frame = fresh, png
        result.resolution = fresh
        return True

    def deliver() -> ActionDeliveryReceipt:
        nonlocal rejected_resolution, rejected_region
        rejected_resolution = replay._active_delivery_resolution
        rejected_region = replay._active_delivery_region
        refusal = replay._delivery_authorization_refusal(workflow, params, step, result)
        if refusal is not None:
            raise StructuralResolutionRefused(refusal)
        replay._require_qualification_environment_current()
        # This frame authorizes this keyboard attempt, including an uncertain
        # acknowledgement. A focusing click may have changed the earlier frame.
        evidence_step_id = (
            Path(result.before_png).name.removesuffix("_before.png")
            if result.before_png
            else step.id
        )
        result.before_png = replay._save_step_png(
            run_dir, evidence_step_id, "before", frame
        )
        return replay._deliver_backend_call(
            result, lambda: call(hashlib.sha256(frame).hexdigest())
        )

    def record(
        exc: KeyboardFrameChangedBeforeInput, retried: bool, attempt: int
    ) -> None:
        result.fresh_actuation_events.append(
            replay._fresh_actuation_event(
                exc,
                step=step,
                workflow=workflow,
                resolution=rejected_resolution,
                matched_region=rejected_region,
                retried=retried,
                attempt=attempt,
            )
        )

    receipt = deliver_with_revalidation(deliver, revalidate, record)
    return receipt, resolution, frame


def fresh_actuation_event(
    replay: "Replayer",
    exc,
    *,
    step,
    workflow,
    resolution,
    matched_region,
    retried: bool,
    attempt: int,
) -> FreshActuationEvent:
    target_region = matched_region
    if target_region is None and step.anchor is not None:
        target_region = replay._translate_recorded_region(
            step, resolution, step.anchor.region
        )
    target_intersection = (
        replay._regions_intersect(exc.changed_bbox, target_region)
        if target_region is not None
        else None
    )
    identity_regions = replay._fresh_actuation_identity_regions(
        step, workflow, resolution
    )
    identity_intersection = (
        any(
            replay._regions_intersect(exc.changed_bbox, region)
            for region in identity_regions
        )
        if identity_regions
        else None
    )
    return FreshActuationEvent(
        attempt=attempt,
        operation=exc.operation,
        changed_pixel_count=exc.changed_pixel_count,
        changed_bbox=exc.changed_bbox,
        frame_size=exc.frame_size,
        target_intersection=target_intersection,
        identity_intersection=identity_intersection,
        retried=retried,
        phase="keyboard"
        if isinstance(exc, KeyboardFrameChangedBeforeInput)
        else "step",
        lease_sha256=exc.lease_sha256
        if isinstance(exc, KeyboardFrameChangedBeforeInput)
        else None,
    )
