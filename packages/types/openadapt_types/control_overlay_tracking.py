"""Exact, privacy-safe target tracking for control-overlay presentation.

Version 2 is additive: the version 1 control-state and timeline contracts stay
unchanged.  This module carries only source geometry and viewport metadata.
It deliberately excludes selectors, accessibility content, typed values, URLs,
screenshots, identities, report text, and other customer data.

A target rectangle is usable only when its binding matches the current private
observation or the exact decoded frame of the bound media.  Renderers must omit
the rectangle when that match cannot be established.  They must never replay a
selector or infer a target from nearby timeline states.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from hmac import compare_digest
from math import isfinite
from typing import Annotated, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from openadapt_types.control_overlay import (
    CONTROL_OVERLAY_STATUS_BY_PHASE,
    CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE,
    ControlOverlayControlsV1,
    ControlOverlayDataClassification,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayStepV1,
    ControlOverlayWorkflowLabel,
    control_overlay_state_id,
)

CONTROL_OVERLAY_FRAME_V2_SCHEMA = "openadapt.control-overlay-frame/v2"
CONTROL_OVERLAY_TIMELINE_V2_SCHEMA = "openadapt.control-overlay-timeline/v2"

_TARGET_TRACKING_JSON_SCHEMA_EXTRA = {
    "x-openadapt-render-only-on-exact-binding": True,
    "x-openadapt-missing-binding-behavior": "omit_target",
    "x-openadapt-runtime-resolution-replay": False,
    "x-openadapt-renderer-mapping": "actual_content_box",
}

_TARGET_TIMELINE_JSON_SCHEMA_EXTRA = {
    "x-openadapt-target-validity": "exact_decoded_media_frame_only",
    "x-openadapt-missing-frame-behavior": "omit_target",
}


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlOverlayTargetActionKind(str, Enum):
    """Closed, presentation-only kinds for actions with a visible target."""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    SELECT = "select"
    TOGGLE = "toggle"
    INVOKE = "invoke"
    EXPAND_COLLAPSE = "expand_collapse"
    SCROLL = "scroll"
    HOVER = "hover"


class ControlOverlayNormalizedRectV2(_StrictContract):
    """Axis-aligned rectangle normalized to the source top-level viewport."""

    x: StrictFloat = Field(ge=0, le=1)
    y: StrictFloat = Field(ge=0, le=1)
    width: StrictFloat = Field(gt=0, le=1)
    height: StrictFloat = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _validate_extent(self) -> "ControlOverlayNormalizedRectV2":
        values = (self.x, self.y, self.width, self.height)
        if not all(isfinite(value) for value in values):
            raise ValueError("normalized rectangle values must be finite")
        if self.x + self.width > 1 + 1e-9:
            raise ValueError("normalized rectangle exceeds viewport width")
        if self.y + self.height > 1 + 1e-9:
            raise ValueError("normalized rectangle exceeds viewport height")
        return self


class ControlOverlaySourceViewportV2(_StrictContract):
    """Source viewport used when the runtime resolved the target."""

    width_css_px: StrictInt = Field(gt=0, le=32768)
    height_css_px: StrictInt = Field(gt=0, le=32768)
    device_pixel_ratio: StrictFloat = Field(gt=0, le=16)

    @model_validator(mode="after")
    def _validate_finite_dpr(self) -> "ControlOverlaySourceViewportV2":
        if not isfinite(self.device_pixel_ratio):
            raise ValueError("device_pixel_ratio must be finite")
        return self


class ControlOverlayObservationBindingV2(_StrictContract):
    """Opaque binding to one exact private runtime observation.

    The digest must be HMAC-SHA256 over a canonical private observation ID,
    domain-separated for this contract and keyed with a run/export-scoped
    secret.  A raw screenshot, frame, or observation SHA-256 is not valid here:
    it can be linkable across evidence boundaries.
    """

    kind: Literal["observation_hmac_sha256"] = "observation_hmac_sha256"
    observation_hmac_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class ControlOverlayMediaFrameBindingV2(_StrictContract):
    """Binding to one exact decoded frame of one exact immutable media file."""

    kind: Literal["media_frame"] = "media_frame"
    media_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    frame_index: StrictInt = Field(ge=0)


ControlOverlayTargetBindingV2 = Annotated[
    ControlOverlayObservationBindingV2 | ControlOverlayMediaFrameBindingV2,
    Field(discriminator="kind"),
]


class ControlOverlayTargetTrackingV2(_StrictContract):
    """Exact geometry for presentation, never target-resolution evidence."""

    model_config = ConfigDict(json_schema_extra=_TARGET_TRACKING_JSON_SCHEMA_EXTRA)

    coordinate_space: Literal["top_level_viewport_normalized"] = (
        "top_level_viewport_normalized"
    )
    rect: ControlOverlayNormalizedRectV2
    source_viewport: ControlOverlaySourceViewportV2
    binding: ControlOverlayTargetBindingV2
    action_kind: ControlOverlayTargetActionKind | None = None


def _target_digest(target: ControlOverlayTargetTrackingV2 | None) -> str:
    if target is None:
        return "no-target"
    canonical = json.dumps(
        target.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"target-{hashlib.sha256(canonical).hexdigest()[:16]}"


def control_overlay_state_id_v2(
    *,
    visible: bool,
    phase: ControlOverlayPhase,
    mode: ControlOverlayMode,
    profile: ControlOverlayProfile | None,
    step: ControlOverlayStepV1,
    controls: ControlOverlayControlsV1,
    target_tracking: ControlOverlayTargetTrackingV2 | None,
) -> str:
    """Build V2 frame identity from control state and exact tracking state."""

    return ":".join(
        (
            control_overlay_state_id(
                visible=visible,
                phase=phase,
                mode=mode,
                profile=profile,
                step=step,
                controls=controls,
            ),
            _target_digest(target_tracking),
        )
    )


class ControlOverlayFrameV2(_StrictContract):
    """Control state plus optional exactly bound target presentation geometry."""

    schema_version: Literal["openadapt.control-overlay-frame/v2"] = (
        CONTROL_OVERLAY_FRAME_V2_SCHEMA
    )
    state_id: StrictStr = Field(min_length=1, max_length=280)
    event_sequence: StrictInt = Field(ge=0)
    observed_at_unix_ms: StrictInt = Field(ge=0)
    observed_at_monotonic_ms: StrictFloat = Field(ge=0)
    visible: StrictBool
    phase: ControlOverlayPhase
    workflow_label: ControlOverlayWorkflowLabel
    mode: ControlOverlayMode
    profile: ControlOverlayProfile | None = None
    step: ControlOverlayStepV1
    controls: ControlOverlayControlsV1
    status: StrictStr = Field(min_length=1, max_length=64)
    target_tracking: ControlOverlayTargetTrackingV2 | None = None
    presentation: Literal[True] = True

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> "ControlOverlayFrameV2":
        if not isfinite(self.observed_at_monotonic_ms):
            raise ValueError("observed_at_monotonic_ms must be finite")
        if self.workflow_label != CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE[self.mode]:
            raise ValueError("workflow_label does not match mode")
        if self.status != CONTROL_OVERLAY_STATUS_BY_PHASE[self.phase]:
            raise ValueError("status does not match phase")
        expected_state_id = control_overlay_state_id_v2(
            visible=self.visible,
            phase=self.phase,
            mode=self.mode,
            profile=self.profile,
            step=self.step,
            controls=self.controls,
            target_tracking=self.target_tracking,
        )
        if self.state_id != expected_state_id:
            raise ValueError("state_id does not match the semantic V2 frame state")
        return self

    @classmethod
    def build(
        cls,
        *,
        event_sequence: int,
        observed_at_unix_ms: int,
        observed_at_monotonic_ms: float,
        visible: bool,
        phase: ControlOverlayPhase,
        mode: ControlOverlayMode,
        profile: ControlOverlayProfile | None = None,
        current_step: int | None = None,
        total_steps: int | None = None,
        pause: bool = False,
        resume: bool = False,
        stop: bool = False,
        target_tracking: ControlOverlayTargetTrackingV2 | None = None,
    ) -> "ControlOverlayFrameV2":
        """Build a V2 frame without deriving or reconstructing target geometry."""

        step = ControlOverlayStepV1(current=current_step, total=total_steps)
        controls = ControlOverlayControlsV1(
            pause=pause,
            resume=resume,
            stop=stop,
        )
        return cls(
            state_id=control_overlay_state_id_v2(
                visible=visible,
                phase=phase,
                mode=mode,
                profile=profile,
                step=step,
                controls=controls,
                target_tracking=target_tracking,
            ),
            event_sequence=event_sequence,
            observed_at_unix_ms=observed_at_unix_ms,
            observed_at_monotonic_ms=observed_at_monotonic_ms,
            visible=visible,
            phase=phase,
            workflow_label=CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE[mode],
            mode=mode,
            profile=profile,
            step=step,
            controls=controls,
            status=CONTROL_OVERLAY_STATUS_BY_PHASE[phase],
            target_tracking=target_tracking,
        )

    def tracking_for_observation(
        self, observation_hmac_sha256: str
    ) -> ControlOverlayTargetTrackingV2 | None:
        """Return tracking only for an exact private-observation HMAC match."""

        target = self.target_tracking
        if target is None or not isinstance(
            target.binding, ControlOverlayObservationBindingV2
        ):
            return None
        if not compare_digest(
            target.binding.observation_hmac_sha256,
            observation_hmac_sha256,
        ):
            return None
        return target


class ControlOverlayTimelineEventV2(_StrictContract):
    at_ms: StrictInt = Field(ge=0)
    media_frame_index: StrictInt = Field(ge=0)
    frame: ControlOverlayFrameV2


class ControlOverlayTimelineBindingV2(_StrictContract):
    evidence_pack_id: StrictStr = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,95}$",
    )
    media_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    media_frame_count: StrictInt = Field(gt=0)


class ControlOverlayTimelineV2(_StrictContract):
    """Exact runtime frames aligned to decoded frames of immutable media."""

    model_config = ConfigDict(json_schema_extra=_TARGET_TIMELINE_JSON_SCHEMA_EXTRA)

    schema_version: Literal["openadapt.control-overlay-timeline/v2"] = (
        CONTROL_OVERLAY_TIMELINE_V2_SCHEMA
    )
    data_classification: ControlOverlayDataClassification
    evidence_pack_id: StrictStr = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,95}$",
    )
    media_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    media_frame_count: StrictInt = Field(gt=0)
    duration_ms: StrictInt = Field(gt=0)
    events: tuple[ControlOverlayTimelineEventV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_event_order_and_bindings(self) -> "ControlOverlayTimelineV2":
        if self.events[0].at_ms != 0 or self.events[0].media_frame_index != 0:
            raise ValueError("timeline must begin at 0 ms and media frame 0")
        previous_at = -1
        previous_frame_index = -1
        previous_sequence = -1
        previous_monotonic = -1.0
        for event in self.events:
            if event.at_ms <= previous_at or event.at_ms > self.duration_ms:
                raise ValueError("timeline media offsets must increase within duration")
            if (
                event.media_frame_index <= previous_frame_index
                or event.media_frame_index >= self.media_frame_count
            ):
                raise ValueError(
                    "timeline media frame indexes must increase within frame count"
                )
            if event.frame.event_sequence <= previous_sequence:
                raise ValueError(
                    "timeline event_sequence values must strictly increase"
                )
            if event.frame.observed_at_monotonic_ms < previous_monotonic:
                raise ValueError("timeline monotonic timestamps cannot go backwards")
            target = event.frame.target_tracking
            if target is not None:
                if not isinstance(
                    target.binding,
                    ControlOverlayMediaFrameBindingV2,
                ):
                    raise ValueError(
                        "timeline target tracking requires a media-frame binding"
                    )
                binding = target.binding
                if binding.media_sha256 != self.media_sha256:
                    raise ValueError("target tracking does not match timeline media")
                if binding.frame_index != event.media_frame_index:
                    raise ValueError("target tracking does not match timeline frame")
            previous_at = event.at_ms
            previous_frame_index = event.media_frame_index
            previous_sequence = event.frame.event_sequence
            previous_monotonic = event.frame.observed_at_monotonic_ms
        return self

    def assert_binding(self, binding: ControlOverlayTimelineBindingV2) -> None:
        """Refuse composition with different evidence or decoded media."""

        if self.evidence_pack_id != binding.evidence_pack_id:
            raise ValueError("timeline belongs to a different evidence pack")
        if self.media_sha256 != binding.media_sha256:
            raise ValueError("timeline does not match the exact media digest")
        if self.media_frame_count != binding.media_frame_count:
            raise ValueError("timeline does not match the decoded media frame count")

    def event_for_media_frame(
        self, media_frame_index: int
    ) -> ControlOverlayTimelineEventV2 | None:
        """Return only an event bound to the exact decoded media frame."""

        if isinstance(media_frame_index, bool) or not isinstance(
            media_frame_index, int
        ):
            raise TypeError("media_frame_index must be an integer")
        low = 0
        high = len(self.events) - 1
        while low <= high:
            midpoint = (low + high) // 2
            event = self.events[midpoint]
            if event.media_frame_index == media_frame_index:
                return event
            if event.media_frame_index < media_frame_index:
                low = midpoint + 1
            else:
                high = midpoint - 1
        return None

    def tracking_for_media_frame(
        self, media_frame_index: int
    ) -> ControlOverlayTargetTrackingV2 | None:
        """Return tracking only at its exact retained decoded frame."""

        event = self.event_for_media_frame(media_frame_index)
        if event is None:
            return None
        target = event.frame.target_tracking
        if target is None or not isinstance(
            target.binding,
            ControlOverlayMediaFrameBindingV2,
        ):
            return None
        return target


def build_control_overlay_timeline_v2(
    *,
    data_classification: ControlOverlayDataClassification,
    evidence_pack_id: str,
    media_sha256: str,
    media_frame_count: int,
    duration_ms: int,
    events: Sequence[ControlOverlayTimelineEventV2],
) -> ControlOverlayTimelineV2:
    """Build and validate an immutable exactly aligned V2 timeline."""

    return ControlOverlayTimelineV2(
        data_classification=data_classification,
        evidence_pack_id=evidence_pack_id,
        media_sha256=media_sha256,
        media_frame_count=media_frame_count,
        duration_ms=duration_ms,
        events=tuple(events),
    )
