"""Planner-Grounder agent for GUI automation.

Separates "what to do" (planner) from "where to click" (grounder).

The planner sees the screenshot + accessibility tree and outputs a
high-level instruction. The grounder sees the screenshot + instruction
and outputs precise pixel coordinates.

This architecture follows the planner-grounder paradigm established in
the GUI agent literature. The separation of planning (high-level action
selection) from grounding (precise element localization) is a
well-established pattern with extensive prior art.

Usage:
    from openadapt_evals.agents import PlannerGrounderAgent

    # Both planner and grounder as API model names
    agent = PlannerGrounderAgent(
        planner="claude-sonnet-4-20250514",
        grounder="gpt-4.1-mini",
        planner_provider="anthropic",
        grounder_provider="openai",
    )

    # Planner as existing agent instance, grounder via HTTP
    agent = PlannerGrounderAgent(
        planner=my_api_agent,
        grounder="http",
        grounder_provider="http",
        grounder_endpoint="http://gpu-box:8080",
    )

Prior Art:
    - SeeAct: Zheng et al., "GPT-4V(ision) is a Generalist Web Agent,
      if Grounded", ICML 2024. Introduced the "action generation +
      action grounding" two-stage paradigm for web agents.
    - UFO: Zhang et al., "UFO: A UI-Focused Agent for Windows OS
      Interaction", arXiv 2402.07939, 2024. HostAgent + AppAgent
      architecture for Windows desktop automation.
    - Mind2Web: Deng et al., "Mind2Web: Towards a Generalist Agent for
      the Web", NeurIPS 2023. Early planner-grounder pattern for GUI
      agents with candidate element ranking.
    - MindAct: Deng et al., "Mind2Web", NeurIPS 2023 (same work).
      Formalized the element-level action prediction pipeline.
    - Agent S2: Agashe et al., "Agent S2: A Compositional Generalist-
      Specialist Framework for Computer Use", 2025.
    - CODA: Wang et al., "CODA: Computer Agent Data Generation via
      Grounded Decomposition and Augmentation", 2025.
    - STRIPS: Fikes & Nilsson, "STRIPS: A New Approach to the
      Application of Theorem Proving to Problem Solving", AIJ 1971.
      The plan-then-execute paradigm dates back 55 years.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openadapt_evals.action_envelope import parse_single_json_object
from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import (
    BenchmarkAgent,
    action_to_string,
    format_accessibility_tree,
)
from openadapt_evals.errors import ActionParseError

try:
    from openadapt_evals.integrations.weave_integration import weave_op
except ImportError:
    weave_op = lambda fn: fn  # noqa: E731

if TYPE_CHECKING:
    from openadapt_evals.training.planner_cache import PlannerCache
    from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger

logger = logging.getLogger(__name__)

# Maximum number of recent actions to include in planner context.
_MAX_HISTORY_ACTIONS = 10

# Prompt templates -------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are a desktop automation planner. You MUST follow the task "
    "instruction exactly. Given the current screenshot, accessibility tree, "
    "and task, decide the single next action the agent should take. "
    "Be precise and unambiguous in your instruction. "
    "IMPORTANT: Only interact with applications and UI elements that are "
    "relevant to the task instruction. Do NOT open or click on applications "
    "that the task does not ask you to use."
)

_PLANNER_PROMPT = """\
=== YOUR TASK (follow this EXACTLY) ===
{task_instruction}
=== END TASK ===

IMPORTANT: Every action you take MUST work toward completing the task above.
Do NOT open or interact with applications that the task does not require.
If you see icons on the desktop (e.g., Chrome, Edge, Recycle Bin) that are NOT
related to your task, IGNORE them. Only interact with what the task asks for.

If the application you need is not visible on the desktop or taskbar:
- Use the Start menu (click the Start button or press the Windows key)
- Or use Win+R (Run dialog) to launch it by name
- Do NOT default to clicking desktop icons that are unrelated to your task

Previous actions:
{action_history}

Accessibility tree:
{a11y_tree}
{demo_guidance}
Look at the screenshot and decide the next action to advance the TASK above.

Output a JSON object with exactly these fields:
{{"decision": "COMMAND" | "DONE" | "FAIL",
  "action_type": "click" | "double_click" | "type" | "key" | "scroll",
  "action_value": "<text to type, key to press, or empty for click/double_click>",
  "target_description": "<what element to interact with>",
  "reasoning": "<brief explanation of how this action advances the task>"}}

