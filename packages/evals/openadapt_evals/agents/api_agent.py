"""API-backed agent that uses Claude Sonnet 4.5 or GPT-5.1 directly.

This module provides an agent that wraps hosted VLM APIs (Claude or GPT-5.1)
for benchmark evaluation baselines. It can be used both as a BenchmarkAgent
for the openadapt-evals framework and as a WAA-compatible agent.

CRITICAL P0 FIX PRESERVED:
    The demo is included at EVERY step, not just step 1. This is the key fix
    that enables 100% first-action success to translate to episode success.
    See lines 287-296 in the original implementation.

Usage:
    # As BenchmarkAgent
    from openadapt_evals.agents import ApiAgent
    agent = ApiAgent(provider="anthropic")
    action = agent.act(observation, task)

    # For WAA integration (drop-in replacement for NaviAgent)
    agent = ApiAgent(provider="openai", demo="Step 1: Click button...")
    response, actions, logs, update_args = agent.predict(instruction, obs)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_evals.action_envelope import parse_single_json_object
from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent
from openadapt_evals.errors import ActionParseError

logger = logging.getLogger("openadapt_evals.agents.api")

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    # Try to find .env in current directory or parent directories
    current_dir = Path.cwd()
    for path in [current_dir] + list(current_dir.parents):
        env_file = path / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            logger.debug(f"Loaded .env from {env_file}")
            break
except ImportError:
    pass  # python-dotenv not installed, will rely on environment variables

# Clarification prompt for retry on parse failure
CLARIFICATION_PROMPT = """Your previous response could not be parsed. Please respond with EXACTLY this format:

```memory
# Your notes about the task state
```

```decision
CONTINUE
```

```python
computer.click(x, y)
```

Or use one of these actions:
- computer.click(x, y)
- computer.double_click(x, y)
- computer.right_click(x, y)
- computer.type("text")
- computer.press("key")
- computer.hotkey("key1", "key2")
- computer.scroll(amount)
- computer.drag(x1, y1, x2, y2)

Respond with your action now:
"""


# System prompt for GUI automation
SYSTEM_PROMPT = """You are a GUI automation agent controlling a Windows desktop. Given a screenshot and task instruction, determine the next action to take.

You must respond with a Python code block that uses the pyautogui API. Available functions:
- computer.click(x, y) - Click at pixel coordinates
- computer.double_click(x, y) - Double-click at pixel coordinates
- computer.right_click(x, y) - Right-click at pixel coordinates
- computer.type(text) - Type the given text
- computer.hotkey(key1, key2, ...) - Press key combination (e.g., 'ctrl', 'c')
- computer.press(key) - Press a single key (e.g., 'enter', 'tab', 'escape')
- computer.scroll(direction) - Scroll up (-3) or down (3)
- computer.drag(x1, y1, x2, y2) - Drag from (x1,y1) to (x2,y2)

Coordinates are pixel values within the screen (matching the screenshot dimensions).

Format your response as:

```memory
# Your notes about the task state (optional)
```

```decision
CONTINUE
```

```python
computer.click(500, 300)
```

Important:
- Use DONE in the decision block when the task is complete
- Use FAIL if the task cannot be completed
- Always output exactly one action per response
- Click on UI elements by their visual center coordinates
- For text input, first click to focus the field, then type

Think step by step:
1. What is the current state of the UI?
2. What is the goal?
3. What is the next logical action?
"""

# System prompt with element-based grounding (used when a11y tree is available)
SYSTEM_PROMPT_A11Y = """You are a GUI automation agent controlling a Windows desktop. Given a screenshot, accessibility tree, and task instruction, determine the next action to take.

## Accessibility Tree & Element IDs

The "UI Elements" section lists every visible element in this format:

  [ELEMENT_ID] Role: Display Name [left,top,right,bottom]

For example:
  [NotepadWindow] Window: Notepad [0,0,1920,1200]
    [15] Edit: Text Editor [0,40,1920,1170]
    [saveBtn] Button: Save [400,100,500,140]
    [Start] togglebutton: Start [418,672,463,720]

The value in square brackets [ELEMENT_ID] at the start of each line is the element ID. Use EXACTLY that string (e.g., "NotepadWindow", "15", "saveBtn", "Start") when calling click_element or type_element.

CRITICAL: The element ID is the short identifier in brackets, NOT the full line, NOT the role, and NOT the display name with extra attributes.

## Actions

PREFERRED — use element IDs from the tree:
- computer.click_element("element_id") — Click a UI element by its ID
- computer.type_element("element_id", "text") — Click element to focus, then type text

FALLBACK — only when no matching element exists in the tree:
- computer.click(x, y) — Click at pixel coordinates
- computer.double_click(x, y) — Double-click at pixel coordinates
- computer.right_click(x, y) — Right-click at pixel coordinates
- computer.type(text) — Type the given text
- computer.hotkey(key1, key2, ...) — Press key combination (e.g., 'ctrl', 'c')
- computer.press(key) — Press a single key (e.g., 'enter', 'tab', 'escape')
- computer.scroll(direction) — Scroll up (-3) or down (3)
- computer.drag(x1, y1, x2, y2) — Drag from (x1,y1) to (x2,y2)

