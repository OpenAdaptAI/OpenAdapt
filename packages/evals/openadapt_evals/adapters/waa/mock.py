"""Windows Agent Arena (WAA) benchmark adapter.

This module provides integration with the Windows Agent Arena benchmark,
enabling evaluation of GUI agents on 154 Windows tasks across 11 domains.

WAA Repository: https://github.com/microsoft/WindowsAgentArena

Example:
    from openadapt_evals.benchmarks import WAAAdapter, evaluate_agent_on_benchmark

    adapter = WAAAdapter(waa_repo_path="/path/to/WindowsAgentArena")
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)
    print(f"Success rate: {sum(r.success for r in results) / len(results):.1%}")
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)

logger = logging.getLogger(__name__)


# WAA domain mapping (11 domains, 154 tasks)
WAA_DOMAINS = [
    "chrome",
    "clock",
    "file_explorer",
    "libreoffice_calc",
    "libreoffice_writer",
    "microsoft_paint",
    "msedge",
    "notepad",
    "settings",
    "vlc",
    "vs_code",
    "windows_calc",
]


@dataclass
class WAAConfig:
    """Configuration for WAA adapter.

    Attributes:
        waa_repo_path: Path to cloned WindowsAgentArena repository.
        use_azure: Whether to use Azure VMs (enables parallelism).
        observation_type: Type of observation to capture.
        a11y_backend: Accessibility backend ("uia" or "win32").
        screen_width: Screen width in pixels.
        screen_height: Screen height in pixels.
        max_steps: Default maximum steps per task.
        action_delay: Delay between actions in seconds.
    """

    waa_repo_path: str
    use_azure: bool = False
    observation_type: str = "screenshot_a11y_tree"  # "screenshot", "a11y_tree", "som"
    a11y_backend: str = "uia"  # "uia" or "win32"
    screen_width: int = 1920
    screen_height: int = 1200
    max_steps: int = 15
    action_delay: float = 0.5


class WAAAdapter(BenchmarkAdapter):
    """Windows Agent Arena benchmark adapter.

    Integrates with the WAA benchmark to evaluate GUI agents on 154 Windows
    desktop automation tasks spanning 11 application domains.

    The adapter wraps WAA's DesktopEnv and provides:
    - Task loading from WAA's JSON task definitions
    - VM/environment reset to task initial state
    - Action execution via WAA's controller
    - Evaluation using WAA's native evaluators

    Args:
        waa_repo_path: Path to cloned WindowsAgentArena repository.
        use_azure: Use Azure VMs for execution (enables parallelism).
        config: Full WAAConfig (overrides other args if provided).
        **kwargs: Additional config options passed to WAAConfig.

    Raises:
        ValueError: If waa_repo_path doesn't exist.
        ImportError: If WAA dependencies not available.
    """

    def __init__(
        self,
        waa_repo_path: str | Path | None = None,
        use_azure: bool = False,
        config: WAAConfig | None = None,
        **kwargs,
    ):
        if config is not None:
            self.config = config
        else:
            if waa_repo_path is None:
                raise ValueError("waa_repo_path is required")
            self.config = WAAConfig(
                waa_repo_path=str(waa_repo_path),
                use_azure=use_azure,
                **kwargs,
            )

        self.waa_repo = Path(self.config.waa_repo_path)
        if not self.waa_repo.exists():
            raise ValueError(f"WAA repository not found at: {self.waa_repo}")

        # Paths to WAA components
        self._client_path = self.waa_repo / "src" / "win-arena-container" / "client"
        self._tasks_path = self._client_path / "evaluation_examples_windows"

        # Lazy-loaded WAA components
        self._desktop_env = None
        self._task_cache: dict[str, BenchmarkTask] = {}
        self._current_task: BenchmarkTask | None = None
        self._waa_imported = False

    def _ensure_waa_imported(self) -> None:
        """Import WAA modules (lazy loading)."""
        if self._waa_imported:
            return

        # Add WAA client to path
        client_path = str(self._client_path)
        if client_path not in sys.path:
            sys.path.insert(0, client_path)

        try:
            # Import WAA's DesktopEnv
            from desktop_env import DesktopEnv

            self._DesktopEnv = DesktopEnv
            self._waa_imported = True
            logger.info("WAA modules imported successfully")
        except ImportError as e:
            raise ImportError(
                f"Failed to import WAA modules. Ensure WAA is properly installed "
                f"and dependencies are available: {e}"
            ) from e

    @property
    def name(self) -> str:
        """Benchmark name."""
        return "waa"

    @property
    def benchmark_type(self) -> str:
        """Benchmark type (interactive)."""
        return "interactive"

    @property
    def supports_parallel(self) -> bool:
        """Whether parallel execution is supported (requires Azure)."""
        return self.config.use_azure

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        """List available WAA tasks.

        WAA has 154 tasks across 11 domains:
        - browser: Edge/Chrome navigation and settings
        - office: Word, Excel, Outlook
        - coding: VSCode, terminal
        - settings: Windows Settings app
        - file_explorer: File operations
        - notepad: Text editing
        - paint: Drawing operations
        - media: Video/audio playback
        - clock: Alarms, timers
        - edge: Browser-specific
        - vscode: IDE-specific

        Args:
            domain: Optional domain filter.

        Returns:
            List of BenchmarkTask objects.
        """
        tasks = self._load_all_tasks()

        if domain is not None:
            tasks = [t for t in tasks if t.domain == domain]

        return tasks

    def load_task(self, task_id: str) -> BenchmarkTask:
        """Load a specific task by ID.

        Args:
            task_id: Task identifier (e.g., "notepad_1", "browser_5").

        Returns:
            BenchmarkTask object.

        Raises:
            KeyError: If task_id not found.
        """
        if task_id in self._task_cache:
            return self._task_cache[task_id]

        # Try to load from disk
        tasks = self._load_all_tasks()
        task_map = {t.task_id: t for t in tasks}

        if task_id not in task_map:
            raise KeyError(f"Task '{task_id}' not found. Available: {list(task_map.keys())[:10]}...")

        return task_map[task_id]

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Reset environment to task's initial state.

        This initializes the Windows VM/desktop to the state required for
        the task, including opening required applications and setting up
        any pre-conditions.

        Args:
            task: Task to initialize.

        Returns:
            Initial observation (screenshot + accessibility tree).
        """
        self._ensure_waa_imported()
        self._current_task = task

        # Initialize DesktopEnv if needed
        if self._desktop_env is None:
            self._desktop_env = self._create_desktop_env()

        # Load task config and reset environment
        task_config = self._load_waa_task_config(task)
        obs = self._desktop_env.reset(task_config=task_config)

        return self._to_benchmark_observation(obs)

    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute action and return new observation.

        Args:
            action: Action to execute.

        Returns:
            Tuple of (observation, done, info).
        """
        if self._desktop_env is None:
            raise RuntimeError("Call reset() before step()")

        # Convert to WAA action format
        waa_action = self._to_waa_action(action)

        # Execute action
        obs, reward, done, info = self._desktop_env.step(waa_action)

        # Optional delay between actions
        if self.config.action_delay > 0:
            time.sleep(self.config.action_delay)

        return self._to_benchmark_observation(obs), done, info

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Run WAA's native evaluation on current state.

        WAA evaluators check the actual OS state (files, settings, app state)
        to determine if the task was completed successfully.

        Args:
            task: Task to evaluate.

        Returns:
            BenchmarkResult with success/score.
        """
        if self._desktop_env is None:
            raise RuntimeError("Call reset() and step() before evaluate()")

        # Run WAA's evaluator
        try:
            result = self._desktop_env.evaluate()
        except Exception as e:
            logger.error(f"Evaluation failed for task {task.task_id}: {e}")
            error_type = getattr(e, "error_type", "evaluation")
            if error_type not in ("infrastructure", "evaluation"):
                error_type = "evaluation"
            return BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                reason=str(e),
                error=str(e),
                error_type=error_type,
            )

        return self._result_from_evaluate_response(task, result)

    def _result_from_evaluate_response(
        self, task: BenchmarkTask, result: Any
    ) -> BenchmarkResult:
        """Validate one native WAA evaluator result without inventing a score."""

        def invalid(reason: str) -> BenchmarkResult:
            return BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                reason=f"Malformed evaluation response: {reason}",
                error_type="evaluation",
            )

        if isinstance(result, Real) and not isinstance(result, bool):
            score = float(result)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                return invalid("scalar score must be a finite number between 0 and 1")
            return BenchmarkResult(
                task_id=task.task_id,
                success=score >= 1.0,
                score=score,
            )

        if not isinstance(result, dict):
            return invalid("body must be an object")

        success = result.get("success")
        if not isinstance(success, bool):
            return invalid("success must be a boolean")

        score = result.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
        ):
            return invalid("score must be a finite number between 0 and 1")
        score = float(score)

        scored = result.get("scored")
        if scored is not None and not isinstance(scored, bool):
            return invalid("scored must be a boolean when present")

        error_type = result.get("error_type")
        if error_type not in (None, "infrastructure", "evaluation"):
            return invalid("error_type must be infrastructure, evaluation, or null")
        if scored is True and error_type is not None:
            return invalid("a scored result cannot carry an error_type")
        if success != (score >= 1.0):
            return invalid("success contradicts score")

        if scored is False and error_type is None:
            error_type = "evaluation"
        if error_type is not None and (success or score != 0.0):
            return invalid("an unscored result must have success=false and score=0")

        return BenchmarkResult(
            task_id=task.task_id,
            success=success,
            score=score,
            reason=result.get("reason"),
            error_type=error_type,
        )

    def close(self) -> None:
        """Clean up resources."""
        if self._desktop_env is not None:
            try:
                self._desktop_env.close()
            except Exception as e:
                logger.warning(f"Error closing DesktopEnv: {e}")
            self._desktop_env = None

    def _create_desktop_env(self):
        """Create WAA DesktopEnv instance."""
        require_a11y = self.config.observation_type in [
            "a11y_tree",
            "screenshot_a11y_tree",
            "som",
        ]

        return self._DesktopEnv(
            screen_size=(self.config.screen_width, self.config.screen_height),
            require_a11y_tree=require_a11y,
            a11y_backend=self.config.a11y_backend,
        )

    def _load_all_tasks(self) -> list[BenchmarkTask]:
        """Load all WAA tasks from the repository."""
        if self._task_cache:
            return list(self._task_cache.values())

        tasks = []

        # Load test_all.json metadata
        meta_path = self._tasks_path / "test_all.json"
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)

            for domain, task_ids in meta.items():
                if domain in WAA_DOMAINS:
                    for task_id in task_ids:
                        task = self._load_task_from_json(domain, task_id)
                        if task:
                            tasks.append(task)
                            self._task_cache[task.task_id] = task
        else:
            # Fallback: scan examples directory
            examples_dir = self._tasks_path / "examples"
            if examples_dir.exists():
                for domain_dir in examples_dir.iterdir():
                    if domain_dir.is_dir() and domain_dir.name in WAA_DOMAINS:
                        for task_file in domain_dir.glob("*.json"):
                            task = self._load_task_from_file(task_file, domain_dir.name)
                            if task:
                                tasks.append(task)
                                self._task_cache[task.task_id] = task

        logger.info(f"Loaded {len(tasks)} WAA tasks")
        return tasks

    def _load_task_from_json(self, domain: str, task_id: str) -> BenchmarkTask | None:
        """Load a task from its JSON file."""
        task_file = self._tasks_path / "examples" / domain / f"{task_id}.json"
        if not task_file.exists():
            logger.warning(f"Task file not found: {task_file}")
            return None

        return self._load_task_from_file(task_file, domain)

    def _load_task_from_file(self, task_file: Path, domain: str) -> BenchmarkTask | None:
        """Load a task from a JSON file."""
        try:
            with open(task_file, encoding="utf-8") as f:
                config = json.load(f)

            task_id = f"{domain}_{task_file.stem}"
            instruction = config.get("instruction", config.get("task", ""))

            return BenchmarkTask(
                task_id=task_id,
                instruction=instruction,
                domain=domain,
                initial_state_ref=config.get("snapshot", None),
                time_limit_steps=config.get("max_steps", self.config.max_steps),
                raw_config=config,
                evaluation_spec=config.get("evaluation", None),
            )
        except Exception as e:
            logger.warning(f"Failed to load task from {task_file}: {e}")
            return None

    def _load_waa_task_config(self, task: BenchmarkTask) -> dict:
        """Convert BenchmarkTask to WAA's task config format."""
        return task.raw_config

    def _to_benchmark_observation(self, waa_obs: dict | Any) -> BenchmarkObservation:
        """Convert WAA observation to canonical format.

        WAA observations may include:
        - screenshot: PIL Image or bytes
        - a11y_tree: UIA accessibility tree dict
        - window_title: Active window title
        """
        # Handle different WAA observation formats
        if isinstance(waa_obs, dict):
            screenshot = waa_obs.get("screenshot")
            a11y_tree = waa_obs.get("a11y_tree", waa_obs.get("accessibility_tree"))
            window_title = waa_obs.get("window_title")
            raw_obs = waa_obs
        else:
            # WAA may return observation as object with attributes
            screenshot = getattr(waa_obs, "screenshot", None)
            a11y_tree = getattr(waa_obs, "a11y_tree", None)
            window_title = getattr(waa_obs, "window_title", None)
            raw_obs = {"waa_obs_type": type(waa_obs).__name__}

        # Convert PIL Image to bytes if needed
        screenshot_bytes = None
        if screenshot is not None:
            if hasattr(screenshot, "tobytes"):
                # PIL Image - convert to PNG bytes
                import io
                buf = io.BytesIO()
                screenshot.save(buf, format="PNG")
                screenshot_bytes = buf.getvalue()
            elif isinstance(screenshot, bytes):
                screenshot_bytes = screenshot

        return BenchmarkObservation(
            screenshot=screenshot_bytes,
            viewport=(self.config.screen_width, self.config.screen_height),
            accessibility_tree=a11y_tree,
            window_title=window_title,
            raw_observation=raw_obs,
        )

    def _to_waa_action(self, action: BenchmarkAction) -> dict:
        """Convert canonical action to WAA format.

        WAA action format:
        - click: {"action_type": "click", "coordinate": [x, y]}
        - double_click: {"action_type": "double_click", "coordinate": [x, y]}
        - type: {"action_type": "type", "text": "..."}
        - key: {"action_type": "key", "key": "...", "modifiers": [...]}
        - scroll: {"action_type": "scroll", "direction": "...", "amount": ...}
        - drag: {"action_type": "drag", "start": [x, y], "end": [x, y]}
        """
        action_type = action.type

        # Map canonical action types to WAA format
        if action_type == "click":
            x = action.x or 0
            y = action.y or 0
            # Convert normalized coords to pixels if needed
            if 0 <= x <= 1 and 0 <= y <= 1:
                x = int(x * self.config.screen_width)
                y = int(y * self.config.screen_height)
            return {
                "action_type": "click",
                "coordinate": [int(x), int(y)],
            }

        elif action_type == "double_click":
            x = action.x or 0
            y = action.y or 0
            if 0 <= x <= 1 and 0 <= y <= 1:
                x = int(x * self.config.screen_width)
                y = int(y * self.config.screen_height)
            return {
                "action_type": "double_click",
                "coordinate": [int(x), int(y)],
            }

        elif action_type == "right_click":
            x = action.x or 0
            y = action.y or 0
            if 0 <= x <= 1 and 0 <= y <= 1:
                x = int(x * self.config.screen_width)
                y = int(y * self.config.screen_height)
            return {
                "action_type": "right_click",
                "coordinate": [int(x), int(y)],
            }

        elif action_type == "type":
            return {
                "action_type": "type",
                "text": action.text or "",
            }

        elif action_type == "key":
            waa_action = {
                "action_type": "key",
                "key": action.key or "",
            }
            if action.modifiers:
                waa_action["modifiers"] = action.modifiers
            return waa_action

        elif action_type == "scroll":
            return {
                "action_type": "scroll",
                "direction": action.scroll_direction or "down",
                "amount": action.scroll_amount or 3,  # Default scroll amount
            }

        elif action_type == "drag":
            x1 = action.x or 0
            y1 = action.y or 0
            x2 = action.end_x or 0
            y2 = action.end_y or 0
            # Convert normalized coords
            if 0 <= x1 <= 1:
                x1 = int(x1 * self.config.screen_width)
                y1 = int(y1 * self.config.screen_height)
            if 0 <= x2 <= 1:
                x2 = int(x2 * self.config.screen_width)
                y2 = int(y2 * self.config.screen_height)
            return {
                "action_type": "drag",
                "start": [int(x1), int(y1)],
                "end": [int(x2), int(y2)],
            }

        elif action_type == "done":
            return {"action_type": "done"}

        elif action_type == "wait":
            return {"action_type": "wait"}

        else:
            logger.warning(f"Unknown action type: {action_type}")
            return {"action_type": action_type, "raw": action.raw_action}


