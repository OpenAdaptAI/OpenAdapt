# mypy: disable-error-code="empty-body"
"""Consequential delivery: revalidate the target and authorization at the last point before input.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_ConsequentialDeliveryHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Optional, cast  # noqa: F401

from openadapt_flow.backend import (
    FocusedElementActuationLeaseBackend,
    FreshActuationRequired,
    GuardedCoordinateActionBackend,
    GuardedDragActionBackend,
    GuardedKeyboardActionBackend,
    PreparedPointerActuationBackend,
    RemoteActuationBackend,
    RemoteFrameContractBackend,
    SelectOptionBackend,
    StructuralResolutionRefused,
    SubmitBindingBackend,
)
from openadapt_flow.ir import (
    ActionKind,
    FreshActuationEvent,
    Point,
    Region,
    SafetyRefusalEvidence,
)
from openadapt_flow.privacy import scrub_text as _scrub_phi
from openadapt_flow.qualification_faults import sha256_bytes
from openadapt_flow.run_gate import is_consequential
from openadapt_flow.runtime.authorization import runtime_inputs_digest
from openadapt_flow.runtime.step_timing import timed_span
from openadapt_flow.vision.ocr import OcrResolutionRefused

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Callable, Mapping

    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import Resolution, Step, StepResult, Workflow
    from openadapt_flow.production_qualification import ProductionQualificationGuard
    from openadapt_flow.qualification_campaign_authority import (
        QualificationCampaignGuard,
    )
    from openadapt_flow.qualification_faults import QualificationFaultDriver
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        RuntimeParamScalar,
    )
    from openadapt_flow.runtime.replay._shared import _DeliveryResultT


class _ConsequentialDeliveryHost:
    _active_delivery_acknowledgers: tuple[Any, ...]
    _active_delivery_region: Optional[Region]
    _active_delivery_resolution: Optional[Resolution]

    def _active_program_frame_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
        base_params: Mapping[str, RuntimeParamScalar],
    ) -> Optional[str]: ...

    _active_runtime_worklists: Optional[dict[str, list[dict[str, str]]]]
    _active_surface_is_external_remote: Any
    _actuation_epoch: Any

    def _approval_gate_refusal(
        self, workflow: Workflow, step: Step, result: StepResult
    ) -> Optional[str]: ...

    _durable_continuation_guard: Optional[Any]
    _governed_asset_mutation: Optional[str]

    def _governed_authorization_snapshot_refusal(self) -> Optional[str]: ...

    _governed_base_params: Optional[dict[str, RuntimeParamScalar]]

    def _identity_gate_error(
        self,
        step: Step,
        resolution: Resolution,
        frame_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        *,
        evidence_run_dir: Optional[Path] = None,
    ) -> Optional[str]: ...

    _interstitials: Any
    _is_qualification_fault_step: Any

    def _managed_dispatch_refusal(self) -> Optional[str]: ...
    def _profile_runtime_refusal(self, workflow: Workflow) -> Optional[str]: ...
    def _resolve_step(
        self,
        step: Step,
        screen_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
        *,
        allow_grounder: bool = True,
        allow_target_ocr: bool = True,
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]: ...

    _settle_seed: Optional[tuple[Any, int]]

    def _workflow_snapshot_refusal(self, workflow: Workflow) -> Optional[str]: ...

    approval_gate: Optional["ApprovalGate"]
    backend: Backend
    governed_authorization: Optional[GovernedRunAuthorization]
    managed_dispatch_binding: Optional[Any]
    production_qualification_guard: Optional["ProductionQualificationGuard"]
    qualification_campaign_guard: Optional["QualificationCampaignGuard"]
    qualification_fault_driver: Optional[QualificationFaultDriver]


if TYPE_CHECKING:
    _Base = _ConsequentialDeliveryHost
else:
    _Base = object


class ConsequentialDelivery(_Base):
    """Consequential delivery: revalidate the target and authorization at the last point before input."""

    def _step_is_consequential(self, step: Step, workflow: Workflow) -> bool:
        authorization = self.governed_authorization
        return is_consequential(step, workflow) or (
            authorization is not None
            and authorization.requires_verified_identity(step.id)
        )

    def _step_requires_remote_identity_binding(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether an external opaque-remote step needs an identity lease.

        ``identity_armed`` says that the remote action must remain bound to the
        identity that was verified on the exact live frame. It does not change
        the business-risk classification of the action on browser or native
        surfaces.
        """

        return (
            step.identity_armed is True
            and self._active_surface_is_external_remote(workflow)
            and isinstance(self.backend, RemoteActuationBackend)
        )

    def _step_requires_remote_guarded_actuation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether a remote input edge must use the fresh-frame guard path."""

        return isinstance(self.backend, RemoteActuationBackend) and (
            self._step_is_consequential(step, workflow)
            or self._step_requires_remote_identity_binding(step, workflow)
            or self.qualification_fault_driver is not None
        )

    def _selection_contract_error(
        self, step: Step, workflow: Workflow
    ) -> Optional[str]:
        """Return a fail-closed error for an unusable composite selection."""

        if step.action is not ActionKind.SELECT_OPTION:
            return None
        if step.selection_region is None or step.selection_commit_key is None:
            return (
                f"Step '{step.id}' ({step.intent}) has an incomplete exact "
                "option-selection contract; refusing input"
            )
        if not self._active_surface_is_external_remote(workflow) or not isinstance(
            self.backend, RemoteActuationBackend
        ):
            return (
                f"Step '{step.id}' ({step.intent}) carries a remote "
                "option-selection contract outside its qualified external "
                "opaque remote surface; refusing input"
            )
        if not isinstance(self.backend, SelectOptionBackend):
            return (
                f"Step '{step.id}' ({step.intent}) requires atomic option "
                "selection, but this backend cannot bind type-ahead and "
                "commit under one input lease"
            )
        return None

    def _step_has_identity_contract(self, step: Step, workflow: Workflow) -> bool:
        """Whether this step carries identity evidence or an explicit mandate."""
        authorization = self.governed_authorization
        if authorization is not None and authorization.requires_verified_identity(
            step.id
        ):
            return True
        qualification = workflow.qualification
        if qualification is not None and step.id in qualification.identity_policies:
            return True
        # A submit binding ties an unlabeled control to the record the run
        # entered; it must be verified on the guarded path like any identity.
        if step.submit_binding is not None:
            return True
        anchor = step.anchor
        return anchor is not None and bool(
            anchor.context_text
            or anchor.structured_identity
            or anchor.identity_template
            or anchor.identifier_crop
        )

    def _unbound_submit_refusal(
        self, step: Step, workflow: Workflow, result: StepResult
    ) -> Optional[str]:
        """Refuse a consequential click the compiler could not bind.

        The compiler records ``submit_binding_unbound_reason`` when the
        demonstrated click on an unlabeled control could not be tied to the
        form it submits (for example, two unlabeled controls follow the
        filled fields). Such a click has no record tie at all, so it is
        refused before input whatever identity evidence the step carries.
        """

        if (
            step.submit_binding_unbound_reason is None
            or step.action not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            or not self._step_is_consequential(step, workflow)
        ):
            return None
        result.safety_halt = True
        result.failure_category = "safety_halt"
        return (
            f"Step '{step.id}' ({step.intent}) clicks an unlabeled control that "
            "could not be tied to the record this run entered: "
            f"{step.submit_binding_unbound_reason} — refusing to act; run aborted"
        )

    def _step_needs_consequential_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether delivery needs a second, immediately fresh observation.

        Opaque remote surfaces always use the two-phase lease for a
        consequential action. Browser/native surfaces use the same pre-delivery
        refresh when identity is part of the action contract.
        """
        if (
            step.action is ActionKind.SELECT_OPTION
            and self._active_surface_is_external_remote(workflow)
            and isinstance(self.backend, RemoteActuationBackend)
        ):
            # A selection is a composite focus -> typeahead -> commit gesture.
            # It always needs an exact fresh-frame lease before the focus click
            # and another after focus before text+commit, independent of a
            # generic risk classifier's opinion of the TYPE step.
            return True
        if (
            self.qualification_fault_driver is not None
            and step.action
            in {
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
            }
            and isinstance(self.backend, RemoteActuationBackend)
        ):
            # Exact fault-prefix proof includes read-only prior actions. Arm a
            # fresh remote lease for those actions only during the fault run,
            # so ordinary demo replay keeps its reversible fast path.
            return True
        if self._step_requires_remote_guarded_actuation(step, workflow):
            return True
        return self._step_is_consequential(
            step, workflow
        ) and self._step_has_identity_contract(step, workflow)

    def _requires_atomic_identity_pointer(self, step: Step, workflow: Workflow) -> bool:
        """Whether a pointer edge may not cross an unleased identity boundary.

        An anchored TYPE has a focusing click before keyboard delivery.  That
        first input edge needs the same target/record lease as a consequential
        CLICK; otherwise a visually identical replacement can receive its
        handler before the later keyboard lease notices the change.
        """

        return (
            step.action
            in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
            )
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and self._step_has_identity_contract(step, workflow)
        )

    def _requires_atomic_identity_keyboard(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether keyboard delivery must stay bound to the verified focus."""

        return (
            step.action
            in (
                ActionKind.KEY,
                ActionKind.HOTKEY,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
            )
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and self._step_has_identity_contract(step, workflow)
        )

    def _typed_remote_receipt_required(self) -> bool:
        """Whether a remote pointer edge must carry a typed delivery receipt.

        :class:`RemoteActuationBackend` already owns the exact-frame contract:
        after ``acquire_actuation_frame`` the backend captures once more under
        its own input lock and refuses *before the first input edge* when the
        window/session, dimensions, readiness, or exact frame content changed.
        :class:`GuardedRemotePointerActionBackend` adds an explicit
        expected-frame hash plus a typed, target-bound receipt on top of that
        contract; its absence is therefore missing *evidence*, not a missing
        safety property.

        A governed (Standard/Regulated) run and an exact qualification-fault
        proof both consume that receipt as retained evidence, so they must
        refuse before delivery rather than actuate without it. Ordinary replay
        keeps the documented lease path — refusing there over-halts every
        conforming opaque remote backend, including a pixel-only no-DOM canvas
        that can only expose the lease. Such a result stays unlabeled, which
        the outcome classifier already maps to ``COMPLETED_UNVERIFIED`` instead
        of a production-eligible success.
        """

        return (
            self.governed_authorization is not None
            or self.qualification_fault_driver is not None
        )

    def _cancel_guarded_coordinate(self) -> None:
        """Clean an unconsumed local coordinate lease, if one was armed."""

        if isinstance(self.backend, GuardedCoordinateActionBackend):
            self.backend.cancel_guarded_coordinate()

    def _cancel_guarded_keyboard(self) -> None:
        """Clean an unconsumed local focused-element lease, if one was armed."""

        if isinstance(self.backend, GuardedKeyboardActionBackend):
            self.backend.cancel_guarded_keyboard()
        if isinstance(self.backend, FocusedElementActuationLeaseBackend):
            self.backend.cancel_focused_element_lease()

    def _local_atomic_capability_error(
        self,
        step: Step,
        resolution: Optional[Resolution],
        workflow: Workflow,
        *,
        arm_keyboard: bool,
    ) -> Optional[str]:
        """Refuse before input when a local identity lease cannot be enforced.

        Consequential TYPE has two input edges: its optional focusing click and
        the subsequent keyboard delivery.  The first revalidation must prove
        both local seams are available before it focuses anything.  The second
        revalidation (``arm_keyboard=True``) only needs the keyboard seam.
        Structural focusing actions already carry a native target fingerprint;
        visual/coordinate focusing actions require the coordinate guard.
        """

        if (
            step.submit_binding is not None
            and step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and not isinstance(self.backend, SubmitBindingBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) clicks an unlabeled control "
                "whose only record tie is its recorded submit binding, but this "
                "backend cannot read the page structure to verify it — refusing "
                "input; run aborted"
            )

        if isinstance(self.backend, RemoteActuationBackend):
            return None

        requires_keyboard = self._requires_atomic_identity_keyboard(step, workflow)
        if requires_keyboard and not isinstance(
            self.backend, GuardedKeyboardActionBackend
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential "
                "identity-gated keyboard action, but this backend cannot bind "
                "the verified focus and execution context to the same input "
                "operation — refusing unguarded keyboard delivery; run aborted"
            )

        requires_pointer = self._requires_atomic_identity_pointer(step, workflow)
        pointer_edge_pending = step.action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.DRAG,
        ) or (
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and not arm_keyboard
        )
        structural_pointer = bool(
            step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and resolution is not None
            and resolution.rung == "structural"
            and resolution.structural_handle is not None
            and resolution.structural_handle.target_fingerprint is not None
            and step.anchor is not None
            and step.anchor.structural is not None
            and callable(getattr(self.backend, "act_structural", None))
        )
        if (
            requires_pointer
            and pointer_edge_pending
            and resolution is not None
            and not structural_pointer
            and not isinstance(self.backend, GuardedCoordinateActionBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential "
                "identity-gated pointer action, but this backend cannot bind "
                "target and context verification to the same actuation operation "
                "— refusing raw coordinate delivery; run aborted"
            )
        if (
            requires_pointer
            and step.action is ActionKind.DRAG
            and not isinstance(self.backend, RemoteActuationBackend)
            and not isinstance(self.backend, GuardedDragActionBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential drag, "
                "but this backend cannot bind both endpoints to the fresh "
                "identity-verified frame — refusing input; run aborted"
            )
        return None

    def _guarded_keyboard_revalidation(
        self, step: Step, workflow: Workflow, *, arm_keyboard: bool
    ) -> bool:
        return bool(
            arm_keyboard
            and self._requires_atomic_identity_keyboard(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )

    def _guarded_type_pointer_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        return bool(
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and self._requires_atomic_identity_pointer(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedCoordinateActionBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )

    def _guarded_click_pointer_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        return bool(
            step.action
            in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
            )
            and self._requires_atomic_identity_pointer(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedCoordinateActionBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )

    def _revalidation_frame_capture(
        self, step: Step, workflow: Workflow, *, arm_keyboard: bool
    ) -> Optional[Callable[[], bytes]]:
        """The capture a local revalidation takes its fresh frame with.

        ``None`` means the ordinary screenshot. A remote backend's actuation
        frame arms a lease, so it is never taken outside the revalidation.
        """

        if isinstance(self.backend, RemoteActuationBackend):
            return None
        if (
            self._guarded_keyboard_revalidation(
                step, workflow, arm_keyboard=arm_keyboard
            )
            or self._guarded_type_pointer_revalidation(step, workflow)
            or self._guarded_click_pointer_revalidation(step, workflow)
        ):
            return cast(
                GuardedKeyboardActionBackend, self.backend
            ).guarded_keyboard_frame
        return None

    @timed_span("identity")
    def _revalidate_consequential_actuation(
        self,
        step: Step,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        *,
        run_dir: Path,
        arm_keyboard: bool = False,
    ) -> tuple[
        Optional[Resolution],
        Optional[Region],
        bytes,
        Optional[str],
    ]:
        """Freshly re-resolve and identity-check a consequential GUI step.

        Browser/native backends provide a fresh screenshot immediately before
        delivery. ``RemoteActuationBackend`` additionally arms a one-shot
        exact-frame lease and refuses if pixels change before the first input
        edge. Reversible actions retain the ordinary replay path.
        """
        if not self._step_needs_consequential_revalidation(step, workflow):
            return resolution, matched_region, before_png, None

        capability_error = self._local_atomic_capability_error(
            step,
            resolution,
            workflow,
            arm_keyboard=arm_keyboard,
        )
        if capability_error is not None:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return resolution, matched_region, before_png, capability_error

        # The ordinary resolution pass may have armed a one-shot structural
        # handle. Consequential revalidation deliberately replaces that
        # observation with a fresh resolve immediately before delivery. Drop
        # the stale handle first: on browser backends, binding a second token
        # mutates the target node, which correctly invalidates the first
        # MutationObserver; if the old observer remains live, its cleanup
        # mutation can in turn invalidate the new guard and falsely refuse an
        # unchanged target.
        cancel_stale_structural = getattr(
            self.backend,
            "cancel_pending_structural_guards",
            None,
        )
        if callable(cancel_stale_structural):
            cancel_stale_structural()

        guarded_keyboard_backend = self._guarded_keyboard_revalidation(
            step, workflow, arm_keyboard=arm_keyboard
        )
        focused_element_backend = (
            arm_keyboard
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, FocusedElementActuationLeaseBackend)
        )
        guarded_type_pointer_backend = self._guarded_type_pointer_revalidation(
            step, workflow
        )
        guarded_click_pointer_backend = self._guarded_click_pointer_revalidation(
            step, workflow
        )
        prepared_pointer: Optional[Point] = None
        pointer_edge_pending = step.action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.DRAG,
        ) or (
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and resolution is not None
            and not arm_keyboard
        )
        if (
            pointer_edge_pending
            and resolution is not None
            and isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, PreparedPointerActuationBackend)
        ):
            prepared_pointer = resolution.point
            try:
                self.backend.prepare_pointer_actuation(*prepared_pointer)
            except Exception as exc:  # noqa: BLE001 - backend boundary must halt
                if self.governed_authorization is not None:
                    result.safety_halt = True
                detail = _scrub_phi(str(exc)) or type(exc).__name__
                return (
                    resolution,
                    matched_region,
                    before_png,
                    "Pointer preflight HALTED before final target resolution for "
                    f"step '{step.id}' ({step.intent}): {detail}",
                )
        try:
            fresh_png = (
                self.backend.acquire_actuation_frame()
                if isinstance(self.backend, RemoteActuationBackend)
                else (
                    cast(
                        GuardedKeyboardActionBackend, self.backend
                    ).guarded_keyboard_frame()
                    if (
                        guarded_keyboard_backend
                        or guarded_type_pointer_backend
                        or guarded_click_pointer_backend
                    )
                    else self.backend.screenshot()
                )
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary must halt
            if self.governed_authorization is not None:
                result.safety_halt = True
            detail = _scrub_phi(str(exc)) or type(exc).__name__
            return (
                resolution,
                matched_region,
                before_png,
                "Actuation preflight HALTED before input for step "
                f"'{step.id}' ({step.intent}): {detail}",
            )

        try:
            fresh_resolution, fresh_region, error = self._resolve_step(
                step,
                fresh_png,
                bundle_dir,
                workflow,
                # Opening an opaque select control can repeat its current value
                # in the option popup. That text is valid target evidence before
                # the focusing click, but it is no longer unique afterward. The
                # second phase therefore requires the retained template or
                # independent relational landmarks.
                allow_target_ocr=not (
                    arm_keyboard and step.action is ActionKind.SELECT_OPTION
                ),
            )
        except (OcrResolutionRefused, StructuralResolutionRefused) as exc:
            result.safety_halt = True
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="actuation_revalidation",
                code="actuation_observation_changed",
                detector_input_sha256=sha256_bytes(fresh_png),
            )
            return (
                None,
                None,
                fresh_png,
                "Actuation preflight HALTED because the fresh target was "
                f"ambiguous for step '{step.id}' ({step.intent}): {exc}",
            )
        if error is not None:
            if self.governed_authorization is not None:
                result.safety_halt = True
            return fresh_resolution, fresh_region, fresh_png, error
        if (
            prepared_pointer is not None
            and fresh_resolution is not None
            and fresh_resolution.point != prepared_pointer
        ):
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return (
                fresh_resolution,
                fresh_region,
                fresh_png,
                f"Step '{step.id}' ({step.intent}) resolved to "
                f"{fresh_resolution.point!r} after remote pointer hover, not "
                f"the prepared point {prepared_pointer!r}; refusing a click "
                "whose target moved before actuation",
            )
        identity_required = (
            self.governed_authorization is not None
            and self.governed_authorization.requires_verified_identity(step.id)
        ) or (
            workflow.qualification is not None
            and step.id in workflow.qualification.identity_policies
        )
        if fresh_resolution is None and identity_required:
            error = (
                f"Step '{step.id}' ({step.intent}) requires verified identity "
                "but no exact target observation was available immediately "
                "before actuation — refusing to act; run aborted"
            )
        elif (
            fresh_resolution is not None
            and step.submit_binding is not None
            and (
                fresh_resolution.rung == "structural"
                or isinstance(self.backend, RemoteActuationBackend)
                or not isinstance(self.backend, GuardedCoordinateActionBackend)
            )
        ):
            error = (
                f"Step '{step.id}' ({step.intent}) carries a submit binding, which "
                "is verified only on the guarded visual path of a DOM backend; "
                "this observation would bypass it — refusing to act; run aborted"
            )
            result.safety_halt = True
            result.failure_category = "safety_halt"
        elif fresh_resolution is not None:
            guarded_coordinate = (
                self._requires_atomic_identity_pointer(step, workflow)
                and not (
                    arm_keyboard
                    and step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
                )
                # CLICK/DOUBLE_CLICK consume the native DOM/UIA handle when
                # structural resolution wins. RIGHT_CLICK has no native
                # structural actuator, so bind its resolved point to the
                # identity-verified target instead. DRAG is armed only after
                # its independently resolved destination is known below.
                and step.action is not ActionKind.DRAG
                and (
                    fresh_resolution.rung != "structural"
                    or step.action is ActionKind.RIGHT_CLICK
                )
                and not isinstance(self.backend, RemoteActuationBackend)
                and isinstance(self.backend, GuardedCoordinateActionBackend)
            )
            if guarded_coordinate:
                coordinate_backend = cast(GuardedCoordinateActionBackend, self.backend)
                if step.action is ActionKind.RIGHT_CLICK:
                    cancel_structural = getattr(
                        self.backend,
                        "cancel_pending_structural_guards",
                        None,
                    )
                    if callable(cancel_structural):
                        cancel_structural()
                editable_arm = getattr(
                    coordinate_backend,
                    "arm_guarded_editable_coordinate",
                    None,
                )
                if step.action in (
                    ActionKind.TYPE,
                    ActionKind.SELECT_OPTION,
                ) and callable(editable_arm):
                    editable_arm(*fresh_resolution.point)
                elif step.submit_binding is not None:
                    # No row identity exists for an unlabeled control outside
                    # any row. Its record tie is the recorded binding to the
                    # form this run filled; the backend verifies it live and
                    # leases the exact observation until delivery, or raises.
                    try:
                        cast(
                            SubmitBindingBackend, coordinate_backend
                        ).arm_guarded_submit_coordinate(
                            *fresh_resolution.point, binding=step.submit_binding
                        )
                    except StructuralResolutionRefused:
                        result.submit_binding_verified = False
                        raise
                    result.submit_binding_verified = True
                else:
                    coordinate_backend.arm_guarded_coordinate(*fresh_resolution.point)
            guarded_keyboard = guarded_keyboard_backend
            if guarded_keyboard:
                cast(GuardedKeyboardActionBackend, self.backend).arm_guarded_keyboard(
                    *fresh_resolution.point
                )
            if focused_element_backend:
                cast(
                    FocusedElementActuationLeaseBackend,
                    self.backend,
                ).arm_focused_element_lease(*fresh_resolution.point)
            try:
                error = self._identity_gate_error(
                    step,
                    fresh_resolution,
                    fresh_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    evidence_run_dir=run_dir,
                )
                if error is not None and result.identity is not None:
                    code: Literal["identity_conflict", "identity_unverifiable"] = (
                        "identity_conflict"
                        if result.identity.status == "mismatch"
                        else "identity_unverifiable"
                    )
                    result.safety_refusal_evidence = SafetyRefusalEvidence(
                        stage="identity_verification",
                        code=code,
                        detector_input_sha256=sha256_bytes(fresh_png),
                    )
            except Exception:
                if guarded_coordinate:
                    self._cancel_guarded_coordinate()
                if guarded_keyboard:
                    self._cancel_guarded_keyboard()
                if focused_element_backend:
                    self._cancel_guarded_keyboard()
                raise
            if error is not None and guarded_coordinate:
                self._cancel_guarded_coordinate()
            if error is not None and guarded_keyboard:
                self._cancel_guarded_keyboard()
            if error is not None and focused_element_backend:
                self._cancel_guarded_keyboard()
            if error is None and isinstance(self.backend, RemoteFrameContractBackend):
                protected: list[Region] = list(
                    fresh_resolution.resolution_evidence_regions
                    if fresh_resolution is not None
                    else ()
                )
                if fresh_region is not None:
                    protected.append(fresh_region)
                elif fresh_resolution is not None:
                    x, y = fresh_resolution.point
                    protected.append((x - 1, y - 1, 3, 3))
                protected.extend(
                    pc.region for pc in step.expect if pc.region is not None
                )
                if workflow.qualification is not None:
                    policy = workflow.qualification.identity_policies.get(step.id)
                    if policy is not None:
                        protected.extend(
                            s.region for s in policy.signals if s.region is not None
                        )
                try:
                    self.backend.arm_remote_frame_contract(
                        protected_regions=tuple(protected)
                    )
                except Exception as exc:
                    return (
                        fresh_resolution,
                        fresh_region,
                        fresh_png,
                        (
                            "Actuation preflight HALTED because the remote frame mask "
                            f"overlaps protected evidence: {type(exc).__name__}"
                        ),
                    )
        # Retain the exact observation that authorizes the next input edge.
        # Composite TYPE/SELECT_OPTION and retry paths can re-resolve inside
        # ``_act`` after the outer scope captured its initial geometry. A typed
        # zero-input mismatch must describe this live edge, not the stale
        # pre-focus target.
        self._active_delivery_resolution = fresh_resolution
        self._active_delivery_region = fresh_region
        return fresh_resolution, fresh_region, fresh_png, error

    def _deliver_backend_call(
        self,
        result: StepResult,
        call: Callable[[], _DeliveryResultT],
    ) -> _DeliveryResultT:
        """Call one backend delivery boundary with exact attempt reporting.

        A typed fresh-frame mismatch or structural refusal proves that this
        call emitted no input edge, so it preserves the step's prior delivery
        state. Every other exception is conservative: the boundary may have
        been crossed and the step must not enter the pre-delivery retry loop.
        """

        self._actuation_epoch = getattr(self, "_actuation_epoch", 0) + 1
        self._settle_seed = None
        attempted_before = result.delivery_attempted
        acknowledgers = self._active_delivery_acknowledgers
        self._active_delivery_acknowledgers = ()
        try:
            delivered = call()
        except (FreshActuationRequired, StructuralResolutionRefused):
            result.delivery_attempted = attempted_before
            raise
        except Exception:
            result.delivery_attempted = True
            raise
        result.delivery_attempted = True
        for guard in acknowledgers:
            guard.acknowledge_delivery()
        return delivered

    @staticmethod
    def _regions_intersect(left: Region, right: Region) -> bool:
        left_x, left_y, left_width, left_height = left
        right_x, right_y, right_width, right_height = right
        return not (
            left_x + left_width <= right_x
            or right_x + right_width <= left_x
            or left_y + left_height <= right_y
            or right_y + right_height <= left_y
        )

    @staticmethod
    def _translate_recorded_region(
        step: Step,
        resolution: Optional[Resolution],
        region: Region,
    ) -> Region:
        """Map recorded anchor-relative geometry onto the live target point."""

        if step.anchor is None or resolution is None:
            return region
        recorded_x, recorded_y = step.anchor.click_point
        live_x, live_y = resolution.point
        x, y, width, height = region
        return (
            live_x + x - recorded_x,
            live_y + y - recorded_y,
            width,
            height,
        )

    @classmethod
    def _fresh_actuation_identity_regions(
        cls,
        step: Step,
        workflow: Workflow,
        resolution: Optional[Resolution],
    ) -> list[Region]:
        regions: list[Region] = []
        if step.anchor is not None and step.anchor.identifier_region is not None:
            regions.append(
                cls._translate_recorded_region(
                    step, resolution, step.anchor.identifier_region
                )
            )
        if workflow.qualification is not None:
            policy = workflow.qualification.identity_policies.get(step.id)
            if policy is not None:
                regions.extend(
                    cls._translate_recorded_region(step, resolution, signal.region)
                    for signal in policy.signals
                    if signal.region is not None
                )
        return regions

    def _fresh_actuation_event(
        self,
        exc: FreshActuationRequired,
        *,
        step: Step,
        workflow: Workflow,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        retried: bool,
        attempt: int,
    ) -> FreshActuationEvent:
        target_region = matched_region
        if target_region is None and step.anchor is not None:
            target_region = self._translate_recorded_region(
                step, resolution, step.anchor.region
            )
        target_intersection = (
            self._regions_intersect(exc.changed_bbox, target_region)
            if target_region is not None
            else None
        )
        identity_regions = self._fresh_actuation_identity_regions(
            step, workflow, resolution
        )
        identity_intersection = (
            any(
                self._regions_intersect(exc.changed_bbox, region)
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
        )

    def _fresh_actuation_authorization_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
    ) -> Optional[str]:
        """Recheck exact governed authority and inputs before fresh input."""

        snapshot_refusal = self._workflow_snapshot_refusal(workflow)
        if snapshot_refusal is not None:
            return snapshot_refusal
        authorization_snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if authorization_snapshot_refusal is not None:
            return authorization_snapshot_refusal
        profile_refusal = self._profile_runtime_refusal(workflow)
        if profile_refusal is not None:
            return profile_refusal
        base_params = self._governed_base_params
        if base_params is None:
            if self.governed_authorization is not None or workflow.program is not None:
                return "runtime lost its admitted parameter scope before input"
            return self._governed_asset_mutation
        authorization = self.governed_authorization
        if authorization is not None:
            authorization_refusal = authorization.validate_workflow(workflow)
            if authorization_refusal is not None:
                return authorization_refusal
            actual_inputs = runtime_inputs_digest(
                workflow,
                base_params,
                self._active_runtime_worklists,
                interstitials=list(self._interstitials),
            )
            if actual_inputs != authorization.runtime_inputs_digest:
                return (
                    "governed run authorization no longer matches the current "
                    "runtime inputs"
                )
        try:
            frame_refusal = self._active_program_frame_refusal(
                workflow, params, step, base_params
            )
        except Exception as exc:  # noqa: BLE001 - mutated interpreter boundary
            frame_refusal = (
                "program frame validation failed closed before input "
                f"({type(exc).__name__})"
            )
        if frame_refusal is not None:
            return frame_refusal
        return self._governed_asset_mutation

    def _delivery_authorization_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
        result: StepResult,
        *,
        interstitial: bool = False,
    ) -> Optional[str]:
        """Recheck exact authority at the last point before input delivery.

        ``interstitial`` marks a declared, reversible dialog dismissal at step
        entry. The approval gate is consulted only for the step's own input
        edges, after its target and right-record check resolved; a dismissal
        is still audited on the result and judged by the gate's screen check.
        """

        self._active_delivery_acknowledgers = ()
        acknowledgers: list[Any] = []
        refusal = self._managed_dispatch_refusal()
        if refusal is None and self.production_qualification_guard is not None:
            try:
                refusal = self.production_qualification_guard.refusal(workflow)
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "Production qualification could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
        if (
            refusal is None
            and self.production_qualification_guard is None
            and self.managed_dispatch_binding is not None
        ):
            from openadapt_flow.production_qualification import (
                managed_qualification_edge_refusal,
            )

            try:
                edge_refusal = managed_qualification_edge_refusal(
                    self.governed_authorization, workflow
                )
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "Production qualification could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
            else:
                if edge_refusal is not None:
                    refusal = (
                        "Production qualification could not be revalidated before "
                        "delivery"
                    )
        if refusal is None and self.qualification_campaign_guard is not None:
            try:
                refusal = self.qualification_campaign_guard.refusal(workflow)
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "qualification campaign could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
        if refusal is None:
            refusal = self._fresh_actuation_authorization_refusal(
                workflow, params, step
            )
        if (
            refusal is None
            and not interstitial
            and self._is_qualification_fault_step(step)
        ):
            # Every path (screen or API, any approval mode, or none): a drill
            # whose detector didn't refuse its changed input still saves
            # nothing. The drill then hasn't passed: its contract requires the
            # detector's own refusal, never this one.
            refusal = (
                "the bound qualification fault case did not produce its "
                "required detector refusal; refusing to cross the input boundary"
            )
        if (
            refusal is None
            and not interstitial
            and self.approval_gate is not None
            and self._step_is_consequential(step, workflow)
        ):
            refusal = self._approval_gate_refusal(workflow, step, result)
        initial_guard = getattr(self, "_durable_initial_delivery_guard", None)
        if refusal is None and initial_guard is not None:
            try:
                initial_guard.before_delivery()
                acknowledgers.append(initial_guard)
            except Exception as exc:  # noqa: BLE001 - durable fencing boundary
                refusal = (
                    f"managed initial delivery was preempted before delivery: {exc}"
                )
                result.failure_category = "continuation_preempted"
        if refusal is None and self._durable_continuation_guard is not None:
            try:
                self._durable_continuation_guard.before_delivery()
                acknowledgers.append(self._durable_continuation_guard)
            except Exception as exc:  # noqa: BLE001 - durable fencing boundary
                refusal = f"durable continuation was preempted before delivery: {exc}"
                result.failure_category = "continuation_preempted"
        if refusal is not None:
            self._cancel_guarded_coordinate()
            self._cancel_guarded_keyboard()
            result.safety_halt = True
            if result.failure_category != "continuation_preempted":
                result.failure_category = (
                    "governed_refusal"
                    if self.governed_authorization is not None
                    else "safety_halt"
                )
        else:
            self._active_delivery_acknowledgers = tuple(acknowledgers)
        return refusal
