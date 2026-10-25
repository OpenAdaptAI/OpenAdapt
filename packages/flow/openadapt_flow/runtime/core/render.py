# mypy: disable-error-code="empty-body"
"""Render scale and layout drift: keep recorded templates comparable to the live screen.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_RenderScaleAndLayoutDriftHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast  # noqa: F401

from openadapt_flow.backend import Backend

if TYPE_CHECKING:
    from typing import Any, Callable

    from openadapt_flow.ir import ExecutionTargetKind, Step, Workflow
    from openadapt_flow.runtime.surfaces import SurfaceRouter


class _RenderScaleAndLayoutDriftHost:
    def _active_surface_binding(
        self, workflow: Workflow
    ) -> tuple[Optional[ExecutionTargetKind], Optional[str]]: ...
    def _asset_bytes(
        self, bundle_dir: Path, rel: str, *, workflow: Optional[Workflow] = None
    ) -> Optional[bytes]: ...

    _layout_fingerprints: Optional[dict[str, Any]]
    _layout_target_kind: Optional[str]
    _render_scale_attempted: bool
    _render_scale_evidence: Optional[dict[str, Any]]
    _render_scale_inner: Optional[Backend]
    _surface_router: Optional[SurfaceRouter]
    backend: Backend
    layout_drift: Optional[bool]
    layout_drift_advisor: Optional[Callable[[Any], Optional[str]]]
    layout_drift_tolerances: Optional[Any]
    render_scale: Optional[bool]
    vision: Any


if TYPE_CHECKING:
    _Base = _RenderScaleAndLayoutDriftHost
else:
    _Base = object


class RenderScaleAndLayoutDrift(_Base):
    """Render scale and layout drift: keep recorded templates comparable to the live screen."""

    def _layout_drift_on(self, workflow: Workflow) -> bool:
        if self.layout_drift is not None:
            return self.layout_drift
        kind: Optional[str]
        if self._surface_router is not None:
            # Multi-surface run: follow the surface the step acts on, so an
            # RDP segment of a web-first bundle keeps the check.
            kind, _ = self._active_surface_binding(workflow)
        else:
            kind = self._layout_target_kind or workflow.surface
        if kind in ("rdp", "citrix"):
            return True
        backend = self._render_scale_inner or self.backend
        return type(backend).__name__ == "FreeRDPBackend"

    def _live_session_profile(self) -> Optional[str]:
        """Digest of the pinned remote-session profile, when there is one."""
        transport = getattr(self.backend, "transport", None)
        profile = getattr(getattr(transport, "profile", None), "sha256", None)
        return profile if isinstance(profile, str) else None

    def _session_profile_changed(self, recorded: Any) -> bool:
        live = self._live_session_profile()
        return bool(
            recorded.profile_sha256 and live and recorded.profile_sha256 != live
        )

    def _reset_render_scale(self) -> None:
        """Return to the caller's backend and forget the last estimate."""
        if self._render_scale_inner is not None:
            release = getattr(self.backend, "release_frame_sources", None)
            if callable(release):
                release()
            self.backend = self._render_scale_inner
        self._render_scale_inner = None
        self._render_scale_attempted = False
        self._render_scale_evidence = None

    def _load_layout_fingerprints(
        self, bundle_dir: Path, workflow: Workflow
    ) -> dict[str, Any]:
        from openadapt_flow.runtime import layout_drift as drift_mod

        if self._layout_fingerprints is None:
            self._layout_fingerprints = drift_mod.load_bundle_fingerprints(
                self._asset_bytes(
                    Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
                )
            )
        return self._layout_fingerprints

    def _maybe_scale_render(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Optional[Path],
        workflow: Workflow,
    ) -> bytes:
        """Estimate the session's display scale once; return the frame to use.

        Runs at the first step that has a recorded fingerprint. A frame that
        matches the fingerprint, or gives no confident estimate, is returned
        unchanged and the layout-drift check judges it as before. Otherwise
        the backend is replaced by a
        :class:`~openadapt_flow.runtime.render_scale.RenderScaledBackend` and
        the frame is returned in the recorded geometry, where the same
        layout-drift check must then pass.
        """
        if self._render_scale_attempted or bundle_dir is None:
            return frame_png
        enabled = (
            self.render_scale
            if self.render_scale is not None
            else self._layout_drift_on(workflow)
        )
        if not enabled or self._surface_router is not None:
            return frame_png
        from openadapt_flow.runtime import layout_drift as drift_mod
        from openadapt_flow.runtime import render_scale as scale_mod

        recorded = self._load_layout_fingerprints(Path(bundle_dir), workflow).get(
            step.id
        )
        if recorded is None:
            return frame_png
        self._render_scale_attempted = True
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(frame_png)) as img:
                viewport = (int(img.width), int(img.height))
        except Exception:  # noqa: BLE001 - undecodable: leave it to the drift check
            return frame_png
        if self._session_profile_changed(recorded):
            # A pinned remote-session profile (scale, DPI, font smoothing) is
            # a deployment decision; a different profile still halts below.
            self._render_scale_evidence = {
                "status": "no_estimate",
                "reason": "the pinned remote session profile changed",
            }
            return frame_png
        lines = self.vision.ocr(frame_png)
        live = drift_mod.fingerprint(lines, viewport)
        verdict = drift_mod.compare(
            recorded,
            live,
            self.layout_drift_tolerances or drift_mod.Tolerances(),
        )
        if not verdict.drifted:
            self._render_scale_evidence = {"status": "recorded_geometry"}
            return frame_png
        salt, hashed = drift_mod.load_scale_landmarks(
            self._asset_bytes(
                Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
            )
        )
        landmarks = scale_mod.hashed_landmarks(
            hashed.get(step.id, [])
        ) + scale_mod.anchor_landmarks(workflow.steps)
        pairs = scale_mod.match_landmarks(landmarks, lines, salt_hex=salt)
        transform, reason = scale_mod.estimate_render_transform(
            pairs,
            recorded_viewport=recorded.viewport,
            live_viewport=viewport,
        )
        if transform is None:
            self._render_scale_evidence = {
                "status": "no_estimate",
                "reason": reason,
                "drift": list(verdict.reasons),
            }
            return frame_png
        scaled = scale_mod.render_scaled_backend(self.backend, transform)
        self._render_scale_inner = self.backend
        self.backend = cast(Backend, scaled)
        self._render_scale_evidence = {"status": "normalized", **transform.evidence()}
        # The layout-drift check below now judges the normalized frame, so a
        # font or layout change that is not a pure rescale still stops here.
        return scaled.normalize(frame_png)

    def _layout_drift_error(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Optional[Path],
        workflow: Workflow,
    ) -> Optional[str]:
        """Halt message when the frame drifted from the recorded fingerprint.

        Steps without a recorded fingerprint (older bundles, non-click steps)
        are not checked. Model-free: the advisor, when configured, is called
        only after drift is found and never changes the verdict.
        """
        if bundle_dir is None or not self._layout_drift_on(workflow):
            return None
        from openadapt_flow.runtime import layout_drift as drift_mod

        recorded = self._load_layout_fingerprints(Path(bundle_dir), workflow).get(
            step.id
        )
        if recorded is None:
            return None
        live_profile = self._live_session_profile()
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(frame_png)) as img:
                viewport = (int(img.width), int(img.height))
        except Exception:  # noqa: BLE001 - an undecodable frame is drift
            viewport = (0, 0)
        lines = self.vision.ocr(frame_png)
        tolerances = self.layout_drift_tolerances or drift_mod.Tolerances()
        if self._render_scale_inner is not None and not self._session_profile_changed(
            recorded
        ):
            # A normalized frame is judged label by label: the recorded label
            # set says where each label sat and how large it was, so OCR box
            # jitter on other lines cannot pass for a font change.
            from openadapt_flow.runtime import render_scale as scale_mod

            salt, hashed = drift_mod.load_scale_landmarks(
                self._asset_bytes(
                    Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
                )
            )
            details = (
                scale_mod.check_normalized_frame(
                    hashed.get(step.id, []),
                    lines,
                    salt_hex=salt,
                    text_height_ratio=tolerances.text_height_ratio,
                    landmark_px=tolerances.landmark_px,
                )
                if salt is not None
                else None
            )
            if details:
                return (
                    f"Layout drift before step '{step.id}' ({step.intent}) on "
                    f"the display-scaled session: {'; '.join(details)} — the "
                    "screen no longer matches the recording; refusing to act."
                )
            if details is not None:
                return None
        live = drift_mod.fingerprint(
            lines,
            viewport,
            landmarks=list(recorded.landmarks),
            profile_sha256=(live_profile if isinstance(live_profile, str) else None),
        )
        try:
            drift_mod.check_layout(
                recorded,
                live,
                tolerances=tolerances,
                advisor=self.layout_drift_advisor,
            )
        except drift_mod.LayoutDriftHalt as halt:
            advice = f" Advice: {halt.verdict.advice}" if halt.verdict.advice else ""
            return (
                f"Layout drift before step '{step.id}' ({step.intent}): "
                f"{drift_mod.describe(halt.verdict)} — the screen no longer "
                f"matches the recording; refusing to act.{advice}"
            )
        return None
