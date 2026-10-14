"""TRL GRPOTrainer rollout function for WAA desktop environments.

Wraps WAADesktopEnv into TRL's experimental ``rollout_func`` API, enabling
GRPO training of VLM agents against live (or mock) Windows VMs.

The rollout_func receives prompts (task instructions) from the trainer,
runs multi-step episodes against the environment, collects action tokens
and logprobs, computes dense rewards via milestones, and returns everything
in the format TRL expects.

GRPO (Group Relative Policy Optimization) training uses group-level
advantage estimation from multiple rollouts of the same prompt, as
introduced in the DeepSeek-Math work. This module integrates that
algorithm with live desktop environments via TRL's rollout API.

Constrained decoding (optional):
    When ``constrained_decoding=True``, Outlines is used to force model
    output to match the ``Thought: ...\nAction: CLICK/TYPE/WAIT/DONE``
    format. This eliminates 5-15% of wasted rollouts from unparseable
    VLM output. Requires ``pip install outlines>=0.1.0``.

Usage with TRL:
    from trl import GRPOConfig, GRPOTrainer
    from openadapt_evals.training.trl_rollout import make_waa_rollout_func

    rollout_func = make_waa_rollout_func(
        adapter=WAALiveAdapter(WAALiveConfig(server_url="http://localhost:5001")),
        task_configs=TaskConfig.from_dir("./tasks/"),
        max_steps=15,
        constrained_decoding=True,
    )

    trainer = GRPOTrainer(
        model=model,
        processing_class=processor,
        args=GRPOConfig(...),
        train_dataset=dataset,
        rollout_func=rollout_func,
    )
    trainer.train()

Usage with mock adapter (no VM):
    from openadapt_evals.training.trl_rollout import make_waa_rollout_func
    from openadapt_evals.adapters.waa.mock import WAAMockAdapter

    rollout_func = make_waa_rollout_func(
        adapter=WAAMockAdapter(),
        task_configs=task_configs,
    )

Prior Art:
    - GRPO: Shao et al., "DeepSeekMath: Pushing the Limits of
      Mathematical Reasoning in Open Language Models", arXiv 2402.03300,
      2024. Introduced Group Relative Policy Optimization.
    - TRL: Hugging Face, "TRL: Transformer Reinforcement Learning",
      https://github.com/huggingface/trl. Open-source library providing
      GRPOTrainer and the experimental rollout_func API.
    - PPO for LLMs: Schulman et al., "Proximal Policy Optimization
      Algorithms", arXiv 2017. Foundation for policy gradient methods
      in language model fine-tuning.
    - RLHF: Ouyang et al., "Training Language Models to Follow
      Instructions with Human Feedback", NeurIPS 2022. Established the
      RL fine-tuning paradigm for language models.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel

from openadapt_evals.action_envelope import (
    parse_single_dsl_action,
    parse_single_json_object,
    require_exact_fields,
)
from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
from openadapt_evals.errors import ActionParseError, RolloutInfrastructureError

# Re-exported on purpose, not merely imported: this module must use the SAME
# system prompt object as the standalone trainer, and
# tests/test_trl_parity.py asserts the identity so the two training paths
# cannot drift. The base model (Qwen2.5-VL-7B-Instruct) was SFT'd on the DSL
# format (Thought: ...\nAction: CLICK(x=0.XX, y=0.XX)); a different prompt
# produces garbage because the model has never seen that format.
from openadapt_evals.training.standalone.prompt import SYSTEM_PROMPT  # noqa: F401

logger = logging.getLogger(__name__)


def _required_fraction(data: dict, key: str, action_type: str) -> float:
    """Return one required finite normalized coordinate."""
    if key not in data:
        raise ActionParseError(f"{action_type} requires {key}")
    try:
        value = float(data[key])
    except (TypeError, ValueError) as exc:
        raise ActionParseError(
            f"{action_type} has invalid {key}: {data[key]!r}"
        ) from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ActionParseError(f"{action_type} {key} must be between 0 and 1")
    return value


def _parse_json_action(data: dict) -> BenchmarkAction:
    """Validate one JSON action without inventing missing arguments."""
    action_type = data.get("type")
    if action_type == "click":
        _require_json_fields(data, {"type", "x", "y"})
        return BenchmarkAction(
            type=action_type,
            x=_required_fraction(data, "x", action_type),
            y=_required_fraction(data, "y", action_type),
        )
    if action_type == "scroll":
        _require_json_fields(data, {"type", "x", "y", "direction"})
        direction = data.get("direction")
        if direction not in ("up", "down"):
            raise ActionParseError("scroll requires direction='up' or direction='down'")
        return BenchmarkAction(
            type="scroll",
            x=_required_fraction(data, "x", action_type),
            y=_required_fraction(data, "y", action_type),
            scroll_direction=direction,
        )
    if action_type == "type":
        _require_json_fields(data, {"type", "text"})
        if "text" not in data or not isinstance(data["text"], str):
            raise ActionParseError("type requires string text")
        return BenchmarkAction(type="type", text=data["text"])
    if action_type == "key":
        _require_json_fields(data, {"type", "key"})
        if not isinstance(data.get("key"), str) or not data["key"]:
            raise ActionParseError("key requires non-empty string key")
        return BenchmarkAction(type="key", key=data["key"])
    if action_type in ("done", "noop"):
        _require_json_fields(data, {"type"})
        return BenchmarkAction(type=action_type)
    raise ActionParseError(f"Unsupported JSON action type: {action_type!r}")


def _require_json_fields(data: dict, required: set[str]) -> None:
    """Reject missing or unrelated fields in one JSON action object."""
    fields = set(data) - {"reasoning"}
    missing = required - fields
    extra = fields - required
    if missing:
        raise ActionParseError(
            f"JSON action requires {', '.join(sorted(missing))}"
        )
    if extra:
        raise ActionParseError(
            f"JSON action has unsupported fields: {', '.join(sorted(extra))}"
        )

# ---------------------------------------------------------------------------
# Constrained decoding regex -- ported from standalone trainer
# ---------------------------------------------------------------------------
# Matches the ``Thought: <reasoning>\nAction: <action>`` format.
# All repetitions use unbounded quantifiers (+, *) instead of bounded ({1,N})
# to avoid DFA state explosion in Outlines.
_ACTION_RE = (
    r"CLICK\(x=0\.\d+,\s*y=0\.\d+\)"
    r'|TYPE\(text="[^"]*"\)'
    r"|WAIT\(\)"
    r"|DONE\(\)"
)
ACTION_REGEX = r"Thought: [^\n]+\nAction: (" + _ACTION_RE + r")"


# ---------------------------------------------------------------------------
# JSON schema for future Outlines JSON-mode constrained decoding
# ---------------------------------------------------------------------------
# When the model is SFT'd on JSON format (not DSL), switch constrained
# decoding to: outlines.json(model, _AgentOutput) instead of regex.
# This is NOT the default -- the default uses DSL regex (ACTION_REGEX).
class _AgentOutput(BaseModel):
    """Pydantic schema for Outlines JSON-mode constrained decoding.

    Use with: ``outlines.json(model, _AgentOutput)`` once the model has
    been SFT'd on JSON action format. Currently unused -- default is DSL
    regex via ACTION_REGEX.
    """

    reasoning: str
    type: str  # click, type, key, scroll, wait, done
    x: Optional[float] = None
    y: Optional[float] = None
    text: Optional[str] = None
    key: Optional[str] = None


def _build_outlines_generator(model: Any, processor: Any) -> Any | None:
    """Build an Outlines Generator for constrained generation.

    Outlines v1.2 uses its own Generator API. The Generator wraps the model
    and handles tokenization, generation, and decoding internally.

    Args:
        model: The HuggingFace model (may be a PEFT model).
        processor: The HuggingFace processor/tokenizer.

    Returns:
        An Outlines Generator, or None if creation fails.
    """
    try:
        import outlines

        wrapped_model = outlines.from_transformers(model, processor)
        constraint = outlines.regex(ACTION_REGEX)
        generator = outlines.Generator(wrapped_model, constraint)
        logger.info(
            "Outlines constrained decoding enabled for TRL rollout "
            "(model=%s, regex compiled successfully)",
            type(wrapped_model).__name__,
        )
        return generator
    except ImportError:
        logger.error(
            "constrained_decoding=True but 'outlines' is not installed. "
            "Install with: pip install outlines>=0.1.0"
        )
        return None
    except Exception as exc:
        logger.error(
            "Outlines Generator creation failed: %s. "
            "Falling back to unconstrained generation.",
            exc,
        )
        return None


def parse_action_json(text: str) -> BenchmarkAction:
    """Parse a VLM output string into a BenchmarkAction.

    Accepts BOTH formats:
    - JSON: ``{"type": "click", "x": 0.5, "y": 0.3}``
    - DSL:  ``Thought: ...\nAction: CLICK(x=0.50, y=0.30)``

    The DSL fallback is critical for backward compatibility: existing trained
    checkpoints produce DSL format, and constrained decoding constrains to DSL.

    Args:
        text: Raw VLM output text.

    Returns:
        BenchmarkAction parsed from the text.
    """
    data = parse_single_json_object(text)
    if data is not None:
        return _parse_json_action(data)

    # --- DSL fallback (Thought/Action format from standalone trainer) ---
    # This handles output from constrained decoding and existing checkpoints.
    # Extract fractional coordinates directly from DSL rather than using
    # parse_vlm_output_to_action (which converts to pixels). The TRL path
    # needs fractional coords for pixel_action(x_frac=, y_frac=).
    command, arguments = parse_single_dsl_action(
        text,
        allowed_commands={"CLICK", "TYPE", "WAIT", "DONE"},
    )
    if command == "CLICK":
        require_exact_fields(arguments, {"x", "y"}, command)
        return BenchmarkAction(
            type="click",
            x=_required_fraction(arguments, "x", command),
            y=_required_fraction(arguments, "y", command),
        )
    if command == "TYPE":
        require_exact_fields(arguments, {"text"}, command)
        value = arguments["text"]
        value = value.replace("\\\\", "\\").replace('\\"', '"').replace("\\'", "'")
        return BenchmarkAction(type="type", text=value)
    if command == "WAIT":
        require_exact_fields(arguments, set(), command)
        return BenchmarkAction(type="wait")
    if command == "DONE":
        require_exact_fields(arguments, set(), command)
        return BenchmarkAction(type="done")

    raise ActionParseError(f"Unsupported action command: {command!r}")


def _run_episode(
    env: RLEnvironment,
    generate_fn: Callable[[bytes, str], tuple[str, list[int], list[float]]],
    task_instruction: str,
    task_id: str,
    max_steps: int,
    stuck_threshold: int = 3,
) -> tuple[list[int], list[int], list[float], float]:
    """Run a single episode and return token-level data + reward.

    Args:
        env: The RL environment (already has task_config loaded).
        generate_fn: Function(screenshot_bytes, instruction) -> (text, token_ids, logprobs).
        task_instruction: Natural language task description.
        task_id: Task ID for reset.
        max_steps: Maximum steps per episode.
        stuck_threshold: Number of consecutive identical screenshots before
            breaking the episode early. Set to 0 to disable stuck detection.

    Returns:
        Tuple of (prompt_ids, completion_ids, logprobs, reward).
    """
    obs = env.reset(config=ResetConfig(task_id=task_id))

    all_completion_ids: list[int] = []
    all_logprobs: list[float] = []
    prompt_ids: list[int] = []
    recent_hashes: list[str] = []

    for step in range(max_steps):
        screenshot = obs.screenshot or b""

        # --- Stuck detection (P1) ---
        # Track screenshot hashes to detect when the agent is looping on an
        # identical screen (no learning signal). Ported from standalone
        # trainer's WAADirect.is_stuck().
        if stuck_threshold > 0:
            screenshot_hash = hashlib.md5(screenshot).hexdigest()
            recent_hashes.append(screenshot_hash)
            if len(recent_hashes) > stuck_threshold:
                recent_hashes.pop(0)
            if (
                len(recent_hashes) == stuck_threshold
                and len(set(recent_hashes)) == 1
            ):
                logger.warning(
                    "Stuck detected: %d identical screenshots in a row. "
                    "Breaking episode early.",
                    stuck_threshold,
                )
                break

        # Generate action from VLM
        action_text, token_ids, logprobs = generate_fn(screenshot, task_instruction)

        # Track token-level data
        if step == 0:
            # First generation includes the prompt encoding
            # In practice, the generate_fn should separate prompt from completion
            pass
        all_completion_ids.extend(token_ids)
        all_logprobs.extend(logprobs)

        # Parse and execute action
        action = parse_action_json(action_text)
        if action.type == "done":
            break

        # Handle fractional coordinates
        if action.type in ("click", "double_click", "right_click"):
            if action.x is None or action.y is None:
                raise ActionParseError(f"{action.type} requires x and y")
            if 0 <= action.x <= 1 and 0 <= action.y <= 1:
                step_result = env.pixel_action(
                    x_frac=action.x, y_frac=action.y,
                    action_type=action.type, text=action.text, key=action.key,
                )
            else:
                step_result = env.pixel_action(
                    x=int(action.x), y=int(action.y),
                    action_type=action.type, text=action.text, key=action.key,
                )
        else:
            step_result = env.step(action)

        obs = step_result.observation
        if step_result.done:
            break

    # Evaluate -- dense rewards if milestones, binary otherwise
    reward = env.evaluate_dense()

    return prompt_ids, all_completion_ids, all_logprobs, reward


def make_waa_rollout_func(
    adapter: Any,
    task_configs: list | None = None,
    max_steps: int = 15,
    constrained_decoding: bool = False,
    max_new_tokens: int = 256,
    temperature: float = 1.0,
    screenshot_retries: int = 3,
    screenshot_retry_delay: float = 1.0,
    stuck_threshold: int = 3,
    on_before_collect: Optional[Callable] = None,
    on_rollout_complete: Optional[Callable] = None,
    cache_vision_fn: Optional[Callable] = None,
) -> Callable:
    """Create a TRL-compatible rollout_func for WAA environments.

    The returned function has signature:
        rollout_func(prompts: list[str], trainer: GRPOTrainer) -> dict[str, list]

    Args:
        adapter: A BenchmarkAdapter (WAALiveAdapter or WAAMockAdapter).
        task_configs: List of TaskConfig objects. Each prompt in the training
            dataset should have a matching task_config by name or index.
        max_steps: Maximum steps per episode.
        constrained_decoding: If True, use Outlines to constrain generation
            to the ``Thought: ...\nAction: CLICK/TYPE/WAIT/DONE`` format.
            Requires ``pip install outlines>=0.1.0``.
        max_new_tokens: Maximum tokens per generation step.
        temperature: Sampling temperature for generation.
        screenshot_retries: Number of retry attempts when a screenshot is
            corrupt (cannot be opened by PIL). Ported from the standalone
            trainer's screenshot retry logic.
        screenshot_retry_delay: Seconds to sleep between screenshot retry
            attempts.
        stuck_threshold: Number of consecutive identical screenshots before
            breaking an episode early. Set to 0 to disable stuck detection.
            Ported from the standalone trainer's WAADirect.is_stuck().
        on_before_collect: ``(task_id, env) -> None`` callback fired before
            each episode begins. Useful for health checks, logging, or
            pre-rollout setup. A raised exception aborts collection so stale
            state cannot be evaluated as the requested task.
        on_rollout_complete: ``(rollout, index) -> None`` callback fired
            after each episode completes. ``rollout`` is a dict with keys
            ``prompt``, ``task_id``, ``reward``, ``gen_idx``. A raised
            exception is caught and logged as a warning.

    Returns:
        A callable suitable for GRPOTrainer(rollout_func=...).
    """
    positive_integer_options = {
        "max_steps": max_steps,
        "max_new_tokens": max_new_tokens,
        "screenshot_retries": screenshot_retries,
    }
    for name, value in positive_integer_options.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(stuck_threshold, bool)
        or not isinstance(stuck_threshold, int)
        or stuck_threshold < 0
    ):
        raise ValueError("stuck_threshold must be a non-negative integer")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or temperature <= 0
    ):
        raise ValueError("temperature must be finite and positive")

    # Index task configs by exact prompt identity for lookup.
    config_map: dict[str, Any] = {}
    if task_configs:
        for tc in task_configs:
            for key in (tc.name, tc.id):
                if not isinstance(key, str) or not key:
                    raise ValueError("Task config names and IDs must be non-empty strings")
                existing = config_map.get(key)
                if existing is not None and existing is not tc:
                    raise ValueError(f"Duplicate task prompt identity: {key!r}")
                config_map[key] = tc

    # Outlines generator is created lazily on first rollout call
    # (needs the trainer's model and processor which aren't available yet).
    _outlines_state: dict[str, Any] = {"generator": None, "attempted": False}
    _prompt_logged: list[bool] = [False]  # log the prompt once for diagnostics
    _output_logged: list[bool] = [False]  # log first generation output
    _template_patched: list[bool] = [False]  # patch chat template once

    def rollout_func(prompts: list[str], trainer: Any) -> dict[str, list]:
        """TRL GRPOTrainer rollout function.

        Args:
            prompts: Task instructions from the training dataset.
            trainer: Active GRPOTrainer instance (provides model + processor).

        Returns:
            Dict with prompt_ids, completion_ids, logprobs, env_reward.
        """
        processor = trainer.processing_class
        model = trainer.model

        # --- Disable Qwen3.5 thinking mode at the template level ---
        # Qwen3.5's chat template inserts <think> which produces opaque
        # reasoning tokens instead of DSL actions. Stripping from the
        # rendered text is insufficient because TRL or the processor may
        # re-apply the template. The fix: patch the template itself so
        # <think> is never inserted, regardless of who calls it.
        if not _template_patched[0]:
            _template_patched[0] = True
            for obj in [processor, getattr(processor, "tokenizer", None)]:
                if obj is None:
                    continue
                tpl = getattr(obj, "chat_template", None)
                if tpl and "<think>" in tpl:
                    patched = tpl.replace("<think>", "").replace("</think>", "")
                    obj.chat_template = patched
                    logger.info(
                        "Patched chat_template on %s to remove <think>/<think> "
                        "tags (disables Qwen3.5 thinking mode)",
                        type(obj).__name__,
                    )
        device = next(model.parameters()).device

        num_generations = getattr(trainer.args, "num_generations", 8)
        if (
            isinstance(num_generations, bool)
            or not isinstance(num_generations, int)
            or num_generations <= 0
        ):
            raise RolloutInfrastructureError(
                "trainer.args.num_generations must be a positive integer"
            )

        # --- Pre-rollout health check (P0) ---
        _mod = getattr(type(adapter), "__module__", "") or ""
        _name = type(adapter).__name__.lower()
        _is_mock = "mock" in _name or "mock" in _mod
        if not _is_mock:
            try:
                health_obs = adapter.observe()
                screenshot = getattr(health_obs, "screenshot", None)
                if not isinstance(screenshot, bytes) or len(screenshot) < 100:
                    size = len(screenshot) if isinstance(screenshot, bytes) else None
                    raise RolloutInfrastructureError(
                        "WAA server health check returned an invalid screenshot "
                        f"({size} bytes)"
                    )
            except Exception as exc:
                if isinstance(exc, RolloutInfrastructureError):
                    raise
                raise RolloutInfrastructureError(
                    f"WAA server health check failed: {exc}"
                ) from exc

        # Lazy-init Outlines generator on first call
        if constrained_decoding and not _outlines_state["attempted"]:
            _outlines_state["attempted"] = True
            _outlines_state["generator"] = _build_outlines_generator(
                model, processor,
            )

        outlines_gen = _outlines_state["generator"] if constrained_decoding else None

        all_prompt_ids = []
        all_completion_ids = []
        all_logprobs = []
        all_rewards = []

        def generate_fn(screenshot_bytes: bytes, instruction: str):
            """Generate action tokens from screenshot + instruction."""
            from PIL import Image

            img = None
            for attempt in range(screenshot_retries):
                try:
                    img = Image.open(io.BytesIO(screenshot_bytes))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                        img.format = "PNG"
                    break
                except Exception as exc:
                    if attempt < screenshot_retries - 1:
                        logger.warning(
                            "Corrupt screenshot (attempt %d/%d): %s",
                            attempt + 1,
                            screenshot_retries,
                            exc,
                        )
                        time.sleep(screenshot_retry_delay)
                    else:
                        raise RolloutInfrastructureError(
                            "Screenshot remained unreadable after "
                            f"{screenshot_retries} attempts"
                        ) from exc

            # Use the SAME message construction as the standalone trainer.
            # This includes the "Goal:" prefix, format guidance, and the
            # correct {"type": "image"} tag format that Qwen processors expect.
            # Without this, the model sees just the raw instruction text and
            # produces degenerate output (e.g., "# # # # # # #").
            from openadapt_evals.training.standalone.prompt import build_agent_messages

            messages = build_agent_messages(instruction, include_image=True)

            import torch

            # Disable thinking mode: Qwen3.5's chat template inserts
            # <think> which activates internal reasoning tokens (the
            # "# # # # #" garbage). We need DSL output, not thinking.
            # Try enable_thinking=False first; if not supported, strip
            # <think> from the rendered text.
            chat_kwargs = dict(
                tokenize=False, add_generation_prompt=True,
            )
            try:
                text_input = processor.apply_chat_template(
                    messages, enable_thinking=False, **chat_kwargs,
                )
            except TypeError:
                # Older processor doesn't support enable_thinking kwarg
                text_input = processor.apply_chat_template(
                    messages, **chat_kwargs,
                )

            # Belt-and-suspenders: strip thinking tags if they slipped through
            if "<think>" in text_input or "</think>" in text_input:
                logger.info("Stripping <think>/<think> tags from rendered prompt")
                text_input = (
                    text_input
                    .replace("<think>\n", "")
                    .replace("<think>", "")
                    .replace("</think>\n", "")
                    .replace("</think>", "")
                )

            # Comprehensive prompt diagnostics on first call.
            # This logs everything needed to debug prompt construction:
            # raw messages, rendered text, image presence, generation config.
            if not _prompt_logged[0]:
                _prompt_logged[0] = True
                # 1. Raw messages (before chat template)
                for i, msg in enumerate(messages):
                    role = msg.get("role", "?")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        types = [c.get("type", "?") for c in content]
                        text_parts = [c.get("text", "")[:200] for c in content
                                      if c.get("type") == "text"]
                        logger.info(
                            "TRL prompt msg[%d] role=%s content_types=%s "
                            "text=%.200s",
                            i, role, types, " ".join(text_parts),
                        )
                    else:
                        logger.info(
                            "TRL prompt msg[%d] role=%s content=%.500s",
                            i, role, content,
                        )
                # 2. Rendered text (after chat template) — full prompt
                logger.info(
                    "TRL prompt text_input (%d chars): %s",
                    len(text_input), text_input[:2000],
                )
                # 3. Image info
                logger.info(
                    "TRL prompt image: mode=%s size=%s format=%s",
                    getattr(img, "mode", "?"),
                    getattr(img, "size", "?"),
                    getattr(img, "format", "?"),
                )
                # 4. Generation config
                logger.info(
                    "TRL generation config: max_new_tokens=%d "
                    "temperature=%s do_sample=True "
                    "constrained=%s model_type=%s device=%s",
                    max_new_tokens, temperature,
                    outlines_gen is not None,
                    type(model).__name__, device,
                )

            # --- Constrained decoding path (Outlines) ---
            if outlines_gen is not None:
                import outlines

                model_input = [text_input, outlines.Image(img)]
                decoded = outlines_gen(
                    model_input,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                )
                inner_tok = getattr(processor, "tokenizer", processor)
                completion_ids = inner_tok.encode(
                    decoded, add_special_tokens=False,
                )
                logprobs: list[float] = []

                # Truncation warning — detect when output was cut off
                if len(completion_ids) >= max_new_tokens - 1:
                    logger.warning(
                        "Generation hit max_new_tokens=%d. Output may be "
                        "truncated. If actions are unparseable, increase "
                        "max_new_tokens or enable constrained_decoding.",
                        max_new_tokens,
                    )

                return decoded, completion_ids, logprobs

            # --- Standard HF generate path (unconstrained) ---
            inputs = processor(
                text=[text_input], images=[img],
                return_tensors="pt", padding=True,
            ).to(device)

            # Cache vision inputs so the VLMModelWrapper can inject
            # pixel_values during TRL's training forward pass.
            # Cache vision inputs so the patched forward() can inject
            # pixel_values during TRL's training step and generate() calls.
            if cache_vision_fn is not None:
                cache_vision_fn(dict(inputs.items()) if hasattr(inputs, "items") else inputs)
            elif hasattr(model, "cache_vision_inputs"):
                model.cache_vision_inputs(inputs)

            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=True,
                    temperature=temperature,
                    return_dict_in_generate=True,
                    output_scores=True,
                )

            prompt_len = inputs["input_ids"].shape[1]
            completion_ids = outputs.sequences[0][prompt_len:].tolist()

            logprobs = []
            if hasattr(outputs, "scores") and outputs.scores:
                for i, score in enumerate(outputs.scores):
                    probs = torch.nn.functional.log_softmax(score[0], dim=-1)
                    if i < len(completion_ids):
                        logprobs.append(probs[completion_ids[i]].item())

            text = processor.decode(completion_ids, skip_special_tokens=True)

            # Log first generation output for debugging
            if not _output_logged[0]:
                _output_logged[0] = True
                logger.info(
                    "TRL first generation output (%d tokens): %.500s",
                    len(completion_ids), text,
                )
                # Also log input shape for vision tensor debugging
                logger.info(
                    "TRL input shapes: input_ids=%s attention_mask=%s "
                    "pixel_values=%s image_grid_thw=%s",
                    inputs.get("input_ids", torch.tensor([])).shape,
                    inputs.get("attention_mask", torch.tensor([])).shape,
                    inputs.get("pixel_values", torch.tensor([])).shape
                    if "pixel_values" in inputs else "MISSING",
                    inputs.get("image_grid_thw", torch.tensor([])).shape
                    if "image_grid_thw" in inputs else "MISSING",
                )

            # Truncation warning — detect when output was cut off
            if len(completion_ids) >= max_new_tokens - 1:
                logger.warning(
                    "Generation hit max_new_tokens=%d. Output may be "
                    "truncated. If actions are unparseable, increase "
                    "max_new_tokens or enable constrained_decoding.",
                    max_new_tokens,
                )

            return text, completion_ids, logprobs

        for prompt in prompts:
            if not isinstance(prompt, str) or not prompt:
                raise RolloutInfrastructureError(
                    "Every rollout prompt must be a non-empty task identity"
                )
            tc = config_map.get(prompt)
            if tc is None:
                raise RolloutInfrastructureError(
                    f"Rollout prompt {prompt!r} has no exact TaskConfig match"
                )

            for gen_idx in range(num_generations):
                env = RLEnvironment(adapter, task_config=tc)

                task_id = tc.id

                # --- on_before_collect callback ---
                if on_before_collect is not None:
                    try:
                        on_before_collect(task_id, env)
                    except Exception as exc:
                        raise RolloutInfrastructureError(
                            "on_before_collect failed for "
                            f"task_id={task_id} gen={gen_idx}: {exc}"
                        ) from exc

                try:
                    p_ids, c_ids, lps, reward = _run_episode(
                        env, generate_fn, prompt, task_id, max_steps,
                    )
                except Exception as exc:
                    logger.error(
                        "Rollout failed for prompt=%s gen=%d: %s",
                        prompt[:50], gen_idx, exc,
                    )
                    raise

                # --- on_rollout_complete callback ---
                if on_rollout_complete is not None:
                    try:
                        on_rollout_complete(
                            {
                                "prompt": prompt,
                                "task_id": task_id,
                                "reward": reward,
                                "gen_idx": gen_idx,
                            },
                            gen_idx,
                        )
                    except Exception as exc:
                        logger.warning(
                            "on_rollout_complete callback raised for "
                            "task_id=%s gen=%d: %s",
                            task_id, gen_idx, exc,
                        )

                all_prompt_ids.append(p_ids)
                all_completion_ids.append(c_ids)
                all_logprobs.append(lps)
                all_rewards.append(reward)

        return {
            "prompt_ids": all_prompt_ids,
            "completion_ids": all_completion_ids,
            "logprobs": all_logprobs,
            "env_reward": all_rewards,
        }

    return rollout_func
