# mypy: disable-error-code="empty-body"
"""Settling and interstitials: wait for a stable screen and handle declared interruptions.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_SettlingAndInterstitialsHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional  # noqa: F401

from openadapt_flow.backend import FreshActuationRequired, StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    Interstitial,
    InterstitialActionResult,
    Resolution,
)
from openadapt_flow.runtime.replay._shared import _frame_viewport
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.runtime.step_timing import timed_span

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Mapping

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import Predicate, Step, StepResult, Workflow
    from openadapt_flow.runtime.authorization import RuntimeParamScalar


class _SettlingAndInterstitialsHost:
    _active_delivery_acknowledgers: tuple[Any, ...]
    _asset_bytes: Any

    @staticmethod
    def _canonical_interstitial_snapshot(
        declarations: Any,
    ) -> tuple[tuple[Interstitial, ...], tuple[str, ...]]: ...

    _deliver_backend_call: Any
    _delivery_authorization_refusal: Any

    @staticmethod
    def _describe_predicate(pred: Predicate) -> str: ...

    _durable_continuation_guard: Optional[Any]
    _governed_asset_mutation: Optional[str]
    _interstitials: Any

    def _predicate_holds(
        self,
        pred: Predicate,
        frame_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Optional[Workflow] = None,
    ) -> bool: ...

    _require_qualification_environment_current: Any
    _runtime_interstitial_snapshot: Any
    _runtime_interstitial_snapshot_error: Optional[str]
    _runtime_interstitial_source: Any
    _settle_seed: Optional[tuple[Any, int]]
    _workflow_interstitial_snapshot: tuple[str, ...]
    _workflow_interstitial_snapshot_error: Optional[str]
    _workflow_interstitials: tuple[Interstitial, ...]
    backend: Backend
    poll_interval_s: float
    settle_readiness_timeout_s: float
    use_structural: bool
    vision: Any


if TYPE_CHECKING:
    _Base = _SettlingAndInterstitialsHost
else:
    _Base = object


class SettlingAndInterstitials(_Base):
    """Settling and interstitials: wait for a stable screen and handle declared interruptions."""

    @timed_span("settle")
    def _settle_seeded(self) -> bytes:
        """``vision.wait_settled`` that can extend the last settled run.

        Same contract as ``vision.wait_settled``: a fresh frame is always
        captured and returned, and it counts as settled only after a run of
        identical frames polled at the normal interval. When the previous
        settle on this backend ended settled and no input was delivered since
        (every input crosses ``_deliver_backend_call``, which bumps the
        epoch), an identical fresh frame extends that already-observed run
        instead of starting a new one, saving one poll interval. A changed
        frame starts over exactly as before.
        """

        seed_entry = getattr(self, "_settle_seed", None)
        self._settle_seed = None
        epoch = getattr(self, "_actuation_epoch", 0)
        seeded = getattr(self.vision, "wait_settled_seeded", None)
        seed_type = getattr(self.vision, "SettleSeed", None)
        if seeded is None or seed_type is None:
            return self.vision.wait_settled(self.backend)
        seed = None
        if seed_entry is not None and seed_entry[1] == epoch:
            seed = seed_entry[0]
        result = seeded(self.backend, seed)
        if result.settled and result.phash:
            seed_kwargs: dict[str, Any] = {}
            if getattr(result, "mode", "pixel") == "page":
                seed_kwargs["png"] = result.png
            self._settle_seed = (
                seed_type(
                    phash=result.phash,
                    stable_frames=result.stable_frames,
                    captured_at=result.captured_at,
                    **seed_kwargs,
                ),
                epoch,
            )
        return result.png

    def _interstitial_declaration_mutation(
        self, workflow: Optional[Workflow]
    ) -> Optional[str]:
        """Return a refusal when caller-owned declarations changed mid-run."""

        snapshot_error = (
            self._runtime_interstitial_snapshot_error
            or self._workflow_interstitial_snapshot_error
        )
        if snapshot_error is not None:
            return f"{snapshot_error} - refusing to emit an action; run aborted."
        try:
            _runtime, runtime_snapshot = self._canonical_interstitial_snapshot(
                self._runtime_interstitial_source
            )
            workflow_snapshot: tuple[str, ...] = ()
            if workflow is not None:
                _workflow, workflow_snapshot = self._canonical_interstitial_snapshot(
                    workflow.interstitials
                )
        except Exception as exc:
            return (
                "Interstitial declarations changed after admission and are no "
                f"longer valid ({type(exc).__name__}) - refusing to emit an "
                "action; run aborted."
            )
        if (
            runtime_snapshot != self._runtime_interstitial_snapshot
            or workflow_snapshot != self._workflow_interstitial_snapshot
        ):
            return (
                "Interstitial declarations changed after admission - refusing "
                "to emit an action under a stale authorization; run aborted."
            )
        return None

    def _resettle_after_interstitial(
        self, before_png: bytes
    ) -> tuple[bytes, Optional[str]]:
        """Require a settled frame after every automatic dismissal.

        Interstitial handling is itself opt-in, so unlike the legacy entry-frame
        behavior there is no backwards-compatibility reason to accept a
        proceed-anyway settle timeout here. A transient blank frame could make a
        ``TEXT_ABSENT`` clearance pass immediately before the overlay returned.
        """
        fn = getattr(self.vision, "wait_settled_result", None)
        if not callable(fn):
            return before_png, (
                "The configured vision facade cannot verify that the screen "
                "settled after an interstitial dismissal - refusing to act; "
                "run aborted."
            )
        try:
            result = fn(
                self.backend,
                interval_s=self.poll_interval_s,
                timeout_s=self.settle_readiness_timeout_s,
            )
            frame = result.png
            settled = result.settled
            if not isinstance(frame, bytes) or not isinstance(settled, bool):
                raise TypeError("invalid settle result")
        except Exception as exc:
            return before_png, (
                "The screen readiness check failed after dismissing an "
                f"interstitial ({type(exc).__name__}) - refusing to act; run "
                "aborted."
            )
        if not settled:
            return frame, (
                "The screen did not settle after the interstitial dismissal "
                f"within {self.settle_readiness_timeout_s:.1f}s - refusing to "
                "verify clearance on a mid-transition frame or act beneath it; "
                "run aborted."
            )
        return frame, None

    @timed_span("postcondition")
    def _handle_interstitials(
        self,
        step: Step,
        before_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        audit_events: list[InterstitialActionResult],
        workflow: Optional[Workflow],
    ) -> tuple[bytes, Optional[str]]:
        """Detect + handle KNOWN interstitials on the entry frame (model-free).

        A detected interstitial with an explicitly reversible,
        non-consequential dismissal (``dismiss_key`` / ``dismiss_anchor``) gets
        one audited action followed by its declared visual clearance check. A
        delivery/clearance failure, persistent detection, or a detected
        interstitial with NO dismissal HALTs the run GRACEFULLY naming it. Returns
        ``(frame, None)`` when the screen is clear, else ``(frame, halt_reason)``.

        Unknown / undeclared overlays are NOT guessed at here: one that covers
        the step's target still HALTs safely downstream (the anchor does not
        resolve). This method only automates the RECURRING, declared ones so
        they do not force a babysit-the-queue halt every time they appear.
        """
        mutation_error = self._interstitial_declaration_mutation(workflow)
        if mutation_error is not None:
            return before_png, mutation_error
        interstitials = [*self._workflow_interstitials, *self._interstitials]
        if not interstitials:
            return before_png, None

        # Revalidate serialized values at the point of use. Pydantic validates
        # construction/load, but models are intentionally mutable; an in-memory
        # edit or model_construct must not bypass affirmative detection, visual
        # clearance, Escape-only, or reversible/non-consequential policy.
        validated: list[Interstitial] = []
        for candidate in interstitials:
            try:
                validated.append(
                    Interstitial.model_validate(candidate.model_dump(mode="python"))
                )
            except Exception as exc:
                return before_png, (
                    f"Interstitial '{candidate.name}' has an invalid automatic "
                    f"dismissal declaration ({type(exc).__name__}) - refusing "
                    "to emit an action; run aborted."
                )
        interstitials = validated

        handled_indices: set[int] = set()
        while True:
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            active_index = next(
                (
                    index
                    for index, candidate in enumerate(interstitials)
                    if self._predicate_holds(
                        candidate.detect,
                        before_png,
                        bundle_dir,
                        params,
                        workflow=workflow,
                    )
                ),
                None,
            )
            # Detection invokes vision and can therefore cross a callback
            # boundary. Recheck the caller-owned declarations before trusting
            # its result or preparing an action.
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            if active_index is None:
                return before_png, None
            it = interstitials[active_index]
            if active_index in handled_indices:
                return before_png, (
                    f"Interstitial '{it.name}' reappeared after a verified "
                    "dismissal in the same step - refusing an automatic retry; "
                    "run aborted."
                )
            if len(handled_indices) >= len(interstitials):
                return before_png, (
                    "The interstitial dismissal bound was exhausted before the "
                    "step entry state became clear - refusing further automatic "
                    "actions; run aborted."
                )
            if it.dismiss_key is None and it.dismiss_anchor is None:
                # A known BLOCKING interstitial with no safe auto-dismissal.
                return before_png, (
                    f"Known blocking interstitial '{it.name}' is on screen "
                    f"({self._describe_predicate(it.detect)}) and declares no "
                    "automatic dismissal - refusing to act beneath it; run "
                    "aborted (handle it, then re-run)."
                )
            if (
                it.risk != "reversible"
                or it.consequential is not False
                or it.clearance is None
                or (it.dismiss_key is not None and it.dismiss_key != "Escape")
            ):
                # Defensive runtime check in addition to Pydantic validation:
                # model_construct/in-memory mutation must never turn this into
                # an ungoverned consequential or unverifiable action.
                return before_png, (
                    f"Interstitial '{it.name}' does not declare an automatic "
                    "dismissal that is reversible, non-consequential, and "
                    "visually verifiable - refusing to emit an action; run "
                    "aborted."
                )

            resolution: Optional[Resolution] = None
            action: Literal["key", "click"]
            if it.dismiss_key is not None:
                action = "key"
            else:
                assert it.dismiss_anchor is not None
                structural_only = (
                    not it.dismiss_anchor.template.strip()
                    and it.dismiss_anchor.structural is not None
                )
                structural = (
                    self.backend
                    if self.use_structural
                    and hasattr(self.backend, "locate_structural")
                    else None
                )
                if structural_only and structural is None:
                    return before_png, (
                        f"Interstitial '{it.name}' requires a structural-only "
                        "dismissal, but this backend cannot perform structural "
                        "resolution - refusing an OCR/geometry/coordinate "
                        "downgrade; run aborted."
                    )
                template_png = self._asset_bytes(
                    bundle_dir,
                    it.dismiss_anchor.template,
                    workflow=workflow,
                )
                if self._governed_asset_mutation is not None:
                    return before_png, self._governed_asset_mutation
                res = resolve(
                    it.dismiss_anchor,
                    before_png,
                    self.vision,
                    None,  # NEVER ground a dismissal: stay model-free
                    it.name,
                    template_png=template_png,
                    viewport=_frame_viewport(before_png),
                    structural=structural,
                )
                if res is None:
                    return before_png, (
                        f"Interstitial '{it.name}' detected "
                        f"({self._describe_predicate(it.detect)}) but its dismiss "
                        "control did not resolve on screen - refusing to act; run "
                        "aborted."
                    )
                resolution, _region = res
                if structural_only and resolution.rung != "structural":
                    return before_png, (
                        f"Interstitial '{it.name}' structural dismissal did not "
                        "resolve uniquely - refusing an OCR/geometry/coordinate "
                        "downgrade; run aborted."
                    )
                action = "click"

            # Resolution can invoke vision or a structural backend.  A callback
            # must not be able to replace the declaration between authorization
            # and the audit-before-delivery boundary.
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            # Append BEFORE delivery: a backend exception can never create an
            # unreported key/click attempt. The event carries the exact admitted
            # policy and expected visual outcome.
            event = InterstitialActionResult(
                interstitial=it.name,
                action=action,
                key=it.dismiss_key,
                expected_clearance=it.clearance,
                resolution=resolution,
                before_frame_sha256=hashlib.sha256(before_png).hexdigest(),
            )
            audit_events.append(event)
            handled_indices.add(active_index)
            if workflow is not None:
                authorization_error = self._delivery_authorization_refusal(
                    workflow, params, step, result, interstitial=True
                )
                if authorization_error is not None:
                    event.error = authorization_error
                    return before_png, authorization_error
            elif self._durable_continuation_guard is not None:
                # Normal replay always supplies ``workflow``. Keep the shared
                # continuation fence fail-closed for direct helper/test use as
                # well: every backend input edge must cross the same lease.
                try:
                    self._durable_continuation_guard.before_delivery()
                    self._active_delivery_acknowledgers = (
                        self._durable_continuation_guard,
                    )
                except Exception as exc:  # noqa: BLE001 - fencing boundary
                    result.failure_category = "continuation_preempted"
                    result.safety_halt = True
                    event.error = (
                        f"durable continuation was preempted before delivery: {exc}"
                    )
                    return before_png, event.error
            try:
                if it.dismiss_key is not None:
                    self._require_qualification_environment_current()
                    self._deliver_backend_call(
                        result, lambda: self.backend.press(it.dismiss_key or "Escape")
                    )
                else:
                    assert resolution is not None
                    assert it.dismiss_anchor is not None
                    if resolution.rung == "structural":
                        native_act = getattr(self.backend, "act_structural", None)
                        structural_anchor = it.dismiss_anchor.structural
                        if (
                            not callable(native_act)
                            or resolution.structural_handle is None
                            or structural_anchor is None
                        ):
                            raise StructuralResolutionRefused(
                                "structural dismissal cannot be delivered "
                                "natively without a stable element handle"
                            )
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result,
                            lambda: native_act(
                                structural_anchor,
                                resolution.structural_handle,
                            ),
                        )
                    else:
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result,
                            lambda: self.backend.click(
                                int(resolution.point[0]), int(resolution.point[1])
                            ),
                        )
                event.delivered = True
            except Exception as exc:
                if not isinstance(
                    exc, (FreshActuationRequired, StructuralResolutionRefused)
                ):
                    target_fingerprint = None
                    if (
                        resolution is not None
                        and resolution.structural_handle is not None
                    ):
                        target_fingerprint = (
                            resolution.structural_handle.target_fingerprint
                        )
                    result.delivery_uncertainty = ActionDeliveryUncertainty(
                        operation="interstitial_dismiss",
                        native=(
                            resolution is not None and resolution.rung == "structural"
                        ),
                        target_fingerprint=target_fingerprint,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        cause_type=type(exc).__name__,
                    )
                event.error = (
                    "dismissal delivery failed "
                    f"({type(exc).__name__}); outcome is unknown"
                )
                return before_png, (
                    f"Interstitial '{it.name}' dismissal delivery failed "
                    f"({type(exc).__name__}) - refusing to act beneath it; run "
                    "aborted."
                )

            before_png, settle_error = self._resettle_after_interstitial(before_png)
            event.after_frame_sha256 = hashlib.sha256(before_png).hexdigest()
            if settle_error is not None:
                event.error = settle_error
                return before_png, settle_error

            clearance_holds = self._predicate_holds(
                it.clearance,
                before_png,
                bundle_dir,
                params,
                workflow=workflow,
            )
            if self._governed_asset_mutation is not None:
                event.clearance_ok = False
                event.error = self._governed_asset_mutation
                return before_png, self._governed_asset_mutation
            still_detected = False
            if clearance_holds:
                still_detected = self._predicate_holds(
                    it.detect,
                    before_png,
                    bundle_dir,
                    params,
                    workflow=workflow,
                )
                if self._governed_asset_mutation is not None:
                    event.clearance_ok = False
                    event.error = self._governed_asset_mutation
                    return before_png, self._governed_asset_mutation
            event.clearance_ok = clearance_holds and not still_detected
            event.ok = event.delivered and event.clearance_ok
            if not event.ok:
                event.error = (
                    "declared visual clearance did not hold"
                    if not clearance_holds
                    else "interstitial remained detected after dismissal"
                )
                return before_png, (
                    f"Interstitial '{it.name}' did not satisfy its declared "
                    "visual clearance after one dismissal - refusing a blind "
                    "retry or action beneath it; run aborted."
                )
