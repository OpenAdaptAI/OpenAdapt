"""Baseline agent using the UnifiedBaselineAdapter from openadapt-ml.

This module provides a BenchmarkAgent implementation that wraps the
unified baseline adapter for comparing different VLM providers across
multiple evaluation tracks.

Usage:
    from openadapt_evals.agents import BaselineAgent
    from openadapt_ml.baselines import TrackConfig

    # Quick start with defaults (Claude, Track A)
    agent = BaselineAgent.from_alias("claude-opus-4.5")
    action = agent.act(observation, task)

    # With Set-of-Mark track
    agent = BaselineAgent.from_alias("gemini-3-pro", track=TrackConfig.track_c())
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import TYPE_CHECKING, Any

from PIL import Image

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent

if TYPE_CHECKING:
    from openadapt_ml.baselines import TrackConfig, UnifiedBaselineAdapter

logger = logging.getLogger("openadapt_evals.agents.baseline")


class BaselineAgent(BenchmarkAgent):
    """Agent wrapping UnifiedBaselineAdapter for benchmark evaluation.

    Provides a BenchmarkAgent interface to the openadapt-ml baseline
    adapters, enabling comparison of Claude, GPT, and Gemini across
    multiple evaluation tracks.

    Tracks:
        - Track A: Direct coordinate prediction
        - Track B: ReAct-style reasoning with coordinates
        - Track C: Set-of-Mark element selection

    Args:
        adapter: Configured UnifiedBaselineAdapter instance.
        demo: Optional demo trajectory (persists across all steps).

    Example:
        # Using adapter directly
        from openadapt_ml.baselines import UnifiedBaselineAdapter, BaselineConfig
        adapter = UnifiedBaselineAdapter(config)
        agent = BaselineAgent(adapter)

        # Using convenience constructor
        agent = BaselineAgent.from_alias("claude-opus-4.5")
        action = agent.act(observation, task)
    """

    def __init__(
        self,
        adapter: "UnifiedBaselineAdapter",
        demo: str | None = None,
    ):
        self._adapter = adapter
        self._demo = demo
        self._step_count = 0
        self._history: list[dict[str, Any]] = []

        logger.info(
            f"BaselineAgent initialized: {adapter.config.provider}/{adapter.config.model} "
            f"track={adapter.config.track.track_type.value}"
        )
        if demo:
            logger.info(f"Demo trajectory provided ({len(demo)} chars)")

    @classmethod
    def from_alias(
        cls,
        model_alias: str,
        track: "TrackConfig | None" = None,
        demo: str | None = None,
        **kwargs: Any,
    ) -> "BaselineAgent":
        """Create agent from model alias.

        Convenience constructor that creates the adapter internally.

        Args:
            model_alias: Model alias (e.g., 'claude-opus-4.5', 'gpt-5.2').
            track: Optional track config (defaults to Track A).
            demo: Optional demo trajectory.
            **kwargs: Additional config options.

        Returns:
            BaselineAgent instance.

        Raises:
            ImportError: If openadapt-ml is not installed.
        """
        try:
            from openadapt_ml.baselines import TrackConfig, UnifiedBaselineAdapter
        except ImportError as e:
            raise ImportError(
                "openadapt-ml is required for BaselineAgent. "
                "Install with: pip install openadapt-ml"
            ) from e

        adapter = UnifiedBaselineAdapter.from_alias(
            model_alias,
            track=track or TrackConfig.track_a(),
            demo=demo,
            **kwargs,
        )
        return cls(adapter, demo=demo)

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
        self._step_count += 1

        # Convert observation to PIL Image
        screenshot = self._observation_to_image(observation)
        if screenshot is None:
            logger.warning("No screenshot in observation")
            return BenchmarkAction(type="error", raw_action={"error": "No screenshot"})

        # Format accessibility tree
        a11y_tree = self._format_a11y_tree(observation.accessibility_tree)

        # Format history
        action_history = self._format_history(history or [])

        # Call adapter
        try:
            parsed = self._adapter.predict(
                screenshot=screenshot,
                goal=task.instruction,
                a11y_tree=a11y_tree,
                history=action_history,
            )
        except Exception as e:
            logger.error(f"Adapter predict failed: {e}")
            return BenchmarkAction(type="error", raw_action={"error": str(e)})

        # Convert ParsedAction to BenchmarkAction
        return self._convert_action(parsed, observation)

    def _observation_to_image(self, observation: BenchmarkObservation) -> Image.Image | None:
        """Convert BenchmarkObservation screenshot to PIL Image."""
        if observation.screenshot is None:
            return None

        if isinstance(observation.screenshot, bytes):
            return Image.open(BytesIO(observation.screenshot))
        elif isinstance(observation.screenshot, Image.Image):
            return observation.screenshot
        else:
            logger.warning(f"Unknown screenshot type: {type(observation.screenshot)}")
            return None

    def _format_a11y_tree(self, tree: Any) -> str | dict | None:
        """Format accessibility tree for adapter."""
        if tree is None:
            return None
        if isinstance(tree, str):
            return tree
        if isinstance(tree, dict):
            return tree
        return str(tree)

    def _format_history(
        self, history: list[tuple[BenchmarkObservation, BenchmarkAction]]
    ) -> list[dict[str, Any]]:
        """Convert history tuples to dict format for adapter."""
        result = []
        for obs, action in history:
            entry: dict[str, Any] = {"type": action.type}
            if action.x is not None:
                entry["x"] = action.x
            if action.y is not None:
                entry["y"] = action.y
            if action.target_node_id is not None:
                entry["element_id"] = action.target_node_id
            if action.text is not None:
                entry["text"] = action.text
            if action.key is not None:
                entry["key"] = action.key
            if action.scroll_direction is not None:
                entry["direction"] = action.scroll_direction
            result.append(entry)
        return result

    def _convert_action(
        self,
        parsed: Any,  # ParsedAction from openadapt_ml.baselines
        observation: BenchmarkObservation,
    ) -> BenchmarkAction:
        """Convert ParsedAction to BenchmarkAction."""
        raw_action = {
            "parsed": parsed.to_dict() if hasattr(parsed, "to_dict") else str(parsed),
            "raw_response": getattr(parsed, "raw_response", None),
            "parse_error": getattr(parsed, "parse_error", None),
        }

        action_type = getattr(parsed, "action_type", "unknown")

        if action_type == "click":
            # Check for element_id (SoM) vs coordinates
            element_id = getattr(parsed, "element_id", None)
            if element_id is not None:
                return BenchmarkAction(
                    type="click",
                    target_node_id=str(element_id),
                    raw_action=raw_action,
                )

            x = getattr(parsed, "x", None)
            y = getattr(parsed, "y", None)
            if x is not None and y is not None:
                return BenchmarkAction(
                    type="click",
                    x=x,
                    y=y,
                    raw_action=raw_action,
                )

            return BenchmarkAction(type="error", raw_action={**raw_action, "error": "Missing coords"})

        elif action_type == "type":
            return BenchmarkAction(
                type="type",
                text=getattr(parsed, "text", ""),
                raw_action=raw_action,
            )

        elif action_type == "key":
            return BenchmarkAction(
                type="key",
                key=getattr(parsed, "key", ""),
                raw_action=raw_action,
            )

        elif action_type == "scroll":
            return BenchmarkAction(
                type="scroll",
                scroll_direction=getattr(parsed, "direction", "down"),
                raw_action=raw_action,
            )

        elif action_type == "done":
            return BenchmarkAction(type="done", raw_action=raw_action)

        else:
            return BenchmarkAction(
                type="error",
                raw_action={**raw_action, "error": f"Unknown action: {action_type}"},
            )

    def set_demo(self, demo: str) -> None:
        """Set or update the demo trajectory.

        The demo persists across all steps (P0 fix for episode success).
        """
        self._demo = demo
        self._adapter.config.demo = demo
        logger.info(f"Demo set ({len(demo)} chars)")

    def reset(self) -> None:
        """Reset agent state between tasks."""
        self._step_count = 0
        self._history = []
        # Note: demo persists across resets
        logger.info("BaselineAgent reset")

    @property
    def provider(self) -> str:
        """Get the provider name."""
        return self._adapter.config.provider

    @property
    def model(self) -> str:
        """Get the model ID."""
        return self._adapter.config.model

    @property
    def track(self) -> str:
        """Get the track type."""
        return self._adapter.config.track.track_type.value
