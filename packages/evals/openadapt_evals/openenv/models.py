"""OpenEnv Pydantic models for WAA desktop environment.

Defines the Action, Observation, and State types that flow over the
OpenEnv WebSocket/HTTP protocol. Screenshots are base64-encoded PNG
strings (~800KB) which fits well within OpenEnv's 100MB message limit.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# OpenEnv base types (inlined to avoid hard dependency on openenv-core
# at import time — the server app uses the real openenv imports)
# ---------------------------------------------------------------------------


class _ActionBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, arbitrary_types_allowed=True
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class _ObservationBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, arbitrary_types_allowed=True
    )
    done: bool = Field(default=False)
    reward: float | int | None = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class _StateBase(BaseModel):
    model_config = ConfigDict(
        extra="allow", validate_assignment=True, arbitrary_types_allowed=True
    )
    episode_id: Optional[str] = None
    step_count: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# WAA-specific types
# ---------------------------------------------------------------------------


class WAAAction(_ActionBase):
    """Desktop action sent to the WAA environment.

    Accepts either a JSON action string (for VLM agents) or structured
    fields (for programmatic use).
    """

    type: str = Field(
        ..., description="Action type: click, type, key, scroll, done, noop"
    )
    x: Optional[float] = Field(
        default=None, description="X coordinate (0-1 fractional or pixel)"
    )
    y: Optional[float] = Field(
        default=None, description="Y coordinate (0-1 fractional or pixel)"
    )
    text: Optional[str] = Field(default=None, description="Text for type action")
    key: Optional[str] = Field(default=None, description="Key for key action")


class WAAObservation(_ObservationBase):
    """Observation from the WAA desktop environment."""

    screenshot_b64: Optional[str] = Field(
        default=None, description="Base64-encoded PNG screenshot"
    )
    accessibility_tree: Optional[str] = Field(
        default=None, description="Text accessibility tree"
    )
    window_title: Optional[str] = Field(
        default=None, description="Active window title"
    )
    step_index: int = Field(default=0, description="Current step number")


class WAAState(_StateBase):
    """State of the WAA desktop environment."""

    task_id: Optional[str] = None
    task_name: Optional[str] = None
    done: bool = False
    score: Optional[float] = None
    milestones_passed: int = 0
    milestones_total: int = 0
    status: str = "idle"  # idle, running, completed, failed
