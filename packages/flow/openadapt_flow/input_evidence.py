"""Bounded pre-input refusal evidence retained across report/resume paths."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_serializer, model_validator

Region = tuple[int, int, int, int]


class FreshActuationEvent(BaseModel):
    """PHI-free evidence for one pre-input actuation-frame mismatch.

    Step retries require no earlier input edge. Keyboard-phase retries require
    an authenticated consumed lease proving no keyboard input crossed; an
    earlier focusing click is never repeated. Only geometry and hashes remain.
    """

    attempt: int = Field(ge=1)
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    changed_pixel_count: int = Field(ge=1)
    changed_bbox: Region
    frame_size: tuple[int, int]
    target_intersection: Optional[bool] = None
    identity_intersection: Optional[bool] = None
    retried: bool
    phase: Literal["step", "keyboard"] = "step"
    lease_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def _legacy_shape(self, handler):
        result = handler(self)
        if self.phase == "step":
            result.pop("phase", None)
            result.pop("lease_sha256", None)
        return result

    @model_validator(mode="after")
    def _valid_geometry(self) -> "FreshActuationEvent":
        if (self.phase == "keyboard") != (self.lease_sha256 is not None):
            raise ValueError("keyboard retry evidence requires its consumed lease hash")
        frame_width, frame_height = self.frame_size
        x, y, width, height = self.changed_bbox
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("fresh-actuation frame size must be positive")
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("fresh-actuation bounding box must be positive")
        if x + width > frame_width or y + height > frame_height:
            raise ValueError("fresh-actuation bounding box exceeds the frame")
        return self
