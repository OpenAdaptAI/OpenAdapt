# mypy: disable-error-code="empty-body"
"""Confirming an action: typed input landed, the right option is selected, step guards hold.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_ConfirmingHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional  # noqa: F401

from openadapt_flow.ir import ActionKind
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.authorization import runtime_params_for_gui
from openadapt_flow.runtime.program_predicates import evaluate_program_predicate
from openadapt_flow.runtime.replay._shared import (
    _PAGE_SCALE_BAND,
    FIELD_REGION_PAD,
    FIELD_REGION_SIZE,
    MASKED_MIN_CONFIDENCE,
    MASKED_NEW_TEXT_SLACK,
    MASKED_REPEAT_FRACTION,
    PC_STRUCTURAL_TEMPLATE_THRESHOLD,
    PC_TEMPLATE_SEARCH_PAD,
    PC_TEMPLATE_THRESHOLD,
    _frame_viewport,
    _uniform_page_scale,
)
from openadapt_flow.runtime.resolver import pad_region
from openadapt_flow.runtime.step_timing import timed_span

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Mapping

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import (
        Point,
        Predicate,
        Region,
        Resolution,
        Step,
        StepResult,
        Workflow,
    )
    from openadapt_flow.runtime.authorization import RuntimeParamScalar


class _ConfirmingHost:
    _asset_bytes: Any
    _cancel_guarded_keyboard: Any
    _deliver_backend_call: Any
    _delivery_authorization_refusal: Any

    @staticmethod
    def _describe_postcondition(pc: Any) -> str: ...
    @staticmethod
    def _describe_predicate(pred: Predicate) -> str: ...

    _governed_asset_mutation: Optional[str]
    _handle_interstitials: Any
    _mapped_selection_readback_region: Any
    _pc_page_scale: Any
    _pc_scaled_match: Any

    def _poll_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
    ) -> tuple[bool, bytes]: ...
    @staticmethod
    def _postcondition_confirms_a_write(
        step: Step, workflow: Optional[Workflow]
    ) -> bool: ...
    def _postcondition_passes(
        self,
        pc: Any,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: Optional[dict[str, Any]] = None,
    ) -> bool: ...
    def _profile_runtime_refusal(self, workflow: Workflow) -> Optional[str]: ...

    _require_qualification_environment_current: Any

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

    _revalidate_consequential_actuation: Any
    _settle_seeded: Any
    _step_needs_consequential_revalidation: Any

    def _wait_starting_state_settled(
        self, before_png: bytes
    ) -> tuple[bytes, Optional[str], Optional[bool]]: ...

    backend: Backend
    poll_interval_s: float
    state_verifier: Optional[Any]
    vision: Any


if TYPE_CHECKING:
    _Base = _ConfirmingHost
else:
    _Base = object


class Confirming(_Base):
    """Confirming an action: typed input landed, the right option is selected, step guards hold."""

    def _apply_step_gates(
        self,
        step: Step,
        before_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        workflow: Optional[Workflow] = None,
    ) -> tuple[bool, Optional[str], bytes]:
        """Evaluate readiness, interstitials, guard, then wait_until before acting.

        Runs in state-dependency order, all model-free:

        1. **readiness** (``require_settled``) - refuse to act on a frame that
           never settled (still-loading / mid-transition); HALT gracefully.
        2. **interstitials** - a KNOWN reversible, non-consequential overlay on
           the entry frame gets one audited dismissal plus declared clearance
           verification, or HALTs before the workflow action.
        3. **guard** (precondition) on the entry frame - unmet HALTs (default)
           or SKIPs (``on_unmet="skip"``).
        4. **wait_until** (readiness predicate) - polled, bounded, HALT on
           timeout (never proceed-anyway).

        Returns ``(proceed, error, before_png)``:

        - ``(True, None, frame)`` - proceed to resolve/act (frame may have been
          re-settled while polling a wait_until predicate).
        - ``(False, None, frame)`` - the guard was unmet with ``on_unmet="skip"``:
          the step is a no-op success (caller marks it ``skipped``).
        - ``(False, error, frame)`` - HALT: an un-ready screen, a blocking /
          undismissable interstitial, an unmet ``halt`` guard, or a
          ``wait_until`` predicate that never held within its bound (fail-safe;
          the run NEVER proceeds-anyway on an unmet readiness predicate).

        Model-free: predicates are evaluated by :meth:`_predicate_holds`.
        """
        if workflow is not None:
            profile_refusal = self._profile_runtime_refusal(workflow)
            if profile_refusal is not None:
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                return (
                    False,
                    f"{profile_refusal} at the actuation boundary — refusing "
                    "to act; run aborted",
                    before_png,
                )
        # (1) Readiness: never act on a frame that never settled.
        before_png, readiness_error, settled = self._wait_starting_state_settled(
            before_png
        )
        result.starting_state_settled = settled
        if readiness_error is not None:
            # Like an interstitial refusal, a not-ready starting state is a
            # safety halt. A program on_exception edge must not convert the
            # explicit refusal into a successful terminal outcome.
            result.safety_halt = True
            return False, readiness_error, before_png

        # (2) Interstitials: dismiss a KNOWN overlay, or HALT gracefully.
        before_png, interstitial_error = self._handle_interstitials(
            step,
            before_png,
            bundle_dir,
            params,
            result,
            result.interstitial_actions,
            workflow,
        )
        if interstitial_error is not None:
            # An interstitial refusal is a state-safety halt, not an ordinary
            # workflow exception a program on_exception edge may turn into a
            # successful outcome after an uncertain/blocked pre-action state.
            result.safety_halt = True
            return False, interstitial_error, before_png

        # Guard (precondition) on the entry frame.
        guard_holds = step.guard is None or self._predicate_holds(
            step.guard.predicate,
            before_png,
            bundle_dir,
            params,
            workflow=workflow,
        )
        if self._governed_asset_mutation is not None:
            return False, self._governed_asset_mutation, before_png
        if not guard_holds:
            assert step.guard is not None
            if step.guard.on_unmet == "skip":
                return False, None, before_png
            return (
                False,
                (
                    f"Guard precondition for step '{step.id}' ({step.intent}) is "
                    f"unmet on the current screen "
                    f"({self._describe_predicate(step.guard.predicate)}) — "
                    "refusing to act; run aborted"
                ),
                before_png,
            )

        # wait_until (readiness). SCROLL consumes its own predicate as the stop
        # condition of its closed loop (_act_scroll), so it is skipped here.
        if step.wait_until is not None and step.action is not ActionKind.SCROLL:
            pred = step.wait_until
            deadline = time.monotonic() + pred.timeout_s
            while True:
                if self._predicate_holds(
                    pred,
                    before_png,
                    bundle_dir,
                    params,
                    workflow=workflow,
                ):
                    return True, None, before_png
                if time.monotonic() >= deadline:
                    return (
                        False,
                        (
                            f"wait_until predicate for step '{step.id}' "
                            f"({step.intent}) did not hold within "
                            f"{pred.timeout_s:.1f}s "
                            f"({self._describe_predicate(pred)}) — readiness never "
                            "reached; refusing to proceed-anyway; run aborted"
                        ),
                        before_png,
                    )
                time.sleep(self.poll_interval_s)
                before_png = self.vision.wait_settled(self.backend)

        return True, None, before_png

    def _predicate_holds(
        self,
        pred: Predicate,
        frame_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Optional[Workflow] = None,
    ) -> bool:
        """Evaluate a Predicate against the current frame / run params.

        Deterministic and ZERO model calls (the $0 runtime guarantee):
        ``anchor_resolves`` runs the resolution ladder with NO grounder,
        ``text_present`` / ``text_absent`` use the tolerant OCR presence check,
        ``param_equals`` is a string compare, and ``and`` / ``or`` / ``not``
        compose. An unknown kind fails safe (does not hold).
        """
        return evaluate_program_predicate(
            pred,
            frame_png,
            runtime_params_for_gui(params),
            vision=self.vision,
            viewport=_frame_viewport(frame_png),
            asset_loader=lambda rel: self._asset_bytes(
                bundle_dir,
                rel,
                workflow=workflow,
            ),
        )

    def _field_region(
        self,
        field_point: Optional[Point],
        structural_region: Optional[Region] = None,
    ) -> Optional[Region]:
        """Region to observe for typed input, or None for the whole frame."""
        vw, vh = self.backend.viewport
        if structural_region is not None:
            sx, sy, sw, sh = structural_region
            x = max(0, sx - FIELD_REGION_PAD)
            y = max(0, sy - FIELD_REGION_PAD)
            right = min(vw, sx + sw + FIELD_REGION_PAD)
            bottom = min(vh, sy + sh + FIELD_REGION_PAD)
            if right > x and bottom > y:
                return (x, y, right - x, bottom - y)
        if field_point is None:
            return None
        w = min(FIELD_REGION_SIZE[0], vw)
        h = min(FIELD_REGION_SIZE[1], vh)
        x = min(max(0, field_point[0] - w // 2), max(0, vw - w))
        y = min(max(0, field_point[1] - h // 2), max(0, vh - h))
        return (x, y, w, h)

    def _ocr_squashed(self, png: bytes, region: Optional[Region]) -> str:
        """Squashed OCR text of a (region of a) frame."""
        lines = self.vision.ocr(png, region=region)
        return identity_mod.squash(" ".join(line.text for line in lines))

    def _readable_chars(self, png: bytes, region: Optional[Region]) -> int:
        """Confidently readable alphanumeric characters in a region.

        The masked-acceptance metric (see ``MASKED_NEW_TEXT_SLACK``):
        password dots OCR as nothing, low-confidence noise, punctuation
        runs, or — on some platform renderers — confident homogeneous
        digit runs, while a dialog adds confident words. Counting only
        alphanumeric characters of confident, non-homogeneous lines is
        also invariant to OCR merging or splitting the same text into
        different boxes between frames.
        """
        total = 0
        for line in self.vision.ocr(png, region=region):
            if getattr(line, "confidence", 1.0) < MASKED_MIN_CONFIDENCE:
                continue
            alnum = [ch for ch in line.text if ch.isalnum()]
            if not alnum:
                continue
            most_common = max(alnum.count(ch) for ch in set(alnum))
            if len(alnum) >= 4 and most_common / len(alnum) >= MASKED_REPEAT_FRACTION:
                continue  # homogeneous glyph run: masked-dot misread
            total += len(alnum)
        return total

    def _text_value_at(self, field_point: Optional[Point]) -> Optional[str]:
        """Read an exact editable value when the backend can expose one.

        ``None`` means the substrate cannot make this observation. Empty text
        is returned as ``""`` and remains distinguishable from unavailable.
        Exceptions fail closed into the visual verifier rather than escaping.
        """
        reader = getattr(
            self.backend,
            "text_value_at" if field_point is not None else "focused_text_value",
            None,
        )
        if not callable(reader):
            return None
        try:
            value = reader(*field_point) if field_point is not None else reader()
        except Exception:
            return None
        return value if isinstance(value, str) else None

    def _typed_input_landed(
        self,
        text: str,
        field_point: Optional[Point],
        baseline_png: bytes,
        *,
        field_region: Optional[Region] = None,
        baseline_field_value: Optional[str] = None,
        allow_masked: bool = False,
    ) -> tuple[bool, bool]:
        """Did the just-typed ``text`` visibly land?

        Exact structural readback is authoritative when the backend exposes
        it. Otherwise, for an OCR-able value (>=
        ``identity.MIN_PARAM_CHARS`` squashed chars), a contiguous squashed
        run of the value (retried at 2x resolution) must be readable in the
        field region. Pixel-only acceptance is restricted to steps declared
        ``secret``: masked password dots may render no readable text. A dialog
        painting over the region or an unreadable ordinary field must never
        count as "input landed". Values too short for OCR to arbitrate retain
        the legacy diff fallback.

        Returns:
            ``(landed, changed)`` — the verdict, and whether the field
            region's pixels changed at all (the caller's retry decision:
            retyping is only safe when nothing changed).
        """
        after_png = self._settle_seeded()
        region = self._field_region(field_point, field_region)
        if baseline_field_value is not None:
            expected = baseline_field_value + text
            after_at_point = (
                self._text_value_at(field_point) if field_point is not None else None
            )
            after_focused = self._text_value_at(None)

            # Focus-bound readback is the strongest confirmation. A full-page
            # cover can hide the field from hit-testing while leaving that
            # exact editable element focused, so focused readback also
            # preserves the intended safe-halt-at-the-next-action behavior.
            if after_focused == expected:
                return True, expected != baseline_field_value

            # If hit-testing can read the value but focus moved elsewhere, an
            # interstitial/dialog stole focus after delivery. That is an
            # unknown state, not a success: the value could belong to an
            # obscured/replaced control.
            if after_at_point == expected:
                return False, True

            observed = [
                value for value in (after_at_point, after_focused) if value is not None
            ]
            if observed and all(value == baseline_field_value for value in observed):
                return False, False
            if observed:
                # A partial/different value is an unknown state and must halt
                # without destructive select-all.
                return False, True
            # A structurally readable field disappearing after delivery means
            # it was covered, replaced, or navigated away. Do not downgrade to
            # OCR, which could read the intended text elsewhere on the screen.
            return False, True
        changed = self.vision.pixels_changed(baseline_png, after_png, region=region)
        # Dropdown: typing selects the option whose label matches. When the
        # focused <select> at the field point now shows exactly the typed
        # label (whitespace-squashed, case-insensitive), the input landed.
        # Any other answer falls through to the visual check, unchanged.
        if field_point is not None and not allow_masked:
            reader = getattr(self.backend, "focused_select_label_at", None)
            label: Optional[str] = None
            if callable(reader):
                try:
                    label = reader(int(field_point[0]), int(field_point[1]))
                except Exception:
                    label = None
            if (
                isinstance(label, str)
                and identity_mod.squash(text)
                and identity_mod.squash(label).casefold()
                == identity_mod.squash(text).casefold()
            ):
                return True, True
        needle = identity_mod.squash(text)
        if len(needle) < identity_mod.MIN_PARAM_CHARS:
            return changed, changed  # too short for OCR to arbitrate
        need = identity_mod.required_run(len(needle))
        after_hay = self._ocr_squashed(after_png, region)
        if identity_mod.longest_run(needle, after_hay) >= need:
            return True, changed
        if region is not None:
            upscaled = identity_mod.upscale_crop(after_png, region)
            if upscaled is not None:
                up_hay = self._ocr_squashed(upscaled, None)
                if identity_mod.longest_run(needle, up_hay) >= need:
                    return True, changed
        if not changed:
            return False, False
        # Pixels changed but the value is unreadable: masked rendering is
        # the only acceptable explanation, and masked rendering adds no
        # confidently readable alphanumeric text (dot glyphs OCR as
        # nothing, noise, or punctuation — platform-dependent). Anything
        # else (a dialog over the field, another widget's text) must fail
        # the verdict.
        if not allow_masked:
            return False, changed
        landed = (
            self._readable_chars(after_png, region)
            <= self._readable_chars(baseline_png, region) + MASKED_NEW_TEXT_SLACK
        )
        return landed, changed

    @timed_span("postcondition")
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
    ) -> Optional[str]:
        """Verify a TYPE action landed; one guarded refocus-and-retype retry.

        The retry only fires when exact readback or pixels prove the first
        attempt changed NOTHING in the field (keystrokes fell on a
        non-rendering target): re-click
        the field (when its point is known), select-all so a false-negative
        first attempt is replaced rather than duplicated, retype. When the
        region DID change but the value cannot be read (a dialog over the
        field, an unexpected re-render), retyping is not safe — select-all
        could destroy pre-existing field content and the re-click could
        re-fire whatever now sits at that point — so the run halts
        immediately with the accurate reason. Typed input that cannot be
        confirmed must never be reported as success (VALIDATION.md 'focus
        stolen' finding).
        """
        landed, changed = self._typed_input_landed(
            text,
            field_point,
            baseline_png,
            field_region=field_region,
            baseline_field_value=baseline_field_value,
            allow_masked=allow_masked,
        )
        if landed:
            result.input_verified = True
            return None
        if changed:
            result.input_verified = False
            return (
                f"Typed input could not be verified for step '{step.id}' "
                f"({step.intent}): the field region changed but the typed "
                "value is not readable there (something else rendered over "
                "or instead of the input) — retyping is unsafe in this "
                "state; run aborted"
            )
        if not allow_retry:
            result.input_verified = False
            return (
                f"Typed input could not be verified for step '{step.id}' "
                f"({step.intent}): the identity-bound browser field showed no "
                "confirmed change; refusing an unleased refocus/retype retry"
            )
        result.input_retried = True
        retry_resolution = resolution
        retry_region = field_region
        retry_frame = baseline_png
        needs_revalidation = self._step_needs_consequential_revalidation(step, workflow)
        if field_point is not None:
            if needs_revalidation:
                (
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    retry_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                )
                if retry_error is not None:
                    result.input_verified = False
                    return retry_error
                if retry_resolution is None:
                    result.input_verified = False
                    return (
                        f"Typed input retry for step '{step.id}' ({step.intent}) "
                        "could not freshly resolve its field; run aborted"
                    )
                field_point = retry_resolution.point
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                result.input_verified = False
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.click(*field_point),
            )
            # Replace, don't append: if the first attempt DID land but was
            # not visible to the diff/OCR, retyping raw would double it.
            if needs_revalidation:
                (
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    retry_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                    arm_keyboard=True,
                )
                if retry_error is not None:
                    result.input_verified = False
                    return retry_error
                if retry_resolution is None or retry_resolution.point != field_point:
                    self._cancel_guarded_keyboard()
                    result.input_verified = False
                    return (
                        f"Typed input retry for step '{step.id}' ({step.intent}) "
                        "resolved to a different field after refocus; run aborted"
                    )
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                result.input_verified = False
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.press("ControlOrMeta+a"),
            )

        # The refocus click and select-all key each consume an opaque remote
        # lease.  Acquire and validate one more exact frame for the retype edge.
        if needs_revalidation:
            (
                retry_resolution,
                retry_region,
                retry_baseline,
                retry_error,
            ) = self._revalidate_consequential_actuation(
                step,
                retry_resolution,
                retry_region,
                retry_frame,
                params,
                workflow,
                bundle_dir,
                result,
                run_dir=run_dir,
                arm_keyboard=True,
            )
            if retry_error is not None:
                result.input_verified = False
                return retry_error
        else:
            retry_baseline = self.backend.screenshot()
        if (
            needs_revalidation
            and field_point is not None
            and (retry_resolution is None or retry_resolution.point != field_point)
        ):
            self._cancel_guarded_keyboard()
            result.input_verified = False
            return (
                f"Typed input retry for step '{step.id}' ({step.intent}) "
                "resolved to a different field before retyping; run aborted"
            )
        refusal = self._delivery_authorization_refusal(workflow, params, step, result)
        if refusal is not None:
            result.input_verified = False
            return refusal
        self._require_qualification_environment_current()
        self._deliver_backend_call(result, lambda: self.backend.type_text(text))
        landed, _changed = self._typed_input_landed(
            text,
            field_point,
            retry_baseline,
            field_region=retry_region,
            # Select-all means the retry's expected exact value is ``text``
            # regardless of what the field contained before replacement.
            baseline_field_value="" if baseline_field_value is not None else None,
            allow_masked=allow_masked,
        )
        if landed:
            result.input_verified = True
            return None
        result.input_verified = False
        return (
            f"Typed input could not be verified for step '{step.id}' "
            f"({step.intent}): the screen did not change where the text "
            "should have appeared and OCR could not find the typed value, "
            "after one refocus-and-retype retry — keystrokes likely fell on "
            "a non-input target (focus lost); run aborted"
        )

    @timed_span("postcondition")
    def _verify_selected_option(
        self,
        step: Step,
        text: str,
        after_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
        result: StepResult,
    ) -> Optional[str]:
        """Require the option value on the final settled, re-resolved field.

        Unlike ordinary TYPE verification, selection never accepts pixel
        change alone and never retries: the commit key may already have changed
        the control. Structural readback must equal the intended option, or OCR
        must recover one complete exactly normalized line inside only the final
        live field region. The compiled region qualifies geometry but never
        supplies runtime coordinates. This method does not settle again: its
        caller passes the one final frame used by all post-action checks.
        """

        qualified_readback = step.selection_region
        qualified_target = step.anchor.region if step.anchor is not None else None
        if qualified_readback is None or qualified_target is None:
            result.input_verified = False
            return f"Option selection for step '{step.id}' has no field region"
        resolution, live_region, resolve_error = self._resolve_step(
            step,
            after_png,
            bundle_dir,
            workflow,
            # Final option verification is exact retained-evidence work. A
            # model may propose a governed repair after a halt, but it cannot
            # supply the target region that certifies the committed value.
            allow_grounder=False,
        )
        if (
            resolve_error is not None
            or resolution is None
            or not self._selection_region_compatible(qualified_target, live_region)
        ):
            result.input_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            return (
                f"Selected option field could not be re-resolved with compatible "
                f"geometry on the final settled frame for step '{step.id}' "
                f"({step.intent}); the run halted"
            )
        assert live_region is not None
        live_readback = self._mapped_selection_readback_region(
            qualified_target,
            qualified_readback,
            live_region,
            after_png,
        )
        if live_readback is None:
            result.input_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            return (
                f"Selected option readback region could not be mapped onto the "
                f"final settled frame for step '{step.id}' ({step.intent}); the "
                "run halted"
            )
        exact = self._text_value_at(resolution.point)
        if exact is not None:
            verified = identity_mod.squash(exact) == identity_mod.squash(text)
        else:
            needle = identity_mod.squash(text)
            verified = any(
                getattr(line, "confidence", 1.0) >= 0.5
                and identity_mod.squash(line.text) == needle
                for line in self.vision.ocr(after_png, region=live_readback)
            )
            if not verified:
                upscaled = identity_mod.upscale_crop(after_png, live_readback)
                if upscaled is not None:
                    verified = any(
                        getattr(line, "confidence", 1.0) >= 0.5
                        and identity_mod.squash(line.text) == needle
                        for line in self.vision.ocr(upscaled, region=None)
                    )
        result.input_verified = verified
        if verified:
            return None
        result.safety_halt = True
        result.failure_category = "governed_refusal"
        return (
            f"Selected option could not be verified for step '{step.id}' "
            f"({step.intent}) inside its exact live re-resolved field region; the "
            "commit may have selected a different value, so the run halted"
        )

    @staticmethod
    def _selection_region_compatible(
        qualified: Region,
        live: Optional[Region],
    ) -> bool:
        """Check live field shape without treating recorded coordinates as truth.

        Remote drift may translate the field, so x/y intentionally do not
        participate. A resolution rung that returns no region, flips the field
        orientation, or changes either dimension by more than 25% cannot safely
        inherit the recorded option-selection contract.
        """

        if live is None:
            return False
        _, _, qualified_w, qualified_h = qualified
        _, _, live_w, live_h = live
        if min(qualified_w, qualified_h, live_w, live_h) <= 0:
            return False
        if (qualified_w >= qualified_h) != (live_w >= live_h):
            return False
        return (
            0.75 <= live_w / qualified_w <= 1.25
            and 0.75 <= live_h / qualified_h <= 1.25
        )

    @staticmethod
    def _selection_target_continuous(
        qualified: Region,
        focused: Point,
        refreshed: Point,
    ) -> bool:
        """Require both select phases to name the same focused field.

        Reuse the selection contract's 25% size tolerance for point drift.
        This permits small OCR/template jitter but refuses a second control.
        """

        _, _, qualified_w, qualified_h = qualified
        if qualified_w <= 0 or qualified_h <= 0:
            return False
        return (
            abs(refreshed[0] - focused[0]) <= qualified_w * 0.25
            and abs(refreshed[1] - focused[1]) <= qualified_h * 0.25
        )

    def _structural_state(self) -> dict[str, Any]:
        """Structural observations the backend can provide right now.

        Backends MAY expose ``url`` / ``page_title`` / ``page_count`` (see
        ``openadapt_flow.backend.StructuralBackend``). Missing observations
        are simply absent from the dict.
        """
        state: dict[str, Any] = {}
        for key in ("url", "page_title", "page_count"):
            try:
                value = getattr(self.backend, key, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        return state

    def _structural_changed(
        self, key: str, start_state: dict[str, Any]
    ) -> Optional[bool]:
        """Whether structural observation ``key`` differs from step start.

        Returns None when the observation is unavailable on either side —
        the caller treats that as an honestly-unverifiable pass (a bundle
        recorded on a structural backend may replay on one that is not;
        see docs/LIMITS.md).
        """
        try:
            current = getattr(self.backend, key, None)
        except Exception:
            current = None
        start = start_state.get(key)
        if current is None or start is None:
            return None
        return current != start

    @timed_span("postcondition")
    def _check_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
        result: Optional[StepResult] = None,
        *,
        workflow: Optional[Workflow] = None,
    ) -> tuple[bool, bytes, list[str]]:
        """Poll postconditions until each passes or times out.

        Each postcondition is polled (fresh screenshots) up to its own
        ``timeout_s``. If any fails, the screen is re-settled once and all
        postconditions are re-checked a single time. If a VLM state-verifier
        is configured, a render-drift-sensitive postcondition that still failed
        gets one last drift-oracle pass (see :meth:`_drift_oracle_rescue`),
        but never on a step that can leave a business effect behind (see
        :meth:`_postcondition_confirms_a_write`). ``workflow`` lets a qualified
        operator risk decision count; without it the classification is the
        stricter step-only one.

        Returns:
            (ok, last_frame, failed) — the frame the final verdict was based
            on, plus human-readable descriptions of the postconditions that
            failed the final check (empty when ok).
        """
        self._pc_page_scale = _uniform_page_scale(step, result)
        self._pc_scaled_match = False
        try:
            ok, frame_png = self._poll_postconditions(
                step, frame_png, bundle_dir, start_state
            )
            if ok:
                self._note_page_scale(result)
                return True, frame_png, []
            # One re-settle retry.
            frame_png = self.vision.wait_settled(self.backend)
            failed_pcs = [
                pc
                for pc in step.expect
                if not self._postcondition_passes(
                    pc, frame_png, bundle_dir, start_state
                )
            ]
            if not failed_pcs:
                self._note_page_scale(result)
        finally:
            self._pc_page_scale = None
        if (
            failed_pcs
            and self.state_verifier is not None
            and not self._postcondition_confirms_a_write(step, workflow)
        ):
            failed_pcs = self._drift_oracle_rescue(failed_pcs, frame_png, result)
        failed = [self._describe_postcondition(pc) for pc in failed_pcs]
        return not failed_pcs, frame_png, failed

    def _note_page_scale(self, result: Optional[StepResult]) -> None:
        """Record on ``result`` the page scale a passing check needed."""

        scale = getattr(self, "_pc_page_scale", None)
        if result is not None and scale is not None and self._pc_scaled_match:
            result.postcondition_page_scale = scale

    def _drift_oracle_rescue(
        self,
        failed_pcs: list[Any],
        frame_png: bytes,
        result: Optional[StepResult],
    ) -> list[Any]:
        """Give each deterministically-failed, render-drift-sensitive
        postcondition one confirmation pass through the VLM state-verifier.

        VETO-SAFE by construction:

        * The caller never invokes this on an irreversible or effect-bearing
          step (see :meth:`_postcondition_confirms_a_write`).
        * Only ``text_present`` and ``region_stable`` are eligible — the kinds
          a theme/scale/font re-render can legitimately break. Structural
          postconditions (``url_changed`` etc.) and ``text_absent`` are NEVER
          rescued: those failures are real, not render drift.
        * A postcondition is rescued ONLY on a confident ``"yes"``; ``"no"``,
          ``"uncertain"``, and any appliance outage keep it failed (halt).
        * Every call and every rescue is recorded on ``result`` for the report
          (and counted as a model call), so a rescue is auditable, never silent.

        Returns the postconditions that remain failed after the pass.
        """
        assert self.state_verifier is not None  # guaranteed by the caller (line ~2618)
        survivors: list[Any] = []
        for pc in failed_pcs:
            expected = self._expected_state_text(pc)
            if expected is None:  # not a drift-rescuable kind
                survivors.append(pc)
                continue
            if result is not None:
                result.drift_oracle_calls += 1
            try:
                confirmed = self.state_verifier.holds(frame_png, expected)
            except Exception:
                confirmed = False  # fail-safe: any error keeps the halt
            if confirmed:
                if result is not None:
                    result.postcondition_drift_rescues.append(
                        self._describe_postcondition(pc)
                    )
            else:
                survivors.append(pc)
        return survivors

    @staticmethod
    def _expected_state_text(pc: Any) -> Optional[str]:
        """A natural-language expected-state for the drift-oracle, or None if
        the postcondition kind is not render-drift-rescuable."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind == "text_present" and pc.text:
            return f"the text {pc.text!r} is visible on the screen"
        if kind == "region_stable":
            return "the content recorded in the highlighted region is present"
        return None

    def _scaled_region_match(
        self, frame_png: bytes, template_png: bytes, region: tuple[int, ...]
    ) -> bool:
        """The recorded content, at the page scale this step's click showed.

        A whole-page zoom (browser zoom, or an app's own scale) grows every
        element and moves it away from the top-left corner by the same
        factor, so the recorded region falls outside the search window and
        its content outside the matcher's scale band. When this step's own
        click landed at the recorded point times one uniform factor
        (:func:`_uniform_page_scale`), look for the same template at that
        factor (within one percent), at the recorded region times that
        factor. Thresholds are the unscaled check's: a modal, a missing panel or another layout still
        fails.
        """

        scale = getattr(self, "_pc_page_scale", None)
        if scale is None:
            return False
        x, y, w, h = region
        scaled = (
            int(round(x * scale)),
            int(round(y * scale)),
            int(round(w * scale)),
            int(round(h * scale)),
        )
        search = pad_region(scaled, PC_TEMPLATE_SEARCH_PAD, _frame_viewport(frame_png))
        matchers = [
            (self.vision.find_template, PC_TEMPLATE_THRESHOLD),
            (
                getattr(self.vision, "find_structural_template", None),
                PC_STRUCTURAL_TEMPLATE_THRESHOLD,
            ),
        ]
        for matcher, threshold in matchers:
            if not callable(matcher):
                continue
            try:
                match = matcher(
                    frame_png,
                    template_png,
                    search_region=search,
                    scales=tuple(round(scale * f, 4) for f in _PAGE_SCALE_BAND),
                    threshold=threshold,
                )
            except TypeError:  # a vision stand-in with no scale band
                return False
            if match is not None:
                self._pc_scaled_match = True
                return True
        return False

    def _dom_text_visible(self, text: str) -> bool:
        """True only when the backend proves ``text`` is visibly rendered.

        Optional backend seam ``visible_text_present`` (browser DOM). Any
        other answer, a missing seam, or an error is "not proven" -> False,
        so the caller keeps its visual check.
        """
        reader = getattr(self.backend, "visible_text_present", None)
        if not callable(reader):
            return False
        try:
            return reader(text) is True
        except Exception:
            return False

    def _postcondition_template(self, pc: Any, bundle_dir: Path) -> Optional[bytes]:
        """Bytes of a REGION_STABLE postcondition's template crop, if any."""
        rel = getattr(pc, "template", None)
        if not rel:
            return None
        return self._asset_bytes(bundle_dir, rel)
