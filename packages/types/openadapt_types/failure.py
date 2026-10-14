"""Failure taxonomy for GUI automation agents.

Provides a structured way to classify and record agent failures,
enabling failure-dataset pipelines and targeted improvement.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class FailureCategory(str, Enum):
    """Top-level failure categories for GUI agent actions."""

    GROUNDING = "grounding"          # Could not find / misidentified target element
    NAVIGATION = "navigation"        # Went to wrong screen / wrong app state
    COMPREHENSION = "comprehension"  # Misunderstood task instruction
    EXECUTION = "execution"          # Right target, action failed to execute
    INFRASTRUCTURE = "infrastructure"  # VM, network, Docker, timeout
    LOOP = "loop"                    # Agent repeated same action without progress
    SAFETY = "safety"                # Action blocked by policy gate


class FailureRecord(BaseModel):
    """A single classified failure event."""

    category: FailureCategory
    step_index: int = Field(..., description="Step where failure occurred")
    message: Optional[str] = Field(None, description="Human-readable description")
    action_type: Optional[str] = Field(None, description="Action type that failed")
    target_description: Optional[str] = Field(
        None, description="What the agent was trying to interact with"
    )
    expected_state: Optional[str] = Field(
        None, description="What the agent expected to see"
    )
    actual_state: Optional[str] = Field(
        None, description="What was actually on screen"
    )
    raw: Optional[dict[str, Any]] = None
