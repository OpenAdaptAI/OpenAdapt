"""Demo-guided agent with self-verification.

Wraps any ``BenchmarkAgent`` and augments its observations with demo
guidance from a ``DemoLibrary``.  After each action, optionally verifies
the result against the demo's expected next state using a VLM comparison.

Demo-conditioned execution is a well-established approach in the
imitation learning and learning from demonstrations literature, where
agent behavior is guided by pre-recorded expert trajectories.

Usage:
    from openadapt_evals.agents import DemoGuidedAgent, PlannerGrounderAgent
    from openadapt_evals.demo_library import DemoLibrary

    base = PlannerGrounderAgent(
        planner="claude-sonnet-4-20250514",
        grounder="gpt-4.1-mini",
        planner_provider="anthropic",
        grounder_provider="openai",
    )
    library = DemoLibrary("./demos")
    agent = DemoGuidedAgent(base_agent=base, demo_library=library)

    action = agent.act(observation, task)

Prior Art:
    - DAgger: Ross et al., "A Reduction of Imitation Learning and
      Structured Prediction to No-Regret Online Learning", AISTATS 2011.
      Foundational work on interactive imitation learning with iterative
      dataset aggregation.
    - Argall et al., "A Survey of Robot Learning from Demonstration",
      Robotics and Autonomous Systems, 2009. Comprehensive survey of
      learning from demonstrations techniques.
    - Humphreys et al., "A Data-Driven Approach for Learning to Control
      Computers", ICML 2022. Demo-conditioned evaluation for computer
      control agents.
    - Behavioral Cloning: Pomerleau, "ALVINN: An Autonomous Land Vehicle
      in a Neural Network", NeurIPS 1989. Early demonstration-conditioned
      policy learning.
"""

from __future__ import annotations

import logging
from typing import Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent
from openadapt_evals.demo_library import DemoGuidance, DemoLibrary

logger = logging.getLogger(__name__)

# Default verification confidence threshold.  If a verification score
# falls below this value the step is flagged for correction.
_DEFAULT_VERIFICATION_THRESHOLD = 0.5

# VLM prompt for self-verification: compare two screenshots.
_VERIFY_SYSTEM = (
    "You are a visual comparison assistant. Compare two screenshots and "
    "determine whether they show the same application state."
)

_VERIFY_PROMPT = """\
I just performed the following action: {action_description}

Image 1 is the actual screenshot after the action.
Image 2 is the expected screenshot from a demonstration.

Do these two screenshots show the same application state?
Consider: same window/dialog open, same fields visible, same content.
Minor differences in cursor position or time are acceptable.

Output a JSON object:
{{"match": true | false, "confidence": 0.0-1.0, "explanation": "brief reason"}}
"""


