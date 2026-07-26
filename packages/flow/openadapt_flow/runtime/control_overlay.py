"""Exact, presentation-only runtime events for out-of-process overlays.

The target application's DOM/accessibility tree is not an overlay surface.
This module emits the canonical, PHI-safe ``openadapt-types`` contract for a
native Desktop overlay, a sibling hosted-stream layer, or deterministic media
composition.  It carries no selectors, target text, URLs, screenshots, report
bodies, or execution authority.

``openadapt-types`` remains an optional interoperability dependency.  Import
this module through the ``interop`` extra when an overlay consumer is present;
ordinary Flow replay does not require it.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from collections.abc import Callable, Sequence
from types import MappingProxyType
from typing import Literal, TypeAlias

from openadapt_types import (
    ControlOverlayDataClassification,
    ControlOverlayFrameV2,
    ControlOverlayMediaFrameBindingV2,
    ControlOverlayMode,
    ControlOverlayNormalizedRectV2,
    ControlOverlayObservationBindingV2,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlaySourceViewportV2,
    ControlOverlayTargetActionKind,
    ControlOverlayTargetTrackingV2,
    ControlOverlayTimelineEventV2,
    ControlOverlayTimelineV2,
    build_control_overlay_timeline_v2,
)

from openadapt_flow.ir import Region

ExecutionOutcome: TypeAlias = Literal[
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
]
OverlayFrameSink: TypeAlias = Callable[[ControlOverlayFrameV2], None]
MillisecondsClock: TypeAlias = Callable[[], int | float]
ObservationKeyFactory: TypeAlias = Callable[[], bytes]

_OBSERVATION_BINDING_DOMAIN = b"openadapt.control-overlay-observation/v2\x00"

_TERMINAL_PHASE_BY_OUTCOME = MappingProxyType(
    {
        "VERIFIED": ControlOverlayPhase.VERIFIED,
        "COMPLETED_UNVERIFIED": ControlOverlayPhase.COMPLETED_UNVERIFIED,
        "HALTED": ControlOverlayPhase.HALTED,
        "FAILED": ControlOverlayPhase.FAILED,
        "ROLLED_BACK": ControlOverlayPhase.ROLLED_BACK,
    }
)


def _unix_ms() -> int:
    return time.time_ns() // 1_000_000


def _monotonic_ms() -> float:
    return time.monotonic_ns() / 1_000_000.0


class RuntimeControlOverlayEmitter:
    """Emit one exact canonical event stream without controlling execution."""

    def __init__(
        self,
        sink: OverlayFrameSink,
        *,
        mode: ControlOverlayMode = ControlOverlayMode.REPLAY,
        unix_ms_clock: MillisecondsClock = _unix_ms,
        monotonic_ms_clock: MillisecondsClock = _monotonic_ms,
        observation_key_factory: ObservationKeyFactory = lambda: secrets.token_bytes(
            32
        ),
    ) -> None:
        self._sink = sink
        self._mode = ControlOverlayMode(mode)
        self._unix_ms_clock = unix_ms_clock
        self._monotonic_ms_clock = monotonic_ms_clock
        self._observation_key_factory = observation_key_factory
        self._observation_hmac_key: bytes | None = None
        self._profile: ControlOverlayProfile | None = None
        self._next_sequence = 0
        self._last_monotonic_ms: float | None = None
        self._terminal = False

    @property
    def mode(self) -> ControlOverlayMode:
        return self._mode

    def begin(self, *, profile: ControlOverlayProfile | str) -> None:
        """Begin a fresh run and bind its exact named execution profile."""

        key = self._observation_key_factory()
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("overlay observation key must be at least 32 bytes")
        self._profile = ControlOverlayProfile(profile)
        self._observation_hmac_key = key
        self._next_sequence = 0
        self._last_monotonic_ms = None
        self._terminal = False

    def emit_phase(
        self,
        phase: ControlOverlayPhase | str,
        *,
        current_step: int | None = None,
        total_steps: int | None = None,
        target_tracking: ControlOverlayTargetTrackingV2 | None = None,
    ) -> ControlOverlayFrameV2:
        """Emit an exact runtime phase; all presentation strings are canonical."""

        if self._profile is None:
            raise RuntimeError("control-overlay run has not begun")
        if self._terminal:
            raise RuntimeError("control-overlay run already emitted a terminal outcome")
        canonical_phase = ControlOverlayPhase(phase)
        if canonical_phase in {
            ControlOverlayPhase.VERIFIED,
            ControlOverlayPhase.COMPLETED_UNVERIFIED,
            ControlOverlayPhase.HALTED,
            ControlOverlayPhase.FAILED,
            ControlOverlayPhase.ROLLED_BACK,
        }:
            raise ValueError("terminal phases must be emitted from execution_outcome")

        return self._emit(
            canonical_phase,
            current_step=current_step,
            total_steps=total_steps,
            target_tracking=target_tracking,
            terminal=False,
        )

    def emit_terminal(self, outcome: ExecutionOutcome | str) -> ControlOverlayFrameV2:
        """Emit only an exact evidence-qualified ``RunReport.execution_outcome``.

        Generic success booleans and legacy strings such as ``SUCCESS`` are
        deliberately not accepted.  The producer never upgrades an outcome.
        """

        try:
            phase = _TERMINAL_PHASE_BY_OUTCOME[outcome]
        except KeyError as exc:
            raise ValueError(
                "terminal overlay requires an exact execution_outcome"
            ) from exc
        return self._emit(
            phase,
            current_step=None,
            total_steps=None,
            target_tracking=None,
            terminal=True,
        )

    def observation_hmac_sha256(
        self,
        observation_png: bytes,
        *,
        event_sequence: int,
    ) -> str:
        """Return the run-scoped opaque binding for one exact private frame.

        A sibling viewer that owns the private stream can ask the producer for
        this binding and compare it with the public presentation event.  The
        raw frame digest and the run-scoped key never enter the event contract.
        """

        if self._observation_hmac_key is None:
            raise RuntimeError("control-overlay run has not begun")
        if (
            isinstance(event_sequence, bool)
            or not isinstance(event_sequence, int)
            or event_sequence < 0
        ):
            raise ValueError("overlay observation event_sequence must be non-negative")
        private_observation_id = (
            event_sequence.to_bytes(8, "big") + hashlib.sha256(observation_png).digest()
        )
        return hmac.new(
            self._observation_hmac_key,
            _OBSERVATION_BINDING_DOMAIN + private_observation_id,
            hashlib.sha256,
        ).hexdigest()

    def browser_target_tracking(
        self,
        *,
        observation_png: bytes,
        region_css_px: Region,
        viewport: tuple[int, int, float],
        action_kind: ControlOverlayTargetActionKind | str | None,
    ) -> ControlOverlayTargetTrackingV2:
        """Build exactly bound browser geometry for an external renderer.

        ``region_css_px`` must be the final region used by replay in the
        top-level CSS viewport.  This helper never resolves or reconstructs a
        target, and callers must omit tracking when that exact region is not
        available.
        """

        width, height, dpr = viewport
        if width <= 0 or height <= 0 or not math.isfinite(dpr) or dpr <= 0:
            raise ValueError("browser presentation viewport is invalid")
        x, y, region_width, region_height = region_css_px
        if region_width <= 0 or region_height <= 0:
            raise ValueError("browser presentation target is empty")
        if x < 0 or y < 0 or x + region_width > width or y + region_height > height:
            raise ValueError("browser presentation target exceeds viewport")
        canonical_action = (
            ControlOverlayTargetActionKind(action_kind)
            if action_kind is not None
            else None
        )
        return ControlOverlayTargetTrackingV2(
            rect=ControlOverlayNormalizedRectV2(
                x=float(x / width),
                y=float(y / height),
                width=float(region_width / width),
                height=float(region_height / height),
            ),
            source_viewport=ControlOverlaySourceViewportV2(
                width_css_px=width,
                height_css_px=height,
                device_pixel_ratio=float(dpr),
            ),
            binding=ControlOverlayObservationBindingV2(
                observation_hmac_sha256=self.observation_hmac_sha256(
                    observation_png,
                    event_sequence=self._next_sequence,
                )
            ),
            action_kind=canonical_action,
        )

    def _emit(
        self,
        phase: ControlOverlayPhase,
        *,
        current_step: int | None,
        total_steps: int | None,
        target_tracking: ControlOverlayTargetTrackingV2 | None,
        terminal: bool,
    ) -> ControlOverlayFrameV2:
        if self._profile is None:
            raise RuntimeError("control-overlay run has not begun")
        if self._terminal:
            raise RuntimeError("control-overlay run already emitted a terminal outcome")
        observed_at_unix_ms = int(self._unix_ms_clock())
        observed_at_monotonic_ms = float(self._monotonic_ms_clock())
        if not math.isfinite(observed_at_monotonic_ms):
            raise ValueError("control-overlay monotonic clock must be finite")
        if (
            self._last_monotonic_ms is not None
            and observed_at_monotonic_ms < self._last_monotonic_ms
        ):
            raise ValueError("control-overlay monotonic clock moved backwards")

        frame = ControlOverlayFrameV2.build(
            event_sequence=self._next_sequence,
            observed_at_unix_ms=observed_at_unix_ms,
            observed_at_monotonic_ms=observed_at_monotonic_ms,
            visible=True,
            phase=phase,
            mode=self._mode,
            profile=self._profile,
            current_step=current_step,
            total_steps=total_steps,
            pause=False,
            resume=False,
            stop=False,
            target_tracking=target_tracking,
        )
        # Advance before delivery: a sink that receives the frame and then
        # raises cannot cause a sequence number or terminal event to be reused.
        self._next_sequence += 1
        self._last_monotonic_ms = observed_at_monotonic_ms
        if terminal:
            self._terminal = True
        self._sink(frame)
        return frame


def build_runtime_control_overlay_timeline_v2(
    frames: Sequence[ControlOverlayFrameV2],
    *,
    data_classification: ControlOverlayDataClassification,
    evidence_pack_id: str,
    media_sha256: str,
    media_frame_count: int,
    media_frame_indexes: Sequence[int],
    duration_ms: int,
    media_started_monotonic_ms: float,
) -> ControlOverlayTimelineV2:
    """Bind retained runtime frames to exact immutable-media timing.

    The caller supplies the exact decoded frame index associated with every
    runtime frame; this function never estimates it from elapsed time.  Runtime
    observation bindings are replaced by bindings to the exact immutable media
    digest and decoded frame.  Geometry is never interpolated between events.
    """

    if not frames:
        raise ValueError("control-overlay timeline requires at least one frame")
    if len(media_frame_indexes) != len(frames):
        raise ValueError("each overlay event requires an exact media frame index")
    if not math.isfinite(media_started_monotonic_ms) or (
        media_started_monotonic_ms < 0
    ):
        raise ValueError("media_started_monotonic_ms must be finite and non-negative")
    if frames[0].observed_at_monotonic_ms != media_started_monotonic_ms:
        raise ValueError("first overlay frame must exactly match media start")

    events: list[ControlOverlayTimelineEventV2] = []
    previous_offset = -1
    for frame, media_frame_index in zip(frames, media_frame_indexes, strict=True):
        delta = frame.observed_at_monotonic_ms - media_started_monotonic_ms
        if delta < 0 or not math.isfinite(delta):
            raise ValueError("overlay frame precedes media start")
        offset = int(math.floor(delta + 0.5))
        if offset <= previous_offset:
            raise ValueError("overlay media offsets must be strictly increasing")
        if offset > duration_ms:
            raise ValueError("overlay frame falls outside media duration")
        target = frame.target_tracking
        if target is not None:
            if not isinstance(target.binding, ControlOverlayObservationBindingV2):
                raise ValueError("runtime target must use an observation binding")
            target = target.model_copy(
                update={
                    "binding": ControlOverlayMediaFrameBindingV2(
                        media_sha256=media_sha256,
                        frame_index=media_frame_index,
                    )
                }
            )
        media_frame = ControlOverlayFrameV2.build(
            event_sequence=frame.event_sequence,
            observed_at_unix_ms=frame.observed_at_unix_ms,
            observed_at_monotonic_ms=frame.observed_at_monotonic_ms,
            visible=frame.visible,
            phase=frame.phase,
            mode=frame.mode,
            profile=frame.profile,
            current_step=frame.step.current,
            total_steps=frame.step.total,
            pause=frame.controls.pause,
            resume=frame.controls.resume,
            stop=frame.controls.stop,
            target_tracking=target,
        )
        events.append(
            ControlOverlayTimelineEventV2(
                at_ms=offset,
                media_frame_index=media_frame_index,
                frame=media_frame,
            )
        )
        previous_offset = offset

    return build_control_overlay_timeline_v2(
        data_classification=data_classification,
        evidence_pack_id=evidence_pack_id,
        media_sha256=media_sha256,
        media_frame_count=media_frame_count,
        duration_ms=duration_ms,
        events=events,
    )
