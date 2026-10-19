"""Privacy-safe control-overlay contracts shared across OpenAdapt surfaces.

The overlay is a presentation and control surface, not an evidence payload or
an execution authority.  These models deliberately have no free-form runtime
text, screenshots, targets, typed values, identities, URLs, logs, or report
bodies.  Producers keep those values inside their declared evidence boundary.
"""

from __future__ import annotations

from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

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

CONTROL_OVERLAY_FRAME_SCHEMA = "openadapt.control-overlay-frame/v1"
CONTROL_OVERLAY_TIMELINE_SCHEMA = "openadapt.control-overlay-timeline/v1"


class ControlOverlayPhase(str, Enum):
    """Canonical cross-surface execution and terminal states."""

    IDLE = "idle"
    OBSERVING = "observing"
    RECORDING = "recording"
    EXECUTING = "executing"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    STOPPING = "stopping"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    HALTED = "halted"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ControlOverlayMode(str, Enum):
    DEMONSTRATION = "demonstration"
    REPLAY = "replay"
    GOVERNED = "governed"
    MANAGED = "managed"


class ControlOverlayProfile(str, Enum):
    DEMO = "demo"
    STANDARD = "standard"
    REGULATED = "regulated"


class ControlOverlayDataClassification(str, Enum):
    """The only classifications allowed to leave a private evidence boundary."""

    SYNTHETIC = "synthetic"
    SANITIZED_PUBLIC = "sanitized_public"


class ControlOverlayWorkflowLabel(str, Enum):
    """Closed presentation labels; user-authored workflow names are excluded."""

    DEMONSTRATION = "Workflow demonstration"
    REPLAY = "Workflow replay"
    GOVERNED = "Governed workflow"
    MANAGED = "Managed workflow"


CONTROL_OVERLAY_STATUS_BY_PHASE: Mapping[ControlOverlayPhase, str] = MappingProxyType(
    {
        ControlOverlayPhase.IDLE: "Ready",
        ControlOverlayPhase.OBSERVING: "Observing the application",
        ControlOverlayPhase.RECORDING: "Watching your demonstration",
        ControlOverlayPhase.EXECUTING: "Executing with verification gates",
        ControlOverlayPhase.PAUSING: "Pausing at a safe boundary",
        ControlOverlayPhase.PAUSED: "Execution paused",
        ControlOverlayPhase.RESUMING: "Resuming at a safe boundary",
        ControlOverlayPhase.STOPPING: "Stopping at a safe boundary",
        ControlOverlayPhase.VERIFYING: "Verifying the intended result",
        ControlOverlayPhase.VERIFIED: "Outcome verified",
        ControlOverlayPhase.COMPLETED_UNVERIFIED: (
            "Completed without sufficient verification"
        ),
        ControlOverlayPhase.HALTED: "Halted instead of guessing",
        ControlOverlayPhase.FAILED: "Execution failed",
        ControlOverlayPhase.ROLLED_BACK: "Compensating action completed",
    }
)

CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE: Mapping[
    ControlOverlayMode, ControlOverlayWorkflowLabel
] = MappingProxyType(
    {
        ControlOverlayMode.DEMONSTRATION: ControlOverlayWorkflowLabel.DEMONSTRATION,
        ControlOverlayMode.REPLAY: ControlOverlayWorkflowLabel.REPLAY,
        ControlOverlayMode.GOVERNED: ControlOverlayWorkflowLabel.GOVERNED,
        ControlOverlayMode.MANAGED: ControlOverlayWorkflowLabel.MANAGED,
    }
)

CONTROL_OVERLAY_TERMINAL_PHASES = frozenset(
    {
        ControlOverlayPhase.VERIFIED,
        ControlOverlayPhase.COMPLETED_UNVERIFIED,
        ControlOverlayPhase.HALTED,
        ControlOverlayPhase.FAILED,
        ControlOverlayPhase.ROLLED_BACK,
    }
)

CONTROL_OVERLAY_STATE_ID_COMPONENTS = (
    "visibility",
    "phase",
    "mode",
    "profile",
    "step.current",
    "step.total",
    "controls.pause",
    "controls.resume",
    "controls.stop",
)

_CONTROL_OVERLAY_FRAME_JSON_SCHEMA_EXTRA = {
    "x-openadapt-status-by-phase": {
        phase.value: status for phase, status in CONTROL_OVERLAY_STATUS_BY_PHASE.items()
    },
    "x-openadapt-workflow-label-by-mode": {
        mode.value: label.value
        for mode, label in CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE.items()
    },
    "x-openadapt-terminal-phases": sorted(
        phase.value for phase in CONTROL_OVERLAY_TERMINAL_PHASES
    ),
    "x-openadapt-state-id-components": list(CONTROL_OVERLAY_STATE_ID_COMPONENTS),
}


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlOverlayStepV1(_StrictContract):
    current: StrictInt | None = Field(default=None, ge=1)
    total: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_progress(self) -> "ControlOverlayStepV1":
        if (self.current is None) != (self.total is None):
            raise ValueError("step.current and step.total must both be set or null")
        if (
            self.current is not None
            and self.total is not None
            and self.current > self.total
        ):
            raise ValueError("step.current cannot exceed step.total")
        return self


class ControlOverlayControlsV1(_StrictContract):
    pause: StrictBool = False
    resume: StrictBool = False
    stop: StrictBool = False


