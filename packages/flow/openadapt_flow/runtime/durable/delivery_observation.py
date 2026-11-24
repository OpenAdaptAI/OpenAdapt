"""Closed, nonauthorizing controller evidence for an uncertain delivery.

An observation never acknowledges a permit or proves when input started.
Keep its actual timestamp spelling and precision when retaining raw bytes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

SCHEMA: Final[Literal["openadapt.production-delivery-uncertainty-observation/v1"]] = (
    "openadapt.production-delivery-uncertainty-observation/v1"
)
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_UTC_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|\+00:00)$"
)


class DeliveryUncertaintyObservation(BaseModel):
    """An exact uncertain edge, carrying no receipt or permission fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[
        "openadapt.production-delivery-uncertainty-observation/v1"
    ] = SCHEMA
    status: Literal["uncertain"]
    observation_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    dispatch_session_id: str = Field(pattern=_UUID)
    permit_id: str = Field(pattern=_UUID)
    one_use_claim_id: str = Field(pattern=_UUID)
    runtime_delivery_sequence: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    controller_delivered_at: str = Field(max_length=64)
    received_at: str = Field(max_length=64)

    @field_validator("controller_delivered_at", "received_at")
    @classmethod
    def _utc_time_keeps_precision(cls, value: str) -> str:
        if not _UTC_RFC3339.fullmatch(value):
            raise ValueError("observation time must be UTC RFC3339")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("observation time is not UTC")
        return value