## Response Format

```memory
# Your notes about the task state (optional)
```

```decision
CONTINUE
```

```python
computer.click_element("Start")
```

## Rules

- Use DONE in the decision block when the task is complete
- Use FAIL if the task cannot be completed
- Always output exactly one action per response
- ALWAYS prefer click_element/type_element when the target element appears in the UI Elements list
- The element_id argument must be the EXACT string from the [brackets] — e.g., "Start", "15", "saveBtn"
- For text input, use type_element with the field's element ID

Think step by step:
1. What is the current state of the UI?
2. What elements are available in the accessibility tree?
3. Which element ID (from the [brackets]) matches my target?
4. What is the next logical action?
"""


def _format_accessibility_tree(tree: dict | str, indent: int = 0, max_depth: int = 5) -> str:
    """Format accessibility tree for prompt.

    Produces a clean indented list like::

        [NotepadWindow] Window: Notepad
          [15] Edit: Text Editor
          [SaveBtn] Button: Save

    Each ``[ID]`` is the element ID used with ``click_element("ID")``.

    Args:
        tree: Accessibility tree dict from WAA, or XML string.
        indent: Current indentation level.
        max_depth: Maximum depth to traverse.

    Returns:
        Formatted string representation.
    """
    # Handle XML string input — shouldn't normally happen since the live
    # adapter now parses XML to dict, but keep as safety fallback.
    if isinstance(tree, str):
        return tree

    if indent >= max_depth:
        return ""

    lines = []
    prefix = "  " * indent

    role = tree.get("role", tree.get("control_type", "unknown"))
    name = tree.get("name", "")
    node_id = tree.get("id", tree.get("node_id", ""))

    # Get bounding box — supports both parsed dict and UIA dict formats
    bbox_str = ""
    if "BoundingRectangle" in tree:
        bbox_str = f" [{tree['BoundingRectangle']}]"
    elif "bounding_rectangle" in tree:
        br = tree["bounding_rectangle"]
        bbox_str = f" [{br.get('left', 0)},{br.get('top', 0)},{br.get('right', 0)},{br.get('bottom', 0)}]"

    line = f"{prefix}[{node_id}] {role}"
    if name:
        line += f": {name[:50]}"  # Truncate long names
    if bbox_str:
        line += bbox_str
    lines.append(line)

    for child in tree.get("children", []):
        child_text = _format_accessibility_tree(child, indent + 1, max_depth)
        if child_text:
            lines.append(child_text)

    return "\n".join(lines)


def _prev_actions_to_string(prev_actions: list[str], n_prev: int = 3) -> str:
    """Format previous actions for the prompt.

    Args:
        prev_actions: List of previous action strings.
        n_prev: Number of previous actions to include.

    Returns:
        Formatted string of previous actions.
    """
    result = ""
    n_prev = min(n_prev, len(prev_actions))
    for i in range(1, n_prev + 1):
        action = prev_actions[-i]
        result += f"Action at T-{i}:\n{action}\n\n"
    return result


class ApiAgent(BenchmarkAgent):
    """API-backed agent using Claude or GPT-5.1.

    This agent implements both the BenchmarkAgent interface for use with
    openadapt-evals and the WAA NaviAgent interface for direct WAA integration.

    CRITICAL: The demo is included at EVERY step, not just step 1.
    This is the P0 fix for 100% first-action / 0% episode success.

    Args:
        provider: API provider - "anthropic" (Claude) or "openai" (GPT-5.1).
        api_key: Optional API key. If not provided, uses environment variables.
        model: Optional model name override.
        temperature: Sampling temperature (0.0-1.0).
        max_tokens: Maximum tokens for API response.
        use_accessibility_tree: Whether to include a11y tree in prompts.
        use_history: Whether to include action history in prompts.
        demo: Optional demonstration trajectory to include at every step.
              This is the key fix for 100% first-action / 0% episode success:
              the demo must persist across ALL steps, not just step 1.
    """

    # Default models for each provider
    DEFAULT_MODELS = {
        "anthropic": "claude-sonnet-4-5-20250929",
        "openai": "gpt-5.4",
    }

    def __init__(
        self,
        provider: str = "anthropic",
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.5,
        max_tokens: int = 1500,
        use_accessibility_tree: bool = True,
        use_history: bool = True,
        demo: str | None = None,
    ):
        self.provider = provider
        self.model = model or self.DEFAULT_MODELS.get(provider)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.use_accessibility_tree = use_accessibility_tree
        self.use_history = use_history
        self.demo = demo  # Demo persists across ALL steps - THIS IS THE P0 FIX

        # WAA compatibility
        self.action_space = "code_block"

        # Get API key
        if provider == "anthropic":
            self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is required for provider='anthropic'. "
                    "Set it in environment or pass api_key parameter."
                )
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=self.api_key)
            except ImportError:
                raise RuntimeError(
                    "anthropic package required. Install with: pip install anthropic"
                )

        elif provider == "openai":
            self.api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not self.api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is required for provider='openai'. "
                    "Set it in environment or pass api_key parameter."
                )
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except ImportError:
                raise RuntimeError(
                    "openai package required. Install with: pip install openai"
                )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        # State tracking
        self.prev_actions: list[str] = []  # Raw action codes for WAA compatibility
        self.history: list[str] = []  # Rich history with reasoning (like PC Agent-E)
        self.history_cutoff = 10  # Max history entries to include
        self.memory_block_text = "# empty memory block"
        self.step_counter = 0

        # Parsing configuration
        self.max_parse_retries = 2  # Number of retries on parse failure
        self.loop_detection_threshold = 3  # Trigger alternative strategy after N identical actions
        self._loop_count = 0  # How many consecutive loops detected (for progressive offsets)
        self._a11y_available = False  # Whether a11y tree was present for current step

        logger.info(f"ApiAgent initialized with provider={provider}, model={self.model}")
        if self.demo:
            logger.info(f"Demo trajectory provided ({len(self.demo)} chars) - will persist across all steps")

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """BenchmarkAgent interface: Given observation and task, return next action.

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action to execute.
        """
        # Convert BenchmarkObservation to WAA-style dict
        obs_dict: dict[str, Any] = {}
        if observation.screenshot:
            obs_dict["screenshot"] = observation.screenshot
        if observation.accessibility_tree:
            obs_dict["accessibility_tree"] = observation.accessibility_tree
        if observation.window_title:
            obs_dict["window_title"] = observation.window_title

        # Call predict (WAA interface)
        _, actions, logs, _ = self.predict(task.instruction, obs_dict)

        # Store logs so runner.py can read them for execution trace
        self._last_step_logs = logs

        # Parse response into BenchmarkAction
        if actions and actions[0] == "DONE":
            return BenchmarkAction(type="done", raw_action={"waa_action": "DONE"})
        if actions and actions[0] == "FAIL":
            return BenchmarkAction(type="error", raw_action={"waa_action": "FAIL"})
        if actions and actions[0] == "WAIT":
            return BenchmarkAction(type="wait", raw_action={"waa_action": "WAIT"})

        if actions and actions[0].startswith("computer."):
            return self._parse_computer_action(actions[0], observation)

        return BenchmarkAction(type="error", raw_action={"error": "Could not parse action"})

    def predict(self, instruction: str, obs: dict) -> tuple:
        """WAA-compatible interface: Predict the next action based on observation.

        This method implements the same interface as NaviAgent.predict().

        Args:
            instruction: The task instruction.
            obs: Observation dict containing:
                - screenshot: PNG bytes of current screen
                - accessibility_tree: A11y tree dict (optional)
                - window_title: Current window title
                - window_names_str: List of open windows
                - computer_clipboard: Current clipboard content

        Returns:
            Tuple of (response_text, actions_list, logs_dict, computer_update_args)
        """
        logs: dict[str, Any] = {}
        self.step_counter += 1

        # Extract screenshot
        screenshot_bytes = obs.get("screenshot")
        if screenshot_bytes is None:
            logger.error("No screenshot in observation")
            return "", ["# No screenshot available"], logs, {}

        # Convert screenshot to PIL Image
        try:
            image = Image.open(BytesIO(screenshot_bytes))
            w, h = image.size
        except Exception as e:
            logger.error(f"Failed to load screenshot: {e}")
            return "", ["# Failed to load screenshot"], logs, {}

        logs["image_width"] = w
        logs["image_height"] = h

        # Build the prompt
        content_parts = [f"TASK: {instruction}"]

        # =================================================================
        # CRITICAL P0 FIX: Include demo at EVERY step, not just step 1
        # This is the key fix for 100% first-action / 0% episode success
        # =================================================================
        if self.demo:
            content_parts.append(
                f"DEMONSTRATION (follow this pattern):\n"
                f"---\n{self.demo}\n---\n"
                f"Use the demonstration above as a guide. You are currently at step {self.step_counter}."
            )
            logs["demo_included"] = True
            logs["demo_length"] = len(self.demo)
        # =================================================================

        # Add context
        window_title = obs.get("window_title", "")
        if window_title:
            content_parts.append(f"Current window: {window_title}")
            logs["window_title"] = window_title

        window_names_str = obs.get("window_names_str", "")
        if window_names_str:
            content_parts.append(f"Open windows: {window_names_str}")
            logs["window_names_str"] = window_names_str

        clipboard = obs.get("computer_clipboard", "")
        if clipboard:
            content_parts.append(f"Clipboard: {clipboard[:100]}")
            logs["computer_clipboard"] = clipboard

        # Add accessibility tree if available and enabled
        self._a11y_available = False
        if self.use_accessibility_tree:
            a11y_tree = obs.get("accessibility_tree")
            if a11y_tree:
                self._a11y_available = True
                tree_str = _format_accessibility_tree(a11y_tree)
                if len(tree_str) > 8000:
                    tree_str = tree_str[:8000] + "\n... (truncated)"
                content_parts.append(f"UI Elements:\n{tree_str}")
                logs["accessibility_tree_len"] = len(tree_str)
                logs["a11y_grounding"] = True

        # Add action history if enabled (enhanced: includes reasoning, not just raw actions)
        if self.use_history and self.history:
            # Use rich history with reasoning (like PC Agent-E)
            history_entries = self.history[-self.history_cutoff:]
            history_str = "\n\n".join(
                f"[Step {i+1}] {entry}"
                for i, entry in enumerate(history_entries)
            )
            content_parts.append(f"History of previous steps:\n{history_str}")
            logs["history_entries"] = len(history_entries)
        elif self.use_history and self.prev_actions:
            # Fallback to raw action history
            history_str = _prev_actions_to_string(self.prev_actions, n_prev=5)
            content_parts.append(f"Previous actions:\n{history_str}")

        # Add memory block
        content_parts.append(f"Your memory:\n```memory\n{self.memory_block_text}\n```")

        content_parts.append(f"\nScreen dimensions: {w}x{h} pixels")
        content_parts.append("\nWhat is the next action?")

        user_prompt = "\n\n".join(content_parts)
        logs["user_question"] = user_prompt

        # Call the API with retry logic for parse failures
        response_text = None
        actions = None
        parse_attempts = 0

        while parse_attempts <= self.max_parse_retries:
            try:
                if parse_attempts == 0:
                    response_text = self._call_api(screenshot_bytes, user_prompt)
                else:
                    # Retry with clarification prompt
                    logger.warning(f"Parse retry {parse_attempts}/{self.max_parse_retries}")
                    retry_prompt = user_prompt + "\n\n" + CLARIFICATION_PROMPT
                    response_text = self._call_api(screenshot_bytes, retry_prompt)
                    logs[f"retry_{parse_attempts}_response"] = response_text
            except Exception as e:
                logger.error(f"API call failed: {e}")
                return "", ["# API call failed"], logs, {}

            logs["plan_result"] = response_text

            # Capture token usage from last API call
            token_usage = getattr(self, "_last_token_usage", None)
            if token_usage:
                logs["token_usage"] = token_usage
                self._last_token_usage = None

            # Parse the response with robust error handling
            parse_result = self._parse_api_response(response_text, w, h, logs)

            if parse_result["status"] == "terminal":
                # DONE, FAIL, or WAIT
                terminal_action = parse_result["action"]
                self.prev_actions.append(terminal_action)
                return "", [terminal_action], logs, {}

            if parse_result["status"] == "success":
                actions = [parse_result["action"]]
                self.prev_actions.append(parse_result["action"])
                self._add_to_history(f"Thought: {self.memory_block_text}\nAction: {parse_result['action']}")
                break

            # Parse failed, retry
            parse_attempts += 1
            logs[f"parse_error_{parse_attempts}"] = parse_result.get("error", "Unknown parse error")

        # If all retries failed, return error
        if actions is None:
            logger.error(f"All {self.max_parse_retries + 1} parse attempts failed")
            logs["parse_failure"] = True
            actions = ["# Could not parse action after retries"]

        # Check for action loop detection
        if actions and actions[0].startswith("computer."):
            loop_detected = self._detect_action_loop(actions[0])
            if loop_detected:
                self._loop_count += 1
                logs["loop_detected"] = True
                logs["loop_count"] = self._loop_count
                logger.warning(f"Action loop detected: {actions[0]} repeated {self.loop_detection_threshold}+ times (loop #{self._loop_count})")
                alternative_action = self._generate_alternative_action(actions[0], w, h)
                if alternative_action:
                    logger.info(f"Substituting alternative action: {alternative_action}")
                    actions = [alternative_action]
                    logs["alternative_action"] = alternative_action
            else:
                self._loop_count = 0

        # Build computer_update_args (for WAA compatibility)
        computer_update_args = {
            "rects": [],
            "window_rect": [0, 0, w, h],
            "screenshot": image,
            "scale": (1.0, 1.0),
            "clipboard_content": clipboard,
            "swap_ctrl_alt": False,
        }

        return "", actions, logs, computer_update_args

    def _call_api(self, screenshot_bytes: bytes, user_prompt: str) -> str:
        """Call the VLM API with screenshot and prompt.

        Args:
            screenshot_bytes: PNG image bytes.
            user_prompt: User prompt text.

        Returns:
            Response text from the API.
        """
        image_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        if self.provider == "anthropic":
            content = [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
            ]

            system_prompt = SYSTEM_PROMPT_A11Y if self._a11y_available else SYSTEM_PROMPT
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )

            # Capture token usage
            usage = getattr(resp, "usage", None)
            if usage:
                self._last_token_usage = {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                }

            # Extract text from response
            parts = getattr(resp, "content", [])
            texts = [
                getattr(p, "text", "")
                for p in parts
                if getattr(p, "type", "") == "text"
            ]
            return "\n".join([t for t in texts if t]).strip()

        elif self.provider == "openai":
            system_prompt = SYSTEM_PROMPT_A11Y if self._a11y_available else SYSTEM_PROMPT
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                },
            ]

            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=self.max_tokens,
                temperature=self.temperature,
            )

            # Capture token usage
            usage = getattr(resp, "usage", None)
            if usage:
                self._last_token_usage = {
                    "input_tokens": getattr(usage, "prompt_tokens", None),
                    "output_tokens": getattr(usage, "completion_tokens", None),
                }

            return resp.choices[0].message.content or ""

        raise ValueError(f"Unsupported provider: {self.provider}")

    def _parse_action_from_text(self, text: str, width: int, height: int) -> str | None:
        """Try to parse an action from free-form text response.

        Args:
            text: Response text to parse.
            width: Screen width.
            height: Screen height.

        Returns:
            Python code string or None if parsing failed.
        """
        # Try to find click coordinates
        click_match = re.search(
            r"click.*?(\d+)\s*,\s*(\d+)", text, re.IGNORECASE
        )
        if click_match:
            x, y = int(click_match.group(1)), int(click_match.group(2))
            return f"computer.click({x}, {y})"

        # Try to find type text
        type_match = re.search(
            r'type[:\s]+["\'](.+?)["\']', text, re.IGNORECASE
        )
        if type_match:
            text_to_type = type_match.group(1)
            return f'computer.type("{text_to_type}")'

        # Try to find key press
        key_match = re.search(
            r"press[:\s]+(\w+)", text, re.IGNORECASE
        )
        if key_match:
            key = key_match.group(1).lower()
            return f'computer.press("{key}")'

        # Try to find hotkey
        hotkey_match = re.search(
            r"hotkey[:\s]+(\w+)\s*\+\s*(\w+)", text, re.IGNORECASE
        )
        if hotkey_match:
            key1, key2 = hotkey_match.group(1).lower(), hotkey_match.group(2).lower()
            return f'computer.hotkey("{key1}", "{key2}")'

        return None

    def _parse_api_response(
        self, response_text: str, width: int, height: int, logs: dict
    ) -> dict[str, Any]:
        """Parse API response with multiple strategies and defensive error handling.

        This method tries multiple parsing strategies to extract an action from the
        API response, handling cases where the response format varies (the root cause
        of the 'str' object has no attribute 'get' error in runs 194740, 194940, 195137).

        Strategies tried in order:
        1. Standard code block extraction (```python ... ```)
        2. JSON extraction from response
        3. Code block with alternative markers (```json ... ```, ``` ... ```)
        4. Regex fallback for action patterns
        5. Direct computer.* call extraction

        Args:
            response_text: The raw API response text.
            width: Screen width for coordinate validation.
            height: Screen height for coordinate validation.
            logs: Logs dict to record parsing details.

        Returns:
            Dict with:
                - status: "success", "terminal", or "failed"
                - action: The parsed action string (if success/terminal)
                - error: Error message (if failed)
        """
        try:
            # Model output is an action protocol. Coercing arbitrary objects to
            # strings can manufacture a parseable action from malformed output.
            if not isinstance(response_text, str):
                return {
                    "status": "failed",
                    "error": "API response must be a string action envelope",
                }

            decision_matches = re.findall(
                r"```decision\s*\n(.*?)```", response_text, re.DOTALL | re.IGNORECASE
            )
            if len(decision_matches) > 1:
                return {"status": "failed", "error": "Multiple decision blocks"}

            action_calls = re.findall(
                r"\bcomputer\.[A-Za-z_][A-Za-z0-9_]*\s*\(", response_text
            )
            json_objects = re.findall(r"\{[^{}]*\}", response_text, re.DOTALL)
            fenced_pattern = re.compile(
                r"^```([A-Za-z0-9_-]*)[ \t]*\n(.*?)^```[ \t]*$",
                re.DOTALL | re.IGNORECASE | re.MULTILINE,
            )
            fenced_matches = list(fenced_pattern.finditer(response_text))
            action_blocks = [
                match.group(2)
                for match in fenced_matches
                if match.group(1).lower() in {"", "python", "json"}
            ]
            if len(action_calls) > 1 or len(json_objects) > 1 or len(action_blocks) > 1:
                return {
                    "status": "failed",
                    "error": "Response must contain exactly one action envelope",
                }

            if action_blocks:
                block = action_blocks[0].strip()
                try:
                    block_json = parse_single_json_object(block)
                except ActionParseError as exc:
                    return {"status": "failed", "error": str(exc)}
                if block_json is None and not self._validate_action(
                    block, width, height
                ):
                    return {
                        "status": "failed",
                        "error": "Fenced block must contain one exact action",
                    }

            outside_blocks = fenced_pattern.sub("", response_text)
            outside_calls = re.findall(
                r"\bcomputer\.[A-Za-z_][A-Za-z0-9_]*\s*\(", outside_blocks
            )
            outside_json = re.findall(r"\{[^{}]*\}", outside_blocks, re.DOTALL)
            if action_blocks and (outside_calls or outside_json):
                return {
                    "status": "failed",
                    "error": "Competing action envelopes use different formats",
                }
            if (
                not action_blocks
                and action_calls
                and json_objects
                and "computer." not in json_objects[0]
            ):
                return {
                    "status": "failed",
                    "error": "Competing action envelopes use different formats",
                }

            if json_objects:
                try:
                    parse_single_json_object(json_objects[0])
                except ActionParseError as exc:
                    return {"status": "failed", "error": str(exc)}

            decision = None
            if decision_matches:
                decision = decision_matches[0].strip().upper()
                if decision not in {"DONE", "FAIL", "WAIT", "CONTINUE"}:
                    return {
                        "status": "failed",
                        "error": f"Unknown decision: {decision_matches[0].strip()!r}",
                    }
                if decision in {"DONE", "FAIL", "WAIT"}:
                    if action_calls or json_objects or action_blocks:
                        return {
                            "status": "failed",
                            "error": "Terminal decision cannot include an action",
                        }
                    return {"status": "terminal", "action": decision}

            # Extract memory block (update internal state)
            try:
                memory_match = re.search(r"```memory\n(.*?)```", response_text, re.DOTALL)
                if memory_match:
                    self.memory_block_text = memory_match.group(1).strip()
            except Exception as e:
                logger.warning(f"Memory extraction failed: {e}")

            # Strategy 1: Standard Python code block
            try:
                code_match = re.search(r"```python\n(.*?)```", response_text, re.DOTALL)
                if code_match:
                    code_text = code_match.group(1).strip()
                    if self._validate_action(code_text, width, height):
                        logs["parse_strategy"] = "python_code_block"
                        return {"status": "success", "action": code_text}
            except Exception as e:
                logger.warning(f"Python code block extraction failed: {e}")

            # Strategy 2: JSON extraction
            try:
                action = self._extract_action_from_json(response_text, width, height)
                if action:
                    logs["parse_strategy"] = "json_extraction"
                    return {"status": "success", "action": action}
            except Exception as e:
                logger.warning(f"JSON extraction failed: {e}")

            # Strategy 3: Generic code block (no language specified or ```json)
            try:
                # Try ```json blocks
                json_block_match = re.search(r"```json\n(.*?)```", response_text, re.DOTALL)
                if json_block_match:
                    json_text = json_block_match.group(1).strip()
                    action = self._extract_action_from_json(json_text, width, height)
                    if action:
                        logs["parse_strategy"] = "json_code_block"
                        return {"status": "success", "action": action}

                # Try generic ``` blocks
                generic_block_match = re.search(r"```\n(.*?)```", response_text, re.DOTALL)
                if generic_block_match:
                    code_text = generic_block_match.group(1).strip()
                    if self._validate_action(code_text, width, height):
                        logs["parse_strategy"] = "generic_code_block"
                        return {"status": "success", "action": code_text}
            except Exception as e:
                logger.warning(f"Generic code block extraction failed: {e}")

            # Strategy 4: Direct computer.* pattern matching
            try:
                # Look for computer.action(...) patterns directly in text
                computer_patterns = [
                    r"(computer\.click\s*\(\s*\d+\s*,\s*\d+\s*\))",
                    r"(computer\.double_click\s*\(\s*\d+\s*,\s*\d+\s*\))",
                    r"(computer\.right_click\s*\(\s*\d+\s*,\s*\d+\s*\))",
                    r'(computer\.type\s*\(\s*["\'].*?["\']\s*\))',
                    r'(computer\.press\s*\(\s*["\'].*?["\']\s*\))',
                    r'(computer\.hotkey\s*\(\s*["\'].*?["\']\s*(?:,\s*["\'].*?["\']\s*)*\))',
                    r"(computer\.scroll\s*\(\s*-?\d+\s*\))",
                    r"(computer\.drag\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))",
                    # Element-based actions (a11y tree grounding)
                    r'(computer\.click_element\s*\(\s*["\'].*?["\']\s*\))',
                    r'(computer\.type_element\s*\(\s*["\'].*?["\']\s*,\s*["\'].*?["\']\s*\))',
                ]
                for pattern in computer_patterns:
                    match = re.search(pattern, response_text, re.IGNORECASE)
                    if match:
                        action = match.group(1).strip()
                        # Normalize whitespace
                        action = re.sub(r'\s+', '', action)
                        # Restore proper formatting
                        action = action.replace('computer.', 'computer.')
                        if self._validate_action(action, width, height):
                            logs["parse_strategy"] = "direct_computer_pattern"
                            return {"status": "success", "action": action}
            except Exception as e:
                logger.warning(f"Direct computer pattern extraction failed: {e}")

            # Strategy 5: Fallback regex extraction using _parse_action_from_text
            try:
                action = self._parse_action_from_text(response_text, width, height)
                if action:
                    logs["parse_strategy"] = "regex_fallback"
                    return {"status": "success", "action": action}
            except Exception as e:
                logger.warning(f"Regex fallback extraction failed: {e}")

            # All strategies failed
            return {
                "status": "failed",
                "error": "All parsing strategies failed to extract a valid action"
            }

        except Exception as e:
            logger.error(f"Unexpected error in _parse_api_response: {e}")
            return {"status": "failed", "error": f"Unexpected parsing error: {e}"}

    def _extract_action_from_json(
        self, text: str, width: int, height: int
    ) -> str | None:
        """Extract action from JSON in the response text.

        Args:
            text: Text that may contain JSON.
            width: Screen width.
            height: Screen height.

        Returns:
            Action string or None.
        """
        # Try to find JSON objects in the text
        json_patterns = [
            r'\{[^{}]*"action"[^{}]*\}',  # Simple object with action key
            r'\{[^{}]*"type"[^{}]*\}',     # Object with type key
            r'\{[^{}]*"command"[^{}]*\}',  # Object with command key
        ]

        for pattern in json_patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match)
                    if isinstance(data, dict):
                        # Try various keys that might contain the action
                        for key in ['action', 'command', 'code', 'python']:
                            if key in data and isinstance(data[key], str):
                                action = data[key]
                                if self._validate_action(action, width, height):
                                    return action

                        # Try to construct action from structured data
                        action_type = data.get('type') or data.get('action_type')
                        if action_type:
                            if action_type in ('click', 'left_click'):
                                x = data.get('x', data.get('coordinate', {}).get('x'))
                                y = data.get('y', data.get('coordinate', {}).get('y'))
                                if x is not None and y is not None:
                                    return f"computer.click({int(x)}, {int(y)})"
                            elif action_type == 'type':
                                text_val = data.get('text', data.get('value', ''))
                                if text_val:
                                    return f'computer.type("{text_val}")'
                            elif action_type == 'press':
                                key = data.get('key', data.get('value', ''))
                                if key:
                                    return f'computer.press("{key}")'
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

        return None

    def _validate_action(self, action: str, width: int, height: int) -> bool:
        """Validate that an action string is properly formed.

        Args:
            action: The action string to validate.
            width: Screen width for coordinate bounds.
            height: Screen height for coordinate bounds.

        Returns:
            True if the action is valid.
        """
        if not action or not isinstance(action, str):
            return False

        # Must start with computer.
        if not action.startswith("computer."):
            return False

        # Check for valid action types
        valid_patterns = [
            r"^computer\.click\(\d+,\s*\d+\)$",
            r"^computer\.double_click\(\d+,\s*\d+\)$",
            r"^computer\.right_click\(\d+,\s*\d+\)$",
            r'^computer\.type\(["\'].+["\']\)$',
            r'^computer\.press\(["\'].+["\']\)$',
            r'^computer\.hotkey\(["\'].+["\'](?:,\s*["\'].+["\']\s*)*\)$',
            r"^computer\.scroll\(-?\d+\)$",
            r"^computer\.drag\(\d+,\s*\d+,\s*\d+,\s*\d+\)$",
            # Element-based actions (a11y tree grounding)
            r'^computer\.click_element\(["\'].+["\']\)$',
            r'^computer\.type_element\(["\'].+["\']\s*,\s*["\'].*["\']\)$',
        ]

        for pattern in valid_patterns:
            if re.match(pattern, action):
                # For click/drag actions, validate coordinates are in bounds
                coord_match = re.search(r"\((\d+),\s*(\d+)", action)
                if coord_match:
                    x, y = int(coord_match.group(1)), int(coord_match.group(2))
                    if x > width * 2 or y > height * 2:  # Allow some slack
                        logger.warning(f"Coordinates ({x}, {y}) seem out of bounds for {width}x{height}")
                        return False
                return True

        return False

    def _detect_action_loop(self, current_action: str) -> bool:
        """Detect if the agent is stuck in an action loop.

        Args:
            current_action: The current action to check.

        Returns:
            True if a loop is detected (3+ identical consecutive actions).
        """
        if len(self.prev_actions) < self.loop_detection_threshold - 1:
            return False

        # Check if the last N actions are all identical to current
        recent_actions = self.prev_actions[-(self.loop_detection_threshold - 1):]
        return all(action == current_action for action in recent_actions)

    def _generate_alternative_action(
        self, stuck_action: str, width: int, height: int
    ) -> str | None:
        """Generate an alternative action when a loop is detected.

        Uses progressive strategies based on how many consecutive loops have
        been detected (_loop_count). For clicks, varies the offset direction
        and magnitude. For hotkeys (Ctrl+S etc.), does NOT substitute Escape
        since that destroys dialogs the agent may be trying to interact with.

        Args:
            stuck_action: The action that's being repeated.
            width: Screen width.
            height: Screen height.

        Returns:
            An alternative action, or None to let the original through.
        """
        loop_n = self._loop_count  # 1-based, incremented before this call

        # Hotkey loops (e.g. Ctrl+S): do NOT substitute Escape.
        # The agent may be trying to trigger a dialog that needs time to appear.
        # Let the action through on first loop, then try Enter (to confirm a
        # dialog that may have appeared), then scroll to change context.
        if "computer.hotkey" in stuck_action:
            if loop_n <= 2:
                return None  # Let the hotkey through
            elif loop_n == 3:
                return 'computer.press("enter")'  # Try confirming a dialog
            else:
                return 'computer.press("tab")'  # Try navigating to next element

        # Click loops: use progressive offsets in different directions
        click_match = re.match(
            r"computer\.(click|double_click|right_click)\((\d+),\s*(\d+)\)",
            stuck_action,
        )
        if click_match:
            action_type = click_match.group(1)
            x, y = int(click_match.group(2)), int(click_match.group(3))

            # Progressive strategies: offset in different directions, then try
            # escape or clicking screen center
            offsets = [
                (0, -60),    # Up (try element above)
                (0, 60),     # Down (try element below)
                (-80, 0),    # Left
                (80, 0),     # Right
            ]
            if loop_n <= len(offsets):
                dx, dy = offsets[loop_n - 1]
                nx = max(10, min(x + dx, width - 10))
                ny = max(10, min(y + dy, height - 10))
                return f"computer.{action_type}({nx}, {ny})"
            elif loop_n == len(offsets) + 1:
                return 'computer.press("escape")'  # Try dismissing a popup
            else:
                # Click screen center as last resort
                return f"computer.click({width // 2}, {height // 2})"

        # Type loops: try pressing enter
        if "computer.type" in stuck_action:
            return 'computer.press("enter")'

        # Press loops: try tab to move focus
        if "computer.press" in stuck_action:
            if loop_n <= 2:
                return None  # Let it through
            return 'computer.press("tab")'

        return None  # Unknown action type — let it through

    def _parse_computer_action(
        self, code: str, observation: BenchmarkObservation
    ) -> BenchmarkAction:
        """Parse computer.xxx() action string into BenchmarkAction.

        Args:
            code: Python code string like 'computer.click(100, 200)'.
            observation: Observation for coordinate normalization.

        Returns:
            BenchmarkAction.
        """
        raw_action = {"code": code}

        # Parse click_element (a11y tree grounding)
        click_elem_match = re.match(
            r'computer\.click_element\(["\'](.+?)["\']\)', code
        )
        if click_elem_match:
            elem_id = click_elem_match.group(1)
            return BenchmarkAction(
                type="click", x=0.5, y=0.5,
                target_node_id=elem_id, raw_action=raw_action,
            )

        # Parse type_element (a11y tree grounding)
        type_elem_match = re.match(
            r'computer\.type_element\(["\'](.+?)["\']\s*,\s*["\'](.*)["\']\)', code
        )
        if type_elem_match:
            elem_id = type_elem_match.group(1)
            text = type_elem_match.group(2)
            return BenchmarkAction(
                type="type", text=text,
                target_node_id=elem_id, raw_action=raw_action,
            )

        # Parse click / double_click / right_click (all map to "click" action type)
        click_match = re.match(
            r"computer\.(?:click|double_click|right_click)\((\d+),\s*(\d+)\)", code
        )
        if click_match:
            x, y = int(click_match.group(1)), int(click_match.group(2))
            # Normalize if we have viewport
            if observation.viewport:
                w, h = observation.viewport
                x_norm = x / w
                y_norm = y / h
                return BenchmarkAction(type="click", x=x_norm, y=y_norm, raw_action=raw_action)
            return BenchmarkAction(type="click", x=float(x), y=float(y), raw_action=raw_action)

        # Parse type
        type_match = re.match(r'computer\.type\(["\'](.+?)["\']\)', code)
        if type_match:
            text = type_match.group(1)
            return BenchmarkAction(type="type", text=text, raw_action=raw_action)

        # Parse press
        press_match = re.match(r'computer\.press\(["\'](.+?)["\']\)', code)
        if press_match:
            key = press_match.group(1)
            return BenchmarkAction(type="key", key=key, raw_action=raw_action)

        # Parse hotkey
        hotkey_match = re.match(r'computer\.hotkey\(["\'](\w+)["\'],\s*["\'](\w+)["\']\)', code)
        if hotkey_match:
            mod, key = hotkey_match.group(1), hotkey_match.group(2)
            return BenchmarkAction(type="key", key=key, modifiers=[mod], raw_action=raw_action)

        # Parse scroll
        scroll_match = re.match(r"computer\.scroll\((-?\d+)\)", code)
        if scroll_match:
            amount = int(scroll_match.group(1))
            direction = "up" if amount < 0 else "down"
            return BenchmarkAction(type="scroll", scroll_direction=direction, raw_action=raw_action)

        # Parse drag
        drag_match = re.match(
            r"computer\.drag\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", code
        )
        if drag_match:
            x1, y1 = int(drag_match.group(1)), int(drag_match.group(2))
            x2, y2 = int(drag_match.group(3)), int(drag_match.group(4))
            if observation.viewport:
                w, h = observation.viewport
                return BenchmarkAction(
                    type="drag", x=x1/w, y=y1/h, end_x=x2/w, end_y=y2/h,
                    raw_action=raw_action,
                )
            return BenchmarkAction(
                type="drag", x=float(x1), y=float(y1),
                end_x=float(x2), end_y=float(y2), raw_action=raw_action,
            )

        logger.warning("Unrecognized computer action: %s", code)
        raw_action.update(
            {
                "parse_error": "unrecognized computer action",
                "error_type": "agent",
            }
        )
        return BenchmarkAction(type="error", raw_action=raw_action)

    def _add_to_history(self, entry: str) -> None:
        """Add an entry to the rich history (reasoning + action)."""
        self.history.append(entry)

    def set_demo(self, demo: str) -> None:
        """Set or update the demo trajectory.

        This allows setting the demo after initialization,
        useful for dynamic demo retrieval.
        """
        self.demo = demo
        logger.info(f"Demo set ({len(demo)} chars) - will persist across all steps")

    def reset(self) -> None:
        """Reset agent state between tasks."""
        self.prev_actions = []
        self.history = []  # Clear rich history too
        self.memory_block_text = "# empty memory block"
        self.step_counter = 0
        self._loop_count = 0
        # Note: demo is NOT reset - it persists across resets if set
        logger.info("ApiAgent reset")
