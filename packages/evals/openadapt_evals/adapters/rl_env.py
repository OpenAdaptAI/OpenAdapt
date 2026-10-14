"""RL Environment wrapper for interactive benchmark adapters.

Provides a Gymnasium-style interface (reset/step/observe/evaluate) for RL
training without requiring gymnasium as a dependency. Wraps any interactive
BenchmarkAdapter to expose a clean rollout-collection API suitable for
GRPO, PPO, or other online RL algorithms.

Reward design:
    Reward is 0.0 during the episode and the WAA evaluator score (0.0-1.0)
    at the final step. This matches GRPO where reward comes from an outcome
    verifier, not per-step shaping.

    When ``evaluate_every_step=True``, the evaluator is called after each
    step and the score is included in ``info["evaluation_score"]``. The
    reward signal is NOT changed — training code decides how to use the
    per-step evaluation data.

    Dense milestone rewards (via ``evaluate_dense()``) follow standard
    reward shaping techniques from the RL literature. Milestone-based
    partial credit provides gradient signal when sparse binary outcomes
    yield zero gradient (common at the start of GRPO training).

Example:
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
    from openadapt_evals.adapters.rl_env import RLEnvironment

    adapter = WAALiveAdapter(WAALiveConfig(server_url="http://localhost:5001"))
    env = RLEnvironment(adapter, default_task_id="<WAA_UUID>")

    obs = env.reset()
    for _ in range(15):
        obs_step = env.pixel_action(x_frac=0.5, y_frac=0.5)
        if obs_step.done:
            break
    score = env.evaluate()

    # With per-step evaluation (for RL training loops):
    env = RLEnvironment(adapter, default_task_id="<WAA_UUID>", evaluate_every_step=True)
    obs = env.reset()
    step = env.step(BenchmarkAction(type="click", x=0.5, y=0.3))
    print(step.info["evaluation_score"])  # 0.0 or 1.0

Prior Art:
    - Ng et al., "Policy Invariance Under Reward Transformations:
      Theory and Application to Reward Shaping", ICML 1999. Foundational
      work on potential-based reward shaping for RL.
    - ADMIRE: Wang et al., "ADMIRE: Milestone Rewards for Desktop Agent
      Training", 2025. Dense milestone rewards for GUI agent RL.
    - iStar: Li et al., "Interactive Star Rewards for RL-based GUI
      Agents", 2025. Per-step reward signals for agent training.
    - GUI-Genesis: Chen et al., "GUI-Genesis: Synthetic Reward Models
      for GUI Agent Training", 2025. Learned reward models for desktop
      automation.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    EvaluationUnavailableError,
    normalize_benchmark_result,
)

# Avoid circular import — TaskConfig imported lazily.
# PIL is also type-only here: `observe_pil()` is annotated `PIL.Image.Image`
# but imports Pillow inside the function body, so without this the annotation
# named an undefined module and `typing.get_type_hints()` on this class raised
# NameError.
TYPE_CHECKING = False
if TYPE_CHECKING:
    import PIL.Image

    from openadapt_evals.task_config import TaskConfig


def _fraction_to_pixel(value: float, size: int, name: str) -> int:
    """Map an inclusive normalized coordinate to one valid pixel index."""
    if size <= 0:
        raise ValueError(f"{name} screen dimension must be positive")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_frac must be a finite value between 0 and 1")
    return min(int(value * size), size - 1)


def _measured_score(result: BenchmarkResult) -> float:
    """Return one finite benchmark score in the declared unit interval."""
    score = result.score
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
    ):
        raise EvaluationUnavailableError(
            f"task {result.task_id} returned invalid score {score!r}",
            error_type="evaluation",
        )
    return float(score)


logger = logging.getLogger(__name__)


@dataclass
class ResetConfig:
    """Configuration for environment reset.

    Attributes:
        task_id: Specific task to load, or None to use default_task_id.
        task_setup_only: If True (default), only run WAA setup commands (~5s).
            If False, a full environment reset is attempted.
        qemu_reboot: If True, reboot the QEMU Windows VM (~90s). Only useful
            when the VM is in an unrecoverable state. Requires SSH tunnel to
            QEMU monitor port 7100 (not yet implemented).
    """

    task_id: str | None = None
    task_setup_only: bool = True
    qemu_reboot: bool = False


@dataclass
class RolloutStep:
    """Single step in a rollout trajectory.

    Attributes:
        observation: Screenshot + a11y tree after the action.
        action: The action that was executed.
        reward: 0.0 during the episode; final score at the last step.
        done: Whether the episode has ended.
        info: Additional metadata (step index, command executed, etc.).
    """

    observation: BenchmarkObservation
    action: BenchmarkAction
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class RLEnvironment:
    """Gymnasium-style wrapper for interactive benchmark adapters.

    Provides a reset/step/observe/evaluate cycle suitable for RL training
    loops. No gymnasium dependency required.

    The wrapper is adapter-agnostic: it works with any BenchmarkAdapter
    that implements the interactive interface (reset, step, evaluate).
    For pixel-coordinate convenience methods, the underlying adapter should
    be a WAALiveAdapter (or any adapter with a ``pixel_action`` method).

    The task being run is available as ``current_task`` and ``instruction``,
    and every observation the wrapper returns after a successful reset carries
    the instruction in ``raw_observation["instruction"]``, so a policy that
    only sees observations still knows what it was asked to do.

    Args:
        adapter: An interactive BenchmarkAdapter instance.
        default_task_id: Task ID to use when reset() is called without a
            specific task_id. Required for environments with many tasks.
    """

    def __init__(
        self,
        adapter: BenchmarkAdapter,
        default_task_id: str | None = None,
        evaluate_every_step: bool = False,
        task_config: TaskConfig | None = None,
    ):
        self._adapter = adapter
        self._default_task_id = default_task_id
        self._evaluate_every_step = evaluate_every_step
        self._task_config = task_config
        self._current_task: BenchmarkTask | None = None
        self._step_count = 0
        self._done = False
        self._terminal_error = False
        self._trajectory: list[RolloutStep] = []
        self._last_obs: BenchmarkObservation | None = None

        # Per-step milestone tracking: indices of milestones that have
        # been observed as passing at any point during the episode.
        # This is the "high-water mark" — once a milestone passes, it
        # stays passed even if the transient state disappears.
        self._milestone_passed: set[int] = set()
        self._milestone_evaluated: set[int] = set()

    @property
    def adapter(self) -> BenchmarkAdapter:
        """The underlying benchmark adapter."""
        return self._adapter

    @property
    def current_task(self) -> BenchmarkTask | None:
        """The task loaded by the last successful reset, or None."""
        return self._current_task

    @property
    def instruction(self) -> str | None:
        """The current task's instruction, or None before a successful reset."""
        if self._current_task is None:
            return None
        return self._current_task.instruction

    @staticmethod
    def _with_instruction(
        obs: BenchmarkObservation, task: BenchmarkTask | None
    ) -> BenchmarkObservation:
        """Return a copy of ``obs`` whose raw_observation names the task.

        The adapter's own observation object is not modified.
        """
        if task is None or not isinstance(obs, BenchmarkObservation):
            return obs
        return replace(
            obs,
            raw_observation={**(obs.raw_observation or {}), "instruction": task.instruction},
        )

    @property
    def step_count(self) -> int:
        """Number of steps taken in the current episode."""
        return self._step_count

    @property
    def done(self) -> bool:
        """Whether the current episode has ended."""
        return self._done

    @property
    def trajectory(self) -> list[RolloutStep]:
        """Steps taken in the current episode (read-only copy)."""
        return list(self._trajectory)

    @property
    def screen_size(self) -> tuple[int, int]:
        """Measured screen dimensions from the last observation.

        Coordinate conversion must use the frame that the agent observed. A
        configured or conventional desktop size is not actuation evidence.
        """
        if self._last_obs is None or self._last_obs.viewport is None:
            raise RuntimeError(
                "No measured viewport is available; capture an observation before "
                "converting coordinates"
            )
        viewport = self._last_obs.viewport
        if len(viewport) != 2:
            raise ValueError(f"Invalid measured viewport: {viewport!r}")
        width, height = viewport
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, (int, float))
            or not isinstance(height, (int, float))
            or not math.isfinite(float(width))
            or not math.isfinite(float(height))
            or int(width) != width
            or int(height) != height
        ):
            raise ValueError(f"Invalid measured viewport: {viewport!r}")
        if width <= 0 or height <= 0:
            raise ValueError(
                f"Measured viewport dimension must be positive: {viewport!r}"
            )
        return int(width), int(height)

    def load_task_config(self, task_config: TaskConfig) -> None:
        """Set a TaskConfig for dense reward evaluation.

        When set, collect_rollout() and evaluate_dense() use milestone-based
        partial credit instead of binary evaluation.

        Args:
            task_config: A TaskConfig loaded from YAML.
        """
        self._task_config = task_config
        self._default_task_id = task_config.id
        logger.info(
            "Loaded TaskConfig: %s (%d milestones)",
            task_config.name,
            len(task_config.milestones),
        )

    def reset(self, config: ResetConfig | None = None) -> BenchmarkObservation:
        """Reset environment to a task's initial state.

        Loads the task, runs WAA setup commands, and returns the initial
        observation (screenshot + accessibility tree).

        Args:
            config: Reset configuration. If None, uses defaults with
                default_task_id.

        Returns:
            Initial BenchmarkObservation, with the task instruction in
            ``raw_observation["instruction"]``.

        Raises:
            ValueError: If no task_id is provided and no default was set.
            RuntimeError: If the adapter cannot connect to the WAA server.
        """
        # Invalidate the prior episode before any operation that can fail. A
        # failed reset must not leave an old task, frame, or trajectory usable.
        self._current_task = None
        self._step_count = 0
        self._done = False
        self._terminal_error = False
        self._trajectory = []
        self._last_obs = None
        self._milestone_passed = set()
        self._milestone_evaluated = set()

        config = config or ResetConfig()
        task_id = config.task_id or self._default_task_id

        if task_id is None:
            raise ValueError(
                "No task_id provided and no default_task_id set. "
                "Pass task_id in ResetConfig or set default_task_id in constructor."
            )

        # Load the task — prefer TaskConfig if available (avoids server lookup)
        if self._task_config and self._task_config.id == task_id:
            candidate_task = self._task_config.to_benchmark_task()
        elif hasattr(self._adapter, "load_task_from_json") and self._task_config:
            candidate_task = self._adapter.load_task_from_json(
                task_id, self._task_config.to_waa_config()
            )
        else:
            candidate_task = self._adapter.load_task(task_id)
        obs = self._with_instruction(self._adapter.reset(candidate_task), candidate_task)

        # Publish the task and observation only after reset succeeds.
        self._current_task = candidate_task
        self._last_obs = obs

        logger.info(
            "Environment reset: task=%s, instruction=%s",
            task_id,
            candidate_task.instruction[:80],
        )
        return obs

    def step(self, action: BenchmarkAction) -> RolloutStep:
        """Execute an action and return a RolloutStep.

        Reward is 0.0 during the episode. Use evaluate() after the episode
        ends to get the final task-success score.

        Args:
            action: The action to execute.

        Returns:
            RolloutStep with observation, action, reward=0.0, and done flag.

        Raises:
            RuntimeError: If the environment hasn't been reset yet.
        """
        if self._current_task is None:
            raise RuntimeError("Call reset() before step().")

        if self._done:
            raise RuntimeError("Episode is done. Call reset() to start a new episode.")

        obs, done, info = self._adapter.step(action)
        obs = self._with_instruction(obs, self._current_task)
        self._step_count += 1
        self._last_obs = obs

        # Check for terminal actions
        if action.type in ("done", "error"):
            done = True
        if action.type == "error":
            self._terminal_error = True

        self._done = done
        info["step"] = self._step_count

        # Optional per-step evaluation for RL training loops
        if self._terminal_error:
            info["evaluation_error"] = "agent returned a terminal error action"
            info["evaluation_error_type"] = "agent"
        elif self._evaluate_every_step and self._current_task is not None:
            try:
                result = normalize_benchmark_result(
                    self._adapter.evaluate(self._current_task),
                    expected_task_id=self._current_task.task_id,
                    context="per-step adapter evaluation",
                )
                if result.error_type:
                    # Not scored. Recording a 0.0 here would look identical to
                    # a step the agent genuinely failed.
                    info["evaluation_error"] = (
                        f"{result.error_type}: {result.reason or result.error}"
                    )
                    info["evaluation_error_type"] = result.error_type
                else:
                    info["evaluation_score"] = _measured_score(result)
                    info["evaluation_success"] = result.success
            except Exception as e:
                logger.warning("Per-step evaluation failed at step %d: %s", self._step_count, e)
                info["evaluation_error"] = str(e)

        rollout_step = RolloutStep(
            observation=obs,
            action=action,
            reward=0.0,  # Reward comes from evaluate() at episode end
            done=done,
            info=info,
        )
        self._trajectory.append(rollout_step)
        return rollout_step

    def pixel_action(
        self,
        x: int | float | None = None,
        y: int | float | None = None,
        action_type: str = "click",
        text: str | None = None,
        key: str | None = None,
        x_frac: float | None = None,
        y_frac: float | None = None,
    ) -> RolloutStep:
        """Execute a pixel-coordinate action and return a RolloutStep.

        If the underlying adapter has a ``pixel_action`` method (e.g.,
        WAALiveAdapter), delegates to it for normalized fraction support.
        Otherwise, constructs a BenchmarkAction and calls step().

        Args:
            x: X pixel coordinate (absolute).
            y: Y pixel coordinate (absolute).
            action_type: "click", "double_click", "right_click", "type",
                "key", "scroll", "drag".
            text: Text to type (for action_type="type").
            key: Key name (for action_type="key").
            x_frac: X as fraction of screen width (0.0-1.0). Overrides x.
            y_frac: Y as fraction of screen height (0.0-1.0). Overrides y.

        Returns:
            RolloutStep with observation, action, reward=0.0, and done flag.
        """
        # Delegate to adapter's pixel_action if available (handles frac conversion)
        if hasattr(self._adapter, "pixel_action"):
            if self._current_task is None:
                raise RuntimeError("Call reset() before pixel_action().")
            if self._done:
                raise RuntimeError("Episode is done. Call reset() to start a new episode.")

            fractional_size = None
            dispatch_x = x
            dispatch_y = y
            if x_frac is not None or y_frac is not None:
                fractional_size = self.screen_size
                width, height = fractional_size
                if x_frac is not None:
                    dispatch_x = _fraction_to_pixel(x_frac, width, "x")
                if y_frac is not None:
                    dispatch_y = _fraction_to_pixel(y_frac, height, "y")

            obs, done, info = self._adapter.pixel_action(
                x=dispatch_x,
                y=dispatch_y,
                action_type=action_type,
                text=text,
                key=key,
                x_frac=None,
                y_frac=None,
            )
            obs = self._with_instruction(obs, self._current_task)
            self._step_count += 1
            self._last_obs = obs
            self._done = done
            if action_type == "error":
                done = True
                self._done = True
                self._terminal_error = True
                info["evaluation_error"] = "agent returned a terminal error action"
                info["evaluation_error_type"] = "agent"
            info["step"] = self._step_count

            # Reconstruct the action for the trajectory record
            resolved_x = x
            resolved_y = y
            if fractional_size is not None:
                resolved_x = dispatch_x
                resolved_y = dispatch_y

            action = BenchmarkAction(
                type=action_type, x=resolved_x, y=resolved_y, text=text, key=key
            )
            rollout_step = RolloutStep(
                observation=obs,
                action=action,
                reward=0.0,
                done=done,
                info=info,
            )
            self._trajectory.append(rollout_step)
            return rollout_step

        # Fallback: convert fracs to pixels manually and use step()
        if x_frac is not None or y_frac is not None:
            w, h = self.screen_size
            if x_frac is not None:
                x = _fraction_to_pixel(x_frac, w, "x")
            if y_frac is not None:
                y = _fraction_to_pixel(y_frac, h, "y")

        action = BenchmarkAction(type=action_type, x=x, y=y, text=text, key=key)
        return self.step(action)

    def observe(self) -> BenchmarkObservation:
        """Return the current observation without stepping.

        If the adapter has an ``observe()`` method (e.g., WAALiveAdapter),
        delegates to it for a fresh screenshot. Otherwise returns the most
        recent cached observation.

        Returns:
            BenchmarkObservation with screenshot and accessibility tree.

        Raises:
            RuntimeError: If no observation is available (call reset() first).
        """
        if hasattr(self._adapter, "observe"):
            obs = self._with_instruction(self._adapter.observe(), self._current_task)
            self._last_obs = obs
            return obs
        if self._last_obs is not None:
            return self._last_obs
        raise RuntimeError("Call reset() before observe().")

    def observe_pil(self) -> "PIL.Image.Image":
        """Get current screenshot as a PIL Image.

        Convenience wrapper around observe() for VLM/RL training pipelines
        that work with PIL images directly.

        If the underlying adapter has an ``observe_pil()`` method (e.g.,
        WAALiveAdapter), delegates to it. Otherwise calls observe() and
        converts the screenshot bytes to a PIL Image.

        Returns:
            PIL.Image.Image of the current desktop state.

        Raises:
            RuntimeError: If no screenshot is available (call reset() first).
        """
        if hasattr(self._adapter, "observe_pil"):
            return self._adapter.observe_pil()

        import io

        from PIL import Image

        obs = self.observe()
        if not obs.screenshot:
            raise RuntimeError("No screenshot available from adapter")
        return Image.open(io.BytesIO(obs.screenshot))

    def evaluate_result(self) -> BenchmarkResult:
        """Run the adapter's evaluator and return the full result.

        Unlike :meth:`evaluate`, this preserves ``error_type``, so a caller can
        tell an unreachable VM (``error_type == "infrastructure"``) from a task
        the agent genuinely failed (``error_type is None, score == 0.0``).

        Returns:
            The adapter's :class:`BenchmarkResult`, unmodified.

        Raises:
            RuntimeError: If no task has been loaded (call reset() first).
        """
        if self._current_task is None:
            raise RuntimeError("Call reset() before evaluate().")

        if self._terminal_error:
            result = BenchmarkResult(
                task_id=self._current_task.task_id,
                success=False,
                score=0.0,
                num_steps=self._step_count,
                error_type="agent",
                reason="Agent returned a terminal error action",
            )
            if self._trajectory:
                self._trajectory[-1].reward = 0.0
                self._trajectory[-1].info["evaluation_error_type"] = "agent"
                self._trajectory[-1].info["evaluation_error"] = result.reason
            return result

        result = normalize_benchmark_result(
            self._adapter.evaluate(self._current_task),
            expected_task_id=self._current_task.task_id,
            context="adapter evaluation",
        )

        # Backfill reward on the last trajectory step only when the score is a
        # real measurement; an infrastructure 0.0 is not a reward signal.
        if self._trajectory and not result.error_type:
            self._trajectory[-1].reward = _measured_score(result)
        elif not result.error_type:
            _measured_score(result)

        logger.info(
            "Evaluation: task=%s, success=%s, score=%.2f, error_type=%s",
            self._current_task.task_id,
            result.success,
            result.score,
            result.error_type,
        )
        return result

    def evaluate(self) -> float:
        """Run the WAA evaluator on the current VM state.

        Returns the task-success score (0.0-1.0). For binary tasks, this is
        0.0 (failure) or 1.0 (success). Suitable as the terminal reward for
        GRPO or other RL algorithms.

        The adapters go to real trouble to mark infrastructure failures
        (``BenchmarkResult.error_type``); flattening the result to a bare float
        used to throw that away, so 40 tasks whose VM stopped answering were
        published as 40 genuine agent failures. This method now raises rather
        than returning an unmeasured 0.0. Callers that want the tri-state
        should use :meth:`evaluate_result`.

        Returns:
            Score between 0.0 and 1.0.

        Raises:
            RuntimeError: If no task has been loaded (call reset() first).
            EvaluationUnavailableError: If the task could not be scored
                because of an infrastructure or evaluator failure.
        """
        result = self.evaluate_result()

        if result.error_type:
            raise EvaluationUnavailableError(
                f"task {result.task_id} was not scored "
                f"({result.error_type}): {result.reason or result.error}",
                error_type=result.error_type,
            )

        return _measured_score(result)

    def check_milestones_incremental(
        self,
        screenshot: bytes | None = None,
    ) -> tuple[int, int]:
        """Check milestones against the CURRENT state and update high-water mark.

        Milestones that have passed at ANY point during the episode stay
        passed permanently (high-water mark).  This is critical for
        transient states like "dialog is open" which disappear after the
        agent clicks through them.

        Call this after each step to track progress.  ``evaluate_dense()``
        uses the accumulated high-water mark for the final score.

        Args:
            screenshot: Current screenshot bytes.  If ``None``, takes a
                fresh screenshot from the adapter.

        Returns:
            ``(passed, total)`` where *passed* is the number of milestones
            that have passed at any point (high-water mark), and *total*
            is the total number of milestones.
        """
        if not self._task_config or not self._task_config.milestones:
            return 0, 0

        total = len(self._task_config.milestones)

        if screenshot is None:
            try:
                obs = self._adapter.observe()
                screenshot = obs.screenshot if obs else b""
            except Exception:
                screenshot = b""

        server_url = getattr(getattr(self._adapter, "config", None), "server_url", "") or ""

        for i, ms in enumerate(self._task_config.milestones):
            if i in self._milestone_passed:
                continue  # already passed — skip expensive checks
            try:
                if ms.check.check == "screenshot":
                    from openadapt_evals.vlm_evaluator import vlm_judge

                    success, _ = vlm_judge(screenshot, ms.check.description or "")
                    if success:
                        self._milestone_passed.add(i)
                        logger.info(
                            "Milestone %d/%d PASSED (high-water): %s",
                            i + 1,
                            total,
                            ms.name,
                        )
                    self._milestone_evaluated.add(i)
                elif ms.check.check == "command":
                    result = self._task_config._run_vm_command(
                        ms.check.run or "",
                        server_url,
                    )
                    if self._task_config._check_match(
                        result,
                        ms.check.expect or "",
                        ms.check.match,
                    ):
                        self._milestone_passed.add(i)
                        logger.info(
                            "Milestone %d/%d PASSED (high-water): %s",
                            i + 1,
                            total,
                            ms.name,
                        )
                    self._milestone_evaluated.add(i)
            except Exception as exc:
                logger.debug(
                    "Milestone %d check failed (non-fatal): %s",
                    i,
                    exc,
                )

        passed = len(self._milestone_passed)
        return passed, total

    def evaluate_dense(self) -> float:
        """Evaluate using dense partial rewards via milestones.

        Uses the **high-water mark** from ``check_milestones_incremental()``
        calls during the episode, plus a final check at episode end.
        Milestones that passed at any point stay passed — this correctly
        handles transient states like open dialogs.

        Falls back to binary evaluate() if no milestones are defined.

        Returns:
            Dense reward score between 0.0 and 1.0.
        """
        if self._current_task is None:
            raise RuntimeError("Call reset() before evaluate_dense().")

        if self._terminal_error:
            return self.evaluate()

        # Try milestone evaluation first
        if self._task_config and self._task_config.milestones:
            # Take a FRESH screenshot for the final evaluation pass.
            screenshot = b""
            try:
                fresh_obs = self._adapter.observe()
                if fresh_obs and fresh_obs.screenshot:
                    screenshot = fresh_obs.screenshot
                    logger.info(
                        "evaluate_dense: using fresh screenshot (%d bytes)",
                        len(screenshot),
                    )
            except Exception as e:
                logger.warning(
                    "evaluate_dense: failed to take fresh screenshot, falling back to cached: %s",
                    e,
                )
                if self._last_obs and self._last_obs.screenshot:
                    screenshot = self._last_obs.screenshot

            # Run one final milestone check to catch any end-of-episode
            # state (e.g., "data is cleared" command checks).
            self.check_milestones_incremental(screenshot)

            total = len(self._task_config.milestones)
            passed = len(self._milestone_passed)

            if total > 0:
                milestone_score = passed / total

                # Try binary evaluation (remote /evaluate endpoint).
                # Speed depends on adapter's evaluate_timeout config:
                # - Benchmarking: 180s timeout, 3 retries (thorough)
                # - Training: 15s timeout, 1 retry (fast feedback)
                # The TRL wrapper sets training-appropriate timeouts.
                binary_score = 0.0
                binary_unavailable: Exception | None = None
                try:
                    binary_score = self.evaluate()
                except Exception as exc:
                    # Record WHY rather than folding it into a 0.0. A binary
                    # evaluator that could not run is not the same as one that
                    # ran and said "no", and `max(milestone, 0.0)` silently
                    # publishes the milestone high-water mark as a task score.
                    binary_unavailable = exc
                    logger.warning(
                        "evaluate_dense: binary evaluation did not run: %s",
                        exc,
                    )

                # If binary eval returned 0.0 (endpoint down or task
                # failed), try local evaluation via task config checks.
                local_check_ran = False
                if binary_score == 0.0 and self._task_config.checks and screenshot:
                    server_url = (
                        getattr(
                            getattr(self._adapter, "config", None),
                            "server_url",
                            "",
                        )
                        or ""
                    )
                    try:
                        binary_score = self._task_config.evaluate_checks_local(
                            screenshot,
                            server_url,
                        )
                        local_check_ran = True
                        if binary_score > 0:
                            logger.info(
                                "evaluate_dense: local check fallback returned %.2f",
                                binary_score,
                            )
                    except Exception as exc:
                        logger.debug(
                            "evaluate_dense: local check fallback failed: %s",
                            exc,
                        )

                milestones_measured = len(self._milestone_evaluated) == total
                if (
                    binary_unavailable is not None
                    and not local_check_ran
                    and not milestones_measured
                ):
                    # Nothing scored this task: the binary evaluator could not
                    # run and no local check stood in for it. The milestone
                    # high-water mark is a progress signal, not a task score,
                    # so returning it here would publish a number that was
                    # never measured.
                    raise EvaluationUnavailableError(
                        f"task {self._current_task.task_id} was not scored: "
                        f"binary evaluation did not run "
                        f"({binary_unavailable}) and no local check fallback "
                        f"was available; only "
                        f"{len(self._milestone_evaluated)}/{total} milestone "
                        "contracts were measured",
                        error_type=getattr(
                            binary_unavailable,
                            "error_type",
                            "infrastructure",
                        ),
                    ) from binary_unavailable

                # Use the higher of milestone score and binary score
                score = max(milestone_score, binary_score)

                # Backfill reward on last trajectory step
                if self._trajectory:
                    self._trajectory[-1].reward = score
                    self._trajectory[-1].info["milestone_score"] = milestone_score
                    self._trajectory[-1].info["binary_score"] = binary_score
                    self._trajectory[-1].info["milestones_passed"] = passed
                    self._trajectory[-1].info["milestones_total"] = total

                logger.info(
                    "Dense evaluation: milestones=%d/%d (%.2f) [high-water], "
                    "binary=%.2f, final=%.2f",
                    passed,
                    total,
                    milestone_score,
                    binary_score,
                    score,
                )
                return score

        # Fallback to binary evaluation
        return self.evaluate()

    def collect_rollout(
        self,
        agent_fn: Callable[[BenchmarkObservation], BenchmarkAction],
        max_steps: int = 15,
        stuck_window: int = 3,
        task_id: str | None = None,
    ) -> list[RolloutStep]:
        """Collect a complete rollout using an agent function.

        Resets the environment, runs the agent for up to max_steps, evaluates
        the result, and returns the full trajectory with the terminal reward
        backfilled on the last step.

        Includes stuck detection: if the last ``stuck_window`` screenshots
        are identical (by hash), the episode terminates early to avoid
        wasting compute on frozen VMs.

        Args:
            agent_fn: Callable that takes a BenchmarkObservation and returns
                a BenchmarkAction. This is your model's predict function.
            max_steps: Maximum steps per episode.
            stuck_window: Number of consecutive identical screenshots before
                early termination. Set to 0 to disable.
            task_id: Task to run. If None, uses default_task_id.

        Returns:
            List of RolloutStep objects. The last step's reward contains the
            evaluation score; all other rewards are 0.0.
        """
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")

        config = ResetConfig(task_id=task_id)
        obs = self.reset(config)

        screenshot_hashes: list[str] = []

        for step_idx in range(max_steps):
            # Get action from agent
            action = agent_fn(obs)

            # Execute action
            rollout_step = self.step(action)
            obs = rollout_step.observation

            # Track screenshot hashes for stuck detection
            if stuck_window > 0 and obs.screenshot:
                h = hashlib.md5(obs.screenshot).hexdigest()
                screenshot_hashes.append(h)

                if (
                    len(screenshot_hashes) >= stuck_window
                    and len(set(screenshot_hashes[-stuck_window:])) == 1
                ):
                    logger.warning(
                        "Stuck detected: last %d screenshots identical. "
                        "Terminating episode at step %d.",
                        stuck_window,
                        step_idx + 1,
                    )
                    self._done = True
                    rollout_step.done = True
                    break

            if rollout_step.done:
                break

        if self._terminal_error:
            if self._trajectory:
                self._trajectory[-1].reward = 0.0
                self._trajectory[-1].info["evaluation_error_type"] = "agent"
                self._trajectory[-1].info["evaluation_error"] = (
                    "Agent returned a terminal error action"
                )
            logger.info(
                "Rollout stopped after terminal agent error: %d steps",
                self._step_count,
            )
            return list(self._trajectory)

        # Evaluate and backfill reward — use dense rewards if milestones exist
        if self._task_config and self._task_config.milestones:
            score = self.evaluate_dense()
        else:
            score = self.evaluate()

        logger.info(
            "Rollout complete: %d steps, score=%.2f",
            self._step_count,
            score,
        )
        return list(self._trajectory)


if TYPE_CHECKING:
    # Fork B conformance: the concrete RLEnvironment implements the RolloutEnv
    # Protocol defined in openadapt-ml. openadapt-ml depends on the interface;
    # openadapt-evals provides this implementation. Guarded by the module-level
    # TYPE_CHECKING sentinel (False at runtime) so openadapt-ml remains an
    # optional, non-runtime dependency of openadapt-evals and no import cycle
    # is introduced.
    from openadapt_ml.training.grpo.rollout_env import RolloutEnv

    _rollout_env_conformance: type[RolloutEnv] = RLEnvironment