class DemoGuidedAgent(BenchmarkAgent):
    """Agent wrapper that injects demo guidance and verifies results.

    This agent does **not** replace the base agent's decision-making.
    Instead it enriches each step with contextual guidance from a
    pre-recorded demonstration and, optionally, verifies that the
    result matches expectations.

    The guidance is injected into the base agent's prompt via the
    ``BenchmarkTask.instruction`` field (appended) so it works with
    any ``BenchmarkAgent`` implementation without modifying their
    internals.

    Args:
        base_agent: The underlying agent that makes decisions.
        demo_library: Library of demonstrations.
        verification_threshold: Minimum confidence to consider a step
            verified.  Steps below this threshold are flagged.
        enable_verification: Whether to run self-verification after
            each action (requires an extra VLM call per step).
        verify_model: VLM model name for verification calls.
        verify_provider: VLM provider for verification calls.
    """

    def __init__(
        self,
        base_agent: BenchmarkAgent,
        demo_library: DemoLibrary,
        verification_threshold: float = _DEFAULT_VERIFICATION_THRESHOLD,
        enable_verification: bool = False,
        verify_model: str = "gpt-4.1-mini",
        verify_provider: str = "openai",
        use_visual_alignment: bool = True,
    ):
        self._base_agent = base_agent
        self._demo_library = demo_library
        self._verification_threshold = verification_threshold
        self._enable_verification = enable_verification
        self._verify_model = verify_model
        self._verify_provider = verify_provider
        self._use_visual_alignment = use_visual_alignment

        # Per-episode state
        self._step_index: int = 0
        self._current_task_id: str | None = None
        self._last_observation: BenchmarkObservation | None = None
        self._last_guidance: DemoGuidance | None = None

        # Verification results log (for post-episode analysis)
        self.verification_log: list[dict[str, Any]] = []
        # Steps flagged for correction
        self.flagged_steps: list[dict[str, Any]] = []

        logger.info(
            "DemoGuidedAgent initialized: base=%s, verification=%s, "
            "threshold=%.2f, visual_alignment=%s",
            type(base_agent).__name__,
            enable_verification,
            verification_threshold,
            use_visual_alignment,
        )

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Execute one step with demo guidance and optional verification.

        Steps:
        1. If verification is enabled and we have a previous step,
           verify the previous action's result.
        2. Get demo guidance for the current step.
        3. Augment the task instruction with demo guidance.
        4. Call the base agent with the augmented task.
        5. Store state for next-step verification.

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action from the base agent.
        """
        # Detect task change and reset
        if task.task_id != self._current_task_id:
            # Reset alignment state for the previous task (monotonic
            # progress tracking and adaptive guidance disabling)
            if self._current_task_id is not None:
                self._demo_library.reset_alignment_state(self._current_task_id)
            self._current_task_id = task.task_id
            self._step_index = 0
            self._last_observation = None
            self._last_guidance = None
            logger.info("New task: %s", task.task_id)

        # -- Step 1: Verify previous action if enabled -------------------------
        if (
            self._enable_verification
            and self._last_observation is not None
            and self._last_guidance is not None
            and self._last_guidance.available
            and self._last_guidance.next_screenshot_path is not None
        ):
            score = self.verify_step(
                screenshot_after=observation.screenshot,
                expected_screenshot_path=self._last_guidance.next_screenshot_path,
                action_description=self._last_guidance.instruction,
            )
            entry = {
                "step_index": self._step_index - 1,
                "task_id": task.task_id,
                "confidence": score,
                "passed": score >= self._verification_threshold,
            }
            self.verification_log.append(entry)

            if score < self._verification_threshold:
                logger.warning(
                    "Verification FAILED at step %d (score=%.2f < %.2f)",
                    self._step_index - 1, score, self._verification_threshold,
                )
                self.flagged_steps.append({
                    **entry,
                    "screenshot_bytes_len": (
                        len(observation.screenshot)
                        if observation.screenshot else 0
                    ),
                })
            else:
                logger.info(
                    "Verification passed at step %d (score=%.2f)",
                    self._step_index - 1, score,
                )

        # -- Step 2: Get demo guidance ----------------------------------------
        guidance = self._demo_library.align_step(
            task_id=task.task_id,
            current_screenshot=observation.screenshot,
            step_index=self._step_index,
            use_visual_alignment=self._use_visual_alignment,
        )

        # -- Step 3: Augment task instruction ----------------------------------
        # On step 0, inject the FULL plan overview so the planner sees the
        # complete strategy.  On subsequent steps, inject a lighter per-step
        # hint.  The plan overview is always included so the planner can
        # track progress — it's cheap (a few lines of text).
        augmented_task = task
        if guidance.available:
            # Always include the plan overview so the planner knows the
            # overall strategy.
            plan_overview = self._demo_library.get_plan_overview(task.task_id)
            step_hint = guidance.to_prompt_text()

            if plan_overview:
                guidance_text = f"{plan_overview}\n\n{step_hint}"
            else:
                guidance_text = step_hint

            augmented_task = BenchmarkTask(
                task_id=task.task_id,
                instruction=f"{task.instruction}\n\n{guidance_text}",
                domain=task.domain,
                initial_state_ref=task.initial_state_ref,
                time_limit_steps=task.time_limit_steps,
                raw_config=task.raw_config,
                evaluation_spec=task.evaluation_spec,
            )

            # Also set demo_guidance on the base agent so that anti-loop
            # recovery can reference the strategy (keyboard shortcuts etc.)
            if plan_overview and hasattr(self._base_agent, "demo_guidance"):
                self._base_agent.demo_guidance = plan_overview

            logger.debug(
                "Injected demo guidance for step %d (confidence=%.2f)",
                self._step_index, guidance.confidence,
            )

        # -- Step 4: Call base agent ------------------------------------------
        action = self._base_agent.act(observation, augmented_task, history=history)

        # -- Step 5: Store state for next-step verification -------------------
        self._last_observation = observation
        self._last_guidance = guidance
        self._step_index += 1

        # Attach guidance metadata to the action
        if action.raw_action is None:
            action.raw_action = {}
        action.raw_action["demo_guidance"] = {
            "available": guidance.available,
            "step_index": guidance.step_index,
            "confidence": guidance.confidence,
            "total_demo_steps": guidance.total_demo_steps,
            "visual_alignment_used": guidance.visual_alignment_used,
            "visual_distance": guidance.visual_distance,
        }

        return action

    def verify_step(
        self,
        screenshot_after: bytes | None,
        expected_screenshot_path: str,
        action_description: str = "",
    ) -> float:
        """Verify that a step achieved the expected result.

        Compares the actual post-action screenshot to the demo's expected
        next screenshot using a VLM.

        Args:
            screenshot_after: Actual screenshot bytes after the action.
            expected_screenshot_path: Path to the demo's expected
                screenshot for the next state.
            action_description: Description of the action that was taken.

        Returns:
            Confidence score 0.0 to 1.0 that the action achieved the
            expected result.
        """
        if screenshot_after is None:
            logger.warning("No screenshot available for verification")
            return 0.0

        # Load expected screenshot
        try:
            with open(expected_screenshot_path, "rb") as f:
                expected_bytes = f.read()
        except (OSError, FileNotFoundError) as exc:
            logger.warning(
                "Could not load expected screenshot %s: %s",
                expected_screenshot_path, exc,
            )
            return 0.0

        # VLM comparison
        prompt = _VERIFY_PROMPT.format(
            action_description=action_description or "unknown action",
        )

        try:
            from openadapt_evals.vlm import extract_json, vlm_call

            raw = vlm_call(
                prompt,
                images=[screenshot_after, expected_bytes],
                system=_VERIFY_SYSTEM,
                model=self._verify_model,
                provider=self._verify_provider,
                max_tokens=256,
                cost_label="demo_verify",
            )

            parsed = extract_json(raw)
            if parsed and isinstance(parsed, dict):
                confidence = float(parsed.get("confidence", 0.0))
                match = parsed.get("match", False)
                explanation = parsed.get("explanation", "")
                logger.info(
                    "Verification: match=%s, confidence=%.2f, reason=%s",
                    match, confidence, explanation,
                )
                return confidence if match else confidence * 0.3
            else:
                logger.warning("Could not parse verification response: %s", raw[:200])
                return 0.0
        except Exception as exc:
            logger.error("Verification VLM call failed: %s", exc)
            return 0.0

    def reset(self) -> None:
        """Reset agent state between episodes."""
        # Reset alignment state (monotonic progress, adaptive disabling)
        self._demo_library.reset_alignment_state()

        self._step_index = 0
        self._current_task_id = None
        self._last_observation = None
        self._last_guidance = None
        self.verification_log.clear()
        self.flagged_steps.clear()

        if hasattr(self._base_agent, "reset"):
            self._base_agent.reset()

        logger.info("DemoGuidedAgent reset")

    def get_verification_summary(self) -> dict[str, Any]:
        """Get a summary of verification results for the current episode.

        Returns:
            Dict with verification statistics.
        """
        if not self.verification_log:
            return {
                "total_steps_verified": 0,
                "passed": 0,
                "failed": 0,
                "avg_confidence": 0.0,
                "flagged_steps": [],
            }

        passed = sum(1 for v in self.verification_log if v["passed"])
        confidences = [v["confidence"] for v in self.verification_log]

        return {
            "total_steps_verified": len(self.verification_log),
            "passed": passed,
            "failed": len(self.verification_log) - passed,
            "avg_confidence": sum(confidences) / len(confidences),
            "flagged_steps": list(self.flagged_steps),
        }