Rules:
- action_type must be exactly ONE of: click, double_click, type, key, scroll
- For click: target_description describes WHAT to click. action_value is empty. Use for buttons, menus, links, and UI controls.
- For double_click: use to open/launch applications, files, or desktop icons. action_value is empty.
- For type: action_value is the text to type. Append \\n to submit/press Enter after typing.
- For key: action_value is the key (e.g., "Enter", "Tab", "Ctrl+A").
- For scroll: action_value is "up" or "down".
- Output ONE action per response. Never combine multiple actions.
- Do NOT include pixel coordinates — a grounding model handles that.
- If there are dialog boxes, notifications, or popups blocking your target, dismiss them first (click X, press Escape, or click 'Not now'/'Later'/'Skip').
- If your last 3 actions were the same and failed, you MUST try a completely different approach: dismiss any dialogs, try keyboard shortcuts, or interact with different UI elements.
- Your reasoning MUST explain how the action relates to the task instruction.
{anti_loop_warning}"""

# Warning injected when repeated identical actions are detected.
_ANTI_LOOP_WARNING = (
    "\n⚠ WARNING: You are stuck in a loop. Your last {n} actions were "
    "identical and failed. You MUST try a completely different approach. "
    "Do NOT repeat the same action again.\n"
)

# Stronger warning that references demo strategy when available.
_ANTI_LOOP_WARNING_WITH_DEMO = (
    "\n⚠ WARNING: You are stuck in a loop. Your last {n} actions were "
    "identical and failed. You MUST try a completely different approach.\n\n"
    "A demonstration strategy is available (see above). Use it now:\n"
    "- If the demo mentions keyboard shortcuts (e.g., Ctrl+Shift+Delete, "
    "Win+R, Alt+F4), USE THEM instead of clicking.\n"
    "- If the demo suggests a different sequence of steps, follow that "
    "sequence instead of your current approach.\n"
    "- Prefer 'key' actions over 'click' actions when stuck — keyboard "
    "shortcuts bypass UI elements that may be unresponsive or mislocated.\n\n"
    "Do NOT repeat the same action. Pick a DIFFERENT action type.\n"
)

# Number of consecutive identical actions that triggers the anti-loop warning.
_ANTI_LOOP_THRESHOLD = 3

_GROUNDER_SYSTEM = (
    "You are a GUI grounding model. Given a screenshot and a natural-language "
    "instruction, output the precise coordinates of the element to interact with."
)

# UI-Venus native grounding format — outputs [x1,y1,x2,y2] bounding box
_GROUNDER_PROMPT_BBOX = """\
Outline the position corresponding to the instruction: {instruction}.
The output should be only [x1,y1,x2,y2].
"""

# Generic JSON format for non-UI-Venus grounders
_GROUNDER_PROMPT_JSON = """\
Instruction: {instruction}

Look at the screenshot and output a JSON object describing the action:
{{"type": "click" | "type" | "key" | "scroll" | "drag", "x": <0.0-1.0>, "y": <0.0-1.0>, "text": "...", "key": "..."}}