def control_overlay_state_id(
    *,
    visible: bool,
    phase: ControlOverlayPhase,
    mode: ControlOverlayMode,
    profile: ControlOverlayProfile | None,
    step: ControlOverlayStepV1,
    controls: ControlOverlayControlsV1,
) -> str:
    """Build the semantic identity used for equivalent overlay frames."""

    return ":".join(
        (
            "visible" if visible else "hidden",
            phase.value,
            mode.value,
            profile.value if profile is not None else "no-profile",
            str(step.current) if step.current is not None else "no-step",
            str(step.total) if step.total is not None else "no-total",
            "pause" if controls.pause else "no-pause",
            "resume" if controls.resume else "no-resume",
            "stop" if controls.stop else "no-stop",
        )
    )


class ControlOverlayFrameV1(_StrictContract):
    """A deterministic frame safe for public presentation and composition."""

    model_config = ConfigDict(
        json_schema_extra=_CONTROL_OVERLAY_FRAME_JSON_SCHEMA_EXTRA
    )

    schema_version: Literal["openadapt.control-overlay-frame/v1"] = (
        CONTROL_OVERLAY_FRAME_SCHEMA
    )
    state_id: StrictStr = Field(min_length=1, max_length=256)
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
    presentation: Literal[True] = True

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> "ControlOverlayFrameV1":
        if not isfinite(self.observed_at_monotonic_ms):
            raise ValueError("observed_at_monotonic_ms must be finite")
        expected_label = CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE[self.mode]
        if self.workflow_label != expected_label:
            raise ValueError("workflow_label does not match mode")
        expected_status = CONTROL_OVERLAY_STATUS_BY_PHASE[self.phase]
        if self.status != expected_status:
            raise ValueError("status does not match phase")
        expected_state_id = control_overlay_state_id(
            visible=self.visible,
            phase=self.phase,
            mode=self.mode,
            profile=self.profile,
            step=self.step,
            controls=self.controls,
        )
        if self.state_id != expected_state_id:
            raise ValueError("state_id does not match the semantic frame state")
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
    ) -> "ControlOverlayFrameV1":
        """Build a frame while deriving every presentation string and state ID."""

        step = ControlOverlayStepV1(current=current_step, total=total_steps)
        controls = ControlOverlayControlsV1(
            pause=pause,
            resume=resume,
            stop=stop,
        )
        return cls(
            state_id=control_overlay_state_id(
                visible=visible,
                phase=phase,
                mode=mode,
                profile=profile,
                step=step,
                controls=controls,
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
        )


class ControlOverlayTimelineEventV1(_StrictContract):
    at_ms: StrictInt = Field(ge=0)
    frame: ControlOverlayFrameV1


class ControlOverlayTimelineBindingV1(_StrictContract):
    evidence_pack_id: StrictStr = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,95}$",
    )
    media_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class ControlOverlayTimelineV1(_StrictContract):
    """Presentation frames bound to one exact evidence pack and media clip."""

    schema_version: Literal["openadapt.control-overlay-timeline/v1"] = (
        CONTROL_OVERLAY_TIMELINE_SCHEMA
    )
    data_classification: ControlOverlayDataClassification
    evidence_pack_id: StrictStr = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,95}$",
    )
    media_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    duration_ms: StrictInt = Field(gt=0)
    events: tuple[ControlOverlayTimelineEventV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_event_order(self) -> "ControlOverlayTimelineV1":
        if self.events[0].at_ms != 0:
            raise ValueError("timeline must begin at 0 ms")
        previous_at = -1
        previous_sequence = -1
        previous_monotonic = -1.0
        for event in self.events:
            if event.at_ms <= previous_at or event.at_ms > self.duration_ms:
                raise ValueError("timeline media offsets must increase within duration")
            if event.frame.event_sequence <= previous_sequence:
                raise ValueError(
                    "timeline event_sequence values must strictly increase"
                )
            if event.frame.observed_at_monotonic_ms < previous_monotonic:
                raise ValueError("timeline monotonic timestamps cannot go backwards")
            previous_at = event.at_ms
            previous_sequence = event.frame.event_sequence
            previous_monotonic = event.frame.observed_at_monotonic_ms
        return self

    def assert_binding(self, binding: ControlOverlayTimelineBindingV1) -> None:
        """Refuse composition with a different pack or exact media digest."""

        if self.evidence_pack_id != binding.evidence_pack_id:
            raise ValueError("timeline belongs to a different evidence pack")
        if self.media_sha256 != binding.media_sha256:
            raise ValueError("timeline does not match the exact media digest")

    def frame_at(self, current_time_ms: int | float) -> ControlOverlayFrameV1:
        """Return the latest frame at a bounded media time."""

        if isinstance(current_time_ms, bool) or not isinstance(
            current_time_ms, (int, float)
        ):
            raise TypeError("current_time_ms must be a number")
        if not isfinite(current_time_ms):
            raise ValueError("current_time_ms must be finite")
        bounded_time = min(max(current_time_ms, 0), self.duration_ms)
        low = 0
        high = len(self.events) - 1
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.events[midpoint].at_ms <= bounded_time:
                low = midpoint
            else:
                high = midpoint - 1
        return self.events[low].frame


def build_control_overlay_timeline(
    *,
    data_classification: ControlOverlayDataClassification,
    evidence_pack_id: str,
    media_sha256: str,
    duration_ms: int,
    events: Sequence[ControlOverlayTimelineEventV1],
) -> ControlOverlayTimelineV1:
    """Build and validate an immutable control-overlay timeline."""

    return ControlOverlayTimelineV1(
        data_classification=data_classification,
        evidence_pack_id=evidence_pack_id,
        media_sha256=media_sha256,
        duration_ms=duration_ms,
        events=tuple(events),
    )


def is_terminal_control_overlay_phase(phase: ControlOverlayPhase) -> bool:
    return phase in CONTROL_OVERLAY_TERMINAL_PHASES
