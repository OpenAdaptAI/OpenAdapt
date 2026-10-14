"""VAGEN/verl-agent environment adapter for openadapt-evals.

Wraps RLEnvironment to implement VAGEN's GymImageEnv protocol, enabling
desktop GUI automation training with verl-agent's multi-turn VLM RL pipeline
(GiGPO, GRPO, PPO, etc.).

The adapter translates between:
    - openadapt-evals: BenchmarkObservation (PNG bytes + a11y tree)
    - VAGEN/verl: {"obs_str": "...", "multi_modal_input": {"<image>": [PIL.Image]}}

Dependencies:
    - openadapt-evals (always required)
    - vagen (optional; a vendored copy of the GymImageEnv ABC is used
      as fallback when the full vagen package is not installed)

Usage with VAGEN training:
    1. Register the env on the GPU VM:
        from openadapt_evals.adapters.verl_env import register_in_vagen
        register_in_vagen("/path/to/vagen/configs/env_registry.yaml")

    2. Or manually add to vagen/configs/env_registry.yaml:
        env_registry:
            WAADesktop: openadapt_evals.adapters.verl_env.WAADesktopEnv

    3. Training config (envs section):
        envs:
            - name: WAADesktop
              n_envs: 8
              data_source: waa
              seed: [1, 100, 1]
              max_turns: 15
              response_length_per_turn: 512
              config:
                server_url: "http://localhost:5000"
                evaluate_url: "http://localhost:5001"
                task_id: "<WAA_UUID>"

Usage standalone (without VAGEN):
    from openadapt_evals.adapters.verl_env import WAADesktopEnv

    env = WAADesktopEnv({
        "server_url": "http://localhost:5000",       # Windows VM Flask API
        "evaluate_url": "http://localhost:5001",      # evaluate_server.py
        "task_id": "...",
    })
    obs, info = await env.reset(seed=42)
    obs, reward, done, info = await env.step("CLICK(x=0.50, y=0.30)")
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import re
from pathlib import Path
from typing import Any

from openadapt_evals.action_envelope import (
    parse_single_dsl_action,
    require_exact_fields,
)
from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.adapters.rl_env import RLEnvironment, _fraction_to_pixel
from openadapt_evals.errors import ActionParseError

logger = logging.getLogger(__name__)

# Try importing PIL (required for image conversion)
try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[misc, assignment]

# Import VAGEN's GymImageEnv base class.
# Prefer the real vagen package; fall back to our vendored copy.
try:
    from vagen.envs.gym_image_env import GymImageEnv as _GymImageEnvBase
except ImportError:
    from openadapt_evals.adapters._vendored.gym_image_env import (
        GymImageEnv as _GymImageEnvBase,
    )

# --- Action parsing (matches openadapt-ml trainer DSL) ---

_ACTION_PATTERN = re.compile(
    r"(CLICK|TYPE|SCROLL|KEY|WAIT|DONE|DRAG)\((.*?)\)", re.DOTALL
)


def _required_fraction(kwargs: dict[str, str], key: str, command: str) -> float:
    """Read one required finite normalized coordinate."""
    if key not in kwargs:
        raise ActionParseError(f"{command} requires {key}")
    try:
        value = float(kwargs[key])
    except (TypeError, ValueError) as exc:
        raise ActionParseError(f"{command} has invalid {key}: {kwargs[key]!r}") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ActionParseError(f"{command} {key} must be between 0 and 1")
    return value


def _parse_action_str(action_str: str) -> BenchmarkAction:
    """Parse a VLM action string into a BenchmarkAction.

    Supports the same DSL as the openadapt-ml GRPO trainer:
        CLICK(x=0.50, y=0.30)
        TYPE(text="hello world")
        KEY(key="enter")
        SCROLL(x=0.50, y=0.50, direction="down")
        DRAG(x=0.20, y=0.30, end_x=0.80, end_y=0.70)
        WAIT()
        DONE()
    """
    cmd, kwargs = parse_single_dsl_action(
        action_str,
        allowed_commands={"CLICK", "TYPE", "SCROLL", "KEY", "WAIT", "DONE", "DRAG"},
    )

    if cmd == "CLICK":
        require_exact_fields(kwargs, {"x", "y"}, cmd)
        return BenchmarkAction(
            type="click",
            x=_required_fraction(kwargs, "x", cmd),
            y=_required_fraction(kwargs, "y", cmd),
        )
    elif cmd == "TYPE":
        require_exact_fields(kwargs, {"text"}, cmd)
        return BenchmarkAction(type="type", text=kwargs["text"])
    elif cmd == "KEY":
        require_exact_fields(kwargs, {"key"}, cmd)
        if not kwargs.get("key"):
            raise ActionParseError("KEY requires key")
        return BenchmarkAction(type="key", key=kwargs["key"])
    elif cmd == "SCROLL":
        require_exact_fields(kwargs, {"x", "y", "direction"}, cmd)
        direction = kwargs.get("direction")
        if direction not in ("up", "down"):
            raise ActionParseError("SCROLL requires direction=up or direction=down")
        return BenchmarkAction(
            type="scroll",
            x=_required_fraction(kwargs, "x", cmd),
            y=_required_fraction(kwargs, "y", cmd),
            scroll_direction=direction,
        )
    elif cmd == "WAIT":
        require_exact_fields(kwargs, set(), cmd)
        return BenchmarkAction(type="wait")
    elif cmd == "DONE":
        require_exact_fields(kwargs, set(), cmd)
        return BenchmarkAction(type="done")
    elif cmd == "DRAG":
        require_exact_fields(kwargs, {"x", "y", "end_x", "end_y"}, cmd)
        return BenchmarkAction(
            type="drag",
            x=_required_fraction(kwargs, "x", cmd),
            y=_required_fraction(kwargs, "y", cmd),
            end_x=_required_fraction(kwargs, "end_x", cmd),
            end_y=_required_fraction(kwargs, "end_y", cmd),
        )
    else:
        raise ActionParseError(f"Unsupported action command: {cmd!r}")


def _obs_to_pil(obs: BenchmarkObservation) -> Image.Image | None:
    """Convert BenchmarkObservation screenshot bytes to PIL Image."""
    if Image is None:
        raise ImportError("Pillow is required: pip install Pillow")
    if obs.screenshot:
        return Image.open(io.BytesIO(obs.screenshot))
    return None


def _build_obs_dict(
    obs: BenchmarkObservation,
    prefix: str = "Current desktop state:",
) -> dict[str, Any]:
    """Build a VAGEN-compatible observation dict from BenchmarkObservation."""
    img = _obs_to_pil(obs)
    result: dict[str, Any] = {}
    if img is not None:
        result["obs_str"] = f"{prefix}\n<image>"
        result["multi_modal_input"] = {"<image>": [img]}
    else:
        # Text-only fallback (a11y tree)
        a11y_text = str(obs.accessibility_tree) if obs.accessibility_tree else "No observation available."
        result["obs_str"] = f"{prefix}\n{a11y_text}"
    return result


# --- Anchor state computation for GiGPO ---


def compute_anchor_state(obs: BenchmarkObservation) -> str:
    """Compute a state key for GiGPO anchor grouping.

    GiGPO groups identical intermediate states across rollouts to assign
    per-action advantages. This function produces a hash that identifies
    "the same state" so the training framework can efficiently find groups.

    Strategy:
        1. Accessibility tree hash (primary): Structural identity of UI state.
           Two screenshots showing the same dialog with the same elements
           will hash identically even if pixel rendering differs slightly.
        2. Screenshot MD5 (fallback): Exact pixel match when a11y tree is
           unavailable.

    The returned key is included in the ``info`` dict from ``WAADesktopEnv.step()``
    as ``state_key``. VAGEN/verl can use this for O(1) grouping instead of
    recomputing perceptual hashes across all rollout steps.

    Args:
        obs: A BenchmarkObservation with screenshot and/or accessibility_tree.

    Returns:
        Hex digest string suitable as a dict key.
    """
    # Primary: hash the a11y tree (structural state identity)
    if obs.accessibility_tree:
        tree_str = str(obs.accessibility_tree)
        return hashlib.sha256(tree_str.encode()).hexdigest()[:16]

    # Fallback: hash raw screenshot bytes (exact pixel match)
    if obs.screenshot:
        return hashlib.md5(obs.screenshot).hexdigest()[:16]

    return "empty"


# --- System prompt (matches openadapt-ml trainer) ---

SYSTEM_PROMPT = (
    "You are a desktop automation agent. You can see the screen and interact "
    "using these actions:\n"
    "  CLICK(x=<frac>, y=<frac>) - click at normalized coordinates (0.0-1.0)\n"
    "  TYPE(text=\"<text>\") - type text\n"
    "  KEY(key=\"<name>\") - press a key (enter, tab, escape, ctrl+a, etc.)\n"
    "  SCROLL(x=<frac>, y=<frac>, direction=\"up\"|\"down\") - scroll\n"
    "  DRAG(x=<frac>, y=<frac>, end_x=<frac>, end_y=<frac>) - drag\n"
    "  WAIT() - wait for the screen to update\n"
    "  DONE() - task is complete\n"
    "\n"
    "Respond with exactly one action per turn."
)


# --- Main environment class ---


class WAADesktopEnv(_GymImageEnvBase):
    """VAGEN-compatible environment for WAA desktop automation.

    Implements the GymImageEnv protocol (async reset/step/close/system_prompt)
    so it can be used directly with VAGEN's agent loop and training pipeline.

    The environment wraps openadapt-evals' RLEnvironment, which in turn wraps
    WAALiveAdapter for remote VM interaction.

    Config keys (passed via env_config dict):
        server_url: WAA Windows VM Flask API URL (default: "http://localhost:5000").
            Provides /screenshot, /execute_windows, /accessibility endpoints.
        evaluate_url: Evaluate server URL (default: None, falls back to server_url).
            Provides /setup, /evaluate, /probe, /task endpoints. Set this when
            the evaluate server runs on a different port than the Windows VM API.
        task_id: WAA task UUID to train on
        max_steps: Max steps per episode (default: 15)
        evaluate_at_done: Whether to call WAA evaluator at episode end (default: True)
        action_type: How coordinates are specified: "fractional" (0-1) or "pixel"
    """

    def __init__(self, env_config: dict[str, Any]) -> None:
        super().__init__(env_config)
        self.config = env_config
        self._server_url = env_config.get("server_url", "http://localhost:5000")
        self._evaluate_url = env_config.get("evaluate_url")
        self._task_id = env_config.get("task_id")
        self._max_steps = env_config.get("max_steps", 15)
        self._evaluate_at_done = env_config.get("evaluate_at_done", True)
        self._use_fractional = env_config.get("action_type", "fractional") == "fractional"

        # Lazy initialization — adapter created on first reset()
        self._rl_env: RLEnvironment | None = None
        self._step_count = 0

    def _ensure_env(self) -> RLEnvironment:
        """Create the RLEnvironment on first use (lazy init)."""
        if self._rl_env is not None:
            return self._rl_env

        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(
            WAALiveConfig(
                server_url=self._server_url,
                evaluate_url=self._evaluate_url,
            )
        )
        self._rl_env = RLEnvironment(adapter, default_task_id=self._task_id)
        return self._rl_env

    async def health_check(self) -> dict[str, Any]:
        """Check environment health status.

        Returns a dict with:
            status: "ready" | "busy" | "needs_recovery" | "not_initialized"
            server_url: The WAA server URL
            step_count: Current step count in episode

        Use this from a pool controller to decide whether to send work
        to this environment or retire/restart it.
        """
        if self._rl_env is None:
            return {"status": "not_initialized", "server_url": self._server_url}

        adapter = self._rl_env.adapter
        # Check if the WAA server is reachable
        try:
            check_fn = getattr(adapter, "check_connection", None)
            reachable = await asyncio.to_thread(check_fn) if check_fn else True
        except Exception:
            reachable = False

        if not reachable:
            return {
                "status": "needs_recovery",
                "server_url": self._server_url,
                "step_count": self._step_count,
            }

        status = "busy" if self._step_count > 0 and not self._rl_env.done else "ready"
        return {
            "status": status,
            "server_url": self._server_url,
            "step_count": self._step_count,
        }

    async def close(self) -> None:
        """Release resources."""
        if self._rl_env is not None:
            self._rl_env.adapter.close()
            self._rl_env = None

    async def system_prompt(self) -> dict[str, Any]:
        """Return the system-level prompt for the VLM agent."""
        return {"obs_str": SYSTEM_PROMPT}

    async def reset(self, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reset environment and return initial observation.

        Args:
            seed: Random seed. Currently unused (WAA tasks are deterministic
                given setup commands), but required by the VAGEN protocol.

        Returns:
            (obs_dict, info_dict) where obs_dict contains "obs_str" and
            optionally "multi_modal_input" with PIL images.
        """
        env = self._ensure_env()
        self._step_count = 0

        # Run reset in a thread to avoid blocking the async event loop
        obs = await asyncio.to_thread(env.reset)

        obs_dict = _build_obs_dict(obs, prefix="Desktop initial state:")
        info: dict[str, Any] = {
            "task_id": self._task_id,
            "screen_size": env.screen_size,
            "state_key": compute_anchor_state(obs),
        }
        return obs_dict, info

    async def step(
        self, action_str: str
    ) -> tuple[dict[str, Any], float, bool, dict[str, Any]]:
        """Execute one action and return (obs, reward, done, info).

        Args:
            action_str: Raw LLM output string. Parsed into a BenchmarkAction
                using the standard action DSL (CLICK, TYPE, KEY, etc.).

        Returns:
            Tuple of (obs_dict, reward, done, info).
            - reward is 0.0 during the episode; evaluator score at the end
            - done is True when DONE() is called, step limit reached, or stuck
        """
        env = self._ensure_env()
        self._step_count += 1

        # Parse the LLM output into a BenchmarkAction
        action = _parse_action_str(action_str)

        # Handle fractional → pixel coordinate conversion.
        # When _use_fractional is True, all coordinates from the parser are
        # fractions (0.0-1.0). We convert unconditionally rather than checking
        # value ranges, since pixel values 0 and 1 would be ambiguous.
        if self._use_fractional and action.type in ("click", "scroll", "drag"):
            w, h = env.screen_size
            if action.x is not None:
                action.x = _fraction_to_pixel(action.x, w, "x")
            if action.y is not None:
                action.y = _fraction_to_pixel(action.y, h, "y")
            if action.end_x is not None:
                action.end_x = _fraction_to_pixel(action.end_x, w, "end_x")
            if action.end_y is not None:
                action.end_y = _fraction_to_pixel(action.end_y, h, "end_y")

        # Execute action in a thread
        rollout_step = await asyncio.to_thread(env.step, action)

        # Check step limit
        done = rollout_step.done or self._step_count >= self._max_steps

        # Compute reward
        reward = 0.0
        info: dict[str, Any] = rollout_step.info
        # Action is valid if it was explicitly parsed (not the fallback for unparseable input)
        info["is_action_valid"] = _ACTION_PATTERN.search(action_str) is not None

        if done and self._evaluate_at_done:
            reward = await asyncio.to_thread(env.evaluate)
            info["success"] = reward >= 1.0
        elif done:
            info["success"] = False

        obs_dict = _build_obs_dict(
            rollout_step.observation,
            prefix=f"After action (step {self._step_count}):",
        )

        # GiGPO anchor: include state key for efficient cross-rollout grouping
        info["state_key"] = compute_anchor_state(rollout_step.observation)

        return obs_dict, reward, done, info