Include only the fields relevant to the action type.
"""


class PlannerGrounderAgent(BenchmarkAgent):
    """Planner-Grounder architecture for GUI automation.

    Separates "what to do" (planner) from "where to click" (grounder),
    following the pattern established by SeeAct (ICML 2024), UFO2
    (Microsoft 2025), and CODA (2025).

    The planner sees the screenshot + accessibility tree and outputs a
    high-level instruction. The grounder sees the screenshot + instruction
    and outputs precise pixel coordinates.

    Args:
        planner: BenchmarkAgent instance or API model name string.
        grounder: BenchmarkAgent instance or API model name string.
        planner_provider: VLM provider for planner when using a model name
            (``"anthropic"`` or ``"openai"``).
        grounder_provider: VLM provider for grounder when using a model name
            (``"anthropic"``, ``"openai"``, or ``"http"``).
        grounder_endpoint: HTTP endpoint URL when grounder_provider is
            ``"http"``.
        max_history: Maximum number of recent actions to show the planner.
        planner_cache: Optional :class:`PlannerCache` instance. When provided,
            planner API responses are cached and reused for visually similar
            screenshots with the same task and action history, reducing API
            costs during GRPO training.
        trajectory_logger: Optional PlannerTrajectoryLogger instance. When
            provided, each planner call's inputs and outputs are logged for
            later use as SFT training data.
    """

    def __init__(
        self,
        planner: BenchmarkAgent | str,
        grounder: BenchmarkAgent | str,
        planner_provider: str = "anthropic",
        grounder_provider: str = "anthropic",
        grounder_endpoint: str | None = None,
        max_history: int = _MAX_HISTORY_ACTIONS,
        planner_cache: PlannerCache | None = None,
        trajectory_logger: PlannerTrajectoryLogger | None = None,
    ):
        self._planner = planner
        self._grounder = grounder
        self._planner_provider = planner_provider
        self._grounder_provider = grounder_provider
        self._grounder_endpoint = grounder_endpoint
        self._max_history = max_history
        self._planner_cache = planner_cache
        self._trajectory_logger = trajectory_logger

        # Optional demo guidance text injected into the planner prompt.
        # Set externally (e.g., by DemoGuidedAgent) or left empty.
        self.demo_guidance: str = ""

        # Internal action history for planner context.
        self._action_history: list[str] = []

        # Pending action queue for compound actions (e.g., type then Enter).
        self._pending_actions: list[BenchmarkAction] = []

        # Step counter for trajectory logging (reset per episode).
        self._step_index: int = 0

        # Validate grounder_endpoint when using HTTP provider.
        if grounder_provider == "http" and isinstance(grounder, str):
            if not grounder_endpoint:
                raise ValueError(
                    "grounder_endpoint is required when grounder_provider='http'"
                )

        logger.info(
            "PlannerGrounderAgent initialized: "
            "planner=%s (%s), grounder=%s (%s)",
            planner if isinstance(planner, str) else type(planner).__name__,
            planner_provider,
            grounder if isinstance(grounder, str) else type(grounder).__name__,
            grounder_provider,
        )

    @weave_op
    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Execute the planner-grounder pipeline.

        1. Call the planner with screenshot + a11y tree + task to get a
           high-level instruction.
        2. If the planner says DONE or FAIL, return immediately.
        3. Call the grounder with screenshot + instruction to get precise
           coordinates.
        4. Return the grounded action.

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action to execute.
        """
        # -- Step 0: Drain pending action queue -----------------------------
        if self._pending_actions:
            action = self._pending_actions.pop(0)
            self._action_history.append(
                f"{action_to_string(action)} (queued)"
            )
            return action

        # -- Step 0b: Force keyboard shortcut from demo after extended loop ---
        # If the planner has ignored anti-loop warnings for 2+ rounds
        # (6+ consecutive identical actions), bypass the planner entirely
        # and emit the first unused keyboard shortcut from the demo.
        if self.demo_guidance and len(self._action_history) >= 6:
            import re as _re

            recent6 = self._action_history[-6:]
            instrs6 = []
            for entry in recent6:
                m = _re.search(r"\(instruction:\s*(.+)\)\s*$", entry)
                if m:
                    instrs6.append(m.group(1).strip())
            if len(instrs6) == 6 and len(set(instrs6)) == 1:
                # Extract keyboard shortcuts from demo guidance text
                shortcuts = _re.findall(
                    r"\[([a-z+]+)\]", self.demo_guidance, _re.IGNORECASE,
                )
                if shortcuts:
                    # Pick the first shortcut the agent hasn't tried
                    shortcut = shortcuts[0]
                    logger.warning(
                        "Anti-loop FORCED OVERRIDE: bypassing planner, "
                        "emitting keyboard shortcut %r from demo",
                        shortcut,
                    )
                    action = BenchmarkAction(type="key", key=shortcut)
                    self._action_history.append(
                        f"KEY({shortcut}) (forced-override: {shortcut})"
                    )
                    return action

        # -- Step 1: Call planner ------------------------------------------
        planner_output = self._call_planner(observation, task)

        raw_decision = planner_output.get("decision", "COMMAND")
        decision = raw_decision.upper() if isinstance(raw_decision, str) else ""
        reasoning = planner_output.get("reasoning", "")

        # Extract structured fields (new format) with backward-compat
        # fallback to the old "instruction" field.
        raw_action_type = planner_output.get("action_type", "")
        action_type = (
            raw_action_type.lower() if isinstance(raw_action_type, str) else ""
        )
        action_value = planner_output.get("action_value", "")
        target_description = planner_output.get("target_description", "")
        instruction = planner_output.get("instruction", target_description)

        logger.info(
            "Planner decision=%s, instruction=%r, reasoning=%r",
            decision,
            instruction,
            reasoning,
        )

        # -- Log trajectory if logger is attached ---------------------------
        if self._trajectory_logger is not None:
            try:
                self._trajectory_logger.log_step(
                    episode_id=task.task_id,
                    step_index=self._step_index,
                    screenshot_bytes=observation.screenshot,
                    a11y_tree=observation.accessibility_tree,
                    task_instruction=task.instruction,
                    action_history=list(self._action_history),
                    planner_output=planner_output,
                )
            except Exception:
                logger.warning(
                    "Trajectory logging failed at step %d", self._step_index,
                    exc_info=True,
                )
            self._step_index += 1

        if decision == "DONE":
            self._action_history.append("DONE()")
            return BenchmarkAction(
                type="done",
                raw_action={
                    "planner_output": planner_output,
                    "source": "planner",
                },
            )

        if decision in ("FAIL", "ERROR"):
            self._action_history.append("FAIL()")
            error = (
                planner_output.get("error")
                or reasoning
                or "planner reported failure"
            )
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "source": "planner",
                    "fail_reason": reasoning,
                    "error": error,
                    "error_type": planner_output.get("error_type", "agent"),
                },
            )

        if decision != "COMMAND":
            logger.warning("Planner returned an unknown decision: %r", raw_decision)
            self._action_history.append("ERROR() [invalid decision]")
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "parse_error": "invalid_decision",
                    "error": f"unknown planner decision: {raw_decision!r}",
                    "error_type": "agent",
                },
            )

        supported_action_types = {"click", "double_click", "type", "key", "scroll"}
        if not isinstance(raw_action_type, str) or (
            action_type and action_type not in supported_action_types
        ):
            logger.warning(
                "Planner returned an unknown structured action_type: %r",
                raw_action_type,
            )
            self._action_history.append("ERROR() [invalid action type]")
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "parse_error": "invalid_action_type",
                    "error": f"unknown structured action type: {raw_action_type!r}",
                    "error_type": "agent",
                },
            )

        if action_type == "scroll" and (
            not isinstance(action_value, str)
            or action_value.strip().lower() not in {"up", "down"}
        ):
            logger.warning("Planner returned an invalid scroll direction: %r", action_value)
            self._action_history.append("ERROR() [invalid scroll direction]")
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "parse_error": "invalid_scroll_direction",
                    "error": f"invalid scroll direction: {action_value!r}",
                    "error_type": "agent",
                },
            )

        if action_type in {"click", "double_click"} and (
            not isinstance(target_description, str)
            or not target_description.strip()
        ):
            logger.warning("Structured pointer action has no target description")
            self._action_history.append("ERROR() [missing target description]")
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "parse_error": "missing_target_description",
                    "error": "structured pointer action requires a target description",
                    "error_type": "agent",
                },
            )

        if not instruction and not action_type:
            logger.warning("Planner returned an empty instruction")
            self._action_history.append("ERROR() [empty instruction]")
            return BenchmarkAction(
                type="error",
                raw_action={
                    "planner_output": planner_output,
                    "parse_error": "empty_instruction",
                },
            )

        # -- Step 2: Build action from structured fields or instruction ------
        action = self._build_action_from_structured(
            action_type, action_value, target_description,
        )
        if action is not None:
            # Structured path succeeded — nothing more to do.
            pass
        else:
            # Fallback: parse free-form instruction (backward compat).
            non_click_action = self._parse_non_click_action(instruction)
            if non_click_action is not None:
                action = non_click_action
                # Safety net: check for continuation phrases like
                # "then press Enter" that indicate a compound instruction.
                remaining = self._extract_continuation(instruction)
                if remaining:
                    followup = self._parse_non_click_action(remaining)
                    if followup:
                        self._pending_actions.append(followup)
            else:
                # Call grounder for click-type actions
                action = self._call_grounder(observation, instruction)

                # If the planner requested double_click, override the
                # grounder's returned "click" type to "double_click".
                if action_type == "double_click" and action.type == "click":
                    action = BenchmarkAction(
                        type="double_click",
                        x=action.x,
                        y=action.y,
                        target_node_id=action.target_node_id,
                        target_bbox=action.target_bbox,
                        raw_action=action.raw_action,
                    )

        # Record for history.
        action_str = action_to_string(action)
        self._action_history.append(f"{action_str} (instruction: {instruction})")

        # Attach planner metadata to the action for debugging.
        if action.raw_action is None:
            action.raw_action = {}
        action.raw_action["planner_output"] = planner_output

        return action

    @staticmethod
    def _parse_non_click_action(instruction: str) -> BenchmarkAction | None:
        """Detect type/key/scroll/double-click actions from planner instruction.

        The grounder only returns click coordinates. For type/key/scroll
        actions, we parse the action directly from the planner's
        instruction text.

        Returns None if the instruction is a plain click action (needs grounder).
        Returns a ``double_click`` action if the instruction mentions
        "double-click" or "open" (launching an application/file).
        """
        import re

        lower = instruction.lower()

        # Detect explicit "double-click" or "double click" instructions.
        # These need the grounder for coordinates, so we return a marker
        # action with type="double_click" but x/y=None — the caller
        # will route through the grounder and override the type.
        if re.search(r"double[\s-]?click", lower):
            logger.info(
                "Planner instruction parsed as DOUBLE_CLICK: %r", instruction,
            )
            # Return None so the grounder is called, but signal double_click
            # via a special return. The caller checks instruction text too.
            return None

        # Detect "type 'text'" or "type "text"" patterns
        type_match = re.search(
            r"(?:type|enter|input|write)\s+['\"]([^'\"]+)['\"]",
            instruction, re.IGNORECASE,
        )
        if type_match:
            text = type_match.group(1)
            logger.info("Planner instruction parsed as TYPE action: %r", text)
            return BenchmarkAction(type="type", text=text)

        # Detect keyboard shortcuts with modifier+key combos
        mod_key_match = re.search(
            r"press\s+(ctrl|alt|shift)\s*\+\s*(\w+)", instruction, re.IGNORECASE
        )
        if mod_key_match:
            modifier = mod_key_match.group(1).lower()
            key = mod_key_match.group(2).lower()
            logger.info("Planner instruction parsed as KEY action: %s+%s", modifier, key)
            return BenchmarkAction(type="key", key=key, modifiers=[modifier])

        # Detect single key presses
        single_key_match = re.search(
            r"press\s+(enter|return|tab|escape|esc|backspace|delete|space)",
            instruction, re.IGNORECASE,
        )
        if single_key_match:
            key = single_key_match.group(1).lower()
            logger.info("Planner instruction parsed as KEY action: %s", key)
            return BenchmarkAction(type="key", key=key)

        # Detect scroll
        if "scroll down" in lower:
            return BenchmarkAction(type="scroll", scroll_direction="down")
        if "scroll up" in lower:
            return BenchmarkAction(type="scroll", scroll_direction="up")

        # Default: needs grounder (it's a click action)
        return None

    def _build_action_from_structured(
        self,
        action_type: str,
        action_value: str,
        target_description: str,
    ) -> BenchmarkAction | None:
        """Build a BenchmarkAction from structured planner fields.

        Returns ``None`` when the planner output does not contain structured
        fields (backward-compat path) or when the action type is ``click``
        (needs the grounder for coordinates).
        """
        if not action_type:
            return None

        if action_type == "type":
            text = action_value
            if text.endswith("\\n"):
                # Strip the literal "\n" suffix and queue a follow-up Enter.
                text = text[:-2]
                self._pending_actions.append(
                    BenchmarkAction(type="key", key="enter")
                )
            logger.info(
                "Structured planner output: TYPE %r (pending=%d)",
                text, len(self._pending_actions),
            )
            return BenchmarkAction(type="type", text=text)

        if action_type == "key":
            key_str = action_value.strip()
            # Handle modifier combos like "Ctrl+A"
            if "+" in key_str:
                parts = key_str.split("+")
                key = parts[-1].lower()
                modifiers = [p.lower() for p in parts[:-1]]
                logger.info("Structured planner output: KEY %s+%s", modifiers, key)
                return BenchmarkAction(type="key", key=key, modifiers=modifiers)
            logger.info("Structured planner output: KEY %s", key_str.lower())
            return BenchmarkAction(type="key", key=key_str.lower())

        if action_type == "scroll":
            direction = action_value.strip().lower()
            logger.info("Structured planner output: SCROLL %s", direction)
            return BenchmarkAction(type="scroll", scroll_direction=direction)

        if action_type == "click":
            # Click needs grounder — return None so the caller invokes it
            # using target_description as the instruction.
            return None

        if action_type == "double_click":
            # Double-click also needs grounding for coordinates — return None
            # so the caller invokes the grounder, but store the action type
            # so we can set it on the returned action.
            return None

        logger.warning("Unknown structured action_type: %r", action_type)
        return None

    @staticmethod
    def _extract_continuation(instruction: str) -> str | None:
        """Extract a follow-up action from compound instruction text.

        Looks for continuation phrases like "then press Enter",
        "and then press Tab", "followed by pressing Enter".

        Returns the follow-up portion of the instruction, or None.
        """
        import re

        # Patterns that indicate a second action after the first
        match = re.search(
            r"(?:,?\s*(?:and\s+)?then\s+|,?\s*followed\s+by\s+)"
            r"(press\s+\S+.*|type\s+.+)",
            instruction,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        return None

    def reset(self) -> None:
        """Reset agent state between episodes."""
        self._action_history.clear()
        self._pending_actions.clear()
        self._step_index = 0

        if hasattr(self._planner, "reset"):
            self._planner.reset()
        if hasattr(self._grounder, "reset"):
            self._grounder.reset()

    # -- Private helpers ---------------------------------------------------

    def _check_action_loop(self) -> str:
        """Detect repeated identical planner instructions and return a warning.

        Compares the last ``_ANTI_LOOP_THRESHOLD`` entries in the action
        history. If they all share the same instruction text (extracted
        from the ``(instruction: ...)`` suffix appended by ``act()``),
        returns an anti-loop warning string to inject into the planner
        prompt. Otherwise returns an empty string.

        When demo guidance is available (``self.demo_guidance`` is non-empty),
        the warning explicitly directs the planner to use keyboard shortcuts
        and alternative actions from the demonstration strategy, since the
        demo often contains fallback approaches (e.g., Ctrl+Shift+Delete)
        that work when clicking fails.

        The comparison uses exact string matching on the instruction
        portion of the history entry (the text after ``(instruction: ``
        and before the closing ``)``).
        """
        threshold = _ANTI_LOOP_THRESHOLD
        if len(self._action_history) < threshold:
            return ""

        recent = self._action_history[-threshold:]

        # Extract instruction text from history entries.
        import re

        instructions: list[str] = []
        for entry in recent:
            m = re.search(r"\(instruction:\s*(.+)\)\s*$", entry)
            if m:
                instructions.append(m.group(1).strip())
            else:
                # Entry without instruction suffix (e.g. DONE, queued).
                return ""

        if len(instructions) < threshold:
            return ""

        # Check if all instructions are identical.
        if len(set(instructions)) == 1:
            logger.warning(
                "Anti-loop: last %d instructions are identical: %r",
                threshold,
                instructions[0],
            )
            # Use the demo-aware warning when demo guidance is available,
            # so the planner is directed to use keyboard shortcuts and
            # alternative approaches from the demonstration.
            if self.demo_guidance:
                logger.info(
                    "Anti-loop: demo guidance available, injecting "
                    "demo-strategy recovery prompt"
                )
                return _ANTI_LOOP_WARNING_WITH_DEMO.format(n=threshold)
            return _ANTI_LOOP_WARNING.format(n=threshold)

        return ""

    @weave_op
    def _call_planner(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
    ) -> dict[str, Any]:
        """Call the planner to get a high-level instruction.

        When a :class:`PlannerCache` is configured, checks the cache before
        making an API call and stores the result on a miss.

        Returns:
            Dict with keys ``decision``, ``reasoning``, and either
            structured fields (``action_type``, ``action_value``,
            ``target_description``) or the legacy ``instruction`` field.
        """
        if not isinstance(self._planner, str):
            # Delegate to the agent's act() — interpret the returned action.
            action = self._planner.act(observation, task)
            return _action_to_planner_output(action)

        # -- Check planner cache (before API call) ----------------------------
        screenshot_bytes = observation.screenshot
        if self._planner_cache is not None and screenshot_bytes:
            cached = self._planner_cache.get(
                screenshot_bytes, task.instruction, self._action_history,
            )
            if cached is not None:
                return cached

        # String model name — use vlm_call directly.
        a11y_text = "(not available)"
        if observation.accessibility_tree:
            a11y_text = format_accessibility_tree(observation.accessibility_tree)

        history_text = "(none)" if not self._action_history else "\n".join(
            f"  {i + 1}. {a}"
            for i, a in enumerate(self._action_history[-self._max_history :])
        )

        # Check for repeated identical actions and inject anti-loop warning.
        anti_loop_warning = self._check_action_loop()

        # Build optional demo guidance section.
        demo_guidance_text = ""
        if self.demo_guidance:
            demo_guidance_text = f"\n{self.demo_guidance}\n"

        prompt = _PLANNER_PROMPT.format(
            task_instruction=task.instruction,
            action_history=history_text,
            a11y_tree=a11y_text,
            demo_guidance=demo_guidance_text,
            anti_loop_warning=anti_loop_warning,
        )

        logger.info(
            "Planner task instruction: %r", task.instruction,
        )
        logger.debug("Planner full prompt:\n%s", prompt[:2000])

        images = [observation.screenshot] if observation.screenshot else None

        from openadapt_evals.vlm import extract_json, vlm_call

        raw = vlm_call(
            prompt,
            images=images,
            system=_PLANNER_SYSTEM,
            model=self._planner,
            provider=self._planner_provider,
            max_tokens=512,
            cost_label="planner",
        )

        logger.debug("Planner raw output: %s", raw[:500])

        parsed = extract_json(raw)
        if parsed is None:
            logger.warning("Failed to parse planner JSON, raw=%s", raw[:200])
            parsed = {"decision": "COMMAND", "instruction": raw.strip(), "reasoning": ""}
        elif not isinstance(parsed, dict):
            logger.warning("Planner JSON is not a dict: %s", type(parsed))
            parsed = {"decision": "COMMAND", "instruction": str(parsed), "reasoning": ""}

        # -- Store in planner cache (after API call) --------------------------
        if self._planner_cache is not None and screenshot_bytes:
            self._planner_cache.put(
                screenshot_bytes, task.instruction, self._action_history, parsed,
            )

        return parsed

    def _call_grounder(
        self,
        observation: BenchmarkObservation,
        instruction: str,
        *,
        _retry: bool = True,
    ) -> BenchmarkAction:
        """Call the grounder to get precise coordinates.

        Args:
            observation: Current observation.
            instruction: High-level instruction from the planner.
            _retry: Whether to retry once on parse failure.

        Returns:
            Grounded BenchmarkAction.
        """
        if not isinstance(self._grounder, str):
            # Build a synthetic task with the planner instruction as the
            # instruction, then delegate to the grounder agent.
            synth_task = BenchmarkTask(
                task_id="grounder",
                instruction=instruction,
                domain="desktop",
            )
            return self._grounder.act(observation, synth_task)

        if self._grounder_provider == "http":
            return self._call_grounder_http(observation, instruction)

        # String model name — use vlm_call.
        prompt = _GROUNDER_PROMPT_JSON.format(instruction=instruction)
        images = [observation.screenshot] if observation.screenshot else None

        from openadapt_evals.vlm import vlm_call

        raw = vlm_call(
            prompt,
            images=images,
            system=_GROUNDER_SYSTEM,
            model=self._grounder,
            provider=self._grounder_provider,
            max_tokens=256,
            cost_label="grounder",
        )

        logger.debug("Grounder raw output: %s", raw[:500])

        from openadapt_evals.errors import ActionParseError
        from openadapt_evals.training.trl_rollout import parse_action_json

        try:
            action = parse_action_json(raw)
        except ActionParseError:
            action = BenchmarkAction(type="error")

        # If parsing fell through to "done" (no JSON found) and we haven't
        # retried yet, try once more with a simplified prompt.
        if action.type in ("done", "error") and _retry:
            logger.info("Grounder parse failed, retrying with simplified prompt")
            simplified_prompt = (
                f"Where should I click to: {instruction}\n"
                f'Output JSON: {{"type": "click", "x": 0.0-1.0, "y": 0.0-1.0}}'
            )
            raw2 = vlm_call(
                simplified_prompt,
                images=images,
                system=_GROUNDER_SYSTEM,
                model=self._grounder,
                provider=self._grounder_provider,
                max_tokens=128,
                cost_label="grounder_retry",
            )
            try:
                action = parse_action_json(raw2)
            except ActionParseError:
                action = BenchmarkAction(type="error")
            if action.type != "done":
                return action

            logger.warning("Grounder retry also failed")
            return BenchmarkAction(
                type="error",
                raw_action={"parse_error": "grounder output was not actionable"},
            )

        return action

    def _call_grounder_http(
        self,
        observation: BenchmarkObservation,
        instruction: str,
    ) -> BenchmarkAction:
        """Call the grounder via OpenAI-compatible HTTP endpoint.

        Uses the UI-Venus native grounding prompt format which outputs
        [x1,y1,x2,y2] bounding boxes. The center point of the bbox is
        used as the click coordinate.

        Compatible with vLLM, Ollama, or any OpenAI-compatible server.

        Args:
            observation: Current observation with screenshot.
            instruction: High-level instruction from the planner.

        Returns:
            Grounded BenchmarkAction with pixel or fractional coordinates.
        """
        import base64

        import requests

        endpoint = self._grounder_endpoint.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint = endpoint.rstrip("/") + "/v1"
        url = f"{endpoint}/chat/completions"

        # Use UI-Venus native grounding format
        prompt = _GROUNDER_PROMPT_BBOX.format(instruction=instruction)

        content = [
            {"type": "text", "text": prompt},
        ]
        if observation.screenshot:
            b64 = base64.b64encode(observation.screenshot).decode()
            content.insert(0, {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        payload = {
            "model": "UI-Venus-1.5-8B",
            "messages": [
                {"role": "user", "content": content},
            ],
            "max_tokens": 128,
            "temperature": 0.0,
        }

        try:
            resp = requests.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("HTTP grounder call failed: %s", exc)
            return BenchmarkAction(
                type="error",
                raw_action={"error": f"HTTP grounder call failed: {exc}"},
            )

        logger.info("HTTP grounder raw output: %s", raw[:200])

        # Parse [x1, y1, x2, y2] bounding box → center click
        return self._parse_bbox_to_action(raw)

    @staticmethod
    def _parse_bbox_to_action(raw: str) -> BenchmarkAction:
        """Parse grounder output to a click action.

        Supports multiple formats:
        - UI-Venus bbox: [x1, y1, x2, y2] → center click
        - JSON action: {"type": "click", "x": 0.5, "y": 0.3}
        - Coordinate pair: [x, y]

        Returns BenchmarkAction with the center of the bbox.
        """
        import math
        import re

        def parse_error(reason: str) -> BenchmarkAction:
            return BenchmarkAction(
                type="error",
                raw_action={"parse_error": reason},
            )

        def finite_number(value: Any, name: str) -> float:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a number")
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError(f"{name} must be finite")
            return parsed

        # Try one strict JSON action first (for non-bbox grounders).
        try:
            data = parse_single_json_object(raw)
            if data is not None:
                if set(data) != {"type", "x", "y"}:
                    return parse_error(
                        "grounder JSON must contain exactly type, x, and y"
                    )
                if data["type"] != "click":
                    return parse_error("grounder JSON must contain type='click'")
                x = finite_number(data["x"], "x")
                y = finite_number(data["y"], "y")
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    return parse_error(
                        "grounder JSON coordinates must both be normalized to [0, 1]"
                    )
                return BenchmarkAction(
                    type="click",
                    x=x,
                    y=y,
                )
        except ActionParseError as exc:
            return parse_error(str(exc))
        except (TypeError, ValueError, KeyError) as exc:
            return parse_error(str(exc))

        # Find list of numbers (bbox format)
        number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
        match = re.fullmatch(
            rf"\s*\[\s*({number})\s*,\s*({number})"
            rf"(?:\s*,\s*({number})\s*,\s*({number}))?\s*\]\s*",
            raw,
        )
        if not match:
            # Last resort: try parse_action_json
            from openadapt_evals.training.trl_rollout import parse_action_json
            try:
                action = parse_action_json(raw)
            except ActionParseError:
                return parse_error("grounder output was not actionable")
            if action.type != "click":
                return parse_error("grounder output must resolve to one click")
            return action

        nums = [float(x) for x in match.groups() if x is not None]

        if len(nums) == 4:
            x1, y1, x2, y2 = nums
            if not all(0.0 <= value <= 1000.0 for value in nums):
                return parse_error("grounder bbox values must be in [0, 1000]")
            if x1 > x2 or y1 > y2:
                return parse_error("grounder bbox corners are reversed")
            # UI-Venus uses a fixed 1000 by 1000 canvas.
            x = ((x1 + x2) / 2) / 1000
            y = ((y1 + y2) / 2) / 1000
        elif len(nums) == 2:
            x, y = nums[0], nums[1]
            normalized = 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            canvas = 1.0 < x <= 1000.0 and 1.0 < y <= 1000.0
            if canvas:
                x, y = x / 1000, y / 1000
            elif not normalized:
                return parse_error(
                    "grounder coordinate pair mixes coordinate spaces or is out of range"
                )
        else:
            logger.warning("Unexpected number count in bbox: %s", nums)
            return parse_error(f"unexpected bbox values: {nums}")

        logger.info("Grounder: bbox=%s → click=(%.3f, %.3f)", nums, x, y)
        return BenchmarkAction(type="click", x=x, y=y)


def _action_to_planner_output(action: BenchmarkAction) -> dict[str, Any]:
    """Convert a BenchmarkAction from a planner agent to a planner output dict.

    When the planner is an existing BenchmarkAgent, its ``act()`` returns a
    BenchmarkAction. We need to interpret that as planner output.

    Args:
        action: The action returned by the planner agent.

    Returns:
        Dict with ``decision``, ``instruction``, ``reasoning`` keys.
    """
    if action.type == "done":
        return {
            "decision": "DONE",
            "instruction": "",
            "reasoning": action.raw_action.get("reasoning", "") if action.raw_action else "",
        }

    if action.type == "error":
        raw_action = action.raw_action or {}
        error = (
            raw_action.get("reason")
            or raw_action.get("error")
            or raw_action.get("parse_error")
            or "planner reported failure"
        )
        return {
            "decision": "ERROR",
            "instruction": "",
            "reasoning": str(error),
            "error": str(error),
            "error_type": raw_action.get("error_type", "agent"),
        }

    # Use the action string representation as the instruction.
    instruction = action_to_string(action)

    # If there's text in raw_action that looks like an instruction, prefer it.
    if action.raw_action:
        for key in ("instruction", "text", "response"):
            if key in action.raw_action and isinstance(action.raw_action[key], str):
                instruction = action.raw_action[key]
                break

    return {
        "decision": "COMMAND",
        "instruction": instruction,
        "reasoning": "",
    }
