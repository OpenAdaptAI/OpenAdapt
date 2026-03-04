"""Episode and Step schemas for GUI trajectory data.

Migrated from ``openadapt_ml.schema.episode`` with adaptations
to use :class:`ComputerState` and :class:`Action` from this package.

An :class:`Episode` is a complete task trajectory: a sequence of
:class:`Step` objects (observation → action pairs) with metadata
about the task, outcome, and provenance.

Schema version: 0.1.0
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from .action import Action, ActionResult
from .computer_state import ComputerState


SCHEMA_VERSION = "0.1.0"


class BenchmarkSource(str, Enum):
    """Source benchmark or dataset."""

    WAA = "waa"
    OSWORLD = "osworld"
    WEBARENA = "webarena"
    MINIWOB = "miniwob"
    HUMAN = "human"
    SYNTHETIC = "synthetic"


class Step(BaseModel):
    """A single step in an episode: observation → action → result."""

    step_index: int = Field(..., ge=0, description="Step number (0-indexed)")

    # Core data
    observation: ComputerState = Field(
        ..., description="State before the action"
    )
    action: Action = Field(..., description="Action taken")
    result: Optional[ActionResult] = Field(
        None, description="Result of executing the action"
    )

    # Agent reasoning
    reasoning: Optional[str] = Field(
        None, description="Agent chain-of-thought for this step"
    )

    # Outcome
    reward: Optional[float] = Field(None, description="Step-level reward signal")
    done: Optional[bool] = Field(
        None, description="Whether the episode ended after this step"
    )

    # Timing
    timestamp: Optional[float] = Field(None, description="Unix timestamp of action")
    duration_ms: Optional[int] = Field(
        None, description="Time taken for this step in ms"
    )


class Episode(BaseModel):
    """A complete task trajectory.

    Migrated from ``openadapt_ml.schema.episode.Episode`` with these changes:

    - ``Observation`` → :class:`ComputerState`
    - ``Action`` → :class:`Action` (with :class:`ActionTarget`)
    - Added optional :class:`ActionResult` per step
    """

    schema_version: str = Field(default=SCHEMA_VERSION)

    # Identification
    episode_id: str = Field(..., description="Unique episode identifier")
    task_id: Optional[str] = Field(None, description="Task ID (from benchmark)")

    # Task specification
    instruction: str = Field(..., description="Natural language task instruction")
    goal: Optional[str] = Field(None, description="Detailed goal description")

    # Trajectory
    steps: list[Step] = Field(..., description="Sequence of steps")

    # Outcome
    success: Optional[bool] = Field(None, description="Task completed successfully?")
    final_reward: Optional[float] = Field(None, description="Final reward / score")

    # Provenance
    source: Optional[BenchmarkSource] = Field(None, description="Source benchmark")
    source_file: Optional[str] = Field(None, description="Original source file path")
    agent_model: Optional[str] = Field(
        None, description="Model that generated this (e.g., 'claude-opus-4-6')"
    )
    environment: Optional[str] = Field(
        None, description="Environment info (OS, VM, browser)"
    )

    # Metadata
    created_at: Optional[datetime] = Field(
        default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc),
        description="When created/recorded",
    )
    tags: Optional[list[str]] = Field(None, description="Tags for categorization")
    metadata: Optional[dict[str, Any]] = Field(None, description="Additional metadata")

    # ── Helpers ──

    @property
    def num_steps(self) -> int:
        return len(self.steps)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> Episode:
        return cls.model_validate_json(json_str)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> Episode:
        with open(path) as f:
            return cls.model_validate(json.load(f))

    def to_file(self, path: Union[str, Path], indent: int = 2) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(indent=indent))