# --- VAGEN registration helpers ---

ENV_REGISTRY_KEY = "WAADesktop"
ENV_CLASS_PATH = "openadapt_evals.adapters.verl_env.WAADesktopEnv"


def register_in_vagen(registry_yaml_path: str | Path | None = None) -> bool:
    """Register WAADesktopEnv in VAGEN's env_registry.yaml.

    If VAGEN is installed and importable, also registers via the Python API.
    Otherwise, edits the YAML file directly.

    Args:
        registry_yaml_path: Path to vagen/configs/env_registry.yaml.
            If None, attempts to find it from the vagen package location.

    Returns:
        True if registration succeeded.
    """
    # Try programmatic registration first (if vagen is installed)
    try:
        from vagen.envs.registry import register_env

        register_env(ENV_REGISTRY_KEY, WAADesktopEnv)
        logger.info("Registered %s in VAGEN env registry (Python API)", ENV_REGISTRY_KEY)
        return True
    except ImportError:
        pass

    # Fall back to YAML file editing
    if registry_yaml_path is None:
        # Try to find it from common locations
        candidates = [
            Path.home() / "verl-agent" / "vagen" / "configs" / "env_registry.yaml",
            Path.home() / "VAGEN" / "vagen" / "configs" / "env_registry.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                registry_yaml_path = candidate
                break

    if registry_yaml_path is not None:
        registry_yaml_path = Path(registry_yaml_path)
        if not registry_yaml_path.exists():
            logger.warning("Registry file not found: %s", registry_yaml_path)
            return False

    if registry_yaml_path is None:
        logger.warning(
            "Cannot find env_registry.yaml. Register manually by adding:\n"
            "  %s: %s\n"
            "to vagen/configs/env_registry.yaml",
            ENV_REGISTRY_KEY,
            ENV_CLASS_PATH,
        )
        return False

    content = registry_yaml_path.read_text()

    if ENV_REGISTRY_KEY in content:
        logger.info("%s already registered in %s", ENV_REGISTRY_KEY, registry_yaml_path)
        return True

    # Add entry to the env_registry section
    entry = f"  {ENV_REGISTRY_KEY}: {ENV_CLASS_PATH}\n"
    if "env_registry:" in content:
        content = content.replace("env_registry:\n", f"env_registry:\n{entry}", 1)
    else:
        content += f"\nenv_registry:\n{entry}"

    registry_yaml_path.write_text(content)
    logger.info("Registered %s in %s", ENV_REGISTRY_KEY, registry_yaml_path)
    return True


def generate_env_spec(
    server_url: str = "http://localhost:5000",
    evaluate_url: str | None = None,
    task_id: str = "REPLACE_WITH_WAA_TASK_UUID",
    n_envs: int = 8,
    max_turns: int = 15,
) -> dict[str, Any]:
    """Generate a VAGEN EnvSpec dict for WAA desktop training.

    Returns a dict suitable for inclusion in a VAGEN training config YAML
    under the ``envs`` key.

    Example:
        spec = generate_env_spec(
            server_url="http://localhost:5000",
            evaluate_url="http://localhost:5001",
            task_id="abc-123",
        )
        # Write to YAML or pass to AgenticDataset
    """
    config: dict[str, Any] = {
        "server_url": server_url,
        "task_id": task_id,
        "max_steps": max_turns,
        "evaluate_at_done": True,
        "action_type": "fractional",
    }
    if evaluate_url:
        config["evaluate_url"] = evaluate_url
    return {
        "name": ENV_REGISTRY_KEY,
        "n_envs": n_envs,
        "data_source": "waa",
        "seed": [1, 100, 1],
        "max_turns": max_turns,
        "response_length_per_turn": 512,
        "config": config,
    }
