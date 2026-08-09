"""Immutable, deployment-bound comparison masks for remote frame settling."""

from __future__ import annotations

import hashlib
import io
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

Region = tuple[int, int, int, int]


class RemoteFrameContract(BaseModel):
    """Reviewed exact-geometry exclusions for derived settle inputs only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["openadapt.remote-frame-contract/v1"] = (
        "openadapt.remote-frame-contract/v1"
    )
    frame_width: int = Field(gt=0, le=32768)
    frame_height: int = Field(gt=0, le=32768)
    volatile_regions: tuple[Region, ...] = Field(min_length=1, max_length=32)
    protected_regions: tuple[Region, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def validate_regions(self) -> "RemoteFrameContract":
        for volatile in self.volatile_regions:
            self._validate(volatile)
            for protected in self.protected_regions:
                self._validate(protected)
                if _overlap(volatile, protected):
                    raise ValueError("volatile region overlaps a protected region")
        return self

    def _validate(self, region: Region) -> None:
        x, y, width, height = region
        if (
            width <= 0
            or height <= 0
            or x < 0
            or y < 0
            or x + width > self.frame_width
            or y + height > self.frame_height
        ):
            raise ValueError("remote frame region is outside the exact qualified frame")

    def require_geometry(self, size: tuple[int, int]) -> None:
        if size != (self.frame_width, self.frame_height):
            raise ValueError("remote frame contract geometry does not match live frame")

    def arm(self, protected_regions: tuple[Region, ...]) -> None:
        """Refuse a newly observed target/identity/effect overlap before input."""
        for region in protected_regions:
            self._validate(region)
            if any(_overlap(region, volatile) for volatile in self.volatile_regions):
                raise ValueError("volatile region overlaps a runtime protected region")

    def comparison_digest(self, png: bytes) -> bytes:
        """Hash a derived masked copy. Raw evidence and leases stay unmasked."""
        image = Image.open(io.BytesIO(png)).convert("RGB")
        self.require_geometry(image.size)
        derived = image.copy()
        draw = ImageDraw.Draw(derived)
        for x, y, width, height in self.volatile_regions:
            draw.rectangle((x, y, x + width - 1, y + height - 1), fill=(0, 0, 0))
        out = io.BytesIO()
        derived.save(out, format="PNG")
        return hashlib.sha256(out.getvalue()).digest()


def _overlap(left: Region, right: Region) -> bool:
    x, y, width, height = left
    a, b, c, d = right
    return x < a + c and a < x + width and y < b + d and b < y + height
