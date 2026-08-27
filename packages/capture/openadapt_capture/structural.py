"""Versioned structural observations captured beside native input events.

Structural observations are optional evidence.  They describe what the native
accessibility API exposed at action time; absent values stay absent rather than
being guessed from pixels, coordinates, or neighboring controls.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

STRUCTURAL_OBSERVATION_SCHEMA_VERSION = "openadapt.capture.structural-observation/v1"
MAX_STRUCTURAL_TEXT_LENGTH = 512
MAX_STRUCTURAL_ANCESTRY_DEPTH = 32

_PROVIDER_PATTERN = r"^[a-z][a-z0-9_.-]*$"
_StructuralText = Annotated[str, Field(max_length=MAX_STRUCTURAL_TEXT_LENGTH)]
_ProviderIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_PROVIDER_PATTERN),
]

_logger = logging.getLogger(__name__)


class StructuralBounds(BaseModel):
    """Screen-space bounds reported by the accessibility provider."""

    model_config = ConfigDict(extra="forbid")

    left: float
    top: float
    right: float
    bottom: float

    @model_validator(mode="after")
    def validate_bounds(self) -> "StructuralBounds":
        """Reject a non-finite or inverted provider rectangle."""
        values = (self.left, self.top, self.right, self.bottom)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("structural bounds must be finite")
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("structural bounds must not be inverted")
        return self


class StructuralElement(BaseModel):
    """Stable and semantic fields exposed for one accessibility element."""

    model_config = ConfigDict(extra="forbid")

    automation_id: _StructuralText | None = None
    role: _StructuralText | None = None
    role_source: _ProviderIdentifier | None = None
    control_type: _StructuralText | None = None
    name: _StructuralText | None = None
    class_name: _StructuralText | None = None
    framework_id: _StructuralText | None = None
    native_window_handle: int | None = None
    bounds: StructuralBounds | None = None
    supported_patterns: list[_StructuralText] | None = Field(
        default=None,
        max_length=64,
    )


class StructuralAncestor(BaseModel):
    """One parent in the target's accessibility ancestry."""

    model_config = ConfigDict(extra="forbid")

    automation_id: _StructuralText | None = None
    role: _StructuralText | None = None
    role_source: _ProviderIdentifier | None = None
    control_type: _StructuralText | None = None
    name: _StructuralText | None = None
    class_name: _StructuralText | None = None
    bounds: StructuralBounds | None = None


class StructuralProcessIdentity(BaseModel):
    """Identity of the process that owned the observed element."""

    model_config = ConfigDict(extra="forbid")

    process_id: int | None = Field(default=None, ge=0)
    process_name: _StructuralText | None = None


class StructuralWindowIdentity(BaseModel):
    """Identity of the target's top-level window."""

    model_config = ConfigDict(extra="forbid")

    title: _StructuralText | None = None
    automation_id: _StructuralText | None = None
    class_name: _StructuralText | None = None
    native_window_handle: int | None = None
    bounds: StructuralBounds | None = None


class StructuralCandidateContext(BaseModel):
    """How candidate cardinality was measured."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["top_level_window"]
    matched_fields: list[Literal["automation_id", "control_type", "name"]] = Field(min_length=1)


class StructuralObservation(BaseModel):
    """UI structure retained beside one native action event."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.capture.structural-observation/v1"] = (
        STRUCTURAL_OBSERVATION_SCHEMA_VERSION
    )
    provider: _ProviderIdentifier = Field(
        description=(
            "Accessibility-provider identifier. Native Capture providers are "
            "windows_uia, macos_ax, and linux_atspi."
        ),
    )
    event_timestamp: float
    observed_at: float
    query_kind: Literal["point", "focused"]
    element: StructuralElement
    process: StructuralProcessIdentity | None = None
    window: StructuralWindowIdentity | None = None
    ancestry: list[StructuralAncestor] | None = Field(
        default=None,
        max_length=MAX_STRUCTURAL_ANCESTRY_DEPTH,
    )
    candidate_count: int | None = Field(default=None, ge=0)
    candidate_context: StructuralCandidateContext | None = None


@dataclass(frozen=True)
class StructuralObservationRequest:
    """An action-time request passed to a platform structural observer."""

    event_timestamp: float
    action_name: str
    x: float | None = None
    y: float | None = None


@runtime_checkable
class StructuralObserver(Protocol):
    """Injectable interface for platform accessibility observation."""

    def observe(
        self,
        request: StructuralObservationRequest,
    ) -> StructuralObservation | None:
        """Return provider evidence for the action, or ``None`` if unavailable."""
        ...


def create_structural_observer(
    *,
    enabled: bool = True,
    platform_name: str | None = None,
) -> StructuralObserver | None:
    """Create the native observer without importing UIA on other platforms."""

    if not enabled:
        return None
    resolved_platform = platform_name or sys.platform
    try:
        if resolved_platform == "win32":
            from openadapt_capture.structural_observer.windows import (
                WindowsUIAStructuralObserver,
            )

            return WindowsUIAStructuralObserver()
        if resolved_platform == "darwin":
            from openadapt_capture.structural_observer.macos import (
                MacOSAXStructuralObserver,
            )

            return MacOSAXStructuralObserver()
        if resolved_platform.startswith("linux"):
            from openadapt_capture.structural_observer.linux import (
                LinuxATSpiStructuralObserver,
            )

            return LinuxATSpiStructuralObserver()
        return None
    except Exception as exc:
        _logger.warning(
            "%s structural observation is unavailable: %s",
            resolved_platform,
            exc,
        )
        return None


def observe_structural_action(
    observer: StructuralObserver | None,
    request: StructuralObservationRequest,
) -> StructuralObservation | None:
    """Call an observer conservatively and validate its public contract."""

    if observer is None:
        return None
    try:
        observation = observer.observe(request)
        if observation is None:
            return None
        return StructuralObservation.model_validate(observation)
    except Exception as exc:
        # Native accessibility trees can disappear between input receipt and
        # observation.  Missing optional authoring evidence must not corrupt the
        # otherwise valid screen/input recording.
        _logger.warning("Structural observation omitted after provider failure: %s", exc)
        return None


__all__ = [
    "MAX_STRUCTURAL_ANCESTRY_DEPTH",
    "MAX_STRUCTURAL_TEXT_LENGTH",
    "STRUCTURAL_OBSERVATION_SCHEMA_VERSION",
    "StructuralAncestor",
    "StructuralBounds",
    "StructuralCandidateContext",
    "StructuralElement",
    "StructuralObservation",
    "StructuralObservationRequest",
    "StructuralObserver",
    "StructuralProcessIdentity",
    "StructuralWindowIdentity",
    "create_structural_observer",
    "observe_structural_action",
]
