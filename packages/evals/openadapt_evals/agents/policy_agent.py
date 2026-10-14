"""Policy-based agent using trained models from openadapt-ml.

This module provides an agent that uses trained policy models from
openadapt-ml for benchmark evaluation. It imports the model classes
from openadapt-ml to avoid code duplication.

Example:
    from openadapt_evals.agents import PolicyAgent

    # Load a trained checkpoint
    agent = PolicyAgent(checkpoint_path="/path/to/checkpoint")
    action = agent.act(observation, task)
"""

from __future__ import annotations

import logging

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent

logger = logging.getLogger("openadapt_evals.agents.policy")


class PolicyAgent(BenchmarkAgent):
    """Agent that uses a trained policy model from openadapt-ml.

    This agent loads a trained VLM policy model and uses it for
    benchmark evaluation. The model is expected to be trained using
    the openadapt-ml training pipeline.

    Prompt format is aligned with convert_demos.py training data.

    Args:
        checkpoint_path: Path to LoRA adapter weights.
        model_name: HuggingFace model name (must contain 'Qwen3-VL' or 'Qwen2.5-VL').
        device: Device to run on ('cuda' or 'cpu').
        use_thinking: Whether to include <think> instruction in prompts.
    """

    def __init__(
        self,
        checkpoint_path: str | None = None,
        model_name: str = "Qwen/Qwen3-VL-8B-Instruct",
        device: str = "cuda",
        use_thinking: bool = True,
    ):
        self.checkpoint_path = checkpoint_path
        self.model_name = model_name
        self.device = device
        self.use_thinking = use_thinking

        # Lazy load model to avoid import overhead
        self._model = None
        self._processor = None
        self._previous_actions: list[str] = []
        self._temp_files: list[str] = []

    def _load_model(self) -> None:
        """Load the model adapter from checkpoint."""
        if self._model is not None:
            return

        try:
            import torch
            from openadapt_ml.models.qwen_vl import QwenVLAdapter

            device = torch.device(self.device) if isinstance(self.device, str) else self.device
            lora_config = (
                {"weights_path": self.checkpoint_path}
                if self.checkpoint_path
                else None
            )
            self._model = QwenVLAdapter.from_pretrained(
                model_name=self.model_name,
                lora_config=lora_config,
                device=device,
            )
            logger.info(f"PolicyAgent loaded model from {self.checkpoint_path}")
        except ImportError as e:
            raise RuntimeError(
                "PolicyAgent requires openadapt-ml. "
                "Install with: pip install openadapt-ml"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to load model: {e}") from e

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
        # Ensure model is loaded
        self._load_model()

        # Build prompt (aligned with training format)
        prompt = self._build_prompt(observation, task, history)

        # Get model prediction
        try:
            response = self._run_inference(observation, prompt)
            action = self._parse_response(response, observation)

            # Track action in training format for "Previous actions" section
            self._previous_actions.append(self._format_action_qwen(action))

            return action
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            return BenchmarkAction(type="error", raw_action={"error": str(e)})

    def _build_prompt(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> str:
        """Build user-turn text aligned with convert_demos.convert_step.

        Format matches training data exactly::

            <image>
            Instruction: {instruction}

            Previous actions:
              Step 0: {action}
              Step 1: {action}

            First reason about what you see in <think>...</think> tags,
            then output exactly one action.

        Args:
            observation: Current observation.
            task: Task being performed.
            history: Previous steps.

        Returns:
            Prompt string.
        """
        parts = ["<image>"]
        parts.append(f"Instruction: {task.instruction}")

        # Previous actions (matches training format)
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

    @staticmethod
    def _format_action_qwen(action: BenchmarkAction) -> str:
        """Format action matching convert_demos._format_action_qwen training format.

        Uses [0, 1000] coordinate range and lowercase function-call style
        to match what the model was trained on.
        """
        def _to_1000(v: float | None) -> int:
            return round((v or 0.0) * 1000)

        if action.type == "click":
            return f"click(x={_to_1000(action.x)}, y={_to_1000(action.y)})"
        if action.type == "double_click":
            return f"double_click(x={_to_1000(action.x)}, y={_to_1000(action.y)})"
        if action.type == "right_click":
            return f"right_click(x={_to_1000(action.x)}, y={_to_1000(action.y)})"
        if action.type == "type":
            return f'type(text="{action.text or ""}")'
        if action.type == "key":
            keys = (action.modifiers or []) + ([action.key] if action.key else [])
            keys_fmt = ", ".join(f'"{k}"' for k in keys)
            return f"press(keys=[{keys_fmt}])"
        if action.type == "scroll":
            return f'scroll(direction="{action.scroll_direction or "down"}", amount=3)'
        if action.type == "drag":
            return (
                f"drag(from_coord=[{_to_1000(action.x)}, {_to_1000(action.y)}], "
                f"to_coord=[{_to_1000(action.end_x)}, {_to_1000(action.end_y)}])"
            )
        if action.type == "done":
            return "finished()"
        return f"# unknown: {action.type}"

    def _build_sample(self, observation: BenchmarkObservation, prompt: str) -> dict:
        """Build SFT-style sample matching training format from convert_demos.py.

        NOTE: No system message is included here because
        ``QwenVLAdapter.generate()`` only extracts the user role message
        and drops any system role.  The model was trained under the same
        conditions (no system prompt), so omitting it at inference keeps
        behaviour consistent.

        Args:
            observation: Observation with screenshot.
            prompt: User-turn prompt text (from _build_prompt).

        Returns:
            SFT sample dict with messages and optional images.
        """
        sample = {
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }
        if observation.screenshot_path:
            sample["images"] = [observation.screenshot_path]
        return sample

    def _run_inference(self, observation: BenchmarkObservation, prompt: str) -> str:
        """Run model inference using SFT-style message format.

        Args:
            observation: Observation with screenshot.
            prompt: User-turn prompt text.

        Returns:
            Model response text.
        """
        if not observation.screenshot and not observation.screenshot_path:
            raise ValueError("No screenshot in observation")

        sample = self._build_sample(observation, prompt)

        # If screenshot_path is missing but bytes are available, write to temp file
        if "images" not in sample and observation.screenshot:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(observation.screenshot)
            tmp.close()
            sample["images"] = [tmp.name]
            self._temp_files.append(tmp.name)

        # Use the adapter's generate method (works with both local and remote)
        response = self._model.generate(sample)
        return response

    def _parse_response(
        self, response: str, observation: BenchmarkObservation
    ) -> BenchmarkAction:
        """Parse model response into BenchmarkAction.

        Uses ``parse_qwen_action`` from ``qwen3vl_agent`` which handles the
        lowercase keyword format the trained model outputs (e.g.
        ``click(x=500, y=300)``).  The base ``parse_action_response`` only
        recognises UPPERCASE format (``CLICK(0.5, 0.3)``), so every Qwen
        model output would silently fall through to ``type="done"``.

        Args:
            response: Model response text.
            observation: Observation for coordinate normalization.

        Returns:
            Parsed action.
        """
        from openadapt_evals.agents.qwen3vl_agent import parse_qwen_action
        return parse_qwen_action(response, observation.viewport)

    def reset(self) -> None:
        """Reset agent state between tasks."""
        import os

        self._previous_actions = []
        for path in self._temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._temp_files = []
