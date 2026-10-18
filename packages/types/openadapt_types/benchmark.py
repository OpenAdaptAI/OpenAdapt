"""Canonical benchmark vocabulary shared across the OpenAdapt ecosystem.

This module hosts the ``Benchmark*`` types that describe the interaction
surface between GUI-agent benchmarks and the agents/environments that run
them. They live here (in the canonical schema package) so that both
``openadapt-ml`` (model library) and ``openadapt-evals`` (evaluation
harness) can import them without depending on each other -- breaking the
historical ``ml <-> evals`` import cycle.

The definitions are deliberately dependency-free (plain ``dataclasses`` and
an ``abc.ABC``) so importing them never pulls in heavy optional deps.

Example::

    from openadapt_types import (
        BenchmarkAgent,
        BenchmarkAction,
        BenchmarkObservation,
        BenchmarkTask,
    )

    class MyAgent(BenchmarkAgent):
        def act(self, observation, task, history=None):
            return BenchmarkAction(type="click", x=0.5, y=0.5)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkTask:
    """Canonical task representation.

    Attributes:
        task_id: Unique identifier for the task.
        instruction: Natural language task instruction.
        domain: Task domain ("web", "desktop", "mobile").
        initial_state_ref: Reference to initial state (VM snapshot, URL, etc.).
        time_limit_steps: Maximum steps allowed for the task.
        raw_config: Original benchmark config (lossless preservation).
        evaluation_spec: Benchmark-native evaluation specification.
    """

    task_id: str
    instruction: str
    domain: str  # "web", "desktop", "mobile"

    # Environment setup
    initial_state_ref: str | None = None  # VM snapshot, storage_state, start URL
    time_limit_steps: int | None = None

    # Preserve original config losslessly
    raw_config: dict[str, Any] = field(default_factory=dict)

    # Evaluation spec (benchmark-native)
    evaluation_spec: dict[str, Any] | None = None


@dataclass
class BenchmarkObservation:
    """Canonical observation at each step.

    Supports multiple observation modalities:
    - Visual: screenshots with viewport info
    - Structured UI: accessibility tree (UIA/AXTree/DOM)
    - Context: URL, window title, focused element

    Attributes:
        screenshot: PNG image bytes.
        screenshot_path: Path to saved screenshot.
        viewport: (width, height) of the viewport.
        accessibility_tree: Platform-specific UI tree (UIA/AXTree/DOM).
        dom_html: Raw HTML for web tasks.
        url: Current URL for web tasks.
        window_title: Active window title for desktop tasks.
        focused_element: Currently focused UI element.
        raw_observation: Original benchmark observation (lossless).
    """

    # Visual
    screenshot: bytes | None = None  # PNG image bytes
    screenshot_path: str | None = None
    viewport: tuple[int, int] | None = None  # (width, height)

    # Structured UI (format varies by platform)
    accessibility_tree: dict | None = None  # UIA (Windows), AXTree (macOS), DOM (web)
    dom_html: str | None = None  # Raw HTML for web

    # Context
    url: str | None = None  # For web tasks
    window_title: str | None = None  # For desktop tasks
    app_name: str | None = None  # Active application
    focused_element: dict | None = None  # {node_id, bbox, text}

    # Raw benchmark-specific data (lossless)
    raw_observation: dict[str, Any] | None = None


@dataclass
class BenchmarkAction:
    """Canonical action representation.

    Supports multiple action types with both coordinate-based and element-based
    grounding. The "grounding-first" approach stores both when available.

    Attributes:
        type: Action type ("click", "type", "scroll", "key", "drag", "answer", "done").
        x: X coordinate (normalized [0,1] or pixels).
        y: Y coordinate (normalized [0,1] or pixels).
        target_node_id: Element ID from accessibility tree.
        target_bbox: Element bounding box.
        target_role: Element role (button, textfield, etc.).
        target_name: Element accessible name.
        text: Text to type (for "type" action).
        key: Single key (for "key" action, e.g., "Enter", "Tab").
        modifiers: Key modifiers (["ctrl", "shift", "alt"]).
        scroll_direction: Scroll direction ("up", "down", "left", "right").
        scroll_amount: Scroll amount (pixels or normalized).
        end_x: Drag end X coordinate.
        end_y: Drag end Y coordinate.
        answer: Answer string (for benchmarks that score by answer).
        raw_action: Original benchmark action (lossless).
    """

    type: str  # "click", "type", "scroll", "key", "drag", "answer", "done", "error"

    # Pointer actions - coordinates
    x: float | None = None  # Normalized [0,1] or pixel
    y: float | None = None

    # Element grounding (when available)
    target_node_id: str | None = None  # DOM/AX/UIA node ID
    target_bbox: tuple[float, float, float, float] | None = None
    target_role: str | None = None  # "button", "textfield", etc.
    target_name: str | None = None  # Accessible name

    # Keyboard actions
    text: str | None = None  # For "type" action - text to type
    key: str | None = None  # For "key" action - single key
    modifiers: list[str] | None = None  # ["ctrl", "shift", "alt"]

    # Scroll actions
    scroll_direction: str | None = None  # "up", "down", "left", "right"
    scroll_amount: float | None = None  # Pixels or normalized

    # Drag actions
    end_x: float | None = None
    end_y: float | None = None

    # Answer action (some benchmarks score by final answer)
    answer: str | None = None

    # Raw benchmark-specific format (lossless)
    raw_action: dict[str, Any] | None = None


class BenchmarkAgent(ABC):
    """Abstract interface for agents evaluated on benchmarks.

    Agents must implement the `act` method to receive observations
    and return actions. The agent can maintain internal state across
    steps within an episode.
    """

    @abstractmethod
    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Given observation and task, return next action.

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action to execute.
        """
        ...

    def reset(self) -> None:
        """Reset agent state between episodes.

        Called before starting a new task. Override to clear any
        internal state.
        """
        pass


__all__ = [
    "BenchmarkAction",
    "BenchmarkAgent",
    "BenchmarkObservation",
    "BenchmarkTask",
]
