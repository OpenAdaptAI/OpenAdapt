"""Scripted and baseline agents for benchmark evaluation.

This module provides simple agent implementations for testing and baselines:
- ScriptedAgent: Follows predefined action sequence
- RandomAgent: Takes random actions
- SmartMockAgent: Designed to pass mock adapter tests
"""

from __future__ import annotations

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent


class ScriptedAgent(BenchmarkAgent):
    """Agent that follows a predefined script of actions.

    Useful for testing benchmark adapters or replaying trajectories.

    Args:
        actions: List of actions to execute in order.
    """

    def __init__(self, actions: list[BenchmarkAction]):
        self.actions = actions
        self._step = 0

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Return the next scripted action.

        Args:
            observation: Ignored.
            task: Ignored.
            history: Ignored.

        Returns:
            Next action from script, or DONE if script exhausted.
        """
        if self._step < len(self.actions):
            action = self.actions[self._step]
            self._step += 1
            return action
        return BenchmarkAction(type="done")

    def reset(self) -> None:
        """Reset step counter."""
        self._step = 0


class RandomAgent(BenchmarkAgent):
    """Agent that takes random actions.

    Useful for baseline comparisons.

    Args:
        action_types: List of action types to randomly select from.
        seed: Random seed for reproducibility.
    """

    def __init__(
        self,
        action_types: list[str] | None = None,
        seed: int | None = None,
    ):
        import random

        self.action_types = action_types or ["click", "type", "scroll", "done"]
        self.rng = random.Random(seed)

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Return a random action.

        Args:
            observation: Used to get viewport bounds.
            task: Ignored.
            history: Used to decide when to stop.

        Returns:
            Random action.
        """
        # Stop after many actions
        if history and len(history) > 20:
            return BenchmarkAction(type="done")

        action_type = self.rng.choice(self.action_types)

        if action_type == "click":
            return BenchmarkAction(
                type="click",
                x=self.rng.random(),
                y=self.rng.random(),
            )
        elif action_type == "type":
            return BenchmarkAction(
                type="type",
                text="test",
            )
        elif action_type == "scroll":
            return BenchmarkAction(
                type="scroll",
                scroll_direction=self.rng.choice(["up", "down"]),
            )
        else:
            return BenchmarkAction(type="done")

    def reset(self) -> None:
        """Nothing to reset."""
        pass


class SmartMockAgent(BenchmarkAgent):
    """Agent designed to pass WAAMockAdapter evaluation.

    Performs a fixed sequence of actions that satisfy the mock adapter's
    success criteria. Use for validating the benchmark pipeline locally.

    The mock adapter evaluates success based on:
    - Clicking Submit (ID 4) - primary success path
    - Typing something AND clicking OK (ID 1) - form submission path
    - Calling DONE after at least 2 actions - reasonable completion

    This agent clicks Submit (ID 4) which is the simplest success path.
    """

    def __init__(self):
        """Initialize the agent."""
        self._step = 0
        # Simple action sequence: click Submit button (ID 4), then done
        self._actions = [
            BenchmarkAction(type="click", target_node_id="4"),  # Click Submit
            BenchmarkAction(type="done"),
        ]

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Return the next scripted action.

        Args:
            observation: Ignored.
            task: Ignored.
            history: Ignored.

        Returns:
            Next action from script, or DONE if script exhausted.
        """
        if self._step < len(self._actions):
            action = self._actions[self._step]
            self._step += 1
            return action
        return BenchmarkAction(type="done")

    def reset(self) -> None:
        """Reset step counter."""
        self._step = 0
