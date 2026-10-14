"""WAA-compatible API Agent that uses Claude Sonnet 4.5 or GPT-5.1 directly.

This module provides a drop-in replacement for the Navi agent in Windows Agent Arena
that uses hosted VLM APIs (Claude or GPT-5.1) instead of the buggy Navi agent.

The agent receives observations from WAA and returns actions in WAA's expected format
(code blocks for the pyautogui action space).

Why this exists:
    The default Navi agent in WAA has NoneType errors and other bugs.
    This API agent provides a reliable alternative that uses Claude Sonnet 4.5
    or GPT-5.1 directly, bypassing the problematic Navi implementation.

Usage from CLI:
    # Run with Claude Sonnet 4.5 (requires ANTHROPIC_API_KEY)
    uv run python -m openadapt_evals.benchmarks.vm_cli vm run-waa --agent api-claude --num-tasks 5

    # Run with GPT-5.1 (requires OPENAI_API_KEY)
    uv run python -m openadapt_evals.benchmarks.vm_cli vm run-waa --agent api-openai --num-tasks 5

How it works:
    1. The Dockerfile copies this file to /client/mm_agents/api_agent.py
    2. The Dockerfile patches run.py to recognize "api-claude" and "api-openai" agents
    3. When the agent is selected, it:
       - Receives screenshots from WAA's DesktopEnv
       - Sends them to Claude or GPT-5.1 via their respective APIs
       - Parses the response into pyautogui code blocks
       - Returns actions in WAA's expected format

Example usage in WAA run.py (auto-patched by Dockerfile):
    if cfg_args["agent_name"] == "api-claude":
        from mm_agents.api_agent import ApiAgent
        agent = ApiAgent(provider="anthropic")
    elif cfg_args["agent_name"] == "api-openai":
        from mm_agents.api_agent import ApiAgent
        agent = ApiAgent(provider="openai")
"""

from __future__ import annotations

import base64
import logging
import os
import re
from io import BytesIO
from typing import Dict, List

from PIL import Image

logger = logging.getLogger("desktopenv.agent.api")


# System prompt for GUI automation - adapted from APIBenchmarkAgent
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

Coordinates are pixel values within the screen (1920x1200 by default).

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


def format_accessibility_tree(tree: dict, indent: int = 0, max_depth: int = 5) -> str:
    """Format accessibility tree for prompt.

    Args:
        tree: Accessibility tree dict from WAA.
        indent: Current indentation level.
        max_depth: Maximum depth to traverse.

    Returns:
        Formatted string representation.
    """
    if indent >= max_depth:
        return ""

    lines = []
    prefix = "  " * indent

    role = tree.get("role", tree.get("control_type", "unknown"))
    name = tree.get("name", "")
    node_id = tree.get("id", tree.get("node_id", ""))

    # Get bounding box if available
    bbox_str = ""
    if "bounding_rectangle" in tree:
        br = tree["bounding_rectangle"]
        bbox_str = (
            f" [{br.get('left', 0)},{br.get('top', 0)},{br.get('right', 0)},{br.get('bottom', 0)}]"
        )

    line = f"{prefix}[{node_id}] {role}"
    if name:
        line += f": {name[:50]}"  # Truncate long names
    if bbox_str:
        line += bbox_str
    lines.append(line)

    for child in tree.get("children", []):
        child_text = format_accessibility_tree(child, indent + 1, max_depth)
        if child_text:
            lines.append(child_text)

    return "\n".join(lines)


def prev_actions_to_string(prev_actions: List[str], n_prev: int = 3) -> str:
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


