"""Qwen3-VL agent for benchmark evaluation.

Uses Qwen3-VL-8B-Instruct (or fine-tuned variants) for local GUI agent
inference. Coordinates use the Qwen normalized [0, 1000] range and are
denormalized to [0, 1] for BenchmarkAction compatibility.

The prompt format is aligned with the training data produced by
``openadapt_ml.training.convert_demos`` so that fine-tuned checkpoints
see the same structure at inference time.

Action space (Qwen3-VL format):
    click(x=500, y=300)
    double_click(x=500, y=300)
    right_click(x=500, y=300)
    type(text="hello world")
    press(keys=["ctrl", "c"])
    scroll(direction="up", amount=3)
    drag(from_coord=[200, 300], to_coord=[800, 500])
    wait()
    finished()

Usage:
    from openadapt_evals.agents import Qwen3VLAgent

    agent = Qwen3VLAgent()
    action = agent.act(observation, task)

    # With demo conditioning
    agent = Qwen3VLAgent(demo="Step 0: click(x=450, y=950)...")

    # Fine-tuned PEFT adapter (auto-detects base model from adapter_config.json)
    agent = Qwen3VLAgent(model_path="/path/to/peft/adapter/")
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent

logger = logging.getLogger("openadapt_evals.agents.qwen3vl")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

# Coordinate scale used by Qwen3-VL
QWEN_COORD_SCALE = 1000

# System prompt — MUST match openadapt_ml.training.convert_demos.SYSTEM_PROMPT
SYSTEM_PROMPT = (
    "You are a GUI agent. You observe screenshots of a desktop and output "
    "exactly one action per step. Use the following action format:\n"
    "click(x=<int>, y=<int>)\n"
    "double_click(x=<int>, y=<int>)\n"
    "right_click(x=<int>, y=<int>)\n"
    'type(text="<string>")\n'
    'press(keys=["<key1>", ...])\n'
    'scroll(direction="<up|down|left|right>", amount=<int>)\n'
    "drag(from_coord=[<x1>, <y1>], to_coord=[<x2>, <y2>])\n"
    "wait()\n"
    "finished()\n\n"
    "Coordinates are in [0, 1000] range where (0,0) is top-left and "
    "(1000,1000) is bottom-right."
)

# System prompt with accessibility tree grounding
SYSTEM_PROMPT_A11Y = (
    "You are a GUI agent. You observe screenshots and an accessibility tree "
    "describing UI elements. Output exactly one action per step.\n\n"
    "PREFERRED — use element IDs from the accessibility tree:\n"
    'click_element(id="<element_id>")\n'
    'type_element(id="<element_id>", text="<string>")\n\n'
    "FALLBACK — use coordinates only when no matching element exists:\n"
    "click(x=<int>, y=<int>)\n"
    'type(text="<string>")\n'
    'press(keys=["<key1>", ...])\n'
    'scroll(direction="<up|down|left|right>", amount=<int>)\n'
    "wait()\n"
    "finished()\n\n"
    "Coordinates are in [0, 1000] range where (0,0) is top-left and "
    "(1000,1000) is bottom-right.\n\n"
    "IMPORTANT: Always prefer click_element/type_element when the target "
    "element is visible in the accessibility tree. This is more reliable "
    "than coordinate-based actions."
)

# ---------------------------------------------------------------------------
# Compiled regex patterns for action parsing
# ---------------------------------------------------------------------------

# Accept both keyword (x=100, y=200) and positional (100, 200) args,
# and both int (500) and float (0.500) coordinate values.
_COORD = r"[\d.]+"  # matches int or float

_RE_CLICK = re.compile(
    r"(?:click|left_click)\s*\(\s*(?:x\s*=\s*)?("
    + _COORD
    + r")\s*,\s*(?:y\s*=\s*)?("
    + _COORD
    + r")\s*\)",
    re.IGNORECASE,
)
_RE_DOUBLE_CLICK = re.compile(
    r"double_click\s*\(\s*(?:x\s*=\s*)?("
    + _COORD
    + r")\s*,\s*(?:y\s*=\s*)?("
    + _COORD
    + r")\s*\)",
    re.IGNORECASE,
)
_RE_RIGHT_CLICK = re.compile(
    r"right_click\s*\(\s*(?:x\s*=\s*)?("
    + _COORD
    + r")\s*,\s*(?:y\s*=\s*)?("
    + _COORD
    + r")\s*\)",
    re.IGNORECASE,
)
# Accept both keyword (text="...") and positional ("...") args
_RE_TYPE = re.compile(
    r'type\s*\(\s*(?:text\s*=\s*)?"((?:[^"\\]|\\.)*)"\s*\)',
    re.IGNORECASE,
)
# Accept both keyword (keys=[...]) and positional ([...]) args
_RE_PRESS = re.compile(
    r"press\s*\(\s*(?:keys\s*=\s*)?\[([^\]]*)\]\s*\)",
    re.IGNORECASE,
)
_RE_SCROLL = re.compile(
    r'scroll\s*\(\s*direction\s*=\s*"(\w+)"\s*'
    r"(?:,\s*amount\s*=\s*(\d+)\s*)?\)",
    re.IGNORECASE,
)
_RE_DRAG = re.compile(
    r"drag\s*\(\s*from_coord\s*=\s*\[\s*("
    + _COORD
    + r")\s*,\s*("
    + _COORD
    + r")\s*\]\s*,"
    r"\s*to_coord\s*=\s*\[\s*("
    + _COORD
    + r")\s*,\s*("
    + _COORD
    + r")\s*\]\s*\)",
    re.IGNORECASE,
)
_RE_WAIT = re.compile(r"wait\s*\(\s*\)", re.IGNORECASE)
_RE_FINISHED = re.compile(r"finished\s*\(\s*\)", re.IGNORECASE)
_RE_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)

# Element-based actions (accessibility tree grounding)
_RE_CLICK_ELEMENT = re.compile(
    r'click_element\s*\(\s*(?:id\s*=\s*)?"([^"]+)"\s*\)',
    re.IGNORECASE,
)
_RE_TYPE_ELEMENT = re.compile(
    r'type_element\s*\(\s*(?:id\s*=\s*)?"([^"]+)"\s*,\s*(?:text\s*=\s*)?"((?:[^"\\]|\\.)*)"\s*\)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Action parsing (standalone, used by both agent and tests)
# ---------------------------------------------------------------------------


def _parse_coord(raw: str) -> int:
    """Parse a coordinate value that may be int (500) or float (0.500).

    If the value is a float <= 1.0 with a decimal point, treat it as 0-1
    range and scale to 0-1000. Otherwise treat as Qwen 0-1000 int range.
    """
    val = float(raw)
    if val <= 1.0 and "." in raw:
        # 0-1 range (from fine-tuned model training format) -> scale to 0-1000
        return int(val * QWEN_COORD_SCALE)
    return int(val)


def _denorm_coord(x_qwen: int, y_qwen: int) -> tuple[float, float]:
    """Denormalize coordinates from Qwen [0, 1000] to [0, 1].

    Args:
        x_qwen: X coordinate in [0, 1000] range.
        y_qwen: Y coordinate in [0, 1000] range.

    Returns:
        Tuple of (x_norm, y_norm) in [0, 1] range.
    """
    return x_qwen / QWEN_COORD_SCALE, y_qwen / QWEN_COORD_SCALE


def _format_a11y_tree(tree: dict, indent: int = 0) -> str:
    """Format an accessibility tree dict into a compact text representation.

    Each element is shown as ``[id] role "name"`` on its own line, with
    indentation reflecting the tree structure. Only elements with an ``id``
    are included (they are the ones the agent can target).

    Args:
        tree: Accessibility tree dict with optional ``children`` list.
        indent: Current indentation level (for recursion).

    Returns:
        Formatted string, one line per element.
    """
    lines: list[str] = []
    prefix = "  " * indent

    role = tree.get("role", "")
    name = tree.get("name", "")
    elem_id = tree.get("id")

    if elem_id:
        lines.append(f'{prefix}[{elem_id}] {role} "{name}"')
    elif role:
        lines.append(f"{prefix}{role}: {name}" if name else f"{prefix}{role}")

    for child in tree.get("children", []):
        lines.append(_format_a11y_tree(child, indent + 1))

    return "\n".join(lines)


def _extract_action_string(response: str) -> str | None:
    """Extract the action string from model response, stripping think blocks.

    Args:
        response: Raw model response text.

    Returns:
        Action string (e.g. ``"click(x=500, y=300)"``) or None.
    """
    # Strip <think>...</think> blocks
    text = _RE_THINK.sub("", response).strip()
    if not text:
        return None

    # Take the first line that looks like a known action
    for line in text.splitlines():
        line = line.strip()
        if line and re.match(
            r"(click_element|type_element|click|left_click|double_click"
            r"|right_click|type|press|scroll|drag|wait|finished)\s*\(",
            line,
            re.IGNORECASE,
        ):
            return line

    # Fallback: return entire stripped text
    return text if text else None


def parse_qwen_action(
    response: str,
    viewport: tuple[int, int] | None = None,
) -> BenchmarkAction:
    """Parse Qwen3-VL action output into BenchmarkAction.

    Handles all action formats from the SYSTEM_PROMPT. Coordinates are
    denormalized from [0, 1000] to [0, 1] for BenchmarkAction. The
    original Qwen-scale coordinates are preserved in ``raw_action``.

    Args:
        response: Raw model output text (may include ``<think>`` blocks).
        viewport: Optional ``(width, height)`` of the viewport (stored in
            ``raw_action`` but not used for coordinate conversion since
            denormalization targets [0, 1] independent of viewport).

    Returns:
        Parsed BenchmarkAction with coordinates in [0, 1] range.
    """
    raw: dict[str, Any] = {"response": response}
    if viewport:
        raw["viewport"] = viewport

    # Extract and store thinking content
    think_match = _RE_THINK.search(response)
    if think_match:
        raw["thinking"] = think_match.group(1).strip()

    # Get the action string (stripping think blocks)
    action_str = _extract_action_string(response)
    if action_str:
        raw["action_string"] = action_str
    else:
        raw["parse_error"] = "No action found in response"
        return BenchmarkAction(type="error", raw_action=raw)

    # --- finished() ---
    if _RE_FINISHED.search(action_str):
        return BenchmarkAction(type="done", raw_action=raw)

    # --- wait() ---
    if _RE_WAIT.search(action_str):
        raw["is_wait"] = True
        return BenchmarkAction(type="wait", raw_action=raw)

    # --- click_element(id="<element_id>") --- element-based grounding
    m = _RE_CLICK_ELEMENT.search(action_str)
    if m:
        element_id = m.group(1)
        raw["element_action"] = "click_element"
        raw["target_element_id"] = element_id
        return BenchmarkAction(
            type="click", target_node_id=element_id, raw_action=raw
        )

    # --- type_element(id="<element_id>", text="<text>") ---
    m = _RE_TYPE_ELEMENT.search(action_str)
    if m:
        element_id = m.group(1)
        text = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        raw["element_action"] = "type_element"
        raw["target_element_id"] = element_id
        return BenchmarkAction(
            type="type",
            text=text,
            target_node_id=element_id,
            raw_action=raw,
        )

    # --- double_click(x=<int>, y=<int>) --- (check before click)
    m = _RE_DOUBLE_CLICK.search(action_str)
    if m:
        x_q, y_q = _parse_coord(m.group(1)), _parse_coord(m.group(2))
        x_n, y_n = _denorm_coord(x_q, y_q)
        raw["click_variant"] = "double_click"
        raw["qwen_coords"] = {"x": x_q, "y": y_q}
        return BenchmarkAction(
            type="click", x=x_n, y=y_n, raw_action=raw
        )

    # --- right_click(x=<int>, y=<int>) --- (check before click)
    m = _RE_RIGHT_CLICK.search(action_str)
    if m:
        x_q, y_q = _parse_coord(m.group(1)), _parse_coord(m.group(2))
        x_n, y_n = _denorm_coord(x_q, y_q)
        raw["click_variant"] = "right_click"
        raw["qwen_coords"] = {"x": x_q, "y": y_q}
        return BenchmarkAction(
            type="click", x=x_n, y=y_n, raw_action=raw
        )

    # --- click(x=<int>, y=<int>) ---
    m = _RE_CLICK.search(action_str)
    if m:
        x_q, y_q = _parse_coord(m.group(1)), _parse_coord(m.group(2))
        x_n, y_n = _denorm_coord(x_q, y_q)
        raw["qwen_coords"] = {"x": x_q, "y": y_q}
        return BenchmarkAction(
            type="click", x=x_n, y=y_n, raw_action=raw
        )

    # --- type(text="<string>") ---
    m = _RE_TYPE.search(action_str)
    if m:
        text = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        return BenchmarkAction(type="type", text=text, raw_action=raw)

    # --- press(keys=["<key1>", ...]) ---
    m = _RE_PRESS.search(action_str)
    if m:
        keys_str = m.group(1)
        keys = [k.strip().strip("\"'") for k in keys_str.split(",") if k.strip()]
        if len(keys) == 1:
            return BenchmarkAction(type="key", key=keys[0], raw_action=raw)
        elif len(keys) > 1:
            return BenchmarkAction(
                type="key", key=keys[-1], modifiers=keys[:-1], raw_action=raw
            )
        raw["parse_error"] = "Empty keys list in press()"
        return BenchmarkAction(type="error", raw_action=raw)

    # --- scroll(direction="<dir>", amount=<int>) ---
    m = _RE_SCROLL.search(action_str)
    if m:
        direction = m.group(1).lower()
        amount = int(m.group(2)) if m.group(2) else 3
        return BenchmarkAction(
            type="scroll",
            scroll_direction=direction,
            scroll_amount=float(amount),
            raw_action=raw,
        )

    # --- drag(from_coord=[<x1>, <y1>], to_coord=[<x2>, <y2>]) ---
    m = _RE_DRAG.search(action_str)
    if m:
        fx, fy = _parse_coord(m.group(1)), _parse_coord(m.group(2))
        tx, ty = _parse_coord(m.group(3)), _parse_coord(m.group(4))
        fx_n, fy_n = _denorm_coord(fx, fy)
        tx_n, ty_n = _denorm_coord(tx, ty)
        raw["qwen_coords"] = {
            "from": {"x": fx, "y": fy},
            "to": {"x": tx, "y": ty},
        }
        return BenchmarkAction(
            type="drag",
            x=fx_n, y=fy_n,
            end_x=tx_n, end_y=ty_n,
            raw_action=raw,
        )

    # Unknown action format
    raw["parse_error"] = f"Unknown action format: {action_str}"
    return BenchmarkAction(type="error", raw_action=raw)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------


class Qwen3VLAgent(BenchmarkAgent):
    """Agent using Qwen3-VL for local GUI agent inference.

    Loads the model via HuggingFace transformers and runs inference locally.
    Coordinates use the Qwen normalized [0, 1000] range internally and are
    converted to [0, 1] for BenchmarkAction output.

    The prompt format matches ``openadapt_ml.training.convert_demos`` so
    fine-tuned checkpoints see the same structure at inference time.

    Args:
        model_path: HuggingFace model ID or local checkpoint path.
            Defaults to ``Qwen/Qwen3-VL-8B-Instruct``.
        model_endpoint: Remote inference endpoint. Set to ``"modal"``
            to use Modal cloud GPU, or an HTTP URL for a custom server.
            When set, model is NOT loaded locally.
        demo: Optional demonstration text for demo-conditioned inference.
            Included at every step (not just step 0) following the
            pattern established by ApiAgent.
        use_thinking: Enable thinking mode with ``<think>`` blocks.
        use_accessibility_tree: When True, include the accessibility tree
            from observations in the prompt and prefer element-based actions
            (``click_element``, ``type_element``) over coordinate-based ones.
        device: Torch device (``"cuda"``, ``"cpu"``, ``"auto"``).
        max_new_tokens: Maximum tokens to generate per step.
        torch_dtype: Torch dtype string (``"auto"``, ``"float16"``, ``"bfloat16"``).
        min_pixels: Minimum image pixels for Qwen3-VL processor.
        max_pixels: Maximum image pixels for Qwen3-VL processor.
    """

    def __init__(
        self,
        model_path: str | None = None,
        model_endpoint: str | None = None,
        demo: str | None = None,
        use_thinking: bool = False,
        use_accessibility_tree: bool = False,
        device: str = "auto",
        max_new_tokens: int = 512,
        torch_dtype: str = "auto",
        min_pixels: int = 256 * 28 * 28,
        max_pixels: int = 1280 * 28 * 28,
    ):
        self.model_path = model_path or DEFAULT_MODEL
        self.model_endpoint = model_endpoint
        self.demo = demo
        self.use_thinking = use_thinking
        self.use_accessibility_tree = use_accessibility_tree
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.torch_dtype = torch_dtype
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels

        # State across steps within an episode
        self._previous_actions: list[str] = []
        self._step_count = 0

        # Lazy-loaded model and processor
        self._model = None
        self._processor = None

        logger.info(
            f"Qwen3VLAgent initialized: model={self.model_path}, "
            f"endpoint={self.model_endpoint}, "
            f"thinking={self.use_thinking}, "
            f"a11y_tree={self.use_accessibility_tree}, device={self.device}"
        )
        if self.demo:
            logger.info(f"Demo provided ({len(self.demo)} chars)")

    def _load_model(self) -> None:
        """Lazy-load model and processor on first inference call.

        If ``model_path`` points to a PEFT adapter directory (contains
        ``adapter_config.json``), the base model is loaded first and
        the adapter is applied on top via ``PeftModel.from_pretrained``.

        Raises:
            RuntimeError: If transformers/torch is not installed or model
                loading fails.
        """
        if self._model is not None:
            return

        try:
            import torch
            from transformers import AutoProcessor
        except ImportError as e:
            raise RuntimeError(
                "Qwen3VLAgent requires transformers and torch. "
                "Install with: pip install transformers torch"
            ) from e

        # Resolve torch dtype
        dtype_map = {
            "auto": "auto",
            "float16": torch.float16,
            "fp16": torch.float16,
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
            "float32": torch.float32,
            "fp32": torch.float32,
        }
        resolved_dtype = dtype_map.get(self.torch_dtype, "auto")

        # Check if model_path is a PEFT adapter directory
        adapter_config_path = Path(self.model_path) / "adapter_config.json"
        is_peft_adapter = adapter_config_path.exists()

        if is_peft_adapter:
            import json

            with open(adapter_config_path) as f:
                adapter_cfg = json.load(f)
            base_model_id = adapter_cfg.get(
                "base_model_name_or_path", DEFAULT_MODEL
            )
            logger.info(
                f"PEFT adapter detected at {self.model_path}, "
                f"base model: {base_model_id}"
            )
            hf_model_id = base_model_id
        else:
            hf_model_id = self.model_path

        logger.info(f"Loading model: {hf_model_id}")

        # Qwen3-VL uses the same architecture class as Qwen2.5-VL in
        # current transformers versions. Try AutoModelForImageTextToText first
        # (transformers 5.x), then AutoModelForVision2Seq (older), then
        # fall back to the specific class.
        try:
            try:
                from transformers import AutoModelForImageTextToText as AutoVLM
            except ImportError:
                from transformers import AutoModelForVision2Seq as AutoVLM

            self._model = AutoVLM.from_pretrained(
                hf_model_id,
                torch_dtype=resolved_dtype,
                device_map=self.device,
            )
        except Exception:
            from transformers import Qwen2_5_VLForConditionalGeneration

            self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                hf_model_id,
                torch_dtype=resolved_dtype,
                device_map=self.device,
            )

        # Apply PEFT adapter if detected
        if is_peft_adapter:
            from peft import PeftModel

            logger.info(f"Applying PEFT adapter from {self.model_path}")
            self._model = PeftModel.from_pretrained(
                self._model, self.model_path
            )

        self._processor = AutoProcessor.from_pretrained(
            hf_model_id,
            min_pixels=self.min_pixels,
            max_pixels=self.max_pixels,
        )

        logger.info("Model loaded successfully")

    def reset(self) -> None:
        """Reset agent state between episodes.

        Note: demo is NOT reset -- it persists across resets if set.
        """
        self._step_count = 0
        self._previous_actions = []
        logger.debug("Qwen3VLAgent reset")

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Given observation and task, return next action.

        Builds a prompt matching the training format from convert_demos,
        runs Qwen3-VL inference, and parses the output into a
        BenchmarkAction with coordinates denormalized to [0, 1].

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            BenchmarkAction to execute.
        """
        if not self.model_endpoint:
            self._load_model()
        self._step_count += 1

        # Get screenshot as PIL Image
        image = self._get_image(observation)
        if image is None:
            logger.error("No screenshot available in observation")
            return BenchmarkAction(
                type="error", raw_action={"error": "no_screenshot"}
            )

        # Build user content (aligned with convert_demos training format)
        user_content = self._build_prompt(task.instruction, observation)

        # Build chat messages and run inference
        messages = self._build_messages(user_content)
        try:
            if self.model_endpoint:
                response_text = self._run_remote_inference(messages, image)
            else:
                response_text = self._run_inference(messages, image)
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            return BenchmarkAction(
                type="error", raw_action={"error": f"inference_failed: {e}"}
            )

        logger.info(f"Step {self._step_count} raw response: {response_text!r}")

        # Parse the response into a BenchmarkAction (with denormalization)
        action = parse_qwen_action(response_text, observation.viewport)

        # Track the parsed action string for history in next prompt
        action_str = _extract_action_string(response_text)
        if action_str:
            self._previous_actions.append(action_str)

        # Annotate raw_action with step number
        if action.raw_action is None:
            action.raw_action = {}
        action.raw_action["step"] = self._step_count

        return action

    def _build_prompt(
        self,
        instruction: str,
        observation: BenchmarkObservation | None = None,
    ) -> str:
        """Build the user-turn text, aligned with convert_demos.convert_step.

        The format matches the training data exactly::

            <image>
            Instruction: {instruction}

            Previous actions:
              Step 0: {action}
              Step 1: {action}

            First reason about what you see in <think>...</think> tags,
            then output exactly one action.

        When a demo is provided, it is injected after the instruction
        and before the previous actions (demo-conditioned inference).

        When ``use_accessibility_tree`` is True and the observation contains
        an a11y tree, the tree is formatted and included in the prompt.

        Args:
            instruction: Task instruction text.
            observation: Current observation (for a11y tree extraction).

        Returns:
            User content string.
        """
        parts = ["<image>"]
        parts.append(f"Instruction: {instruction}")

        # Demo injection (included at every step, not just step 0)
        if self.demo:
            parts.append("")
            parts.append(
                "Demonstration (follow this pattern):\n"
                f"{self.demo}"
            )

        # Accessibility tree (when enabled and available)
        if self.use_accessibility_tree and observation is not None:
            a11y_tree = observation.accessibility_tree
            if a11y_tree:
                formatted = _format_a11y_tree(a11y_tree)
                if formatted:
                    parts.append("")
                    parts.append(
                        "Available UI elements (use click_element/type_element "
                        "with these IDs):\n" + formatted
                    )

        # Previous actions
        if self._previous_actions:
            parts.append("")
            parts.append("Previous actions:")
            for i, act in enumerate(self._previous_actions):
                parts.append(f"  Step {i}: {act}")

        # Tail instruction
        parts.append("")
        if self.use_thinking:
            parts.append(
                "First reason about what you see in <think>...</think> "
                "tags, then output exactly one action."
            )
        else:
            parts.append("Output exactly one action.")

        return "\n".join(parts)

    def _build_messages(
        self, user_content: str
    ) -> list[dict[str, Any]]:
        """Build chat messages for the Qwen3-VL processor.

        Args:
            user_content: User message content string (from ``_build_prompt``).

        Returns:
            List of message dicts for ``apply_chat_template``.
        """
        sys_prompt = SYSTEM_PROMPT_A11Y if self.use_accessibility_tree else SYSTEM_PROMPT
        return [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": user_content},
                ],
            },
        ]

    def _get_image(self, observation: BenchmarkObservation):
        """Extract PIL Image from observation.

        Args:
            observation: Current observation.

        Returns:
            PIL.Image.Image or None.
        """
        from PIL import Image

        screenshot_bytes = observation.screenshot
        if screenshot_bytes is None and observation.screenshot_path:
            try:
                screenshot_bytes = Path(observation.screenshot_path).read_bytes()
            except (FileNotFoundError, OSError):
                pass

        if screenshot_bytes is None:
            return None

        try:
            return Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        except Exception as e:
            logger.warning(f"Failed to open screenshot: {e}")
            return None

    def _run_inference(
        self,
        messages: list[dict[str, Any]],
        image: Any,
    ) -> str:
        """Run model inference with the given messages and image.

        Args:
            messages: Chat messages (system + user with image placeholder).
            image: PIL Image for the current screenshot.

        Returns:
            Generated text response.
        """
        import torch

        # Apply chat template to get the full prompt text
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

        # Process inputs (image + text)
        # The processor handles image embedding via the message format
        inputs = self._processor(
            text=[text],
            images=[image],
            padding=True,
            return_tensors="pt",
        )

        # Move to model device
        device = next(self._model.parameters()).device
        inputs = inputs.to(device)

        # Generate
        with torch.no_grad():
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
            )

        # Decode only the generated tokens (trim prompt)
        input_len = inputs["input_ids"].shape[1]
        generated_ids = output_ids[:, input_len:]
        response = self._processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

        return response.strip()

    def _run_remote_inference(
        self,
        messages: list[dict[str, Any]],
        image: Any,
    ) -> str:
        """Run inference via a remote endpoint (Modal or HTTP).

        Args:
            messages: Chat messages (system + user with image placeholder).
            image: PIL Image for the current screenshot.

        Returns:
            Generated text response.
        """
        import base64
        import json

        # Encode image to base64
        buf = BytesIO()
        image.save(buf, format="PNG")
        image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Flatten messages for remote call (strip image dicts, keep text)
        remote_messages = []
        for msg in messages:
            if isinstance(msg.get("content"), list):
                # User message with image + text parts
                text_parts = [
                    p["text"] for p in msg["content"] if p.get("type") == "text"
                ]
                remote_messages.append(
                    {"role": msg["role"], "content": "\n".join(text_parts)}
                )
            else:
                remote_messages.append(msg)

        if self.model_endpoint == "modal":
            # Use Modal's call_inference function
            from openadapt_ml.cloud.modal_cloud import call_inference

            # Determine adapter path from model_path if it's a PEFT adapter
            adapter_path = None
            adapter_config = Path(self.model_path) / "adapter_config.json"
            if adapter_config.exists():
                # Read base model from adapter config
                with open(adapter_config) as f:
                    cfg = json.load(f)
                base_model = cfg.get("base_model_name_or_path", DEFAULT_MODEL)
                # Adapter is uploaded to volume at /adapter by upload_adapter_to_volume()
                # Volume mounts at /training, so full path is /training/adapter
                adapter_path = "/training/adapter"
            else:
                base_model = self.model_path

            response = call_inference(
                messages=remote_messages,
                image_base64=image_b64,
                max_new_tokens=self.max_new_tokens,
                adapter_path=adapter_path,
                base_model=base_model,
            )
            return response
        else:
            # HTTP endpoint
            import requests

            payload = {
                "messages": remote_messages,
                "image_base64": image_b64,
                "max_new_tokens": self.max_new_tokens,
            }
            resp = requests.post(
                f"{self.model_endpoint}/infer",
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")

    def set_demo(self, demo: str) -> None:
        """Set or update the demonstration trajectory.

        Allows setting the demo after initialization, useful for
        dynamic demo retrieval.

        Args:
            demo: Demonstration text to include at every step.
        """
        self.demo = demo
        logger.info(f"Demo set ({len(demo)} chars) - persists across all steps")
