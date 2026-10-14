"""Tiered demo executor: deterministic replay with adaptive grounding.

Executes demo steps directly instead of asking a VLM planner to
interpret them. The planner is only consulted as a recovery mechanism
when the expected screen state doesn't match.

Tier 1 (deterministic): keyboard shortcuts, typing -- execute directly.
Tier 2 (grounder-only): clicks -- grounder finds element by description.
Tier 3 (planner recovery): unexpected state -- planner reasons about
    what to do when the demo doesn't match reality.

Usage:
    from openadapt_evals.agents.demo_executor import DemoExecutor

    executor = DemoExecutor(
        grounder_model="gpt-4.1-mini",
        grounder_provider="openai",
        planner_model="gpt-4.1-mini",   # only used for recovery
        planner_provider="openai",
    )
    score, screenshots = executor.run(env, demo, task_config)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.demo_library import Demo, DemoStep
from openadapt_evals.errors import ActionExecutionError
from openadapt_evals.grounding import (
    GroundingTarget,
    check_state_preconditions,
    ground_by_text,
    run_ocr,
    verify_transition,
)

try:
    from openadapt_evals.integrations.weave_integration import weave_op
except ImportError:
    weave_op = lambda fn: fn  # noqa: E731

logger = logging.getLogger(__name__)


class DemoExecutor:
    """Execute demo steps with tiered intelligence.

    Args:
        grounder_model: VLM model for grounding clicks.
        grounder_provider: API provider for grounder.
        planner_model: VLM model for recovery (unexpected states).
        planner_provider: API provider for planner.
        step_delay: Seconds to wait after each action for UI to settle.
        recovery_budget: Max planner recovery attempts per demo step.
    """

    def __init__(
        self,
        grounder_model: str = "gpt-4.1-mini",
        grounder_provider: str = "openai",
        grounder_endpoint: str | None = None,
        planner_model: str = "gpt-4.1-mini",
        planner_provider: str = "openai",
        step_delay: float = 1.0,
        recovery_budget: int = 2,
    ):
        """Initialize the DemoExecutor.

        Args:
            grounder_endpoint: HTTP endpoint for a dedicated grounding
                model (e.g., vLLM serving UI-Venus-1.5-8B). When set,
                uses the UI-Venus native bbox format for much better
                click accuracy than a general VLM. Example:
                ``"http://gpu-box:8080"``
        """
        self._grounder_model = grounder_model
        self._grounder_provider = grounder_provider
        self._grounder_endpoint = grounder_endpoint
        self._planner_model = planner_model
        self._planner_provider = planner_provider
        self._step_delay = step_delay
        self._recovery_budget = recovery_budget

    @weave_op
    def run(
        self,
        env,  # RLEnvironment
        demo: Demo,
        task_config: Any,
        screenshot_dir: Any | None = None,
    ) -> tuple[float, list[bytes]]:
        """Execute a demo against a live environment.

        Args:
            env: RLEnvironment (already reset).
            demo: Demo to execute.
            task_config: TaskConfig for milestone evaluation.
            screenshot_dir: Optional directory to save screenshots.

        Returns:
            (score, screenshots) -- score from evaluate_dense().
        """
        import time as _time

        from openadapt_evals.adapters.rl_env import ResetConfig
        _t0 = _time.time()
        _tier1 = 0
        _tier2 = 0

        if not demo.steps:
            raise ActionExecutionError("Demo has no executable steps")

        try:
            from openadapt_evals.telemetry import track_demo_execution
            track_demo_execution(
                phase="start", task_id=task_config.id,
                num_steps=len(demo.steps),
            )
        except Exception:
            pass

        obs = env.reset(config=ResetConfig(task_id=task_config.id))
        screenshots: list[bytes] = []
        if obs.screenshot:
            screenshots.append(obs.screenshot)
            if screenshot_dir:
                (screenshot_dir / "step_00_reset.png").write_bytes(obs.screenshot)

        for i, step in enumerate(demo.steps):
            logger.info(
                "Demo step %d/%d: %s %s -- %s",
                i + 1, len(demo.steps),
                step.action_type,
                step.action_value or "",
                step.description,
            )

            # Phase 4: Pre-click state narrowing
            if (
                step.action_type in ("click", "double_click")
                and step.grounding_target is not None
                and obs.screenshot
            ):
                ok, reason = check_state_preconditions(
                    obs.screenshot,
                    step.grounding_target,
                    ocr_fn=None,
                )
                if not ok:
                    logger.warning(
                        "Step %d: state precondition failed: %s",
                        i + 1, reason,
                    )
                    # Observational in Phase 4 -- proceed anyway.
                    # Blocking / state recovery deferred to later phase.

            action = self._execute_step(step, obs)
            if action is None:
                raise ActionExecutionError(
                    f"Demo step {i + 1} did not produce an action"
                )
            if action.type == "error":
                raise ActionExecutionError(
                    f"Demo step {i + 1} failed before actuation: "
                    f"{action.raw_action or {}}"
                )

            # Count tiers for telemetry
            tier = (action.raw_action or {}).get("tier", 2)
            if tier == 1:
                _tier1 += 1
            else:
                _tier2 += 1

            # Execute the action
            step_result = self._dispatch_action(env, action)
            obs = step_result.observation

            # Phase 4: Post-click transition verification
            if (
                step.action_type in ("click", "double_click")
                and step.grounding_target is not None
                and obs.screenshot
            ):
                ok, reason = verify_transition(
                    obs.screenshot,
                    step.grounding_target,
                    ocr_fn=None,
                )
                if not ok:
                    logger.warning(
                        "Step %d: transition verification failed: %s",
                        i + 1, reason,
                    )

            if obs.screenshot:
                screenshots.append(obs.screenshot)
                if screenshot_dir:
                    fname = f"step_{i + 1:02d}.png"
                    (screenshot_dir / fname).write_bytes(obs.screenshot)

            # Per-step milestone check (high-water mark)
            if task_config.milestones:
                passed, total = env.check_milestones_incremental(
                    obs.screenshot,
                )
                if passed > 0:
                    logger.info(
                        "Step %d: milestones %d/%d (high-water)",
                        i + 1, passed, total,
                    )

            time.sleep(self._step_delay)

        # Final evaluation
        if task_config.milestones:
            score = env.evaluate_dense()
        else:
            score = env.evaluate()

        try:
            from openadapt_evals.telemetry import track_demo_execution
            track_demo_execution(
                phase="completed", task_id=task_config.id,
                num_steps=len(demo.steps), score=score,
                duration_seconds=_time.time() - _t0,
                tier1_count=_tier1, tier2_count=_tier2,
            )
        except Exception:
            pass

        return score, screenshots

    def _try_text_anchoring(
        self,
        screenshot: bytes,
        step: DemoStep,
    ) -> BenchmarkAction | None:
        """Attempt to ground a click via OCR text anchoring (Tier 1.5a).

        Creates a :class:`GroundingTarget` from the step and runs OCR-based
        text matching.  If the best candidate scores above ``0.85``, returns
        a click action at those coordinates.  Otherwise returns ``None`` so
        the caller falls through to the VLM grounder (Tier 2).

        Args:
            screenshot: Current screenshot PNG bytes.
            step: The demo step being executed.

        Returns:
            A :class:`BenchmarkAction` if text anchoring succeeds with high
            confidence, or ``None`` to fall through.
        """
        # Build target from step's grounding_target or description
        if step.grounding_target is not None and isinstance(
            step.grounding_target, GroundingTarget
        ):
            target = step.grounding_target
        else:
            description = step.description or step.target_description
            if not description:
                description = step.action_description
            if not description:
                return None
            target = GroundingTarget(description=description)

        if not target.description:
            return None

        # Run OCR and text grounding
        ocr_results = run_ocr(screenshot)
        if not ocr_results:
            logger.debug("Tier 1.5a: no OCR results, falling through to VLM")
            return None

        candidates = ground_by_text(screenshot, target, ocr_results=ocr_results)
        if not candidates:
            logger.debug(
                "Tier 1.5a: no text matches for %r, falling through to VLM",
                target.description,
            )
            return None

        best = candidates[0]
        if best.local_score > 0.85:
            logger.info(
                "Tier 1.5a (text anchor): %r matched %r at %s (score=%.2f)",
                target.description,
                best.matched_text,
                best.point,
                best.local_score,
            )
            return BenchmarkAction(
                type="click",
                x=best.point[0],
                y=best.point[1],
                raw_action={"tier": 1.5, "source": "ocr_text_anchor"},
            )

        logger.debug(
            "Tier 1.5a: best score %.2f < 0.85 for %r, falling through",
            best.local_score,
            target.description,
        )
        return None

    @weave_op
    def _execute_step(
        self,
        step: DemoStep,
        obs: BenchmarkObservation,
    ) -> BenchmarkAction | None:
        """Produce an action for a demo step using tiered intelligence.

        Tier 1: keyboard/type -> direct execution (no VLM).
        Tier 1.5a: click -> OCR text anchoring (cheap, no VLM).
        Tier 2: click -> grounder finds element by description.
        Tier 3: recovery -> planner reasons about unexpected state.
        """
        if step.action_type == "key":
            # Tier 1: deterministic keyboard action
            key = step.action_value
            if not key:
                logger.warning("Key step with no action_value: %s", step)
                return None
            logger.info("Tier 1 (direct): key=%s", key)
            return BenchmarkAction(type="key", key=key, raw_action={"tier": 1})

        if step.action_type == "type":
            # Tier 1: deterministic type action
            text = step.action_value or step.description
            if not text:
                logger.warning("Type step with no value: %s", step)
                return None
            logger.info("Tier 1 (direct): type=%r", text)
            return BenchmarkAction(type="type", text=text, raw_action={"tier": 1})

        if step.action_type == "click":
            # Tier 1.5a: try OCR text anchoring first
            if obs.screenshot:
                text_action = self._try_text_anchoring(obs.screenshot, step)
                if text_action is not None:
                    return text_action

            # Tier 2: grounder finds element by description
            description = step.description or step.target_description
            if not description:
                description = step.action_description
            logger.info("Tier 2 (grounder): %s", description)
            return self._ground_click(obs, description)

        if step.action_type == "double_click":
            # Tier 1.5a: try OCR text anchoring first
            if obs.screenshot:
                text_action = self._try_text_anchoring(obs.screenshot, step)
                if text_action is not None:
                    return BenchmarkAction(
                        type="double_click",
                        x=text_action.x,
                        y=text_action.y,
                        raw_action=text_action.raw_action,
                    )

            description = step.description or step.target_description
            logger.info("Tier 2 (grounder): double-click %s", description)
            action = self._ground_click(obs, description)
            if action and action.type == "click":
                return BenchmarkAction(
                    type="double_click", x=action.x, y=action.y,
                )
            return action

        # Unknown action type -- log and skip
        logger.warning("Unknown action type %r, skipping", step.action_type)
        return None

    def _ground_click(
        self,
        obs: BenchmarkObservation,
        description: str,
    ) -> BenchmarkAction:
        """Find a UI element by description and return a click action.

        When ``grounder_endpoint`` is set, uses the UI-Venus native bbox
        format via an OpenAI-compatible HTTP endpoint (vLLM, Ollama, etc.)
        for much better grounding accuracy than a general VLM.

        Otherwise falls back to a general VLM API call.
        """
        if self._grounder_endpoint:
            return self._ground_click_http(obs, description)
        return self._ground_click_vlm(obs, description)

    def _ground_click_http(
        self,
        obs: BenchmarkObservation,
        description: str,
    ) -> BenchmarkAction:
        """Ground via HTTP endpoint (UI-Venus, UI-TARS, etc.).

        Uses the UI-Venus native grounding prompt format (same as
        ``PlannerGrounderAgent._call_grounder_http``) which outputs
        ``[x1, y1, x2, y2]`` bounding boxes. The center of the bbox
        is returned as the click coordinate.

        Compatible with vLLM, Ollama, or any OpenAI-compatible server
        serving ``inclusionAI/UI-Venus-1.5-8B``.
        """
        import base64

        import requests

        endpoint = self._grounder_endpoint.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        url = f"{endpoint}/chat/completions"

        # Use the same UI-Venus native prompt as PlannerGrounderAgent
        prompt = (
            f"Outline the position corresponding to the instruction: "
            f"{description}.\n"
            f"The output should be only [x1,y1,x2,y2]."
        )

        content = [
            {"type": "text", "text": prompt},
        ]
        if obs.screenshot:
            b64 = base64.b64encode(obs.screenshot).decode()
            content.insert(0, {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        try:
            resp = requests.post(url, json={
                "model": "UI-Venus-1.5-8B",
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 128,
                "temperature": 0.0,
            }, timeout=60)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("HTTP grounder failed: %s", exc)
            return BenchmarkAction(
                type="error",
                raw_action={"grounder_error": str(exc)},
            )

        logger.info("HTTP grounder: %s", raw[:200])

        # Parse [x1,y1,x2,y2] bbox -> center click
        from openadapt_evals.agents.planner_grounder_agent import (
            PlannerGrounderAgent,
        )
        return PlannerGrounderAgent._parse_bbox_to_action(raw)

    def _ground_click_vlm(
        self,
        obs: BenchmarkObservation,
        description: str,
    ) -> BenchmarkAction:
        """Ground via general VLM API (gpt-4.1-mini, etc.)."""
        from openadapt_evals.vlm import vlm_call

        prompt = (
            f"Where should I click to interact with: {description}\n"
            f'Output JSON: {{"type": "click", "x": 0.0-1.0, "y": 0.0-1.0}}'
        )
        images = [obs.screenshot] if obs.screenshot else None

        raw = vlm_call(
            prompt,
            images=images,
            system=(
                "You are a GUI grounding model. Given a screenshot and a "
                "natural-language description, output the click coordinates "
                "as JSON with normalized x,y (0.0-1.0)."
            ),
            model=self._grounder_model,
            provider=self._grounder_provider,
            max_tokens=128,
            cost_label="demo_executor_grounder",
        )

        from openadapt_evals.errors import ActionParseError
        from openadapt_evals.training.trl_rollout import parse_action_json

        try:
            action = parse_action_json(raw)
        except ActionParseError as exc:
            return BenchmarkAction(
                type="error",
                raw_action={"grounder_error": str(exc)},
            )

        if action.type != "click":
            return BenchmarkAction(
                type="error",
                raw_action={
                    "grounder_error": (
                        f"grounder returned {action.type!r}; click required"
                    )
                },
            )
        return action

    def _dispatch_action(self, env, action: BenchmarkAction):
        """Execute an action through the environment."""
        if action.type in ("click", "double_click", "right_click"):
            if action.x is None or action.y is None:
                raise ActionExecutionError(f"{action.type} requires x and y")
            x, y = float(action.x), float(action.y)
            if 0 <= x <= 1 and 0 <= y <= 1:
                return env.pixel_action(
                    x_frac=x, y_frac=y,
                    action_type=action.type,
                    text=action.text,
                    key=action.key,
                )
            else:
                return env.pixel_action(
                    x=int(x), y=int(y),
                    action_type=action.type,
                    text=action.text,
                    key=action.key,
                )
        return env.step(action)
