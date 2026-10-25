# mypy: disable-error-code="empty-body"
"""Acting by kind: deliver clicks, drags, typing, selections, scrolls and dialog answers.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_ActingHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import TYPE_CHECKING, Any, Callable, Optional, cast  # noqa: F401

from openadapt_flow.backend import (
    GuardedCoordinateActionBackend,
    GuardedDragActionBackend,
    GuardedKeyboardActionBackend,
    GuardedRemotePointerActionBackend,
    GuardedSelectOptionBackend,
    RemoteActuationBackend,
    RichPointerActionBackend,
    SelectOptionBackend,
)
from openadapt_flow.ir import ActionKind, Point, Predicate, PredicateKind, Region
from openadapt_flow.privacy import scrub_text as _scrub_phi
from openadapt_flow.runtime.authorization import runtime_param_text
from openadapt_flow.runtime.core import ACTION_EXECUTORS, ActionContext
from openadapt_flow.runtime.replay._shared import (
    _DIALOG_WAIT_S,
    _DIRECT_TARGET_RUNGS,
    _SCROLL_HANDOFF_ACTIONS,
    SCROLL_BUDGET_FACTOR,
    _frame_viewport,
    _scroll_target_positioned,
    secret_env_var,
)
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.runtime.step_timing import timed_span
from openadapt_flow.vision.ocr import OcrResolutionRefused

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Mapping, Union

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import (
        Anchor,
        IdentityCheck,
        Resolution,
        Step,
        StepResult,
        Workflow,
    )
    from openadapt_flow.qualification_faults import QualificationFaultDriver
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        RuntimeParamScalar,
    )
    from openadapt_flow.runtime.replay._shared import _GraphStepContext


class _ActingHost:
    _asset_bytes: Any

    def _authorize_delivery(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
        result: StepResult,
    ) -> Optional[str]: ...

    _cancel_guarded_coordinate: Any
    _cancel_guarded_keyboard: Any
    _deliver_backend_call: Any
    _delivery_authorization_refusal: Any

    @staticmethod
    def _describe_predicate(pred: Predicate) -> str: ...

    _field_reads: dict[str, Any]
    _governed_asset_mutation: Optional[str]
    _last_click_point: Optional[Point]
    _last_click_region: Optional[Region]

    @staticmethod
    def _next_anchored_step(workflow: Workflow, step_index: int) -> Optional[Step]: ...
    def _pixel_recheck(
        self,
        step: Step,
        resolution: Resolution,
        checked_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        run_dir: Path,
    ) -> Union[Resolution, str]: ...
    def _predicate_holds(
        self,
        pred: Predicate,
        frame_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Optional[Workflow] = None,
    ) -> bool: ...

    _read_clipboard: Optional[Any]

    def _read_gate_refusal(self, step: Step, result: StepResult) -> Optional[str]: ...

    _require_qualification_environment_current: Any
    _requires_atomic_identity_keyboard: Any
    _requires_atomic_identity_pointer: Any

    def _resolve_drag_end(
        self, step: Step, screen_png: bytes, bundle_dir: Path, workflow: Workflow
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]: ...
    def _retain_visual_resolution_evidence(
        self,
        *,
        workflow: Workflow,
        anchor: Anchor,
        resolution: Resolution,
        matched_region: Region,
        frame_png: bytes,
        bundle_dir: Path,
        run_dir: Path,
        allow_target_ocr: bool = True,
    ) -> Resolution: ...

    _revalidate_consequential_actuation: Any
    _run_id: str
    _scroll_missed_target: Optional[tuple[str, str]]
    _selection_contract_error: Any

    @staticmethod
    def _selection_region_compatible(
        qualified: Region, live: Optional[Region]
    ) -> bool: ...
    @staticmethod
    def _selection_target_continuous(
        qualified: Region, focused: Point, refreshed: Point
    ) -> bool: ...

    _step_is_consequential: Any
    _step_needs_consequential_revalidation: Any
    _step_requires_remote_guarded_actuation: Any

    def _text_value_at(self, field_point: Optional[Point]) -> Optional[str]: ...

    _translate_recorded_region: Any
    _typed_remote_receipt_required: Any

    def _verify_identity(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Optional[Path] = None,
    ) -> IdentityCheck: ...
    def _verify_typed_input(
        self,
        step: Step,
        text: str,
        field_point: Optional[Point],
        baseline_png: bytes,
        result: StepResult,
        *,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        resolution: Optional[Resolution],
        field_region: Optional[Region] = None,
        baseline_field_value: Optional[str] = None,
        allow_masked: bool = False,
        allow_retry: bool = True,
    ) -> Optional[str]: ...

    backend: Backend
    governed_authorization: Optional[GovernedRunAuthorization]
    poll_interval_s: float
    qualification_fault_driver: Optional[QualificationFaultDriver]
    settle_readiness_timeout_s: float
    use_structural: bool
    vision: Any


if TYPE_CHECKING:
    _Base = _ActingHost
else:
    _Base = object


class Acting(_Base):
    """Acting by kind: deliver clicks, drags, typing, selections, scrolls and dialog answers."""

    def _act_read(
        self,
        step: Step,
        resolution: Optional[Resolution],
        *,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        before_png: bytes,
        result: StepResult,
    ) -> Optional[str]:
        """READ: read one field into ``result.field_read``. Zero model calls:
        OCR and the clipboard are deterministic channels."""

        from openadapt_flow.records.read import read_field
        from openadapt_flow.records.schema import SealRef

        spec = step.read
        assert spec is not None
        region = self._translate_recorded_region(step, resolution, spec.region)
        clipboard = self._read_clipboard
        if spec.proof.double_read and clipboard is None:
            result.failure_category = "runtime_failure"
            return (
                f"Step '{step.id}' ({step.intent}) asks for a double read but "
                "this run has no clipboard channel"
            )
        if clipboard is not None:
            # Focusing and copying a field is input delivery, so it passes the
            # same authorization gate as any other input.
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            result.delivery_attempted = True
        seal = SealRef(run_id=self._run_id) if spec.proof.seal else None
        try:
            field_read = read_field(
                spec.field,
                self.backend,
                region,
                proof=spec.proof,
                clipboard=clipboard,
                copy_keys=tuple(spec.copy_keys),
                ocr=self.vision.ocr,
                step_id=step.id,
                seal=seal,
                frame_png=before_png,
            )
        except Exception as exc:  # noqa: BLE001 - a read failure is a halt
            result.failure_category = "runtime_failure"
            return f"Step '{step.id}' ({step.intent}) could not read: {exc}"
        result.field_read = field_read
        result.actuation = "read"
        self._field_reads[spec.field.name] = field_read
        if field_read.needs_review and spec.on_review == "halt":
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return (
                f"Step '{step.id}' ({step.intent}) read a value that needs "
                f"review ({field_read.review_reason}). Halting for a person"
            )
        return None

    @timed_span("actuate")
    def _act(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        **kwargs: Any,
    ) -> Optional[str]:
        """Perform the step's action.

        A click recorded as closing its own popup (``Step.closes_surface``)
        arms the page backend's one-shot expectation first
        (:class:`~openadapt_flow.backend.PageCloseExpectationBackend`), so a
        "page closed" report from that click counts as delivery once the page
        is confirmed closed. Every other step leaves such a report uncertain.
        """

        expect = (
            getattr(self.backend, "expect_page_close", None)
            if step.closes_surface
            else None
        )
        if not callable(expect):
            return self._act_answering_dialog(step, resolution, params, **kwargs)
        backend = self.backend
        expect()
        try:
            return self._act_answering_dialog(step, resolution, params, **kwargs)
        finally:
            cancel = getattr(backend, "cancel_page_close", None)
            if callable(cancel):
                cancel()

    def _act_answering_dialog(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        **kwargs: Any,
    ) -> Optional[str]:
        """Perform the step's action, answering the dialog it raises, if any.

        A step with a recorded ``dialog`` arms a one-shot answer on the
        browser backend before its input edge. The dialog's message is its
        identity: a different message is dismissed, never accepted, and the
        step halts; so does a dialog that never appears. A backend that cannot
        see browser dialogs refuses the step before any input
        (docs/MULTI_SURFACE.md, "Dialogs"). Errors name a code
        (``dialog_missing``, ``dialog_mismatch``), never the dialog's text:
        the error lands in report.json and REPORT.md, and a live dialog can
        show application data.
        """

        dialog = step.dialog
        if dialog is None:
            return self._act_without_dialog(step, resolution, params, **kwargs)
        arm = getattr(self.backend, "arm_dialog", None)
        take = getattr(self.backend, "take_dialogs", None)
        if not callable(arm) or not callable(take):
            return (
                f"Step '{step.id}' ({step.intent}) answers a browser dialog, but "
                "the active backend cannot see browser dialogs; run aborted"
            )
        self._arm_dialog(arm, dialog)
        try:
            error = self._act_without_dialog(step, resolution, params, **kwargs)
        finally:
            seen = take(0.0)
        if error is not None:
            return error
        if not seen:
            seen = self._wait_for_dialog(take, dialog)
        if not seen:
            return (
                f"Step '{step.id}' ({step.intent}): the recorded browser dialog "
                "did not appear (dialog_missing); run aborted"
            )
        mismatched = [entry for entry in seen if not entry.get("matched")]
        if mismatched:
            return (
                f"Step '{step.id}' ({step.intent}): a browser dialog did not "
                "show the recorded message (dialog_mismatch); it was "
                "dismissed, not answered; run aborted"
            )
        return None

    @staticmethod
    def _arm_dialog(arm: Callable[..., Any], dialog: Any) -> None:
        if dialog.message_pattern is not None:
            arm(dialog.response, None, message_pattern=dialog.message_pattern)
        else:
            arm(dialog.response, dialog.message)

    @timed_span("postcondition")
    def _wait_for_dialog(self, take: Callable[..., Any], dialog: Any) -> list[Any]:
        """Wait briefly for an armed dialog that opens after the input edge."""

        arm = getattr(self.backend, "arm_dialog", None)
        if callable(arm):
            self._arm_dialog(arm, dialog)
        seen = take(_DIALOG_WAIT_S)
        return list(seen) if isinstance(seen, list) else []

    def _act_without_dialog(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        before_png: bytes,
        result: StepResult,
        run_dir: Path,
        graph_ctx: Optional["_GraphStepContext"] = None,
    ) -> Optional[str]:
        """Perform the step's action through the backend.

        Returns:
            An error string (no action performed / partial) or None.
        """
        if step.read_gate is not None:
            refusal = self._read_gate_refusal(step, result)
            if refusal is not None:
                return refusal
        if step.action is ActionKind.READ:
            return self._act_read(
                step,
                resolution,
                workflow=workflow,
                params=params,
                before_png=before_png,
                result=result,
            )
        if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            assert resolution is not None  # guaranteed by _resolve_step
            x, y = resolution.point
            native_act = getattr(self.backend, "act_structural", None)
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            if (
                resolution.rung == "structural"
                and resolution.structural_handle is not None
                and step.anchor is not None
                and step.anchor.structural is not None
                and callable(native_act)
            ):
                if (
                    requires_atomic_identity
                    and resolution.structural_handle.target_fingerprint is None
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "identity-gated click, but its structural target has no "
                        "fresh actuation fingerprint — refusing raw coordinate "
                        "delivery; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                structural_locator = step.anchor.structural
                structural_handle = resolution.structural_handle
                structural_act = cast(Callable[..., Any], native_act)
                delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: structural_act(
                        structural_locator,
                        structural_handle,
                        double=step.action is ActionKind.DOUBLE_CLICK,
                    ),
                )
                result.delivery_receipt = delivery_receipt
                result.actuation = "uia" if delivery_receipt.native else "dom"
            else:
                remote_consequential = self._step_requires_remote_guarded_actuation(
                    step, workflow
                )
                typed_remote = isinstance(
                    self.backend,
                    GuardedRemotePointerActionBackend,
                )
                if (
                    remote_consequential
                    and not typed_remote
                    and self._typed_remote_receipt_required()
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote click, but this backend cannot bind its exact "
                        "fresh frame and target to delivery; run aborted"
                    )
                if remote_consequential:
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    if typed_remote:
                        remote_pointer = cast(
                            GuardedRemotePointerActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: remote_pointer.click_guarded(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                        result.actuation = "remote_guarded"
                    else:
                        # See ``_typed_remote_receipt_required``: the exact
                        # fresh frame is already bound by the backend's own
                        # one-shot lease, armed immediately above by
                        # ``_revalidate_consequential_actuation``.
                        self._deliver_backend_call(
                            result,
                            lambda: self.backend.click(
                                x,
                                y,
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                elif requires_atomic_identity:
                    if isinstance(self.backend, GuardedCoordinateActionBackend):
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        coordinate_backend = cast(
                            GuardedCoordinateActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: coordinate_backend.act_guarded_coordinate(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                        result.actuation = "guarded_coordinate"
                    else:
                        result.safety_halt = True
                        result.failure_category = "safety_halt"
                        return (
                            f"Step '{step.id}' ({step.intent}) is a consequential "
                            "identity-gated click, but this backend cannot bind "
                            "identity verification to the same actuation operation "
                            "— refusing raw coordinate delivery; run aborted"
                        )
                else:
                    rechecked = self._pixel_recheck(
                        step,
                        resolution,
                        before_png,
                        params,
                        workflow,
                        bundle_dir,
                        result,
                        run_dir,
                    )
                    if isinstance(rechecked, str):
                        return rechecked
                    resolution = rechecked
                    x, y = resolution.point
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    self._deliver_backend_call(
                        result,
                        lambda: self.backend.click(
                            x,
                            y,
                            double=step.action is ActionKind.DOUBLE_CLICK,
                        ),
                    )
            self._last_click_point = (x, y)
            # Only a STRUCTURAL handle reports the element's own bounds. A
            # visual rung's ``matched_region`` is where the anchor TEMPLATE
            # matched, which is not the field: narrowing a following TYPE's
            # verification window to it hides the typed value and false-halts
            # a healthy run.
            self._last_click_region = (
                resolution.structural_handle.region
                if resolution.rung == "structural"
                and resolution.structural_handle is not None
                else None
            )
            return None

        if step.action is ActionKind.RIGHT_CLICK:
            assert resolution is not None  # guaranteed by _resolve_step
            x, y = resolution.point
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            remote_consequential = self._step_requires_remote_guarded_actuation(
                step, workflow
            )
            if remote_consequential and not isinstance(
                self.backend,
                GuardedRemotePointerActionBackend,
            ):
                if self._typed_remote_receipt_required():
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote right click, but this backend cannot bind its "
                        "exact fresh frame and target to delivery; run aborted"
                    )
                # See ``_typed_remote_receipt_required``: the exact fresh frame
                # is already bound by the backend's own one-shot lease, and its
                # first input edge consumes it.
                if not isinstance(self.backend, RichPointerActionBackend):
                    return (
                        f"Step '{step.id}' ({step.intent}) requires a right "
                        "click, but this backend has no bounded right-click "
                        "operation"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).right_click(
                        x, y
                    ),
                )
                return None
            if remote_consequential:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                remote_pointer = cast(GuardedRemotePointerActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: remote_pointer.right_click_guarded(
                        x,
                        y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "remote_guarded"
            elif requires_atomic_identity:
                if isinstance(self.backend, GuardedCoordinateActionBackend):
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    coordinate_backend = cast(
                        GuardedCoordinateActionBackend, self.backend
                    )
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: coordinate_backend.act_guarded_coordinate(
                            x,
                            y,
                            expected_frame_sha256=hashlib.sha256(
                                before_png
                            ).hexdigest(),
                            button="right",
                        ),
                    )
                    result.actuation = "guarded_coordinate"
                else:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "identity-gated right click, but this backend cannot bind "
                        "identity verification to delivery; run aborted"
                    )
            elif isinstance(self.backend, RichPointerActionBackend):
                rechecked = self._pixel_recheck(
                    step,
                    resolution,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir,
                )
                if isinstance(rechecked, str):
                    return rechecked
                x, y = rechecked.point
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).right_click(
                        x, y
                    ),
                )
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) requires a right click, "
                    "but this backend does not implement it"
                )
            return None

        if step.action is ActionKind.DRAG:
            assert resolution is not None  # guaranteed by _resolve_step
            drag_end, drag_end_region, drag_error = self._resolve_drag_end(
                step,
                before_png,
                bundle_dir,
                workflow,
            )
            if (
                drag_end is not None
                and drag_end_region is not None
                and step.drag_end_anchor is not None
            ):
                drag_end = self._retain_visual_resolution_evidence(
                    workflow=workflow,
                    anchor=step.drag_end_anchor,
                    resolution=drag_end,
                    matched_region=drag_end_region,
                    frame_png=before_png,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                )
            result.drag_end_resolution = drag_end
            if drag_error is not None:
                self._cancel_guarded_coordinate()
                return drag_error
            assert drag_end is not None
            x, y = resolution.point
            end_x, end_y = drag_end.point
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            structural_drag = getattr(self.backend, "drag_structural_guarded", None)
            if (
                resolution.rung == "structural"
                and drag_end.rung == "structural"
                and resolution.structural_handle is not None
                and drag_end.structural_handle is not None
                and step.anchor is not None
                and step.anchor.structural is not None
                and step.drag_end_anchor is not None
                and step.drag_end_anchor.structural is not None
                and callable(structural_drag)
            ):
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                source_locator = step.anchor.structural
                source_handle = resolution.structural_handle
                destination_locator = step.drag_end_anchor.structural
                destination_handle = drag_end.structural_handle
                structural_drag_act = cast(Callable[..., Any], structural_drag)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: structural_drag_act(
                        source_locator,
                        source_handle,
                        destination_locator,
                        destination_handle,
                    ),
                )
                result.actuation = "dom"
            elif self._step_requires_remote_guarded_actuation(step, workflow) and (
                isinstance(self.backend, GuardedRemotePointerActionBackend)
                or self._typed_remote_receipt_required()
            ):
                if not isinstance(
                    self.backend,
                    GuardedRemotePointerActionBackend,
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote drag, but this backend cannot bind both exact "
                        "fresh-frame endpoints to delivery; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                remote_pointer = cast(GuardedRemotePointerActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: remote_pointer.drag_guarded(
                        x,
                        y,
                        end_x,
                        end_y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "remote_guarded"
            elif requires_atomic_identity and not isinstance(
                self.backend, RemoteActuationBackend
            ):
                if not isinstance(self.backend, GuardedDragActionBackend):
                    self._cancel_guarded_coordinate()
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "drag, but this backend cannot bind both freshly "
                        "resolved endpoints to delivery; run aborted"
                    )
                # Pixel-only source and destination evidence were resolved
                # against ``before_png``. Drop any unused structural tokens,
                # then bind the source coordinate and require that exact frame
                # immediately before mouse-down. Fully structural browser
                # drags take the stronger two-element lease above.
                cancel_structural = getattr(
                    self.backend,
                    "cancel_pending_structural_guards",
                    None,
                )
                if callable(cancel_structural):
                    cancel_structural()
                if not isinstance(self.backend, GuardedCoordinateActionBackend):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "drag, but this backend cannot bind its freshly "
                        "resolved source to delivery; run aborted"
                    )
                try:
                    self.backend.arm_guarded_coordinate(x, y)
                except Exception as exc:  # noqa: BLE001 - backend boundary
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    detail = _scrub_phi(str(exc)) or type(exc).__name__
                    return (
                        f"Step '{step.id}' ({step.intent}) could not bind its "
                        f"freshly resolved drag source: {detail}; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                drag_backend = cast(GuardedDragActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: drag_backend.drag_guarded(
                        x,
                        y,
                        end_x,
                        end_y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "guarded_coordinate"
            elif isinstance(self.backend, RichPointerActionBackend):
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).drag(
                        x, y, end_x, end_y
                    ),
                )
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) requires a drag, but "
                    "this backend does not implement it"
                )
            return None

        if step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
            if step.secret:
                # Secret value is never in the bundle/params: inject it from
                # the environment, failing fast with an actionable message.
                env_var = getattr(self, "secret_env_overrides", {}).get(
                    step.param or ""
                ) or secret_env_var(step.param or "")
                text = os.environ.get(env_var, "")
                if not step.param:
                    return (
                        f"Step '{step.id}' ({step.intent}) is marked secret "
                        "but names no parameter"
                    )
                if not text:
                    return (
                        f"Step '{step.id}' ({step.intent}) requires secret "
                        f"parameter '{step.param}', but the environment "
                        f"variable {env_var} is not set — export it with the "
                        "secret value and re-run"
                    )
            elif step.param is not None:
                if step.param not in params:
                    return (
                        f"Step '{step.id}' ({step.intent}) requires parameter "
                        f"'{step.param}' but it was not provided"
                    )
                text = runtime_param_text(params[step.param])
            elif step.text is not None:
                text = step.text
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) is a TYPE step with "
                    "neither text nor param"
                )
            identity_atomic_keyboard = self._requires_atomic_identity_keyboard(
                step, workflow
            )
            requires_atomic_keyboard = (
                identity_atomic_keyboard or self.qualification_fault_driver is not None
            )
            if (
                requires_atomic_keyboard
                and not isinstance(self.backend, RemoteActuationBackend)
                and not isinstance(self.backend, GuardedKeyboardActionBackend)
            ):
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}) is a qualification or "
                    "identity-gated keyboard action, but this backend cannot "
                    "bind the verified focus and execution context to the same "
                    "input operation — refusing unguarded keyboard delivery; "
                    "run aborted"
                )
            if step.selection_commit_key is not None:
                selection_contract_error = self._selection_contract_error(
                    step, workflow
                )
                if selection_contract_error is not None:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return selection_contract_error
            # The field point: this step's own focusing click (anchored
            # TYPE), or the immediately preceding step's click point (the
            # recorder's click-to-focus-then-type pattern). When focus was
            # moved some other way (Tab between fields), there is no known
            # field point — verification diffs the whole frame and the
            # retry cannot re-click.
            field_point: Optional[Point] = None
            field_region: Optional[Region] = None
            if resolution is not None:
                # Anchored TYPE: click to focus the field first.
                x, y = resolution.point
                native_act = getattr(self.backend, "act_structural", None)
                requires_atomic_identity = self._requires_atomic_identity_pointer(
                    step, workflow
                )
                if (
                    resolution.rung == "structural"
                    and resolution.structural_handle is not None
                    and step.anchor is not None
                    and step.anchor.structural is not None
                    and callable(native_act)
                ):
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    structural_locator = step.anchor.structural
                    structural_handle = resolution.structural_handle
                    structural_act = cast(Callable[..., Any], native_act)
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: structural_act(
                            structural_locator,
                            structural_handle,
                        ),
                    )
                    result.actuation = "uia"
                else:
                    if (
                        requires_atomic_identity
                        and not isinstance(self.backend, RemoteActuationBackend)
                        and isinstance(self.backend, GuardedCoordinateActionBackend)
                    ):
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        coordinate_backend = cast(
                            GuardedCoordinateActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: coordinate_backend.act_guarded_coordinate(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                            ),
                        )
                        result.actuation = "guarded_coordinate"
                    elif requires_atomic_identity and not isinstance(
                        self.backend, RemoteActuationBackend
                    ):
                        result.safety_halt = True
                        result.failure_category = "safety_halt"
                        return (
                            f"Step '{step.id}' ({step.intent}) is a consequential "
                            "identity-gated focusing click, but this backend "
                            "cannot bind target and context verification to the "
                            "same coordinate operation — refusing raw coordinate "
                            "delivery; run aborted"
                        )
                    else:
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result, lambda: self.backend.click(x, y)
                        )
                field_point = (x, y)
                # See ``_last_click_region``: a visual rung's matched region is
                # the anchor template's live position, not the field's bounds.
                # SELECT_OPTION does not need it here — it always revalidates
                # below and consumes that pass's fresh matched region.
                field_region = (
                    resolution.structural_handle.region
                    if resolution.rung == "structural"
                    and resolution.structural_handle is not None
                    else None
                )
                # Fresh baseline AFTER the focusing click so its own focus ring
                # never counts as "input landed". A consequential remote TYPE
                # is a second input gesture after that click, so reacquire and
                # re-run target/identity before typing; the focusing click
                # consumed the first one-shot remote lease.
                if self._step_needs_consequential_revalidation(step, workflow):
                    focused_field_point = field_point
                    (
                        refreshed,
                        refreshed_region,
                        before_png,
                        remote_error,
                    ) = self._revalidate_consequential_actuation(
                        step,
                        resolution,
                        field_region,
                        before_png,
                        params,
                        workflow,
                        bundle_dir,
                        result,
                        run_dir=run_dir,
                        arm_keyboard=True,
                    )
                    if remote_error is not None:
                        return remote_error
                    if refreshed is not None:
                        if (
                            step.action is ActionKind.SELECT_OPTION
                            and focused_field_point is not None
                            and step.anchor is not None
                            and not self._selection_target_continuous(
                                step.anchor.region,
                                focused_field_point,
                                refreshed.point,
                            )
                        ):
                            self._cancel_guarded_keyboard()
                            result.safety_halt = True
                            result.failure_category = "safety_halt"
                            return (
                                f"Step '{step.id}' ({step.intent}) re-resolved "
                                "to a different field after focus; refusing "
                                "option selection"
                            )
                        assert step.anchor is not None
                        if refreshed_region is not None:
                            refreshed = self._retain_visual_resolution_evidence(
                                workflow=workflow,
                                anchor=step.anchor,
                                resolution=refreshed,
                                matched_region=refreshed_region,
                                frame_png=before_png,
                                bundle_dir=bundle_dir,
                                run_dir=run_dir,
                                allow_target_ocr=False,
                            )
                        resolution = refreshed
                        result.resolution = refreshed
                        field_point = refreshed.point
                    # A visual match region is the retained template crop. It
                    # is not the editable control's bounds. Treating that crop
                    # as the typed-value readback region can exclude text that
                    # renders at the left edge of a wide field. Keep exact
                    # structural bounds when the backend supplies them;
                    # otherwise use the point-centred visual readback window.
                    if step.action is ActionKind.SELECT_OPTION:
                        # Composite selection binds its type-ahead and commit
                        # to the exact live resolved selection region.
                        field_region = refreshed_region
                    else:
                        field_region = (
                            refreshed.structural_handle.region
                            if (
                                refreshed is not None
                                and refreshed.rung == "structural"
                                and refreshed.structural_handle is not None
                            )
                            else None
                        )
                else:
                    before_png = self.backend.screenshot()
            elif self._prev_was_click(workflow, step_index, graph_ctx):
                field_point = self._last_click_point
                field_region = self._last_click_region
            baseline_field_value = self._text_value_at(field_point)
            if baseline_field_value is None:
                baseline_field_value = self._text_value_at(None)
            if step.selection_commit_key is not None:
                selection_commit_key = step.selection_commit_key
                assert step.selection_region is not None
                assert isinstance(self.backend, SelectOptionBackend)
                assert step.anchor is not None
                if not self._selection_region_compatible(
                    step.anchor.region, field_region
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) re-resolved to "
                        "incompatible live field geometry; refusing option "
                        "selection"
                    )
                assert field_region is not None
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                if isinstance(self.backend, GuardedSelectOptionBackend):
                    assert field_point is not None
                    guarded_select = cast(GuardedSelectOptionBackend, self.backend)
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: guarded_select.select_option_guarded(
                            text,
                            selection_commit_key,
                            target_point=field_point,
                            expected_frame_sha256=hashlib.sha256(
                                before_png
                            ).hexdigest(),
                        ),
                    )
                    result.actuation = (
                        "remote_guarded"
                        if isinstance(self.backend, RemoteActuationBackend)
                        else "guarded_keyboard"
                    )
                else:
                    select_backend = cast(SelectOptionBackend, self.backend)
                    self._deliver_backend_call(
                        result,
                        lambda: select_backend.select_option(
                            text, selection_commit_key
                        ),
                    )
                return None
            if (
                self.qualification_fault_driver is not None
                and not identity_atomic_keyboard
                and isinstance(self.backend, GuardedKeyboardActionBackend)
            ):
                if field_point is None:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) has no focused field "
                        "point for exact qualification delivery; run aborted"
                    )
                self._require_qualification_environment_current()
                cast(GuardedKeyboardActionBackend, self.backend).arm_guarded_keyboard(
                    *field_point
                )
            guarded_type = (
                requires_atomic_keyboard
                and not isinstance(self.backend, RemoteActuationBackend)
                and isinstance(self.backend, GuardedKeyboardActionBackend)
            )
            if guarded_type:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: cast(
                        GuardedKeyboardActionBackend, self.backend
                    ).type_text_guarded(
                        text,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "guarded_keyboard"
            elif requires_atomic_keyboard and not isinstance(
                self.backend, RemoteActuationBackend
            ):
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}) lost its guarded keyboard "
                    "capability before delivery — refusing unguarded text input; "
                    "run aborted"
                )
            else:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(result, lambda: self.backend.type_text(text))
            if not text:
                return None  # nothing typed, nothing to verify
            return self._verify_typed_input(
                step,
                text,
                field_point,
                before_png,
                result,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                resolution=resolution,
                field_region=field_region,
                baseline_field_value=baseline_field_value,
                allow_masked=step.secret,
                allow_retry=not guarded_type,
            )

        executor = ACTION_EXECUTORS.get(step.action)
        if executor is not None:
            # KEY, HOTKEY and WAIT run in the governance-free core; the gates
            # it must pass before input come from here.
            return executor(
                ActionContext(
                    step=step,
                    result=result,
                    backend=self.backend,
                    before_png=before_png,
                    authorize=lambda: self._authorize_delivery(
                        workflow, params, step, result
                    ),
                    deliver=self._deliver_backend_call,
                    requires_atomic_keyboard=lambda: (
                        self._requires_atomic_identity_keyboard(step, workflow)
                    ),
                )
            )

        if step.action is ActionKind.SCROLL:
            return self._act_scroll(
                step,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                before_png=before_png,
                params=params,
                result=result,
                graph_ctx=graph_ctx,
            )

        return f"Step '{step.id}' has unsupported action {step.action!r}"

    @staticmethod
    def _prev_was_click(
        workflow: Workflow,
        step_index: int,
        graph_ctx: Optional["_GraphStepContext"],
    ) -> bool:
        """Whether the step executed just before this TYPE step was a click.

        Linear mode reads ``workflow.steps[step_index - 1]`` exactly as before;
        program mode reads the interpreter-supplied ``graph_ctx.prev_action``
        (the previously EXECUTED action state -- the graph has no positional
        predecessor). The click-to-focus-then-type heuristic is thereby
        identical on both paths.
        """
        if graph_ctx is not None:
            return graph_ctx.prev_action in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
            )
        return step_index > 0 and workflow.steps[step_index - 1].action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
        )

    @timed_span("resolve")
    def _implicit_scroll_target_ready(
        self,
        step: Step,
        frame_png: bytes,
        *,
        workflow: Workflow,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        scroll_delta: Optional[tuple[int, int]] = None,
        misses: Optional[list[str]] = None,
    ) -> bool:
        """Resolve an implicit scroll target and honor its armed identity.

        With ``scroll_delta``, the target must also be no further along the
        scroll direction than where the demonstration had it (within
        :data:`SCROLL_POSITION_SLACK_FRACTION` of the frame). A target that
        is visible at the bottom edge but was clicked mid-screen in the
        recording still needs the scroll: the step after the click was
        recorded with that scroll position (for example, a section that
        expands below the clicked header).

        A generic input crop can visually resolve in several places on a long
        form. Declaring the target visible from that visual match alone makes
        the preceding scroll a false no-op. Structural resolution stays
        available, model grounding stays disabled, and an identity-armed target
        is ready only when the same pre-action identity gate verifies it.

        When not ready, ``misses`` (if given) gets why: ``unresolved`` (no
        direct deterministic rung found the target), ``offscreen`` (the page
        reports the recorded element outside the viewport), ``position`` or
        ``identity``.
        """

        def miss(reason: str) -> bool:
            if misses is not None:
                misses.append(reason)
            return False

        if step.anchor is None:
            return miss("unresolved")
        structural = (
            self.backend
            if self.use_structural and hasattr(self.backend, "locate_structural")
            else None
        )
        offscreen = getattr(structural, "structural_offscreen", None)
        if (
            callable(offscreen)
            and step.anchor.structural is not None
            and offscreen(step.anchor.structural) is True
        ):
            # The page itself shows the unique recorded target exists below
            # (or above) the viewport: not ready, keep scrolling. Stricter
            # than letting a visual rung find a look-alike on screen, and it
            # skips a full-frame OCR search per probe. The control exists, so
            # this miss never hands the halt to the click (#1331).
            return miss("offscreen")
        template_png = self._asset_bytes(
            bundle_dir, step.anchor.template, workflow=workflow
        )
        try:
            resolved = resolve(
                step.anchor,
                frame_png,
                self.vision,
                None,  # scroll readiness must remain deterministic and model-free
                step.intent,
                template_png=template_png,
                viewport=_frame_viewport(frame_png),
                structural=structural,
            )
        except OcrResolutionRefused:
            # Readiness is a bounded, non-actuating probe. An ambiguous OCR
            # candidate means that the target is not ready yet, so the scroll
            # loop must continue. The later target action runs the full
            # resolver again and still halts on the same ambiguity.
            return miss("unresolved")
        if resolved is None:
            return miss("unresolved")
        resolution, _matched_region = resolved
        # Geometry estimates a point from surrounding landmarks; it does not
        # prove that the target itself is visible. In particular, a fixed
        # header can project the recorded point while the form row is still
        # off-screen, turning the preceding scroll into a false no-op. Scroll
        # readiness therefore requires direct deterministic target evidence.
        # Model grounding is disabled above and future weaker rungs must opt in
        # deliberately rather than silently acquiring readiness authority.
        if resolution.rung not in _DIRECT_TARGET_RUNGS:
            return miss("unresolved")
        if scroll_delta is not None and not _scroll_target_positioned(
            step.anchor.click_point, resolution.point, scroll_delta, frame_png
        ):
            return miss("position")
        if not step.identity_armed:
            return True
        check = self._verify_identity(
            step, resolution, frame_png, params, workflow, bundle_dir
        )
        return check.status == "verified" or miss("identity")

    def _act_scroll(
        self,
        step: Step,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        run_dir: Path,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        graph_ctx: Optional["_GraphStepContext"] = None,
    ) -> Optional[str]:
        """Execute a SCROLL step as a closed loop on a wait_until predicate.

        A recorded scroll's purpose is to bring the next target into view, so
        the step scrolls by its recorded delta until a READINESS predicate holds
        on a settled frame — not a fixed number of times. That predicate is the
        step's own ``wait_until`` when set; otherwise it defaults to
        ``ANCHOR_RESOLVES`` on the NEXT anchored step's anchor — today's closed
        loop, now expressed as the first concrete ``wait_until`` predicate
        rather than a special case (RFC §6). The step probes BEFORE scrolling
        (a preceding SCROLL step may already have brought the target into view,
        making this one a no-op) and stops as soon as the predicate holds.

        The loop is bounded: this step may scroll at most
        ``SCROLL_BUDGET_FACTOR`` times its own recorded distance. On budget
        exhaustion the step fails loudly — unless the immediately following
        step is another SCROLL step, which inherits the loop (so a recorded
        run of N scrolls shares a combined ~2.5x budget).

        Falls back to the fixed recorded delta (open-loop, one gesture) when no
        readiness predicate exists (no ``wait_until`` and no later anchor) or
        the recorded delta is zero. Predicate probes never call the grounder:
        closed-loop scrolling must stay model-free.

        Returns:
            An error string on budget exhaustion (see above) or None.
        """
        dx = step.scroll_dx or 0
        dy = step.scroll_dy or 0
        # The next anchored step (default scroll stop condition): linear mode
        # scans forward in ``workflow.steps``; program mode uses the
        # interpreter-supplied successor (the target of this state's
        # unconditional transition chain).
        next_step = (
            graph_ctx.next_anchored
            if graph_ctx is not None
            else self._next_anchored_step(workflow, step_index)
        )
        # The scroll's readiness is a wait_until predicate: an explicit
        # step.wait_until wins; otherwise the next anchor's ANCHOR_RESOLVES.
        stop_pred = step.wait_until
        implicit_target_readiness = stop_pred is None and next_step is not None
        if stop_pred is None and next_step is not None:
            stop_pred = Predicate(
                kind=PredicateKind.ANCHOR_RESOLVES,
                anchor=next_step.anchor,
                intent=next_step.intent,
            )
        if stop_pred is None or (dx == 0 and dy == 0):
            remote_preflight_error = self._prepare_remote_scroll_input(step, result)
            if remote_preflight_error is not None:
                return remote_preflight_error
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.scroll(dx, dy),
            )
            return None

        # Until a wheel edge stops moving the page, readiness also requires
        # the target at (or past) its demonstrated position.
        position_required = [True]
        # Why each readiness probe of the next target failed; they decide who
        # owns a budget-exhaustion halt (see below).
        misses: list[str] = []

        def readiness_holds(frame_png: bytes) -> bool:
            if implicit_target_readiness:
                assert next_step is not None
                return self._implicit_scroll_target_ready(
                    next_step,
                    frame_png,
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    params=params,
                    scroll_delta=(dx, dy) if position_required[0] else None,
                    misses=misses,
                )
            return self._predicate_holds(
                stop_pred,
                frame_png,
                bundle_dir,
                params,
                workflow=workflow,
            )

        holds = readiness_holds(before_png)
        if self._governed_asset_mutation is not None:
            return self._governed_asset_mutation
        if holds:
            # Target already in view; nothing to scroll and no input sent.
            # Record why, only for the next-target readiness check, so the
            # outcome classifier can tell a correctly skipped scroll from an
            # undelivered action (execution_profiles.classify_execution_outcome).
            if implicit_target_readiness and result.delivery_attempted is not True:
                result.scroll_target_ready_before = True
            return None

        increment = math.hypot(dx, dy)
        budget = SCROLL_BUDGET_FACTOR * increment
        scrolled = 0.0
        while scrolled + increment <= budget:
            # Readiness callbacks run after the outer step preflight.  Bind
            # every wheel edge to a new exact remote frame and repeat the
            # context/identity checks after those callbacks.  This also avoids
            # reusing the one-shot lease consumed by the prior wheel edge.
            if self._step_needs_consequential_revalidation(step, workflow):
                (
                    _scroll_resolution,
                    _scroll_region,
                    fresh_scroll_frame,
                    scroll_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    None,
                    None,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                )
                if scroll_error is not None:
                    return scroll_error
                if readiness_holds(fresh_scroll_frame):
                    return None
            remote_preflight_error = self._prepare_remote_scroll_input(step, result)
            if remote_preflight_error is not None:
                return remote_preflight_error
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.scroll(dx, dy),
            )
            scrolled += increment
            frame = self._wait_for_scroll_transition_and_settle(before_png)
            if not self.vision.pixels_changed(before_png, frame):
                # The page did not move (its end): visibility is enough.
                position_required[0] = False
            before_png = frame
            holds = readiness_holds(frame)
            if self._governed_asset_mutation is not None:
                return self._governed_asset_mutation
            if holds:
                return None

        following = (
            graph_ctx.following
            if graph_ctx is not None
            else (
                workflow.steps[step_index + 1]
                if step_index + 1 < len(workflow.steps)
                else None
            )
        )
        if following is not None and following.action is ActionKind.SCROLL:
            # The next SCROLL step continues the loop with its own budget.
            return None
        if (
            graph_ctx is None
            and implicit_target_readiness
            and next_step is not None
            and following is next_step
            and next_step.action in _SCROLL_HANDOFF_ACTIONS
            and misses
            and all(miss == "unresolved" for miss in misses)
            and not step.risk_review_required
            and not self._step_is_consequential(step, workflow)
        ):
            # The scroll did its bounded work; what failed is the next click's
            # target, which never resolved on any frame (a relabelled or moved
            # control). Halting here would name the scroll and hide the
            # control that changed. Hand the miss to that click instead: it
            # resolves on direct deterministic rungs only and, when they find
            # nothing, halts on its own target before any input. A target
            # that resolved but sat out of position or failed its identity
            # check still halts here, as before.
            self._scroll_missed_target = (
                next_step.id,
                f"the preceding scroll '{step.id}' used {scrolled:.0f}px of its "
                f"{budget:.0f}px budget and the target never came into view; "
                "no deterministic rung found it, so no input was sent",
            )
            return None
        target_desc = (
            f"the anchor of step '{next_step.id}' ({next_step.intent})"
            if next_step is not None
            else self._describe_predicate(stop_pred)
        )
        return (
            f"Step '{step.id}' ({step.intent}): closed-loop scroll exhausted "
            f"its budget ({scrolled:.0f}px of {budget:.0f}px allowed, "
            f"{SCROLL_BUDGET_FACTOR}x the recorded distance) without "
            f"{target_desc} resolving — target never came into view; run aborted"
        )

    @timed_span("settle")
    def _wait_for_scroll_transition_and_settle(self, baseline_png: bytes) -> bytes:
        """Wait for a delivered scroll to change the frame, then settle it.

        A remote client can return two identical pre-action frames before the
        wheel packet reaches the remote application. A generic settle poll can
        therefore accept the old screen as stable. Require one visual
        transition within the configured readiness window before applying the
        normal settled-state check. At a real scroll boundary, return the last
        unchanged frame after the bounded wait so the closed loop can continue
        or refuse when its action budget is exhausted.
        """

        deadline = time.monotonic() + self.settle_readiness_timeout_s
        frame = self.backend.screenshot()
        while not self.vision.pixels_changed(baseline_png, frame):
            if time.monotonic() >= deadline:
                return frame
            time.sleep(self.poll_interval_s)
            frame = self.backend.screenshot()
        return self.vision.wait_settled(self.backend)

    def _prepare_remote_scroll_input(
        self,
        step: Step,
        result: StepResult,
    ) -> Optional[str]:
        """Bind one remote wheel edge to a fresh, one-use frame lease.

        Target-readiness probes can take longer than a remote backend's frame
        lease.  A wheel gesture has no coordinate to re-resolve, but it is
        still real input into an opaque session.  Acquire the remote backend's
        exact-content lease after readiness checks and before the final
        authorization, environment, and delivery checks.  Do not extend the
        allowed frame age or reuse the observation from the probe.
        """

        if not isinstance(self.backend, RemoteActuationBackend):
            return None
        try:
            self.backend.acquire_actuation_frame()
        except Exception as exc:  # noqa: BLE001 - backend boundary must halt
            if self.governed_authorization is not None:
                result.safety_halt = True
            detail = _scrub_phi(str(exc)) or type(exc).__name__
            return (
                "Remote scroll preflight HALTED before input for step "
                f"'{step.id}' ({step.intent}): {detail}"
            )
        return None
