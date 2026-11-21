"""Bounded passive native focus evidence shared by capture and compilation.

The observed runtime fingerprint proves teach-time uniqueness only. A compiled
anchor keeps the stable locator; replay must obtain a new guarded live lease.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from openadapt_flow.ir import StructuralLocator


class NativeFocusLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    automation_id: str | None = Field(default=None, min_length=1, max_length=512)
    role: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=512)
    window_name: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def exact_locator(self) -> "NativeFocusLocator":
        if not self.automation_id and not (self.role and self.name):
            raise ValueError("native focus needs AutomationId or exact role/name")
        return self


class NativeFocusTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["openadapt.native-focus/v1"] = "openadapt.native-focus/v1"
    locator: NativeFocusLocator
    point: tuple[StrictInt, StrictInt]
    region: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_count: Literal[1]
    complete: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def exact_observation(cls, data: Any) -> Any:
        if isinstance(data, dict) and (
            not isinstance(data.get("candidate_count"), int)
            or isinstance(data.get("candidate_count"), bool)
            or data.get("candidate_count") != 1
            or data.get("complete") is not True
        ):
            raise ValueError("native focus must be one complete, unique observation")
        return data

    @model_validator(mode="after")
    def bounded_geometry(self) -> "NativeFocusTarget":
        x, y, width, height = self.region
        if any(abs(value) > 32768 for value in (*self.point, *self.region)):
            raise ValueError("native focus geometry is out of bounds")
        if (
            width <= 0
            or height <= 0
            or not (x <= self.point[0] < x + width and y <= self.point[1] < y + height)
        ):
            raise ValueError("native focus point must be within its positive rectangle")
        return self

    def structural_locator(self) -> StructuralLocator:
        return StructuralLocator.model_validate(self.locator.model_dump())


def from_native_candidate(candidate: Any) -> NativeFocusTarget | None:
    if not isinstance(candidate, dict):
        return None
    bounds = candidate.get("bounds")
    if (
        not isinstance(bounds, list)
        or len(bounds) != 4
        or any(isinstance(v, bool) or not isinstance(v, int) for v in bounds)
    ):
        return None
    try:
        return NativeFocusTarget.model_validate(
            {
                "locator": {
                    key: candidate.get(key)
                    for key in ("automation_id", "role", "name", "window_name")
                },
                "point": candidate.get("point"),
                "region": [
                    bounds[0],
                    bounds[1],
                    bounds[2] - bounds[0],
                    bounds[3] - bounds[1],
                ],
                "target_fingerprint": candidate.get("fingerprint"),
                "candidate_count": 1,
                "complete": True,
            }
        )
    except ValueError:
        return None