class ApiAgent:
    """WAA-compatible agent that uses Claude or GPT-5.1 API directly.

    This agent implements the same interface as NaviAgent but uses hosted
    VLM APIs instead of the local Navi implementation (which has NoneType bugs).

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
        self.demo = demo  # Demo persists across ALL steps

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
                raise RuntimeError("openai package required. Install with: pip install openai")
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        # State tracking
        self.prev_actions: List[str] = []  # Raw action codes for WAA compatibility
        self.history: List[str] = []  # Rich history with reasoning (like PC Agent-E)
        self.history_cutoff = 10  # Max history entries to include
        self.memory_block_text = "# empty memory block"
        self.step_counter = 0

        logger.info(f"ApiAgent initialized with provider={provider}, model={self.model}")
        if self.demo:
            logger.info(
                f"Demo trajectory provided ({len(self.demo)} chars) - will persist across all steps"
            )

    def predict(self, instruction: str, obs: Dict) -> tuple:
        """Predict the next action based on observation.

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
        logs = {}
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

        # CRITICAL FIX: Include demo at EVERY step, not just step 1
        # This is the key fix for 100% first-action / 0% episode success
        if self.demo:
            content_parts.append(
                f"DEMONSTRATION (follow this pattern):\n"
                f"---\n{self.demo}\n---\n"
                f"Use the demonstration above as a guide. You are currently at step {self.step_counter}."
            )
            logs["demo_included"] = True
            logs["demo_length"] = len(self.demo)

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
        if self.use_accessibility_tree:
            a11y_tree = obs.get("accessibility_tree")
            if a11y_tree:
                tree_str = format_accessibility_tree(a11y_tree)
                # Truncate if too long
                if len(tree_str) > 4000:
                    tree_str = tree_str[:4000] + "\n... (truncated)"
                content_parts.append(f"UI Elements:\n{tree_str}")
                logs["accessibility_tree_len"] = len(tree_str)

        # Add action history if enabled (enhanced: includes reasoning, not just raw actions)
        if self.use_history and self.history:
            # Use rich history with reasoning (like PC Agent-E)
            history_entries = self.history[-self.history_cutoff :]
            history_str = "\n\n".join(
                f"[Step {i + 1}] {entry}" for i, entry in enumerate(history_entries)
            )
            content_parts.append(f"History of previous steps:\n{history_str}")
            logs["history_entries"] = len(history_entries)
        elif self.use_history and self.prev_actions:
            # Fallback to raw action history
            history_str = prev_actions_to_string(self.prev_actions, n_prev=5)
            content_parts.append(f"Previous actions:\n{history_str}")

        # Add memory block
        content_parts.append(f"Your memory:\n```memory\n{self.memory_block_text}\n```")

        content_parts.append(f"\nScreen dimensions: {w}x{h} pixels")
        content_parts.append("\nWhat is the next action?")

        user_prompt = "\n\n".join(content_parts)
        logs["user_question"] = user_prompt

        # Call the API
        try:
            response_text = self._call_api(screenshot_bytes, user_prompt)
        except Exception as e:
            logger.error(f"API call failed: {e}")
            return "", ["# API call failed"], logs, {}

        logs["plan_result"] = response_text

        # Extract memory block
        memory_match = re.search(r"```memory\n(.*?)```", response_text, re.DOTALL)
        if memory_match:
            self.memory_block_text = memory_match.group(1).strip()

        # Extract decision block
        decision_match = re.search(r"```decision\n(.*?)```", response_text, re.DOTALL)
        if decision_match:
            decision = decision_match.group(1).strip().upper()
            if "DONE" in decision:
                self.prev_actions.append("DONE")
                return "", ["DONE"], logs, {}
            elif "FAIL" in decision:
                self.prev_actions.append("FAIL")
                return "", ["FAIL"], logs, {}
            elif "WAIT" in decision:
                self.prev_actions.append("WAIT")
                return "", ["WAIT"], logs, {}

        # Extract Python code block
        code_match = re.search(r"```python\n(.*?)```", response_text, re.DOTALL)
        if code_match:
            code_text = code_match.group(1).strip()
            actions = [code_text]
            self.prev_actions.append(code_text)
            # Store rich history with reasoning (memory + action)
            self._add_to_history(f"Thought: {self.memory_block_text}\nAction: {code_text}")
        else:
            # Try to extract action from response text
            action = self._parse_action_from_text(response_text, w, h)
            if action:
                actions = [action]
                self.prev_actions.append(action)
                self._add_to_history(f"Thought: {self.memory_block_text}\nAction: {action}")
            else:
                logger.warning("Could not extract action from response")
                actions = ["# Could not parse action"]

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

            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )

            # Extract text from response
            parts = getattr(resp, "content", [])
            texts = [getattr(p, "text", "") for p in parts if getattr(p, "type", "") == "text"]
            return "\n".join([t for t in texts if t]).strip()

        elif self.provider == "openai":
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
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
        click_match = re.search(r"click.*?(\d+)\s*,\s*(\d+)", text, re.IGNORECASE)
        if click_match:
            x, y = int(click_match.group(1)), int(click_match.group(2))
            return f"computer.click({x}, {y})"

        # Try to find type text
        type_match = re.search(r'type[:\s]+["\'](.+?)["\']', text, re.IGNORECASE)
        if type_match:
            text_to_type = type_match.group(1)
            return f'computer.type("{text_to_type}")'

        # Try to find key press
        key_match = re.search(r"press[:\s]+(\w+)", text, re.IGNORECASE)
        if key_match:
            key = key_match.group(1).lower()
            return f'computer.press("{key}")'

        # Try to find hotkey
        hotkey_match = re.search(r"hotkey[:\s]+(\w+)\s*\+\s*(\w+)", text, re.IGNORECASE)
        if hotkey_match:
            key1, key2 = hotkey_match.group(1).lower(), hotkey_match.group(2).lower()
            return f'computer.hotkey("{key1}", "{key2}")'

        return None

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
        # Note: demo is NOT reset - it persists across resets if set
        logger.info("ApiAgent reset")
