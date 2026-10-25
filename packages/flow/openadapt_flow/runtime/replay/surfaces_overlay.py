# mypy: disable-error-code="empty-body"
"""Surfaces and the control overlay: which backend a step acts on, and the progress shown to the person watching.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_SurfacesAndOverlayHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast  # noqa: F401

from openadapt_flow.backend import BrowserPresentationGeometryBackend
from openadapt_flow.ir import ActionKind, ExecutionTargetKind
from openadapt_flow.runtime.surfaces import SurfaceRouter

if TYPE_CHECKING:
    from pathlib import Path

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import (
        ObservedSurfaceEnvironment,
        Point,
        Region,
        Resolution,
        Step,
        Workflow,
    )
    from openadapt_flow.runtime.authorization import GovernedRunAuthorization
    from openadapt_flow.runtime.control_overlay import RuntimeControlOverlayEmitter
    from openadapt_flow.runtime.replayer import _GraphStepContext


class _SurfacesAndOverlayHost:
    _actuation_epoch: Any

    def _cancel_guarded_coordinate(self) -> None: ...
    def _cancel_guarded_keyboard(self) -> None: ...

    _control_overlay_disabled: Any
    _control_overlay_last_observation_png: Optional[bytes]
    _durable_linear_snapshot: tuple[Any, ...]
    _last_click_point: Optional[Point]
    _last_click_region: Optional[Region]
    _managed_dispatch_snapshot: tuple[str, str, str] | None
    _settle_seed: Optional[tuple[Any, int]]
    _surface_environment_observations: dict[str, ObservedSurfaceEnvironment]

    def _surface_identity_check(self, backend: Any, step: Step) -> Optional[bool]: ...

    _surface_identity_context: Any
    _surface_router: Optional[SurfaceRouter]
    backend: Backend
    control_overlay: Optional["RuntimeControlOverlayEmitter"]
    control_overlay_error: Optional[str]
    governed_authorization: Optional[GovernedRunAuthorization]
    poll_interval_s: float
    surface_backends: dict[str, Backend]


if TYPE_CHECKING:
    _Base = _SurfacesAndOverlayHost
else:
    _Base = object


class SurfacesAndOverlay(_Base):
    """Surfaces and the control overlay: which backend a step acts on, and the progress shown to the person watching."""

    def _begin_control_overlay(self, profile: Optional[str]) -> None:
        """Start an optional presentation rail without affecting execution."""

        self.control_overlay_error = None
        self._control_overlay_disabled = False
        self._control_overlay_last_observation_png = None
        if self.control_overlay is None:
            return
        try:
            if profile is None:
                raise ValueError("run report has no execution profile")
            self.control_overlay.begin(profile=profile)
            # Prime browser-only presentation metadata at the run boundary,
            # never between final target revalidation and one-shot delivery.
            # Subsequent reads are cache-only and fail closed if viewport
            # dimensions changed.
            if isinstance(self.backend, BrowserPresentationGeometryBackend):
                self.backend.browser_presentation_viewport()
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    def _emit_control_overlay_phase(
        self,
        phase: str,
        *,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
        target_tracking: Optional[Any] = None,
        observation_png: Optional[bytes] = None,
    ) -> None:
        if self.control_overlay is None or self._control_overlay_disabled:
            return
        try:
            self.control_overlay.emit_phase(
                phase,
                current_step=current_step,
                total_steps=total_steps,
                target_tracking=target_tracking,
                observation_png=observation_png,
            )
            if observation_png is not None:
                self._control_overlay_last_observation_png = observation_png
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    def _control_overlay_browser_target(
        self,
        step: Step,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        observation_png: bytes,
    ) -> Optional[Any]:
        """Build presentation-only geometry from the final resolved target.

        Only a backend that explicitly advertises top-level browser CSS
        geometry can produce this V2 target.  Missing/invalid presentation
        metadata omits the rectangle without disabling the status rail or
        influencing execution.
        """

        if (
            self.control_overlay is None
            or self._control_overlay_disabled
            or resolution is None
            or not isinstance(self.backend, BrowserPresentationGeometryBackend)
        ):
            return None
        viewport = self.backend.browser_presentation_viewport()
        if viewport is None:
            return None
        # Visual ladder regions are screenshot-pixel coordinates.  The
        # reference browser runner records CSS-scale screenshots (DPR 1); for
        # any other DPR, omit visual tracking until an exact pixel-to-CSS
        # projection is retained.  Structural handle regions are already CSS
        # viewport coordinates and remain exact at any DPR.
        if resolution.structural_handle is None and viewport[2] != 1.0:
            return None
        region = (
            resolution.structural_handle.region
            if resolution.structural_handle is not None
            and resolution.structural_handle.region is not None
            else matched_region
        )
        if region is None:
            return None
        action_kind = {
            ActionKind.CLICK: "click",
            ActionKind.DOUBLE_CLICK: "double_click",
            ActionKind.RIGHT_CLICK: "right_click",
            ActionKind.DRAG: "drag",
            ActionKind.TYPE: "type",
            ActionKind.SELECT_OPTION: "type",
            ActionKind.SCROLL: "scroll",
        }.get(step.action)
        if action_kind is None:
            return None
        try:
            return self.control_overlay.browser_target_tracking(
                observation_png=observation_png,
                region_css_px=region,
                viewport=viewport,
                action_kind=action_kind,
            )
        except Exception:
            return None

    def _emit_control_overlay_terminal(self, outcome: Optional[str]) -> None:
        if self.control_overlay is None or self._control_overlay_disabled:
            return
        try:
            if outcome is None:
                raise ValueError("run report has no exact execution outcome")
            self.control_overlay.emit_terminal(
                outcome,
                observation_png=self._control_overlay_last_observation_png,
            )
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    @staticmethod
    def _control_overlay_progress(
        workflow: Workflow,
        step_index: int,
        graph_ctx: Optional["_GraphStepContext"],
    ) -> tuple[Optional[int], Optional[int]]:
        if workflow.program is not None or graph_ctx is not None:
            return None, None
        return step_index + 1, len(workflow.steps)

    def _begin_surfaces(
        self,
        workflow: Workflow,
        *,
        resume: bool,
        resume_from: Optional[int] = None,
        resume_program: bool = False,
        bundle_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Bind the workflow's surfaces for this run, or say why not.

        A single-surface workflow binds nothing and runs exactly as before.
        A surface is a name plus an identity check, never a live handle, so a
        linear resume re-binds every surface by identity from the last
        checkpoint's surface observations (docs/MULTI_SURFACE.md).
        """

        self._surface_router = None
        if not workflow.surfaces:
            if self.surface_backends:
                return (
                    "surface_backends were supplied, but this workflow declares "
                    "no surfaces; refusing to run"
                )
            return None
        # Governed, qualified, durable, resumed, and managed (Cloud) runs are
        # admitted: the governed authorization binds the sealed bundle, whose
        # workflow.json carries the declared surface map; every step result
        # records the surface it ran on; the outcome classifier requires that
        # record to match the contract; a qualification case observes each
        # surface against its own qualified boundary; and checkpoints carry
        # each surface's last-seen identity observation, never a handle.
        if resume_program:
            return (
                "a multi-surface workflow program cannot be resumed yet: program "
                "checkpoints do not carry surface observations; refusing to run"
            )
        if self._managed_dispatch_snapshot is not None:
            # An unattended run must not start when a surface it will switch
            # to has no target: it would stop half-way after earlier writes.
            unbound = [
                spec.id
                for spec in workflow.surfaces[1:]
                if not spec.is_page and spec.id not in (self.surface_backends or {})
            ]
            if unbound:
                return (
                    "a managed dispatch of a multi-surface workflow needs a "
                    "backend for every backend surface in the deployment "
                    "profile's surfaces: map; unbound: "
                    + ", ".join(unbound)
                    + "; refusing to run"
                )
        resume_active: Optional[str] = None
        resume_observations: dict[str, Any] = {}
        if resume and resume_from:
            last = None
            for checkpoint in self._durable_linear_snapshot:
                if getattr(checkpoint, "step_index", None) == resume_from - 1:
                    last = checkpoint
            if last is None or getattr(last, "active_surface", None) is None:
                return (
                    "the checkpoint this multi-surface run resumes from carries "
                    "no surface observation, so its surfaces cannot be re-bound "
                    "by identity; refusing to run"
                )
            resume_active = last.active_surface
            resume_observations = dict(last.surface_observations)
        authorization = self.governed_authorization
        if (
            authorization is not None
            and authorization.qualification_case_id is not None
        ):
            project = workflow.qualification
            if project is None:
                return (
                    "a multi-surface qualification case needs the bundle's "
                    "qualification project; refusing to run"
                )
            for spec in workflow.surfaces[1:]:
                boundary = project.surface_environments.get(spec.id)
                if boundary is None:
                    return (
                        f"the qualification project has no environment boundary "
                        f"for surface '{spec.id}'; every surface of a "
                        "multi-surface workflow must be qualified; refusing to run"
                    )
                if boundary.target_kind != spec.kind:
                    return (
                        f"the qualification boundary for surface '{spec.id}' is "
                        f"{boundary.target_kind}, but the surface is declared "
                        f"{spec.kind}; refusing to run"
                    )
            extra = sorted(
                set(project.surface_environments)
                - {spec.id for spec in workflow.surfaces[1:]}
            )
            if extra:
                return (
                    "the qualification project bounds surfaces the workflow "
                    "does not declare: " + ", ".join(extra) + "; refusing to run"
                )
        self._surface_environment_observations = {}
        try:
            self._surface_identity_context = (workflow, bundle_dir)
            self._surface_router = SurfaceRouter(
                workflow,
                self.backend,
                self.surface_backends,
                poll_interval_s=self.poll_interval_s,
                identity_check=self._surface_identity_check,
                resume_active=resume_active,
                observations=resume_observations,
            )
        except ValueError as exc:
            return f"{exc}; refusing to run"
        return None

    def _end_surfaces(self) -> None:
        """Restore the primary backend after a multi-surface run."""

        router = self._surface_router
        if router is None:
            return
        self._surface_router = None
        if self.backend is not router.primary_backend:
            self._activate_surface_backend(router.primary_backend)
        # Drop the qualification input-edge guard from every other surface
        # backend; the primary's guard follows the single-surface lifecycle.
        for backend in router.backends.values():
            if backend is router.primary_backend:
                continue
            clear_guard = getattr(backend, "set_qualification_input_guard", None)
            if callable(clear_guard):
                try:
                    clear_guard(None)
                except Exception:
                    pass

    def _activate_surface_backend(self, backend: Backend) -> None:
        """Make ``backend`` current and drop every cache bound to the old one.

        Nothing observed on one surface may be reused on another: the settle
        seed, the on-screen read-back verifier, the last click point, and any
        armed pointer/keyboard lease all belong to the surface they came from.
        """

        self._cancel_guarded_coordinate()
        self._cancel_guarded_keyboard()
        self.backend = backend
        self._settle_seed = None
        self._actuation_epoch += 1
        # The on-screen read-back verifier is bound to one backend; drop it so
        # the next step builds one for the new surface.
        vars(self).pop("_readback_verifier", None)
        self._last_click_point = None
        self._last_click_region = None
        self._control_overlay_last_observation_png = None

    def _active_surface_binding(
        self, workflow: Workflow
    ) -> tuple[Optional[ExecutionTargetKind], Optional[str]]:
        """(kind, execution mode) of the surface the run is on right now.

        Single-surface runs use the bundle-level binding, unchanged. In a
        multi-surface run the remote-surface safety rules follow the ACTIVE
        surface, so an RDP segment of a web-first bundle keeps them.
        """

        router = self._surface_router
        if router is None:
            return workflow.surface, workflow.execution_mode
        from openadapt_flow.surface_selection import execution_mode_for_surface

        kind = cast(ExecutionTargetKind, router.active_kind())
        return kind, execution_mode_for_surface(kind)

    def _active_surface_is_external_remote(self, workflow: Workflow) -> bool:
        kind, mode = self._active_surface_binding(workflow)
        return kind in ("rdp", "citrix") and mode == "external"
