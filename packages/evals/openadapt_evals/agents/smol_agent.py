"""SmolVLM2 agent for benchmark evaluation.

Uses SmolVLM2-2.2B-Instruct-Agentic-GUI (Smol2Operator) for local GUI agent
inference. Coordinates are already in [0, 1] range — no denormalization needed.

The model was fine-tuned in two phases:
  1. Stage 1: 400K grounding samples (aguvis-stage-1)
  2. Stage 2: 784K agentic samples (aguvis-stage-2)

Action space (SmolVLM2 format):
    click(x=0.5, y=0.3)
    double_click(x=0.5, y=0.3)
    long_press(x=0.5, y=0.3)
    type(text='hello world')
    press(keys=['ctrl', 'c'])
    scroll(direction='up', amount=10)
    swipe(from_coord=[0.1, 0.2], to_coord=[0.8, 0.5])
    drag(from_coord=[0.1, 0.2], to_coord=[0.8, 0.5])
    open_app(app_name='notepad')
    navigate_home()
    final_answer('success')

Usage:
    from openadapt_evals.agents import SmolOperatorAgent

    agent = SmolOperatorAgent()
    action = agent.act(observation, task)

    # With demo conditioning
    agent = SmolOperatorAgent(demo="Step 0: click(x=0.45, y=0.95)...")
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

logger = logging.getLogger("openadapt_evals.agents.smol")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "smolagents/SmolVLM2-2.2B-Instruct-Agentic-GUI"

SYSTEM_PROMPT = (
    "You are a helpful GUI agent that can interact with user interfaces. "
    "Please generate the next move according to the UI screenshot, "
    "instruction and previous actions."
)

# ---------------------------------------------------------------------------
# Compiled regex patterns for action parsing
# ---------------------------------------------------------------------------

_FLOAT = r"[\d.]+"

_RE_CLICK = re.compile(
    r"(?<!double_)(?<!long_)click\s*\(\s*x\s*=\s*(" + _FLOAT + r")\s*,\s*y\s*=\s*(" + _FLOAT + r")\s*\)",
    re.IGNORECASE,
)
_RE_DOUBLE_CLICK = re.compile(
    r"double_click\s*\(\s*x\s*=\s*(" + _FLOAT + r")\s*,\s*y\s*=\s*(" + _FLOAT + r")\s*\)",
    re.IGNORECASE,
)
_RE_LONG_PRESS = re.compile(
    r"long_press\s*\(\s*x\s*=\s*(" + _FLOAT + r")\s*,\s*y\s*=\s*(" + _FLOAT + r")\s*\)",
    re.IGNORECASE,
)
_RE_TYPE = re.compile(
    r"type\s*\(\s*text\s*=\s*['\"](.+?)['\"]\s*\)",
    re.IGNORECASE,
)
_RE_PRESS = re.compile(
    r"press\s*\(\s*keys\s*=\s*\[([^\]]*)\]\s*\)",
    re.IGNORECASE,
)
_RE_SCROLL = re.compile(
    r"scroll\s*\(\s*direction\s*=\s*['\"](\w+)['\"]\s*"
    r"(?:,\s*amount\s*=\s*(\d+)\s*)?\)",
    re.IGNORECASE,
)
_RE_DRAG = re.compile(
    r"drag\s*\(\s*from_coord\s*=\s*\[\s*(" + _FLOAT + r")\s*,\s*(" + _FLOAT + r")\s*\]\s*,"
    r"\s*to_coord\s*=\s*\[\s*(" + _FLOAT + r")\s*,\s*(" + _FLOAT + r")\s*\]\s*\)",
    re.IGNORECASE,
)
_RE_SWIPE = re.compile(
    r"swipe\s*\(\s*from_coord\s*=\s*\[\s*(" + _FLOAT + r")\s*,\s*(" + _FLOAT + r")\s*\]\s*,"
    r"\s*to_coord\s*=\s*\[\s*(" + _FLOAT + r")\s*,\s*(" + _FLOAT + r")\s*\]\s*\)",
    re.IGNORECASE,
)
_RE_FINAL_ANSWER = re.compile(
    r"final_answer\s*\(\s*['\"](.+?)['\"]\s*\)",
    re.IGNORECASE,
)
_RE_OPEN_APP = re.compile(
    r"open_app\s*\(\s*app_name\s*=\s*['\"](.+?)['\"]\s*\)",
    re.IGNORECASE,
)
_RE_NAVIGATE_HOME = re.compile(r"navigate_home\s*\(\s*\)", re.IGNORECASE)

_RE_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_RE_CODE = re.compile(r"<code>(.*?)</code>", re.DOTALL)


# ---------------------------------------------------------------------------
# Action parsing
# ---------------------------------------------------------------------------


def _extract_action_string(response: str) -> str | None:
    """Extract the action string from model response.

    SmolVLM2 Phase 2 outputs <think>...</think> followed by <code>...</code>.
    """
    # Try to extract from <code> block first
    code_match = _RE_CODE.search(response)
    if code_match:
        return code_match.group(1).strip()

    # Strip <think> blocks
    text = _RE_THINK.sub("", response).strip()
    if not text:
        return None

    # Take the first line that looks like a known action
    for line in text.splitlines():
        line = line.strip()
        if line and re.match(
            r"(click|double_click|long_press|type|press|scroll|drag|swipe"
            r"|open_app|navigate_home|final_answer)\s*\(",
            line,
            re.IGNORECASE,
        ):
            return line

    return text if text else None


def parse_smol_action(
    response: str,
    viewport: tuple[int, int] | None = None,
) -> BenchmarkAction:
    """Parse SmolVLM2 action output into BenchmarkAction.

    Coordinates are already in [0, 1] range — no conversion needed.

    Args:
        response: Raw model output text (may include <think>/<code> blocks).
        viewport: Optional (width, height) stored in raw_action for reference.

    Returns:
        Parsed BenchmarkAction with coordinates in [0, 1] range.
    """
    raw: dict[str, Any] = {"response": response}
    if viewport:
        raw["viewport"] = viewport

    think_match = _RE_THINK.search(response)
    if think_match:
        raw["thinking"] = think_match.group(1).strip()

    action_str = _extract_action_string(response)
    if action_str:
        raw["action_string"] = action_str
    else:
        raw["parse_error"] = "No action found in response"
        return BenchmarkAction(type="error", raw_action=raw)

    # --- final_answer('...') → done ---
    m = _RE_FINAL_ANSWER.search(action_str)
    if m:
        answer = m.group(1)
        return BenchmarkAction(type="done", answer=answer, raw_action=raw)

    # --- navigate_home() ---
    if _RE_NAVIGATE_HOME.search(action_str):
        return BenchmarkAction(
            type="key", key="super", raw_action=raw
        )

    # --- double_click(x=..., y=...) --- (check before click)
    m = _RE_DOUBLE_CLICK.search(action_str)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        raw["click_variant"] = "double_click"
        return BenchmarkAction(type="click", x=x, y=y, raw_action=raw)

    # --- long_press(x=..., y=...) --- (check before click)
    m = _RE_LONG_PRESS.search(action_str)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        raw["click_variant"] = "long_press"
        return BenchmarkAction(type="click", x=x, y=y, raw_action=raw)

    # --- click(x=..., y=...) ---
    m = _RE_CLICK.search(action_str)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        return BenchmarkAction(type="click", x=x, y=y, raw_action=raw)

    # --- type(text='...') ---
    m = _RE_TYPE.search(action_str)
    if m:
        text = m.group(1)
        return BenchmarkAction(type="type", text=text, raw_action=raw)

    # --- press(keys=['...', ...]) ---
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

    # --- scroll(direction='...', amount=...) ---
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

    # --- drag(from_coord=[...], to_coord=[...]) ---
    m = _RE_DRAG.search(action_str)
    if m:
        fx, fy = float(m.group(1)), float(m.group(2))
        tx, ty = float(m.group(3)), float(m.group(4))
        return BenchmarkAction(
            type="drag", x=fx, y=fy, end_x=tx, end_y=ty, raw_action=raw
        )

    # --- swipe(from_coord=[...], to_coord=[...]) → drag ---
    m = _RE_SWIPE.search(action_str)
    if m:
        fx, fy = float(m.group(1)), float(m.group(2))
        tx, ty = float(m.group(3)), float(m.group(4))
        raw["action_variant"] = "swipe"
        return BenchmarkAction(
            type="drag", x=fx, y=fy, end_x=tx, end_y=ty, raw_action=raw
        )

    # --- open_app(app_name='...') → type + enter ---
    m = _RE_OPEN_APP.search(action_str)
    if m:
        app_name = m.group(1)
        raw["open_app"] = app_name
        return BenchmarkAction(type="type", text=app_name, raw_action=raw)

    # Unknown action format
    raw["parse_error"] = f"Unknown action format: {action_str}"
    return BenchmarkAction(type="error", raw_action=raw)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------


class SmolOperatorAgent(BenchmarkAgent):
    """Agent using SmolVLM2-2.2B for local GUI agent inference.

    Loads the model via HuggingFace transformers. Coordinates are natively
    in [0, 1] range — no conversion needed for BenchmarkAction.

    Args:
        model_path: HuggingFace model ID or local path. Defaults to
            ``smolagents/SmolVLM2-2.2B-Instruct-Agentic-GUI``.
        demo: Optional demonstration text for demo-conditioned inference.
        device: Torch device (``"cuda"``, ``"cpu"``, ``"auto"``).
        max_new_tokens: Maximum tokens to generate per step.
        torch_dtype: Torch dtype string.
        image_size: Target image size for processor.
    """

    def __init__(
        self,
        model_path: str | None = None,
        demo: str | None = None,
        device: str = "auto",
        max_new_tokens: int = 512,
        torch_dtype: str = "auto",
        image_size: int = 1152,
    ):
        self.model_path = model_path or DEFAULT_MODEL
        self.demo = demo
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.torch_dtype = torch_dtype
        self.image_size = image_size

        self._previous_actions: list[str] = []
        self._step_count = 0
        self._model = None
        self._processor = None

        logger.info(
            f"SmolOperatorAgent initialized: model={self.model_path}, "
            f"device={self.device}"
        )
        if self.demo:
            logger.info(f"Demo provided ({len(self.demo)} chars)")

    def _load_model(self) -> None:
        """Lazy-load model and processor on first inference call."""
        if self._model is not None:
            return

        try:
            import torch
            from transformers import AutoProcessor

            try:
                from transformers import AutoModelForImageTextToText as AutoVLM
            except ImportError:
                from transformers import AutoModelForVision2Seq as AutoVLM
        except ImportError as e:
            raise RuntimeError(
                "SmolOperatorAgent requires transformers and torch. "
                "Install with: pip install transformers torch"
            ) from e

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

        logger.info(f"Loading model: {self.model_path}")
        self._model = AutoVLM.from_pretrained(
            self.model_path,
            torch_dtype=resolved_dtype,
            device_map=self.device,
        )
        self._processor = AutoProcessor.from_pretrained(self.model_path)
        logger.info("Model loaded successfully")

    def reset(self) -> None:
        """Reset agent state between episodes."""
        self._step_count = 0
        self._previous_actions = []
        logger.debug("SmolOperatorAgent reset")

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Given observation and task, return next action."""
        self._load_model()
        self._step_count += 1

        image = self._get_image(observation)
        if image is None:
            logger.error("No screenshot available in observation")
            return BenchmarkAction(
                type="error", raw_action={"error": "no_screenshot"}
            )

        user_content = self._build_prompt(task.instruction)
        messages = self._build_messages(user_content, image)

        try:
            response_text = self._run_inference(messages, image)
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            return BenchmarkAction(
                type="error", raw_action={"error": f"inference_failed: {e}"}
            )

        logger.info(f"Step {self._step_count} raw response: {response_text!r}")

        action = parse_smol_action(response_text, observation.viewport)

        action_str = _extract_action_string(response_text)
        if action_str:
            self._previous_actions.append(action_str)

        if action.raw_action is None:
            action.raw_action = {}
        action.raw_action["step"] = self._step_count

        return action

    def _build_prompt(self, instruction: str) -> str:
        """Build the user-turn text."""
        parts = [f"Instruction: {instruction}"]

        if self.demo:
            parts.append("")
            parts.append(f"Demonstration (follow this pattern):\n{self.demo}")

        if self._previous_actions:
            parts.append("")
            parts.append("Previous actions:")
            for i, act in enumerate(self._previous_actions):
                parts.append(f"  Step {i}: {act}")

        parts.append("")
        parts.append("Output exactly one action.")

        return "\n".join(parts)

    def _build_messages(
        self, user_content: str, image: Any
    ) -> list[dict[str, Any]]:
        """Build chat messages for the SmolVLM2 processor."""
        return [
            {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]},
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": user_content},
                ],
            },
        ]

    def _get_image(self, observation: BenchmarkObservation):
        """Extract PIL Image from observation."""
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
            img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
            # Resize to target size while maintaining aspect ratio
            if self.image_size:
                img.thumbnail((self.image_size, self.image_size))
            return img
        except Exception as e:
            logger.warning(f"Failed to open screenshot: {e}")
            return None

    def _run_inference(
        self,
        messages: list[dict[str, Any]],
        image: Any,
    ) -> str:
        """Run model inference."""
        import torch

        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

        inputs = self._processor(
            text=[text],
            images=[image],
            padding=True,
            return_tensors="pt",
        )

        device = next(self._model.parameters()).device
        inputs = inputs.to(device)

        with torch.no_grad():
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
            )

        input_len = inputs["input_ids"].shape[1]
        generated_ids = output_ids[:, input_len:]
        response = self._processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

        return response.strip()

    def set_demo(self, demo: str) -> None:
        """Set or update the demonstration trajectory."""
        self.demo = demo
        logger.info(f"Demo set ({len(demo)} chars) - persists across all steps")