class WAAMockAdapter(BenchmarkAdapter):
    """Mock WAA adapter for testing without Windows VM.

    This adapter generates synthetic tasks for testing the benchmark infrastructure
    without requiring a Windows VM or WAA server. Task IDs are prefixed with "mock_"
    to clearly distinguish them from real WAA task IDs.

    Useful for:
    - Testing the benchmark integration without actual WAA
    - Development on non-Windows platforms
    - Unit tests
    - Verifying agent behavior before running real evaluations

    Args:
        num_tasks: Number of mock tasks to generate.
        domains: Domains to include in mock tasks.

    Note:
        Mock task IDs use the format "mock_{domain}_{number}" (e.g., "mock_notepad_001").
        These IDs are explicitly rejected by WAALiveAdapter to prevent confusion
        between testing and real evaluation runs.
    """

    def __init__(
        self,
        num_tasks: int = 20,
        domains: list[str] | None = None,
    ):
        self._num_tasks = num_tasks
        self._domains = domains or WAA_DOMAINS[:3]  # Default to first 3 domains
        self._tasks: list[BenchmarkTask] = []
        self._current_task: BenchmarkTask | None = None
        self._step_count = 0
        self._temp_dir: Path | None = None
        self._actions: list[BenchmarkAction] = []  # Track actions for evaluation
        self._text_entered: str | None = None  # Track typed text
        self._generate_mock_tasks()

    @property
    def name(self) -> str:
        return "waa-mock"

    @property
    def benchmark_type(self) -> str:
        return "interactive"

    def _generate_mock_tasks(self) -> None:
        """Generate mock tasks for testing.

        Task IDs use the format "mock_{domain}_{number}" (e.g., "mock_notepad_001")
        to clearly distinguish them from real WAA UUIDs. This prevents accidental
        use of synthetic tasks with the live adapter.
        """
        tasks_per_domain = self._num_tasks // len(self._domains)
        extra = self._num_tasks % len(self._domains)

        for i, domain in enumerate(self._domains):
            count = tasks_per_domain + (1 if i < extra else 0)
            for j in range(count):
                # Use mock_ prefix to clearly indicate synthetic task
                task_id = f"mock_{domain}_{j + 1:03d}"
                self._tasks.append(
                    BenchmarkTask(
                        task_id=task_id,
                        instruction=f"Mock task {j + 1} in {domain} domain",
                        domain=domain,
                        time_limit_steps=15,
                        raw_config={"mock": True, "synthetic": True},
                    )
                )

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        if domain is not None:
            return [t for t in self._tasks if t.domain == domain]
        return self._tasks

    def load_task(self, task_id: str) -> BenchmarkTask:
        for task in self._tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"Task '{task_id}' not found")

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        self._current_task = task
        self._step_count = 0
        self._actions = []  # Clear action history
        self._text_entered = None
        return self._mock_observation()

    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        self._step_count += 1
        self._actions.append(action)  # Track action for evaluation

        # Track typed text
        if action.type == "type" and action.text:
            self._text_entered = action.text

        done = action.type in ("done", "error") or self._step_count >= 15
        return self._mock_observation(), done, {"step": self._step_count}

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Evaluate task based on actions taken.

        Success criteria for mock tasks:
        - Agent clicked the Submit button (ID 4) OR
        - Agent typed text AND clicked OK (ID 1) OR
        - Agent completed with DONE action after meaningful interaction

        This provides deterministic evaluation based on actual agent behavior,
        not random chance. The mock UI has:
        - ID 1: OK button at (100, 100)-(200, 140)
        - ID 2: Text input field at (100, 160)-(500, 200)
        - ID 3: Cancel button at (220, 100)-(320, 140)
        - ID 4: Submit button (logical, no visual but accepted at approx 400, 120)
        """
        # Check what actions were taken
        clicked_ids = set()
        typed_text = False
        called_done = False

        # Button bounds for coordinate-based detection (pixel coordinates)
        button_bounds = {
            "1": (100, 100, 200, 140),   # OK button
            "2": (100, 160, 500, 200),   # Text field
            "3": (220, 100, 320, 140),   # Cancel button
            "4": (350, 80, 450, 160),    # Submit button (logical area)
        }

        for action in self._actions:
            if action.type == "click":
                # Extract target node ID from action
                target_id = getattr(action, "target_node_id", None)
                if target_id:
                    clicked_ids.add(str(target_id))
                # Also check coordinate-based clicks (for API agents)
                elif action.x is not None and action.y is not None:
                    # Convert normalized coords to pixels if needed
                    x, y = action.x, action.y
                    if 0 <= x <= 1 and 0 <= y <= 1:
                        x = int(x * 1920)
                        y = int(y * 1200)
                    else:
                        x, y = int(x), int(y)

                    # Check which button was clicked based on coordinates
                    for btn_id, (x1, y1, x2, y2) in button_bounds.items():
                        if x1 <= x <= x2 and y1 <= y <= y2:
                            clicked_ids.add(btn_id)
                            break
            elif action.type == "type" and action.text:
                typed_text = True
            elif action.type == "done":
                called_done = True

        # Success criteria:
        # 1. Clicked Submit (ID 4) - primary success path
        # 2. Typed something AND clicked OK (ID 1) - form submission path
        clicked_submit = "4" in clicked_ids
        clicked_ok = "1" in clicked_ids
        form_submitted = typed_text and clicked_ok

        success = clicked_submit or form_submitted

        # Calculate partial credit score
        score = 0.0
        if success:
            score = 1.0
        elif typed_text or clicked_ids:
            # Partial credit for taking meaningful actions
            score = 0.3 + (0.1 * min(len(clicked_ids), 3)) + (0.2 if typed_text else 0.0)

        return BenchmarkResult(
            task_id=task.task_id,
            success=success,
            score=score,
            num_steps=self._step_count,
            reason=f"clicked={list(clicked_ids)}, typed={typed_text}, done={called_done}",
        )

    def _mock_observation(self) -> BenchmarkObservation:
        """Generate a mock observation with a real screenshot file."""
        import tempfile

        # Create temp directory if needed
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="waa_mock_"))

        # Generate a simple mock screenshot (gray image with text)
        screenshot_path = self._temp_dir / f"mock_step_{self._step_count}.png"
        self._generate_mock_screenshot(screenshot_path)

        return BenchmarkObservation(
            screenshot=screenshot_path.read_bytes(),
            screenshot_path=str(screenshot_path),
            viewport=(1920, 1200),
            accessibility_tree={
                "role": "window",
                "name": "Mock Window",
                "children": [
                    {"role": "button", "name": "OK", "id": "1"},
                    {"role": "textfield", "name": "Input", "id": "2"},
                    {"role": "button", "name": "Cancel", "id": "3"},
                    {"role": "button", "name": "Submit", "id": "4"},
                ],
            },
            window_title="Mock Window - Testing",
        )

    def _generate_mock_screenshot(self, path: Path) -> None:
        """Generate a simple mock screenshot image."""
        try:
            from PIL import Image, ImageDraw

            # Create a simple gray image with some UI elements
            img = Image.new("RGB", (1920, 1200), color=(240, 240, 240))
            draw = ImageDraw.Draw(img)

            # Draw a title bar
            draw.rectangle([0, 0, 1920, 40], fill=(60, 60, 60))
            draw.text((20, 10), "Mock Application Window", fill=(255, 255, 255))

            # Draw some buttons
            draw.rectangle([100, 100, 200, 140], fill=(0, 120, 215))
            draw.text((120, 110), "OK", fill=(255, 255, 255))

            draw.rectangle([220, 100, 320, 140], fill=(200, 200, 200))
            draw.text((240, 110), "Cancel", fill=(0, 0, 0))

            draw.rectangle([350, 100, 450, 140], fill=(0, 180, 0))
            draw.text((370, 110), "Submit", fill=(255, 255, 255))

            # Draw a text field
            draw.rectangle([100, 160, 500, 200], outline=(100, 100, 100))
            draw.text((110, 170), "Enter text here...", fill=(150, 150, 150))

            # Draw task instruction
            task_name = self._current_task.task_id if self._current_task else "Unknown"
            draw.text((100, 250), f"Task: {task_name}", fill=(0, 0, 0))
            draw.text((100, 280), f"Step: {self._step_count}", fill=(0, 0, 0))

            img.save(path)
        except ImportError:
            # Fallback: create a minimal valid PNG if PIL not available
            # This is a 1x1 gray PNG
            minimal_png = bytes([
                0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
                0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk
                0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
                0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
                0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,  # IDAT chunk
                0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
                0x00, 0x00, 0x03, 0x00, 0x01, 0x00, 0x05, 0xFE,
                0xD4, 0xEF, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,  # IEND chunk
                0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82
            ])
            path.write_bytes(minimal_png)
