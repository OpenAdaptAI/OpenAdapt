"""Windows Agent Arena Live adapter.

This module provides a live HTTP-based adapter for WAA that connects to the
WAA Flask server running inside a Windows VM. Unlike WAAAdapter which imports
WAA's DesktopEnv locally, this adapter talks to the server remotely.

Architecture:
    The adapter uses WAA's element-based execution model:
    1. Fetch accessibility tree from /accessibility endpoint
    2. Extract element bboxes and POST to /update_computer as rects dict
    3. Agent outputs actions with target_node_id (element-based grounding)
    4. Execute via /execute_windows using computer.mouse.move_id(id) commands

    This keeps grounding authority on WAA side - we send element IDs,
    not pixel coordinates. WAA's Computer class handles the grounding.

Example:
    from openadapt_evals.adapters.waa import WAALiveAdapter, WAALiveConfig

    adapter = WAALiveAdapter(WAALiveConfig(server_url="http://vm-ip:5000"))
    agent = DemoConditionedAgent(base_agent, retriever)
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.errors import (
    ActionDeliveredObservationError,
    ActionDeliveryState,
    ActionDeliveryUncertainError,
    ActionExecutionError,
)

if TYPE_CHECKING:
    # Both are type-only: `requests` and Pillow are imported inside the method
    # bodies that use them, to keep them off this module's import path. The
    # `requests.Response` and `PIL.Image.Image` annotations below named
    # undefined modules until this block existed, so `typing.get_type_hints()`
    # on this adapter raised NameError.
    import PIL.Image
    import requests

logger = logging.getLogger(__name__)


def _fraction_to_pixel(value: float, size: int, name: str) -> int:
    """Map an inclusive normalized coordinate to one valid pixel index."""
    if size <= 0:
        raise ValueError(f"{name} screen dimension must be positive")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_frac must be a finite value between 0 and 1")
    return min(int(value * size), size - 1)


# WAA task IDs are UUIDs with a domain suffix, e.g., "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx-WOS"
# Common suffixes: WOS (Windows OS), CHR (Chrome), NTP (Notepad), etc.
WAA_TASK_ID_PATTERN = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(-[A-Za-z0-9]+)?$'
)

# Synthetic task ID patterns (from mock adapter or testing)
SYNTHETIC_TASK_PATTERNS = [
    re.compile(r'^(mock_)?[a-z_]+_\d+$'),  # notepad_1, mock_chrome_001
    re.compile(r'^mock_'),  # any mock_ prefix
]


def is_real_waa_task_id(task_id: str) -> bool:
    """Check if a task ID matches the real WAA UUID format.

    Real WAA task IDs are UUIDs from test_small.json or test_all.json, e.g.:
    - "a1b2c3d4-e5f6-7890-abcd-ef1234567890-WOS"
    - "12345678-1234-1234-1234-123456789012-CHR"

    Synthetic task IDs are simple patterns like:
    - "notepad_1", "chrome_2" (from mock adapter)
    - "mock_notepad_001" (explicit mock prefix)

    Args:
        task_id: Task identifier to check.

    Returns:
        True if the task ID appears to be a real WAA UUID.
    """
    return bool(WAA_TASK_ID_PATTERN.match(task_id))


def is_synthetic_task_id(task_id: str) -> bool:
    """Check if a task ID appears to be synthetic (for testing).

    Args:
        task_id: Task identifier to check.

    Returns:
        True if the task ID matches synthetic patterns.
    """
    for pattern in SYNTHETIC_TASK_PATTERNS:
        if pattern.match(task_id):
            return True
    return False


class SyntheticTaskError(ValueError):
    """Raised when a synthetic task ID is used with the live adapter."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"Task ID '{task_id}' appears to be synthetic (for testing). "
            f"The live adapter requires real WAA task IDs (UUIDs from test_small.json or test_all.json). "
            f"\n\nTo fix this:"
            f"\n  1. Use --mock flag for testing without a Windows VM"
            f"\n  2. Or provide real WAA task IDs with --task-ids"
            f"\n  3. Or use --tasks N to select N random real tasks"
            f"\n\nExample real task ID: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890-WOS'"
        )


class AdapterInfrastructureError(RuntimeError):
    """Base exception for deterministic infrastructure/setup failures."""

    error_type = "infrastructure"


class SetupReadinessError(AdapterInfrastructureError):
    """Raised when task setup finished but app readiness could not be verified."""


def _parse_xml_a11y_tree(xml_str: str) -> dict | None:
    """Parse a UIA XML accessibility tree string into a dict.

    WAA's /accessibility endpoint may return the tree as XML with elements like::

        <Window Name="Notepad" AutomationId="NotepadWindow"
                BoundingRectangle="0,0,1920,1200">
          <Edit Name="Text Editor" AutomationId="15"
                BoundingRectangle="0,40,1920,1170"/>
        </Window>

    This function converts it to the dict format expected by
    ``_extract_rects_from_a11y``::

        {
            "role": "Window",
            "name": "Notepad",
            "id": "NotepadWindow",
            "BoundingRectangle": "0,0,1920,1200",
            "children": [
                {"role": "Edit", "name": "Text Editor", "id": "15",
                 "BoundingRectangle": "0,40,1920,1170", "children": []}
            ]
        }

    Args:
        xml_str: XML string from WAA /accessibility endpoint.

    Returns:
        Dict representation of the tree, or None on parse failure.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        logger.debug("Failed to parse a11y XML")
        return None

    # Namespace for AT-SPI component coordinates
    CP_NS = "{uri:deskat:component.at-spi.gnome.org}"

    def _elem_to_dict(elem: ET.Element) -> dict:
        node: dict[str, Any] = {"role": elem.tag}

        # Map common UIA attributes — support both UIA (Name) and AT-SPI (name)
        name = elem.get("Name", "") or elem.get("name", "")
        if name:
            node["name"] = name

        # Try AutomationId first (most stable), then RuntimeId, then name
        auto_id = elem.get("AutomationId", "")
        if auto_id:
            node["id"] = auto_id
        else:
            runtime_id = elem.get("RuntimeId", "")
            if runtime_id:
                node["id"] = runtime_id
            elif name:
                # Use element name as ID — this is what Claude sees in the
                # a11y tree and uses with click_element()/type_element()
                node["id"] = name

        # Preserve BoundingRectangle for rect extraction
        bbox = elem.get("BoundingRectangle", "")
        if bbox:
            node["BoundingRectangle"] = bbox
        else:
            # AT-SPI format: cp:screencoord="(x, y)" + cp:size="(w, h)"
            screencoord = elem.get(f"{CP_NS}screencoord", "")
            size = elem.get(f"{CP_NS}size", "")
            if screencoord and size:
                try:
                    x, y = [int(v.strip()) for v in screencoord.strip("()").split(",")]
                    w, h = [int(v.strip()) for v in size.strip("()").split(",")]
                    node["BoundingRectangle"] = f"{x},{y},{x + w},{y + h}"
                except (ValueError, IndexError):
                    pass

        # Recurse into children
        children = []
        for child in elem:
            children.append(_elem_to_dict(child))
        node["children"] = children

        return node

    return _elem_to_dict(root)


def _is_failsafe_error(text: str) -> bool:
    """Return True if *text* contains a PyAutoGUI fail-safe error message.

    PyAutoGUI raises ``FailSafeException`` when the mouse is moved to a
    screen corner while ``FAILSAFE`` is enabled.  The exception propagates
    through WAA's /execute_windows endpoint and appears in the ``stderr`` or
    ``stdout`` of the response.  We detect it by looking for the canonical
    substrings that PyAutoGUI includes in the exception message and traceback.

    Args:
        text: Combined stderr + stdout string from the execute response.

    Returns:
        True if the text indicates a fail-safe trigger, False otherwise.
    """
    lower = text.lower()
    return "failsafeexception" in lower or "fail-safe triggered" in lower


def _build_type_commands(text: str) -> str:
    """Build pyautogui command body to type text, handling embedded newlines.

    Uses ``repr()`` for string escaping instead of manual character-by-character
    replacement.  This eliminates the entire class of escaping bugs (newlines,
    tabs, quotes, unicode, null bytes, etc.) because ``repr()`` is Python's own
    mechanism for producing valid string literals from any string content —
    the same principle as parameterized SQL queries vs string concatenation.

    Newlines are handled semantically: split into separate ``write()`` calls
    with ``press('enter')`` between them, since the agent intends "press Enter."

    Returns:
        A pyautogui command body string (without ``import pyautogui;`` prefix).
        Callers must prepend the import themselves.
    """
    text = text.replace("\r", "")
    segments = text.split("\n")
    if len(segments) == 1:
        return f"pyautogui.write({repr(text)}, interval=0.02)"

    commands: list[str] = []
    for i, seg in enumerate(segments):
        if seg:
            commands.append(f"pyautogui.write({repr(seg)}, interval=0.02)")
        if i < len(segments) - 1:
            commands.append("pyautogui.press('enter')")
    return "; ".join(commands) if commands else "pass"


_LIBREOFFICE_RECOVERY_CLEANUP_SCRIPT = r"""
import os, re, shutil, glob

home = os.path.expanduser("~")
lo_base = os.path.join(home, "AppData", "Roaming", "LibreOffice")

# Search ALL profile paths: LibreOffice/4/user, LibreOffice/user, etc.
user_dirs = []
if os.path.isdir(lo_base):
    for entry in os.listdir(lo_base):
        candidate = os.path.join(lo_base, entry, "user")
        if os.path.isdir(candidate):
            user_dirs.append(candidate)
    # Also check LibreOffice/user directly
    direct = os.path.join(lo_base, "user")
    if os.path.isdir(direct) and direct not in user_dirs:
        user_dirs.append(direct)

if not user_dirs:
    print(f"No LibreOffice user dirs found under {lo_base}")

for lo_user in user_dirs:
    print(f"Cleaning {lo_user}")

    # Clear backup directory
    backup_dir = os.path.join(lo_user, "backup")
    if os.path.exists(backup_dir):
        files = os.listdir(backup_dir)
        if files:
            shutil.rmtree(backup_dir)
            os.makedirs(backup_dir)
            print(f"  Cleared {len(files)} backup file(s)")

    # Clear .~lock.* files that block re-opening
    for lockfile in glob.glob(os.path.join(lo_user, ".~lock.*")):
        os.remove(lockfile)
        print(f"  Removed lock file: {os.path.basename(lockfile)}")

    # Edit registrymodifications.xcu to remove recovery entries and disable autosave
    xcu = os.path.join(lo_user, "registrymodifications.xcu")
    if os.path.exists(xcu):
        with open(xcu, "r", encoding="utf-8") as f:
            content = f.read()
        changed = False
        if "RecoveryList" in content:
            content = re.sub(
                r'<item oor:path="/org.openoffice.Office.Recovery/RecoveryList">.*?</item>',
                "",
                content,
                flags=re.DOTALL,
            )
            changed = True
            print("  Removed RecoveryList entries")
        autosave_line = (
            '<item oor:path="/org.openoffice.Office.Recovery/AutoSave">'
            '<prop oor:name="Enabled" oor:op="fuse"><value>false</value></prop></item>'
        )
        if "AutoSave" not in content:
            content = content.replace("</oor:items>", autosave_line + "\n</oor:items>")
            changed = True
            print("  Added AutoSave=false")
        elif ">true<" in content.split("AutoSave")[1].split("</item>")[0]:
            content = re.sub(
                r'<item oor:path="/org.openoffice.Office.Recovery/AutoSave">.*?</item>',
                autosave_line,
                content,
                flags=re.DOTALL,
            )
            changed = True
            print("  Changed AutoSave to false")
        if changed:
            with open(xcu, "w", encoding="utf-8") as f:
                f.write(content)

# Also clear lock files from common download locations
for d in [os.path.join(home, "Downloads"), os.path.join(home, "Documents")]:
    for lockfile in glob.glob(os.path.join(d, ".~lock.*")):
        os.remove(lockfile)
        print(f"Removed download lock: {lockfile}")
"""


@dataclass
class WAALiveConfig:
    """Configuration for WAALiveAdapter.

    Attributes:
        server_url: URL of WAA Flask server (e.g., "http://172.171.112.41:5000").
        evaluate_url: URL of evaluate server (e.g., "http://localhost:5050").
            If None, defaults to server_url. The evaluate server runs on the
            Docker Linux side (port 5050) and has access to WAA evaluator modules.
        a11y_backend: Accessibility backend ("uia" or "win32").
        screen_width: Screen width in pixels.
        screen_height: Screen height in pixels.
        max_steps: Default maximum steps per task.
        action_delay: Delay after actions in seconds (for UI to settle).
        timeout: Request timeout in seconds.
        evaluate_timeout: Timeout in seconds for /evaluate requests. WAA
            evaluators may need to reach the Windows VM (172.30.0.2) to run
            getters/metrics, which can be slow. Defaults to 180s.
        evaluate_retries: Number of retry attempts for /evaluate requests.
            Uses exponential backoff between retries. Defaults to 3.
        evaluate_retry_base_delay: Base delay in seconds between /evaluate
            retries. Actual delay is base * 2^(attempt-1). Defaults to 5.0.
        waa_examples_path: Path to WAA evaluation_examples_windows directory
            for loading task configs with evaluator specs. If not set, tasks
            are loaded from server or created as minimal placeholders.
        clean_desktop: If True, applies deterministic desktop policy before
            task setup (suppresses OneDrive/toast/taskbar noise).
        force_tray_icons: If True, enforces network/audio tray icons visible
            via policy keys before task setup.
        reapply_clean_desktop_each_reset: Re-apply clean desktop policy on
            every reset instead of once per adapter lifecycle.
        waa_image_version: Optional pinned WAA image version identifier to
            record in environment metadata.
        strict_setup_readiness: If True, fail task before step 0 when setup
            succeeded but target app cannot be focused/verified.
        setup_readiness_retries: Number of post-setup focus retries.
    """

    server_url: str = "http://localhost:5000"
    evaluate_url: str | None = None  # Auto-detects port 5050 if /evaluate 404s on server_url
    a11y_backend: str = "uia"
    screen_width: int = 1280   # Default matches typical WAA QEMU resolution
    screen_height: int = 720
    max_steps: int = 15
    action_delay: float = 0.5
    timeout: float = 90.0
    evaluate_timeout: float = 180.0  # Separate timeout for /evaluate (WAA evaluators can be slow)
    evaluate_retries: int = 3  # Number of retries for /evaluate with exponential backoff
    evaluate_retry_base_delay: float = 5.0  # Base delay (seconds) between retries
    waa_examples_path: str | None = None  # Auto-detected from common paths + WAA_EXAMPLES_PATH env
    lightweight: bool = True  # Skip cleanup/focus checks; let the agent handle popups
    clean_desktop: bool = False
    force_tray_icons: bool = False
    reapply_clean_desktop_each_reset: bool = False
    waa_image_version: str | None = None
    strict_setup_readiness: bool = False
    setup_readiness_retries: int = 3
    focus_check_method: str = "win32"  # "win32", "a11y", or "both"


class WAALiveAdapter(BenchmarkAdapter):
    """Live WAA adapter that connects to WAA Flask server over HTTP.

    Unlike WAAAdapter which imports WAA's DesktopEnv locally, this adapter
    talks to the WAA server remotely via HTTP. This enables:
    - Running DemoConditionedAgent from local machine
    - Using our own VLM (Claude/GPT) instead of WAA's built-in navi agent
    - Injecting demos into prompts before each action

    Args:
        config: WAALiveConfig with server URL and settings.
    """

    def __init__(self, config: WAALiveConfig | None = None):
        self.config = config or WAALiveConfig()
        self._auto_detect_waa_examples_path()
        self._current_task: BenchmarkTask | None = None
        self._step_count = 0
        self._current_a11y: dict | None = None
        self._current_rects: dict[str, list[int]] = {}  # element_id -> [l, t, r, b]
        self._current_screenshot: bytes | None = None
        self._actions: list[BenchmarkAction] = []
        self._actual_screen_size: tuple[int, int] | None = None
        self._clean_desktop_applied = False
        self._environment_profile: dict[str, Any] = {}
        self._last_setup_results: list[dict[str, Any]] = []
        self._last_foreground_title: str | None = None

    def _auto_detect_waa_examples_path(self) -> None:
        """Auto-detect waa_examples_path from env var or common locations."""
        if self.config.waa_examples_path:
            return

        import os
        from pathlib import Path

        # Check env var first
        env_path = os.environ.get("WAA_EXAMPLES_PATH")
        if env_path and Path(env_path).is_dir():
            self.config.waa_examples_path = env_path
            logger.info("Auto-detected waa_examples_path from WAA_EXAMPLES_PATH: %s", env_path)
            return

        # Check common paths relative to CWD
        common_paths = [
            "evaluation_examples_windows",
            "src/win-arena-container/evaluation_examples_windows",
            "../WindowsAgentArena/src/win-arena-container/evaluation_examples_windows",
            "../waa/src/win-arena-container/evaluation_examples_windows",
            "../waa/evaluation_examples_windows",
        ]
        for p in common_paths:
            path = Path(p)
            if path.is_dir():
                self.config.waa_examples_path = str(path)
                logger.info("Auto-detected waa_examples_path: %s", path)
                return

    @property
    def name(self) -> str:
        """Benchmark name."""
        return "waa-live"

    @property
    def benchmark_type(self) -> str:
        """Benchmark type (interactive)."""
        return "interactive"

    @property
    def supports_parallel(self) -> bool:
        """Whether parallel execution is supported."""
        return False  # Single VM for now

    def get_environment_profile(self) -> dict[str, Any]:
        """Return the last captured environment profile for this adapter."""
        return dict(self._environment_profile)

    def check_connection(self) -> bool:
        """Check if WAA server is reachable.

        Returns:
            True if server responds to /probe endpoint.
        """
        try:
            import requests
            resp = requests.get(
                f"{self.config.server_url}/probe",
                timeout=5.0
            )
            return resp.status_code == 200
        except Exception:
            return False

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        """List available WAA tasks.

        For live adapter, tasks are typically loaded on-demand.
        Returns empty list - use load_task() directly.
        """
        return []

    def load_task(self, task_id: str) -> BenchmarkTask:
        """Load a specific task by ID.

        Attempts to load the full task config with evaluator specification
        from multiple sources:
        1. WAA server's /task/<task_id> endpoint (if available)
        2. Local WAA examples directory (if waa_examples_path is set)
        3. Creates minimal task as fallback

        Args:
            task_id: Task identifier. Must be a real WAA UUID
                (e.g., "a1b2c3d4-e5f6-7890-abcd-ef1234567890-WOS").

        Returns:
            BenchmarkTask object with evaluator config if available.

        Raises:
            SyntheticTaskError: If task_id appears to be synthetic (e.g., "notepad_1").
                Use WAAMockAdapter for synthetic/testing tasks.
        """
        # Validate that this is a real WAA task ID, not a synthetic one
        if is_synthetic_task_id(task_id):
            raise SyntheticTaskError(task_id)

        import requests

        # Try to load from evaluate server first (has /task/<id> endpoint
        # that reads task configs from WAA's evaluation_examples_windows/)
        evaluate_base = self.config.evaluate_url or self.config.server_url
        for base_url in [evaluate_base, self.config.server_url]:
            try:
                resp = requests.get(
                    f"{base_url}/task/{task_id}",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    config = resp.json()
                    return self._create_task_from_config(task_id, config)
            except Exception:
                continue

        # Try to load from local WAA examples directory
        if hasattr(self.config, "waa_examples_path") and self.config.waa_examples_path:
            task = self._load_task_from_disk(task_id, self.config.waa_examples_path)
            if task:
                return task

        # Fallback: create minimal task
        logger.warning(
            f"Could not load full task config for {task_id}. "
            "Evaluation will use fallback heuristics. "
            "Set waa_examples_path or deploy /task endpoint for proper evaluation."
        )
        return BenchmarkTask(
            task_id=task_id,
            instruction=f"Task {task_id}",
            domain=task_id.split("_")[0] if "_" in task_id else "unknown",
            time_limit_steps=self.config.max_steps,
        )

    def load_task_from_json(self, task_id: str, config: dict) -> BenchmarkTask:
        """Load a task from a JSON config dict.

        Use this to pass in task configs directly without server/disk lookup.

        Args:
            task_id: Task identifier.
            config: Full task configuration including evaluator spec.

        Returns:
            BenchmarkTask with evaluator config.
        """
        return self._create_task_from_config(task_id, config)

    def _create_task_from_config(self, task_id: str, config: dict) -> BenchmarkTask:
        """Create BenchmarkTask from WAA JSON config.

        Args:
            task_id: Task identifier.
            config: WAA task configuration dict.

        Returns:
            BenchmarkTask with raw_config containing evaluator spec.
        """
        # Parse domain from task_id if not in config
        domain = config.get("domain")
        if not domain:
            domain = task_id.split("_")[0] if "_" in task_id else "unknown"

        return BenchmarkTask(
            task_id=task_id,
            instruction=config.get("instruction", config.get("task", f"Task {task_id}")),
            domain=domain,
            initial_state_ref=config.get("snapshot"),
            time_limit_steps=config.get("max_steps", self.config.max_steps),
            raw_config=config,  # Includes evaluator spec
            evaluation_spec=config.get("evaluator"),
        )

    def _load_task_from_disk(self, task_id: str, base_path: str) -> BenchmarkTask | None:
        """Load task from WAA examples directory on disk.

        Args:
            task_id: Task identifier (e.g., "notepad_1" or "notepad_abc123-def").
            base_path: Path to WAA evaluation_examples_windows directory.

        Returns:
            BenchmarkTask or None if not found.
        """
        import json
        from pathlib import Path

        base = Path(base_path)

        # Parse task_id to get domain and task file name
        # Format: domain_taskname or domain_uuid (or bare UUID)
        parts = task_id.split("_", 1)
        if len(parts) >= 2:
            domain = parts[0]
            task_name = parts[1]

            # Try different file locations
            candidates = [
                base / "examples" / domain / f"{task_name}.json",
                base / domain / f"{task_name}.json",
                base / "examples" / domain / f"{task_id}.json",
            ]

            for task_file in candidates:
                if task_file.exists():
                    try:
                        with open(task_file, encoding="utf-8") as f:
                            config = json.load(f)
                        logger.info(f"Loaded task config from {task_file}")
                        return self._create_task_from_config(task_id, config)
                    except Exception as e:
                        logger.warning(f"Failed to load {task_file}: {e}")

        # Fallback: recursive glob search for {task_id}.json
        # Handles UUID task IDs that don't follow domain_taskname format
        matches = list(base.rglob(f"{task_id}.json"))
        if matches:
            try:
                with open(matches[0], encoding="utf-8") as f:
                    config = json.load(f)
                logger.info(f"Loaded task config from {matches[0]}")
                return self._create_task_from_config(task_id, config)
            except Exception as e:
                logger.warning(f"Failed to load {matches[0]}: {e}")

        return None

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Reset environment to task's initial state.

        Args:
            task: Task to initialize.

        Returns:
            Initial observation (screenshot + accessibility tree).

        Raises:
            RuntimeError: If server is not reachable.
        """
        # Invalidate every task-bound observation before any reset work. A
        # failed reset must not leave an earlier task actionable.
        self._current_task = None
        self._step_count = 0
        self._actions = []
        self._actual_screen_size = None
        self._current_screenshot = None
        self._current_a11y = None
        self._current_rects = {}

        if not self.check_connection():
            raise RuntimeError(
                f"Cannot connect to WAA server at {self.config.server_url}. "
                f"Ensure Windows VM is running and server is started."
            )

        import requests

        if self.config.lightweight:
            # Lightweight mode: run task setup commands, take screenshot, go.
            # No cleanup, no notification dismissal, no focus checks.
            # The agent handles popups autonomously (proven more reliable).
            if task.raw_config:
                self._run_task_setup(task.raw_config)
                time.sleep(2.0)
            observation = self._get_observation()
            self._current_task = task
            return observation

        # Legacy mode (lightweight=False): full cleanup + focus checks.
        # Kept for backward compatibility but not recommended — the cleanup
        # commands frequently crash the WAA Flask server.
        try:
            dismiss_cmd = (
                "import pyautogui, subprocess; "
                "subprocess.run('taskkill /F /IM OneDriveStandaloneUpdater.exe /T', "
                "shell=True, capture_output=True, timeout=5); "
                "pyautogui.press('escape'); "
                "pyautogui.press('escape')"
            )
            resp = requests.post(
                f"{self.config.server_url}/execute_windows",
                json={"command": dismiss_cmd},
                timeout=8.0,
            )
            if resp.status_code == 200:
                logger.debug("OneDrive dismiss sent")
        except Exception as e:
            logger.debug("OneDrive dismiss failed (non-fatal): %s", e)

        if self.config.clean_desktop:
            self._apply_clean_desktop_policy(requests)

        if task.raw_config and self._is_libreoffice_task(task.raw_config):
            self._prepare_libreoffice_clean_state(requests)

        if task.raw_config and self._is_chrome_task(task.raw_config):
            self._prepare_chrome_clean_state(requests)

        if task.raw_config:
            self._run_task_setup(task.raw_config)

        time.sleep(5.0)

        if task.raw_config:
            focused = self._ensure_app_focused(task.raw_config)
            if not focused and self.config.strict_setup_readiness:
                raise SetupReadinessError(
                    "Post-setup app readiness check failed: target application "
                    "was not foregrounded after deterministic remediation. "
                    "Marking task as infrastructure failure before step budget."
                )

        observation = self._get_observation()
        self._current_task = task
        return observation

    def _send_command(self, command: str) -> None:
        """Send a pyautogui command to WAA via /execute_windows.

        A fail-safe response can occur after a multi-call command delivered a
        prefix. The command is retried only when the server explicitly reports
        ``delivery_state=not_delivered``. Otherwise recovery only prepares a
        later separately authorized attempt.

        Args:
            command: Python command string to execute on the WAA server.
        """
        import requests

        logger.info("Sending command to WAA: %r", command)
        try:
            resp = requests.post(
                f"{self.config.server_url}/execute_windows",
                json={"command": command},
                timeout=self.config.timeout,
            )
        except Exception as exc:
            raise ActionDeliveryUncertainError(
                "WAA command request failed; delivery outcome is uncertain: "
                f"{exc}"
            ) from exc

        response_text, stderr = self._command_response_details(resp)

        # WAA can report the fail-safe through HTTP 500 or through stderr on
        # HTTP 200. Recovery is bounded to one attempt and must itself succeed.
        if _is_failsafe_error(response_text):
            logger.warning("PyAutoGUI fail-safe detected; attempting recovery...")
            delivery_state = self._command_delivery_state(resp)
            if not self._recover_failsafe():
                if delivery_state is ActionDeliveryState.NOT_DELIVERED:
                    raise ActionExecutionError(
                        "WAA fail-safe recovery failed after confirmed non-delivery"
                    )
                raise ActionDeliveryUncertainError(
                    "WAA fail-safe recovery failed; delivery could not be confirmed"
                )

            if delivery_state is not ActionDeliveryState.NOT_DELIVERED:
                raise ActionDeliveryUncertainError(
                    "WAA fail-safe may have occurred after part of the command was "
                    "delivered; refusing a blind retry"
                )

            logger.info(
                "Fail-safe cleared after an explicit non-delivery receipt; "
                "retrying command: %s",
                command,
            )
            try:
                retry_resp = requests.post(
                    f"{self.config.server_url}/execute_windows",
                    json={"command": command},
                    timeout=self.config.timeout,
                )
            except Exception as exc:
                raise ActionDeliveryUncertainError(
                    "WAA command retry failed; delivery outcome is uncertain: "
                    f"{exc}"
                ) from exc

            retry_text, retry_stderr = self._command_response_details(retry_resp)
            retry_delivery_state = self._command_delivery_state(retry_resp)
            if _is_failsafe_error(retry_text):
                if retry_delivery_state is ActionDeliveryState.NOT_DELIVERED:
                    raise ActionExecutionError(
                        "WAA command retry triggered a fail-safe before delivery"
                    )
                raise ActionDeliveryUncertainError(
                    "WAA command retry triggered a fail-safe; refusing another retry"
                )
            if retry_resp.status_code != 200:
                message = (
                    "WAA command retry was not confirmed: "
                    f"HTTP {retry_resp.status_code}: {retry_resp.text}"
                )
                if retry_delivery_state is ActionDeliveryState.NOT_DELIVERED:
                    raise ActionExecutionError(message)
                raise ActionDeliveryUncertainError(message)
            self._validate_command_receipt(retry_resp, context="retry")
            if retry_stderr:
                raise ActionDeliveryUncertainError(
                    f"WAA command retry returned stderr: {retry_stderr}"
                )
            logger.debug("Executed after fail-safe recovery: %s", command)
            return

        if resp.status_code != 200:
            message = (
                "WAA command was not confirmed: "
                f"HTTP {resp.status_code}: {resp.text}"
            )
            if self._command_delivery_state(resp) is ActionDeliveryState.NOT_DELIVERED:
                raise ActionExecutionError(message)
            raise ActionDeliveryUncertainError(message)
        self._validate_command_receipt(resp, context="command")
        if stderr:
            raise ActionDeliveryUncertainError(
                f"WAA command returned stderr: {stderr}"
            )
        logger.debug("Executed: %s", command)

    @classmethod
    def _validate_command_receipt(cls, response: Any, *, context: str) -> None:
        """Reject an explicit failed or incomplete HTTP-200 command receipt."""
        try:
            result = response.json()
        except Exception:
            return
        if not isinstance(result, dict):
            return

        reason: str | None = None
        if "success" in result and result["success"] is not True:
            reason = f"success={result['success']!r}"
        if result.get("error"):
            reason = f"error={result['error']!r}"
        if "returncode" in result and (
            isinstance(result["returncode"], bool)
            or not isinstance(result["returncode"], int)
            or result["returncode"] != 0
        ):
            reason = f"returncode={result['returncode']!r}"

        delivery_state = cls._command_delivery_state(response)
        if "delivery_state" in result and delivery_state is None:
            reason = f"invalid delivery_state={result['delivery_state']!r}"
        if reason is None and delivery_state in {
            ActionDeliveryState.NOT_DELIVERED,
            ActionDeliveryState.UNCERTAIN,
        }:
            reason = f"delivery_state={delivery_state.value}"
        if reason is None:
            return

        message = f"WAA {context} receipt did not confirm execution: {reason}"
        if delivery_state is ActionDeliveryState.NOT_DELIVERED:
            raise ActionExecutionError(message)
        raise ActionDeliveryUncertainError(message)

    @staticmethod
    def _command_delivery_state(response: Any) -> ActionDeliveryState | None:
        """Read an explicit typed delivery state from one server receipt."""
        try:
            result = response.json()
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        try:
            return ActionDeliveryState(result.get("delivery_state"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _command_response_details(response: Any) -> tuple[str, str]:
        """Return response diagnostics and reject malformed success receipts."""
        response_text = str(response.text or "")
        try:
            result = response.json()
        except Exception as exc:
            if response.status_code == 200:
                raise ActionDeliveryUncertainError(
                    "WAA returned an invalid command receipt; delivery outcome is uncertain"
                ) from exc
            return response_text, ""

        if not isinstance(result, dict):
            if response.status_code == 200:
                raise ActionDeliveryUncertainError(
                    "WAA returned an invalid command receipt; delivery outcome is uncertain"
                )
            return response_text, ""

        stderr = str(result.get("stderr") or "")
        stdout = str(result.get("stdout") or "")
        message = str(result.get("message") or "")
        return "\n".join((response_text, stderr, stdout, message)), stderr

    def step(self, action: BenchmarkAction) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute action and return new observation.

        Uses element-based grounding via WAA's Computer class. Click actions
        are translated to computer.mouse.move_id(id) commands that WAA executes
        using the rects we POSTed to /update_computer.

        If PyAutoGUI's fail-safe is triggered, the adapter refuses a blind
        retry unless the server proves that the command was not dispatched.

        Args:
            action: Action to execute.

        Returns:
            Tuple of (observation, done, info).
        """
        if self._current_task is None:
            raise ActionExecutionError("WAA action requires a successfully loaded task")

        pointer_action = action.type in {
            "click",
            "double_click",
            "right_click",
            "scroll",
            "drag",
        } or (action.type == "type" and action.target_node_id is not None)
        if pointer_action:
            self._require_pointer_context()

        # Translate action to element-based command for WAA's Computer
        command = self._translate_action(action)

        if action.type not in ("done", "error") and (
            not command or command.lstrip().startswith("pass")
        ):
            raise ActionExecutionError(
                f"WAA action {action.type!r} did not produce an executable command"
            )

        # Execute command via /execute_windows (has access to computer object)
        if command:
            self._send_command(command)

        self._step_count += 1
        self._actions.append(action)

        # Wait for UI to settle
        time.sleep(self.config.action_delay)

        # Check if done (error actions are also terminal)
        done = action.type in ("done", "error") or self._step_count >= self.config.max_steps

        try:
            obs = self._get_observation()
        except Exception as exc:
            raise ActionDeliveredObservationError(
                f"WAA action {action.type!r} was delivered, but observation failed: {exc}"
            ) from exc
        info = {
            "step": self._step_count,
            "command": command,
        }

        return obs, done, info

    def _recover_failsafe(self) -> bool:
        """Move mouse away from corner to clear PyAutoGUI fail-safe.

        Sends a recovery command via the /execute endpoint on the WAA server
        using the ``python -c "..."`` wrapper format required by that endpoint.

        The command disables the fail-safe flag and moves the mouse to a safe
        position (500, 400) near the center of the screen.

        Returns:
            True if the recovery request succeeded (HTTP 200), False otherwise.
        """
        import requests

        recovery_script = (
            "import pyautogui\n"
            "previous = pyautogui.FAILSAFE\n"
            "try:\n"
            "    pyautogui.FAILSAFE = False\n"
            "    pyautogui.moveTo(500, 400)\n"
            "finally:\n"
            "    pyautogui.FAILSAFE = previous\n"
        )
        recovery_payload = base64.b64encode(recovery_script.encode()).decode()
        recovery_command = (
            'python -c "import base64;'
            f"exec(base64.b64decode('{recovery_payload}'))\""
        )
        try:
            resp = requests.post(
                f"{self.config.server_url}/execute",
                json={"command": recovery_command},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error(
                    f"Fail-safe recovery request returned HTTP {resp.status_code}: {resp.text}"
                )
                return False
            try:
                _, stderr = self._command_response_details(resp)
                self._validate_command_receipt(resp, context="fail-safe recovery")
            except ActionExecutionError as exc:
                logger.error("Fail-safe recovery returned an invalid receipt: %s", exc)
                return False
            if stderr:
                logger.error("Fail-safe recovery returned stderr: %s", stderr)
                return False
            logger.info("Fail-safe recovery command succeeded")
            return True
        except Exception as e:
            logger.error(f"Fail-safe recovery request failed: {e}")
            return False

    def run_powershell(self, script: str) -> str:
        """Execute a PowerShell command on the Windows VM and return stdout.

        Sends the script to the WAA server's ``/execute`` endpoint wrapped
        in a ``python -c "..."`` command that invokes PowerShell via
        ``subprocess.run``.

        This is primarily intended for use by task verifiers that need to
        inspect VM state (e.g., checking file counts, registry values, etc.).

        Args:
            script: PowerShell command or script to execute. Multi-line
                scripts are supported but should be kept simple.

        Returns:
            The stdout output from the PowerShell command as a string.

        Raises:
            RuntimeError: If the command execution fails or the server
                is unreachable.
        """
        import requests

        # Build a python -c command that runs PowerShell via subprocess.
        # The /execute endpoint requires the "python -c ..." format.
        # We use repr() for safe escaping of the PowerShell script.
        python_code = (
            "import subprocess; "
            f"r = subprocess.run(['powershell', '-Command', {repr(script)}], "
            "capture_output=True, text=True, timeout=30); "
            "print(r.stdout)"
        )
        command = f'python -c "{python_code}"'

        try:
            resp = requests.post(
                f"{self.config.server_url}/execute",
                json={"command": command},
                timeout=self.config.timeout,
            )
            if resp.status_code == 200:
                result = resp.json()
                stdout = result.get("stdout", "")
                stderr = result.get("stderr", "")
                if stderr:
                    logger.warning("PowerShell stderr: %s", stderr)
                return stdout
            else:
                raise RuntimeError(
                    f"PowerShell execution failed (HTTP {resp.status_code}): "
                    f"{resp.text}"
                )
        except requests.RequestException as e:
            raise RuntimeError(
                f"Failed to connect to WAA server for PowerShell execution: {e}"
            ) from e

    def _evaluate_request(
        self,
        endpoint: str,
        eval_request: dict,
        timeout: float,
    ) -> "requests.Response":
        """Send a single evaluate request with retry logic.

        Retries on timeout and connection errors with exponential backoff.
        Returns the first successful response or raises the last exception.

        Args:
            endpoint: Full URL of the /evaluate endpoint.
            eval_request: JSON payload.
            timeout: Per-request timeout in seconds.

        Returns:
            The HTTP response (may be non-200).

        Raises:
            requests.Timeout: If all retries timed out.
            requests.RequestException: If all retries failed with connection errors.
        """
        import requests

        max_attempts = max(1, self.config.evaluate_retries)
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Evaluate request attempt %d/%d to %s (timeout=%.0fs)",
                    attempt, max_attempts, endpoint, timeout,
                )
                resp = requests.post(
                    endpoint,
                    json=eval_request,
                    timeout=timeout,
                )
                # Got a response (even if non-200) -- return it, don't retry
                # HTTP errors like 404/500 are not transient and won't benefit
                # from retry; the caller handles them.
                return resp

            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                error_type = "timed out" if isinstance(e, requests.Timeout) else "connection error"
                if attempt < max_attempts:
                    delay = self.config.evaluate_retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Evaluate %s on attempt %d/%d to %s, retrying in %.1fs: %s",
                        error_type, attempt, max_attempts, endpoint, delay, e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "Evaluate %s on attempt %d/%d to %s (final): %s",
                        error_type, attempt, max_attempts, endpoint, e,
                    )

        # All retries exhausted -- re-raise the last exception
        raise last_exc  # type: ignore[misc]

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Evaluate current state against task success criteria.

        Calls the /evaluate endpoint on the WAA server which runs WAA's
        native evaluators (getters + metrics) to determine task success.

        If the task has an evaluator config in raw_config, it's sent to
        the server. If the /evaluate endpoint is not available, falls back
        to a simple heuristic based on actions taken.

        Uses ``evaluate_timeout`` (default 180s) instead of the general
        ``timeout`` (90s) because WAA evaluators often need to reach the
        Windows VM at 172.30.0.2 to run getters/metrics, which can be slow.
        Retries up to ``evaluate_retries`` times (default 3) with exponential
        backoff on timeout or connection errors.

        Args:
            task: Task to evaluate.

        Returns:
            BenchmarkResult with success/score.
        """
        import requests

        # Build evaluation request
        eval_request = {}

        # Include evaluator config from task if available
        if task.raw_config:
            eval_request = task.raw_config.copy()

        # Add agent's last action for infeasible task detection
        if self._actions:
            last_action = self._actions[-1]
            if last_action.type == "done":
                eval_request["agent_last_action"] = "DONE"
            elif last_action.answer:
                eval_request["agent_last_action"] = last_action.answer

        # Determine evaluate URL: prefer dedicated evaluate server (port 5050),
        # fall back to WAA server's /evaluate endpoint
        evaluate_base = self.config.evaluate_url or self.config.server_url
        evaluate_endpoint = f"{evaluate_base}/evaluate"
        eval_timeout = self.config.evaluate_timeout

        # Try the /evaluate endpoint (with retries on timeout/connection errors)
        try:
            resp = self._evaluate_request(evaluate_endpoint, eval_request, eval_timeout)

            if resp.status_code == 200:
                result = resp.json()
                return self._result_from_evaluate_response(task, result)

            elif resp.status_code == 404 or (
                resp.status_code == 500 and "404 Not Found" in resp.text
            ):
                # Auto-detect: try port 5050 (evaluate_server.py) if not already tried
                if self.config.evaluate_url is None:
                    from urllib.parse import urlparse
                    parsed = urlparse(self.config.server_url)
                    fallback_url = f"{parsed.scheme}://{parsed.hostname}:5050"
                    logger.info(
                        "/evaluate not found at %s, trying fallback: %s",
                        evaluate_endpoint, fallback_url,
                    )
                    try:
                        resp2 = self._evaluate_request(
                            f"{fallback_url}/evaluate", eval_request, eval_timeout,
                        )
                        if resp2.status_code == 200:
                            # Cache the working URL for future calls
                            self.config.evaluate_url = fallback_url
                            result = resp2.json()
                            return self._result_from_evaluate_response(
                                task, result,
                            )
                    except Exception as exc:
                        logger.warning("Fallback evaluate at %s failed: %s", fallback_url, exc)

                logger.warning(
                    f"/evaluate endpoint not found at {evaluate_endpoint}. "
                    "Ensure the evaluate server is running on port 5050."
                )
                # The evaluate server is not deployed. That is an
                # infrastructure fact about our harness, not a verdict on the
                # agent, and it must not be aggregated as a scored 0.0.
                result = self._evaluate_fallback(task)
                result.error_type = "infrastructure"
                return result

            else:
                logger.error(
                    f"Evaluation request failed ({resp.status_code}): {resp.text}"
                )
                return BenchmarkResult(
                    task_id=task.task_id,
                    success=False,
                    score=0.0,
                    num_steps=self._step_count,
                    reason=f"Evaluation failed: HTTP {resp.status_code}",
                    # The evaluate server erroring is our infrastructure
                    # failing, not the agent failing the task.
                    error_type="infrastructure",
                )

        except requests.Timeout:
            logger.error(
                "Evaluation request timed out after %d retries (timeout=%.0fs each)",
                self.config.evaluate_retries, eval_timeout,
            )
            return BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                num_steps=self._step_count,
                reason=(
                    f"Evaluation timed out after {self.config.evaluate_retries} "
                    f"retries ({eval_timeout:.0f}s timeout each)"
                ),
                error_type="infrastructure",
            )

        except requests.RequestException as e:
            logger.error(f"Evaluation request error after retries: {e}")
            result = self._evaluate_fallback(task)
            result.error_type = "infrastructure"
            return result

    def _result_from_evaluate_response(
        self, task: BenchmarkTask, result: Any,
    ) -> BenchmarkResult:
        """Build a BenchmarkResult from an /evaluate 200 body.

        The evaluate server distinguishes "measured" from "could not measure"
        via ``scored``/``error_type``; a 200 carrying ``scored: false`` is an
        unscored task, and copying only ``success``/``score`` out of it is how
        an unreachable VM becomes a legitimate 0% row. Bodies from older
        servers have neither field and are treated as measurements, which is
        what they were.
        """
        def invalid(reason: str) -> BenchmarkResult:
            return BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                num_steps=self._step_count,
                reason=f"Malformed evaluation response: {reason}",
                error_type="evaluation",
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
            num_steps=self._step_count,
            reason=result.get("reason"),
            error_type=error_type,
        )

    def _evaluate_fallback(self, task: BenchmarkTask) -> BenchmarkResult:
        """Fallback when proper evaluation unavailable - returns failure.

        This method explicitly fails instead of providing fake heuristic scores.
        Proper evaluation requires either:
        1. WAA server with /evaluate endpoint deployed
        2. Task configs with evaluator specs (set waa_examples_path)
        3. Real WAA task IDs (UUIDs from test_small.json/test_all.json)

        Args:
            task: Task to evaluate.

        Returns:
            BenchmarkResult with success=False and score=0.0.
        """
        # Check if task has evaluator config
        has_evaluator = bool(
            task.raw_config and task.raw_config.get("evaluator")
        )

        if has_evaluator:
            reason = (
                "Evaluation unavailable: WAA /evaluate endpoint not deployed. "
                "Task has evaluator config but server cannot run it."
            )
        else:
            reason = (
                "Evaluation unavailable: task config missing evaluator spec. "
                "Set waa_examples_path in config or use real WAA task IDs "
                "(UUIDs from test_small.json/test_all.json, not synthetic IDs like 'notepad_1')."
            )

        logger.error(reason)

        return BenchmarkResult(
            task_id=task.task_id,
            success=False,
            score=0.0,
            num_steps=self._step_count,
            reason=reason,
        )

    def close(self) -> None:
        """Clean up resources."""
        self._current_task = None
        self._current_a11y = None
        self._actions = []

    # --- Public convenience methods for RL / pixel-coordinate agents ---

    @property
    def screen_size(self) -> tuple[int, int]:
        """Actual screen dimensions (width, height) detected from screenshots.

        Configured dimensions are not observation evidence. The property
        refuses access until a screenshot establishes the live viewport.
        """
        if self._actual_screen_size is None:
            raise ActionExecutionError(
                "WAA screen size requires an exact measured viewport"
            )
        return self._actual_screen_size

    def _require_pointer_context(self) -> tuple[int, int]:
        """Return the current measured viewport or refuse pointer actuation."""
        if self._current_task is None:
            raise ActionExecutionError("WAA pointer action requires a loaded task")
        if self._actual_screen_size is None:
            raise ActionExecutionError(
                "WAA pointer action requires an exact measured viewport"
            )
        return self.screen_size

    def observe(self) -> BenchmarkObservation:
        """Get current observation (screenshot + a11y tree) without stepping.

        Returns a BenchmarkObservation containing:
        - screenshot: raw PNG bytes (decode with PIL.Image.open(BytesIO(obs.screenshot)))
        - viewport: (width, height) tuple of actual screen dimensions
        - accessibility_tree: parsed UIA tree dict (or None if unavailable)
        - window_title: title of the foreground window (or None)

        This does NOT advance the step counter or record an action.
        """
        return self._get_observation()

    def observe_pil(self) -> "PIL.Image.Image":
        """Get current screenshot as a PIL Image.

        Convenience wrapper around observe() for VLM/RL training pipelines
        that work with PIL images directly.

        Returns:
            PIL.Image.Image of the current desktop state.

        Raises:
            RuntimeError: If no screenshot is available.
        """
        import io

        from PIL import Image

        obs = self.observe()
        if not obs.screenshot:
            raise RuntimeError("No screenshot available from WAA server")
        return Image.open(io.BytesIO(obs.screenshot))

    def pixel_action(
        self,
        x: int | float | None = None,
        y: int | float | None = None,
        action_type: str = "click",
        text: str | None = None,
        key: str | None = None,
        x_frac: float | None = None,
        y_frac: float | None = None,
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute a pixel-coordinate action and return (obs, done, info).

        Convenience method for RL agents that output raw pixel coordinates
        instead of element IDs. Builds pyautogui commands directly and sends
        them to WAA via /execute_windows, bypassing the element-based routing
        in _translate_action/_translate_click_action entirely.

        Coordinates can be specified as:
        - Absolute pixels: pixel_action(x=885, y=22)
        - Normalized fractions (0.0-1.0): pixel_action(x_frac=0.461, y_frac=0.021)
        If x_frac/y_frac are provided, they are converted to absolute pixels
        using screen_size.

        Args:
            x: X pixel coordinate (absolute).
            y: Y pixel coordinate (absolute).
            action_type: One of "click", "double_click", "right_click", "type",
                "key", "scroll", "drag".
            text: Text to type (required when action_type="type").
            key: Key name (for action_type="key", e.g. "enter", "ctrl+a").
            x_frac: X as fraction of screen width (0.0-1.0). Overrides x.
            y_frac: Y as fraction of screen height (0.0-1.0). Overrides y.

        Returns:
            Tuple of (observation, done, info) -- same as step().
        """
        if action_type not in ("done", "error") and self._current_task is None:
            raise ActionExecutionError(
                "WAA pixel action requires a successfully loaded task"
            )

        pointer_actions = {
            "click",
            "double_click",
            "right_click",
            "type",
            "scroll",
            "drag",
        }
        if action_type in pointer_actions:
            measured_viewport = self._require_pointer_context()

        if x_frac is not None or y_frac is not None:
            # Pointer actions above require this exact measured value. Do not
            # substitute configured dimensions as actuation evidence.
            if action_type not in pointer_actions:
                measured_viewport = self._require_pointer_context()
            w, h = measured_viewport
            if x_frac is not None:
                x = _fraction_to_pixel(x_frac, w, "x")
            if y_frac is not None:
                y = _fraction_to_pixel(y_frac, h, "y")

        # Build and validate the command before changing rollout state.
        action = BenchmarkAction(type=action_type, x=x, y=y, text=text, key=key)
        command = self._build_pixel_command(
            action_type=action_type,
            x=x,
            y=y,
            text=text,
            key=key,
        )

        if action_type not in ("done", "error") and (
            not command or command.lstrip().startswith("pass")
        ):
            raise ActionExecutionError(
                f"WAA pixel action {action_type!r} did not produce an executable command"
            )

        if command:
            self._send_command(command)

        self._step_count += 1
        self._actions.append(action)

        # Wait for UI to settle
        time.sleep(self.config.action_delay)

        # Check if done (error actions are also terminal)
        done = action_type in ("done", "error") or self._step_count >= self.config.max_steps

        try:
            obs = self._get_observation()
        except Exception as exc:
            raise ActionDeliveredObservationError(
                f"WAA pixel action {action_type!r} was delivered, but observation failed: {exc}"
            ) from exc
        info = {
            "step": self._step_count,
            "command": command,
            "pixel_direct": True,
        }

        return obs, done, info

    def _build_pixel_command(
        self,
        action_type: str,
        x: int | float | None = None,
        y: int | float | None = None,
        text: str | None = None,
        key: str | None = None,
    ) -> str | None:
        """Build a pyautogui command string from pixel coordinates.

        This is the direct-pixel path used by pixel_action(). It generates
        pyautogui commands using absolute pixel coordinates without consulting
        the accessibility tree or element rects.

        Args:
            action_type: One of "click", "double_click", "right_click", "type",
                "key", "scroll", "drag", "done", "error", "wait".
            x: X pixel coordinate (absolute).
            y: Y pixel coordinate (absolute).
            text: Text to type (for action_type="type").
            key: Key name (for action_type="key").

        Returns:
            Python command string for pyautogui, or None for terminal actions.
        """
        if action_type in ("done", "error"):
            return None

        if action_type == "wait":
            return "import time; time.sleep(1)"

        if action_type in {
            "click",
            "double_click",
            "right_click",
            "type",
            "scroll",
            "drag",
        }:
            if x is None or y is None:
                raise ActionExecutionError(
                    f"WAA pixel {action_type!r} action requires x and y coordinates"
                )
            try:
                px, py = self._clamp_pixel_coords(int(x), int(y))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ActionExecutionError(
                    f"WAA pixel {action_type!r} action has invalid coordinates"
                ) from exc

            click_method = {
                "click": "click",
                "double_click": "doubleClick",
                "right_click": "rightClick",
            }.get(action_type)
            if click_method:
                return f"import pyautogui; pyautogui.{click_method}({px}, {py})"

        if action_type == "type":
            if text is None:
                raise ActionExecutionError("WAA pixel 'type' action requires text")
            type_body = _build_type_commands(text)
            return (
                f"import pyautogui; import time; "
                f"pyautogui.click({px}, {py}); "
                f"time.sleep(0.2); "
                f"{type_body}"
            )

        if action_type == "key":
            if not key:
                raise ActionExecutionError("WAA pixel 'key' action requires key")
            # Reuse the key translation logic
            temp_action = BenchmarkAction(type="key", key=key)
            return self._translate_key_action(temp_action)

        if action_type == "scroll":
            return f"import pyautogui; pyautogui.scroll(-3, x={px}, y={py})"

        if action_type == "drag":
            # drag needs end coordinates passed via text as "end_x,end_y"
            if not text:
                raise ActionExecutionError(
                    "WAA pixel 'drag' action requires end coordinates"
                )
            parts = text.split(",")
            if len(parts) != 2:
                raise ActionExecutionError(
                    "WAA pixel 'drag' end coordinates must be 'x,y'"
                )
            try:
                ex, ey = self._clamp_pixel_coords(int(parts[0]), int(parts[1]))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ActionExecutionError(
                    "WAA pixel 'drag' action has invalid end coordinates"
                ) from exc
            return (
                f"import pyautogui; import time; "
                f"pyautogui.moveTo({px}, {py}); "
                f"time.sleep(0.3); "
                f"pyautogui.mouseDown(button='left'); "
                f"time.sleep(0.1); "
                f"pyautogui.moveTo({ex}, {ey}, duration=1.0); "
                f"time.sleep(0.1); "
                f"pyautogui.mouseUp(button='left')"
            )

        raise ActionExecutionError(f"Unsupported WAA pixel action type: {action_type!r}")

    def _get_observation(self) -> BenchmarkObservation:
        """Fetch current observation from WAA server.

        Also extracts element rects from a11y tree and updates WAA's Computer
        so element-based grounding works for subsequent actions.

        Returns:
            BenchmarkObservation with screenshot and accessibility tree.
        """
        import requests

        # Observation evidence is one atomic generation. Do not combine a new
        # screenshot with accessibility data or geometry from an older frame.
        self._current_screenshot = None
        self._current_a11y = None
        self._current_rects = {}
        self._actual_screen_size = None
        screenshot = None
        a11y_tree = None

        # Get screenshot
        try:
            resp = requests.get(
                f"{self.config.server_url}/screenshot",
                timeout=self.config.timeout
            )
            if resp.status_code == 200:
                screenshot = resp.content
                self._current_screenshot = screenshot
                # Detect actual screen dimensions from screenshot
                try:
                    from io import BytesIO

                    from PIL import Image
                    img = Image.open(BytesIO(screenshot))
                    self._actual_screen_size = img.size
                except Exception:
                    pass
                logger.debug(f"Got screenshot: {len(screenshot)} bytes")
            else:
                logger.warning(f"Screenshot request failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Screenshot request error: {e}")

        # Get accessibility tree
        try:
            resp = requests.get(
                f"{self.config.server_url}/accessibility",
                params={"backend": self.config.a11y_backend},
                timeout=self.config.timeout
            )
            if resp.status_code == 200:
                result = resp.json()
                # Handle both dict response with "AT" key and direct string/dict response
                if isinstance(result, dict):
                    a11y_tree = result.get("AT", result)  # Use "AT" if present, else use whole dict
                else:
                    a11y_tree = result  # String (XML) or other format
                self._current_a11y = a11y_tree
                # Parse XML strings into structured dicts so agents see
                # a clean "[ID] role: name" format instead of raw XML
                if isinstance(a11y_tree, str):
                    parsed = _parse_xml_a11y_tree(a11y_tree)
                    if parsed:
                        a11y_tree = parsed
                # Extract rects for element-based grounding
                self._current_rects = self._extract_rects_from_a11y(a11y_tree)
                logger.debug("Got accessibility tree with %d elements", len(self._current_rects))
            else:
                logger.warning(f"A11y request failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"A11y request error: {e}")

        # Update WAA's Computer with current rects for element grounding
        if self._current_rects:
            self._update_waa_computer()

        if not screenshot and not a11y_tree:
            raise AdapterInfrastructureError(
                "WAA observation failed: no screenshot or accessibility evidence was available"
            )

        return BenchmarkObservation(
            screenshot=screenshot,
            viewport=self._actual_screen_size or (self.config.screen_width, self.config.screen_height),
            accessibility_tree=a11y_tree,
            window_title=self._extract_window_title(a11y_tree),
        )

    def _extract_window_title(self, a11y_tree: dict | str | None) -> str | None:
        """Extract window title from accessibility tree."""
        if not a11y_tree:
            return None
        # Handle XML string - parse to dict first
        if isinstance(a11y_tree, str):
            parsed = _parse_xml_a11y_tree(a11y_tree)
            if parsed:
                return parsed.get("name")
            return None
        # Try common field names
        for key in ["Name", "name", "title", "Title"]:
            if key in a11y_tree:
                return a11y_tree[key]
        return None

    def _extract_rects_from_a11y(self, a11y_tree: dict | None) -> dict[str, list[int]]:
        """Extract element ID -> bounding box mapping from accessibility tree.

        This produces the `rects` dict that WAA's Computer class expects.
        The rects are then POSTed to /update_computer so WAA can handle grounding.

        Args:
            a11y_tree: Accessibility tree from /accessibility endpoint.

        Returns:
            Dict mapping element IDs to [left, top, right, bottom] bounding boxes.
        """
        rects: dict[str, list[int]] = {}

        def visit(node: dict) -> None:
            # Get element ID — try explicit IDs first, then fall back to name
            elem_id = None
            for id_field in ["id", "Id", "ID", "AutomationId", "name", "Name"]:
                if id_field in node and node[id_field]:
                    elem_id = str(node[id_field])
                    break

            # Get bounding box
            bbox = None
            for bbox_field in ["bbox", "BoundingRectangle", "Rect", "rect"]:
                if bbox_field in node:
                    bbox = node[bbox_field]
                    break

            # Store if we have both ID and bbox (first match wins for duplicate names)
            if elem_id is not None and bbox is not None and elem_id not in rects:
                # Normalize bbox to [left, top, right, bottom]
                if isinstance(bbox, list) and len(bbox) == 4:
                    # Could be [l, t, r, b] or [l, t, w, h] - assume [l, t, r, b]
                    rects[elem_id] = [int(x) for x in bbox]
                elif isinstance(bbox, dict):
                    x = bbox.get("x", 0)
                    y = bbox.get("y", 0)
                    w = bbox.get("width", 0)
                    h = bbox.get("height", 0)
                    rects[elem_id] = [x, y, x + w, y + h]
                elif isinstance(bbox, str):
                    parts = [int(p) for p in bbox.split(",")]
                    if len(parts) == 4:
                        rects[elem_id] = parts

            # Visit children
            for child_field in ["children", "Children"]:
                children = node.get(child_field, [])
                if isinstance(children, list):
                    for child in children:
                        if isinstance(child, dict):
                            visit(child)

        if a11y_tree:
            # Handle case where a11y_tree is XML string (WAA returns XML)
            if isinstance(a11y_tree, str):
                parsed = _parse_xml_a11y_tree(a11y_tree)
                if parsed:
                    visit(parsed)
                else:
                    logger.debug("A11y tree XML could not be parsed")
                    return rects
            else:
                visit(a11y_tree)

        logger.debug(f"Extracted {len(rects)} element rects from a11y tree")
        return rects

    def _update_waa_computer(self) -> None:
        """POST current rects and screenshot to WAA's /update_computer endpoint.

        This syncs WAA's Computer object with our current element state,
        allowing computer.mouse.move_id(id) to work correctly.
        """
        import requests

        if not self._current_rects:
            logger.warning("No rects to update - skipping /update_computer")
            return

        # Encode screenshot as base64
        screenshot_b64 = ""
        if self._current_screenshot:
            screenshot_b64 = base64.b64encode(self._current_screenshot).decode("utf-8")

        # Window rect (full screen for now)
        screen_w, screen_h = self._actual_screen_size or (self.config.screen_width, self.config.screen_height)
        window_rect = [0, 0, screen_w, screen_h]

        payload = {
            "rects": self._current_rects,
            "window_rect": window_rect,
            "screenshot": screenshot_b64,
            "scale": [1.0, 1.0],
        }

        try:
            resp = requests.post(
                f"{self.config.server_url}/update_computer",
                json=payload,
                timeout=30.0
            )
            if resp.status_code == 200:
                logger.debug("Updated WAA computer with %d rects", len(self._current_rects))
            else:
                logger.warning(f"update_computer failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"update_computer request error: {e}")

    @staticmethod
    def _config_entry_to_command(entry: dict) -> str | None:
        """Translate a WAA config entry to a Python command for /execute_windows.

        Config entries have ``{"type": "<type>", "parameters": {...}}``.
        Returns a Python one-liner suitable for ``/execute_windows``, or *None*
        for types that are handled inline (e.g. ``sleep``).
        """
        entry_type = entry.get("type", "")
        params = entry.get("parameters", {})

        if entry_type == "execute":
            shell_cmd = params.get("command", "")
            if not shell_cmd:
                return None
            # Wrap shell command in subprocess so /execute_windows can run it
            escaped = shell_cmd.replace("\\", "\\\\").replace("'", "\\'")
            return (
                "import subprocess; "
                f"subprocess.run('{escaped}', shell=True, timeout=60, check=True)"
            )

        if entry_type == "launch":
            app = params.get("command", "")
            if not app:
                return None
            escaped = app.replace("\\", "\\\\").replace("'", "\\'")
            return (
                "import subprocess; "
                f"subprocess.Popen('{escaped}', shell=True)"
            )

        if entry_type == "open":
            path = params.get("path", "")
            if not path:
                return None
            escaped = path.replace("\\", "\\\\").replace("'", "\\'")
            return f"import os; os.startfile('{escaped}')"

        if entry_type == "download":
            files = params.get("files", [])
            if not files:
                return None
            # Download each file; files is a list of {url, path} dicts
            lines = ["import urllib.request, os"]
            for f in files:
                url = f.get("url", "")
                dest = f.get("path", "")
                if url and dest:
                    esc_url = url.replace("'", "\\'")
                    esc_dest = dest.replace("\\", "\\\\").replace("'", "\\'")
                    lines.append(
                        f"os.makedirs(os.path.dirname('{esc_dest}'), exist_ok=True)"
                    )
                    lines.append(
                        f"urllib.request.urlretrieve('{esc_url}', '{esc_dest}')"
                    )
            return "; ".join(lines)

        if entry_type == "activate_window":
            window_name = params.get("window_name", "")
            if not window_name:
                return None
            escaped = window_name.replace("'", "\\'")
            return (
                "import pyautogui, subprocess; "
                f"subprocess.run(['powershell', '-Command', "
                f"\"(Get-Process | Where-Object {{$_.MainWindowTitle -like '*{escaped}*'}}).MainWindowHandle | "
                f"ForEach-Object {{[void][System.Runtime.InteropServices.Marshal]::"
                f"SetForegroundWindow($_)}}\"], timeout=10)"
            )

        if entry_type == "verify_apps":
            # WAA uses both "apps" (list) and "app" (list) parameter names
            apps = params.get("apps", params.get("app", []))
            if isinstance(apps, str):
                apps = [apps]
            if not apps:
                return None
            # Use repr() for safe list serialization instead of str().replace()
            apps_repr = repr(apps)
            return (
                "import shutil; "
                f"missing = [a for a in {apps_repr} if not shutil.which(a)]; "
                "print(f'Missing apps: {missing}' if missing else 'All apps found')"
            )

        if entry_type == "update_browse_history":
            history = params.get("history", [])
            if not history:
                return None
            # Build Python script to write Chrome history entries.
            # Chrome stores timestamps as microseconds since 1601-01-01.
            history_repr = repr(history)
            return (
                "import sqlite3, os, time, subprocess, glob; "
                # Kill Chrome so the DB isn't locked
                "subprocess.run('taskkill /F /IM chrome.exe /T', "
                "shell=True, capture_output=True); "
                "time.sleep(1); "
                # Find Chrome's History DB
                "profiles = glob.glob("
                "os.path.expandvars("
                r"r'%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\History'"
                ")); "
                "db_path = profiles[0] if profiles else "
                "os.path.expandvars("
                r"r'%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\History'"
                "); "
                "conn = sqlite3.connect(db_path); "
                "c = conn.cursor(); "
                # Chrome epoch: microseconds since 1601-01-01
                # Offset from Unix epoch: 11644473600 seconds
                "CHROME_EPOCH_OFFSET = 11644473600; "
                "now_us = int((time.time() + CHROME_EPOCH_OFFSET) * 1_000_000); "
                f"history = {history_repr}; "
                "["
                "("
                "c.execute("
                "'INSERT OR IGNORE INTO urls (url, title, visit_count, "
                "typed_count, last_visit_time) VALUES (?, ?, 1, 0, ?)', "
                "(e['url'], e.get('title', ''), "
                "now_us - int(e.get('visit_time_from_now_in_seconds', 0)) * 1_000_000)"
                "), "
                "c.execute("
                "'INSERT INTO visits (url, visit_time, from_visit, transition, "
                "segment_id, visit_duration) VALUES ("
                "(SELECT id FROM urls WHERE url=?), ?, 0, 805306368, 0, 0)', "
                "(e['url'], "
                "now_us - int(e.get('visit_time_from_now_in_seconds', 0)) * 1_000_000)"
                ")"
                ") for e in history"
                "]; "
                "conn.commit(); conn.close(); "
                "print(f'Inserted {len(history)} history entries into {db_path}')"
            )

        if entry_type == "command":
            # Alias for "execute" used by some WAA tasks
            shell_cmd = params.get("command", "")
            if not shell_cmd:
                return None
            escaped = shell_cmd.replace("\\", "\\\\").replace("'", "\\'")
            return (
                "import subprocess; "
                f"subprocess.run('{escaped}', shell=True, timeout=60)"
            )

        if entry_type == "close_all":
            # Close all application windows except File Explorer
            return (
                "import subprocess; "
                "subprocess.run(['powershell', '-Command', "
                "'Get-Process | Where-Object {$_.MainWindowTitle -ne \\\"\\\"} | "
                "Where-Object {$_.ProcessName -ne \\\"explorer\\\"} | "
                "ForEach-Object { $_.CloseMainWindow() }'], "
                "timeout=15, capture_output=True); "
                "import time; time.sleep(1); "
                # Force-kill anything that didn't close gracefully
                "subprocess.run(['powershell', '-Command', "
                "'Get-Process | Where-Object {$_.MainWindowTitle -ne \\\"\\\"} | "
                "Where-Object {$_.ProcessName -ne \\\"explorer\\\"} | "
                "Stop-Process -Force'], "
                "timeout=10, capture_output=True)"
            )

        if entry_type == "create_folder":
            path = params.get("path", "")
            if not path:
                return None
            escaped = path.replace("\\", "\\\\").replace("'", "\\'")
            return f"import os; os.makedirs('{escaped}', exist_ok=True)"

        if entry_type == "create_file":
            path = params.get("path", "")
            content = params.get("content", "")
            if not path:
                return None
            escaped_path = path.replace("\\", "\\\\").replace("'", "\\'")
            escaped_content = content.replace("\\", "\\\\").replace("'", "\\'")
            return (
                "import os; "
                f"os.makedirs(os.path.dirname('{escaped_path}') or '.', exist_ok=True); "
                f"open('{escaped_path}', 'w', encoding='utf-8').write('{escaped_content}')"
            )

        if entry_type == "clear_task_files":
            return (
                "import shutil, glob, os; "
                "[shutil.rmtree(d, ignore_errors=True) "
                "for d in glob.glob(os.path.expandvars("
                r"r'%TEMP%\\task_*'"
                "))]; "
                "print('Cleared task files')"
            )

        if entry_type == "install_apps":
            # NOTE: This is a safety net, not a normal code path. The standard
            # WAA Docker image comes with all required apps pre-installed
            # (Chrome, LibreOffice, Notepad++, VLC, etc.) during image build.
            # For the 154 standard WAA tasks, install_apps should never need
            # to fire. This handler exists only for:
            #   - Custom tasks requiring non-standard software
            #   - Stripped-down or freshly-installed Windows images
            #   - Recovery after app uninstall/corruption
            apps = params.get("apps", params.get("app", []))
            if isinstance(apps, str):
                apps = [apps]
            if not apps:
                return None
            # Map common app names to winget package IDs.  Keys are
            # normalised (lowercase, underscores) to match WAA task
            # configs which use both hyphens and underscores.
            winget_ids = {
                "chrome": "Google.Chrome",
                "google_chrome": "Google.Chrome",
                "firefox": "Mozilla.Firefox",
                "libreoffice": "TheDocumentFoundation.LibreOffice",
                "libreoffice_calc": "TheDocumentFoundation.LibreOffice",
                "libreoffice_writer": "TheDocumentFoundation.LibreOffice",
                "libreoffice_impress": "TheDocumentFoundation.LibreOffice",
                "vlc": "VideoLAN.VLC",
                "vscode": "Microsoft.VisualStudioCode",
                "vs_code": "Microsoft.VisualStudioCode",
                "7zip": "7zip.7zip",
                "notepad++": "Notepad++.Notepad++",
                "notepadplusplus": "Notepad++.Notepad++",
                "gimp": "GIMP.GIMP",
                "obs": "OBSProject.OBSStudio",
                "obs_studio": "OBSProject.OBSStudio",
                "audacity": "Audacity.Audacity",
                "paint.net": "dotPDN.PaintDotNet",
            }
            # Build a Python script that installs each app via winget,
            # falling back to a winget search for unknown app names.
            # Each install is independent: failures are collected but
            # do not prevent later apps from being attempted.
            install_lines = [
                "import subprocess, re",
                "failed = []",
            ]
            seen_ids = set()
            for app in apps:
                norm = re.sub(r'[\s\-]+', '_', app.strip().lower())
                pkg_id = winget_ids.get(norm)
                if pkg_id and pkg_id in seen_ids:
                    continue  # e.g. libreoffice_calc and libreoffice_writer
                if pkg_id:
                    seen_ids.add(pkg_id)
                # Use repr for safe string embedding
                app_repr = repr(app)
                if pkg_id:
                    install_lines.append(
                        f"r = subprocess.run("
                        f"'winget install --id {pkg_id}"
                        f" --accept-package-agreements"
                        f" --accept-source-agreements"
                        f" --silent', shell=True,"
                        f" capture_output=True, text=True, timeout=300)"
                    )
                    install_lines.append(
                        f"print(f'winget install {pkg_id}: rc={{r.returncode}}')"
                    )
                    install_lines.append(
                        "print(r.stdout[-500:] if len(r.stdout) > 500"
                        " else r.stdout)"
                    )
                    # winget returns 0 on success, -1978335189 (0x8A150019)
                    # when already installed — both are fine.
                    install_lines.append(
                        f"failed.append({app_repr})"
                        f" if r.returncode not in (0, -1978335189) else None"
                    )
                else:
                    # Unknown app: try winget search and install first match
                    esc_app = app.replace("'", "\\'")
                    install_lines.append(
                        f"print('Unknown app {app_repr}, "
                        f"attempting winget search...')"
                    )
                    install_lines.append(
                        f"s = subprocess.run("
                        f"'winget search \"{esc_app}\"',"
                        f" shell=True, capture_output=True,"
                        f" text=True, timeout=30)"
                    )
                    # Parse first package ID from winget search output
                    install_lines.append(
                        "match = re.search("
                        "r'(\\S+\\.\\S+)\\s+\\S+\\s+winget', s.stdout)"
                    )
                    install_lines.append(
                        "pkg = match.group(1) if match else None"
                    )
                    install_lines.append(
                        "r2 = subprocess.run("
                        "f'winget install --id {pkg}"
                        " --accept-package-agreements"
                        " --accept-source-agreements"
                        " --silent', shell=True,"
                        " capture_output=True, text=True, timeout=300)"
                        " if pkg else None"
                    )
                    install_lines.append(
                        f"failed.append({app_repr})"
                        f" if not pkg or"
                        f" (r2 and r2.returncode not in (0, -1978335189))"
                        f" else None"
                    )
            install_lines.append(
                "print(f'install_apps done."
                " Failed: {failed}' if failed"
                " else 'install_apps: all apps installed')"
            )
            return "; ".join(install_lines)

        # Unknown type — skip
        logger.warning("Unknown config entry type %r, skipping", entry_type)
        return None

    def _run_task_setup(self, raw_config: dict) -> None:
        """Run task setup commands from raw_config.

        WAA tasks use a 'config' array of {type, parameters} objects specifying
        preconditions (file downloads, app launches, sleeps). Each entry is
        translated to a Python command and dispatched individually via
        ``/execute_windows`` on the WAA server.

        If the task has ``related_apps``, a ``verify_apps`` step is prepended so
        missing applications are caught before the rest of setup runs.

        Args:
            raw_config: Task configuration with setup commands.
        """
        config = raw_config.get("config", [])
        self._last_setup_results = []

        related_apps = raw_config.get("related_apps", [])
        if related_apps and not self.config.lightweight:
            verify_step = {
                "type": "verify_apps",
                "parameters": {"apps": related_apps},
            }
            config = [verify_step] + config

        if not config:
            return

        import requests

        errors = []
        for entry in config:
            entry_type = entry.get("type", "unknown")

            # In lightweight mode, skip non-critical setup entries that are
            # known to hang or crash the WAA server.
            if self.config.lightweight and entry_type in (
                "verify_apps",
                "close_all",
                "activate_window",
            ):
                logger.debug("Lightweight mode: skipping %s", entry_type)
                self._last_setup_results.append(
                    {"type": entry_type, "status": "skipped"}
                )
                continue

            # Sleep is handled locally, not on the server
            if entry_type == "sleep":
                duration = entry.get("parameters", {}).get("seconds", 1)
                logger.info("Setup sleep: %s seconds", duration)
                time.sleep(float(duration))
                self._last_setup_results.append(
                    {"type": "sleep", "status": "ok"}
                )
                continue

            command = self._config_entry_to_command(entry)
            if command is None:
                logger.warning("Skipping setup entry with no command: %s", entry)
                self._last_setup_results.append(
                    {"type": entry_type, "status": "skipped"}
                )
                continue

            # install_apps uses winget which can take several minutes per
            # app, so we give it a longer HTTP timeout.
            http_timeout = 600.0 if entry_type == "install_apps" else 120.0

            try:
                resp = requests.post(
                    f"{self.config.server_url}/execute_windows",
                    json={"command": command},
                    timeout=http_timeout,
                )
                if resp.status_code == 200:
                    _, stderr = self._command_response_details(resp)
                    if stderr:
                        raise ActionDeliveryUncertainError(
                            f"WAA setup command returned stderr: {stderr}"
                        )
                    self._validate_command_receipt(
                        resp,
                        context=f"setup {entry_type}",
                    )
                    logger.info("Setup %s: ok", entry_type)
                    self._last_setup_results.append(
                        {"type": entry_type, "status": "ok"}
                    )
                else:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning("Setup %s failed: %s", entry_type, err_msg)
                    self._last_setup_results.append(
                        {"type": entry_type, "status": "error", "error": err_msg}
                    )
                    errors.append(f"{entry_type}: {err_msg}")
            except Exception as e:
                logger.warning("Setup %s request failed: %s", entry_type, e)
                self._last_setup_results.append(
                    {"type": entry_type, "status": "error", "error": str(e)}
                )
                errors.append(f"{entry_type}: {e}")

        if errors:
            # Only raise for critical setup failures (download, open, execute).
            # verify_apps and activate_window timeouts are non-fatal — the task
            # may still work even if verification times out.
            critical_errors = [
                e for e in errors
                if not any(
                    e.startswith(t) for t in ("verify_apps:", "activate_window:")
                )
            ]
            if critical_errors:
                raise SetupReadinessError(
                    f"Task setup contained failing steps: {'; '.join(critical_errors)}"
                )
            logger.warning(
                "Non-critical setup issues (continuing): %s",
                "; ".join(errors),
            )

    def _run_setup_execute_commands(
        self,
        requests_module: Any,
        commands: list[str],
        *,
        label: str,
        timeout: float = 20.0,
    ) -> None:
        """Run a batch of setup execute commands via /execute_windows."""
        if not commands:
            return
        for cmd in commands:
            entry = {"type": "execute", "parameters": {"command": cmd, "shell": True}}
            py_command = self._config_entry_to_command(entry)
            if py_command is None:
                continue
            try:
                resp = requests_module.post(
                    f"{self.config.server_url}/execute_windows",
                    json={"command": py_command},
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    logger.debug("%s command ok: %s", label, cmd[:80])
                else:
                    logger.warning(
                        "%s command failed: HTTP %s %s",
                        label,
                        resp.status_code,
                        resp.text[:200],
                    )
            except Exception as e:
                logger.warning("%s command request failed: %s", label, e)
        logger.info("%s applied (%d command(s))", label, len(commands))

    def _dismiss_notifications(self, requests_module) -> None:
        """Dismiss system notifications that persist through close_all.

        OneDrive "Turn On Windows Backup" and similar toast notifications
        are not closeable via close_all (they're system toasts, not windows).
        Kill the notification processes and dismiss via keyboard.
        """
        commands = [
            "taskkill /F /IM OneDrive.exe /T",
            "taskkill /F /IM OneDriveStandaloneUpdater.exe /T",
            "taskkill /F /IM ApplicationFrameHost.exe /T",
            # Open/close action center to dismiss any currently surfaced toast.
            (
                "python -c \""
                "import pyautogui; "
                "pyautogui.hotkey('win','a'); "
                "pyautogui.press('esc')"
                "\""
            ),
        ]
        self._run_setup_execute_commands(
            requests_module,
            commands,
            label="notification cleanup",
            timeout=5.0,
        )

    def _apply_clean_desktop_policy(self, requests_module) -> None:
        """Apply deterministic desktop policy for train/eval UI parity."""
        if self._clean_desktop_applied and not self.config.reapply_clean_desktop_each_reset:
            # Keep notifications suppressed each reset even if policy is one-time.
            self._dismiss_notifications(requests_module)
            return

        commands: list[str] = []
        if self.config.clean_desktop:
            commands.extend([
                # Suppress OneDrive first-run backup prompts.
                (
                    'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\OneDrive" '
                    '/v DisableFileSyncNGSC /t REG_DWORD /d 1 /f'
                ),
                # Prevent OneDrive auto-start prompts on login.
                'reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v OneDrive /f',
                # Suppress notification toasts/popovers.
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\PushNotifications" '
                    '/v ToastEnabled /t REG_DWORD /d 0 /f'
                ),
                # Disable common Windows suggestion surfaces/popups.
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager" '
                    '/v SubscribedContent-338389Enabled /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager" '
                    '/v SubscribedContent-338388Enabled /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager" '
                    '/v SubscribedContent-353694Enabled /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
                    '/v ShowCopilotButton /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
                    '/v TaskbarDa /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
                    '/v TaskbarMn /t REG_DWORD /d 0 /f'
                ),
                # Suppress Chrome first-run dialogs (sign-in, sync, what's new).
                # Applied globally so any task that opens Chrome gets a clean UX.
                (
                    'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                    '/v BrowserSignin /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                    '/v SyncDisabled /t REG_DWORD /d 1 /f'
                ),
                (
                    'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                    '/v PromotionalTabsEnabled /t REG_DWORD /d 0 /f'
                ),
                # Create "First Run" sentinel file
                (
                    'python -c "'
                    "import os, pathlib; "
                    "p = pathlib.Path(os.path.expandvars(r'%LOCALAPPDATA%\\\\Google\\\\Chrome\\\\User Data')); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    "(p / 'First Run').touch()"
                    '"'
                ),
            ])

        if self.config.clean_desktop or self.config.force_tray_icons:
            commands.extend([
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer" '
                    '/v HideSCANetwork /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer" '
                    '/v HideSCAVolume /t REG_DWORD /d 0 /f'
                ),
                (
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer" '
                    '/v EnableAutoTray /t REG_DWORD /d 0 /f'
                ),
            ])

        if commands:
            self._run_setup_execute_commands(
                requests_module,
                commands,
                label="clean desktop policy",
                timeout=25.0,
            )

        # Keep this as a final pass to clear already displayed popups.
        self._dismiss_notifications(requests_module)
        self._environment_profile = self._collect_environment_profile()
        if self._environment_profile:
            logger.info("Desktop environment profile: %s", self._environment_profile)
        self._clean_desktop_applied = True

    def _collect_environment_profile(self) -> dict[str, Any]:
        """Capture key environment flags for reproducibility metadata."""
        script = r"""
$ErrorActionPreference='SilentlyContinue'
$os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$one = (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive' -Name DisableFileSyncNGSC).DisableFileSyncNGSC
$toast = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications' -Name ToastEnabled).ToastEnabled
$hideNet = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer' -Name HideSCANetwork).HideSCANetwork
$hideVol = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer' -Name HideSCAVolume).HideSCAVolume
$autoTray = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer' -Name EnableAutoTray).EnableAutoTray
$widgets = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name TaskbarDa).TaskbarDa
$copilot = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name ShowCopilotButton).ShowCopilotButton
$obj = [ordered]@{
  image_version = $env:WAA_IMAGE_VERSION
  os_product = $os.ProductName
  os_release = $os.DisplayVersion
  os_build = $os.CurrentBuildNumber
  one_drive_disable_sync_ngsc = $one
  toast_enabled = $toast
  hide_network_icon = $hideNet
  hide_volume_icon = $hideVol
  enable_auto_tray = $autoTray
  taskbar_widgets = $widgets
  show_copilot_button = $copilot
}
$obj | ConvertTo-Json -Compress
"""
        try:
            output = self.run_powershell(script).strip()
            if not output:
                return {}
            profile: dict[str, Any] | None = None
            for line in reversed(output.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    profile = json.loads(line)
                    break
            if profile is None:
                return {}
            profile["clean_desktop_requested"] = self.config.clean_desktop
            profile["force_tray_icons_requested"] = self.config.force_tray_icons
            if self.config.waa_image_version:
                profile["configured_image_version"] = self.config.waa_image_version
            return profile
        except Exception as e:
            logger.debug("Failed to collect environment profile: %s", e)
            return {}

    def _is_libreoffice_task(self, raw_config: dict) -> bool:
        """Return True if task setup implies LibreOffice app usage."""
        related_apps = raw_config.get("related_apps", [])
        for app in related_apps:
            canonical = self._normalize_app_name(app)
            if canonical.startswith("libreoffice"):
                return True

        config_steps = raw_config.get("config", [])
        for step in config_steps:
            if step.get("type") != "open":
                continue
            path = step.get("parameters", {}).get("path", "")
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in {
                "xlsx", "xls", "ods", "csv",
                "docx", "doc", "odt",
                "pptx", "ppt", "odp",
            }:
                return True
        return False

    def _is_chrome_task(self, raw_config: dict) -> bool:
        """Return True if task setup implies Chrome browser usage."""
        related_apps = raw_config.get("related_apps", [])
        for app in related_apps:
            canonical = self._normalize_app_name(app)
            if canonical == "chrome":
                return True

        config_steps = raw_config.get("config", [])
        for step in config_steps:
            if step.get("type") != "open":
                continue
            path = step.get("parameters", {}).get("path", "")
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in {"html", "htm", "pdf"}:
                return True
            # URLs opened via Chrome
            if path.startswith(("http://", "https://")):
                return True
        return False

    def _prepare_chrome_clean_state(self, requests_module: Any) -> None:
        """Dismiss Chrome first-run dialogs that block automation.

        On a fresh Windows VM, Chrome shows a "Sign in to Chrome" modal dialog
        on first launch.  The planner VLM can see it but UI-Venus cannot
        reliably dismiss it, wasting agent steps.  This method:

        1. Sets Chrome registry/preference flags to suppress first-run UX
        2. Sends Escape keypresses to dismiss any currently visible dialog
        3. Kills and restarts Chrome so it picks up the new preferences
        """
        commands = [
            # Disable Chrome first-run experience via registry policy
            (
                'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                '/v SuppressFirstRunBubble /t REG_DWORD /d 1 /f'
            ),
            # Mark first run as complete via registry
            (
                'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                '/v MetricsReportingEnabled /t REG_DWORD /d 0 /f'
            ),
            # Disable Chrome sign-in prompt
            (
                'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                '/v BrowserSignin /t REG_DWORD /d 0 /f'
            ),
            # Disable "What's New" page
            (
                'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                '/v PromotionalTabsEnabled /t REG_DWORD /d 0 /f'
            ),
            # Suppress sync consent screen
            (
                'reg add "HKLM\\SOFTWARE\\Policies\\Google\\Chrome" '
                '/v SyncDisabled /t REG_DWORD /d 1 /f'
            ),
            # Create "First Run" sentinel file to skip first-run flow
            (
                'python -c "'
                "import os, pathlib; "
                "p = pathlib.Path(os.path.expandvars(r'%LOCALAPPDATA%\\\\Google\\\\Chrome\\\\User Data')); "
                "p.mkdir(parents=True, exist_ok=True); "
                "(p / 'First Run').touch()"
                '"'
            ),
            # Press Escape to dismiss any currently visible Chrome dialog
            (
                'python -c "'
                "import pyautogui; "
                "pyautogui.press('escape'); "
                "import time; time.sleep(0.3); "
                "pyautogui.press('escape')"
                '"'
            ),
        ]
        self._run_setup_execute_commands(
            requests_module,
            commands,
            label="chrome first-run cleanup",
            timeout=20.0,
        )

    def _build_libreoffice_cleanup_command(self) -> str:
        """Build single-line command that clears LibreOffice recovery state."""
        payload = base64.b64encode(_LIBREOFFICE_RECOVERY_CLEANUP_SCRIPT.encode()).decode()
        return (
            "python -c \"import base64,tempfile,os,subprocess;"
            f"d=base64.b64decode('{payload}');"
            "p=os.path.join(tempfile.gettempdir(),'lo_cleanup.py');"
            "open(p,'wb').write(d);"
            "r=subprocess.run(['python',p],capture_output=True,text=True);"
            "print(r.stdout);print(r.stderr)\""
        )

    def _prepare_libreoffice_clean_state(self, requests_module: Any) -> None:
        """Kill stale LibreOffice processes and clear recovery artifacts."""
        commands = [
            "taskkill /F /IM soffice.bin /T",
            "taskkill /F /IM soffice.exe /T",
            self._build_libreoffice_cleanup_command(),
        ]
        self._run_setup_execute_commands(
            requests_module,
            commands,
            label="libreoffice recovery cleanup",
            timeout=30.0,
        )

    def _rerun_open_setup_steps(self, raw_config: dict, requests_module: Any) -> bool:
        """Re-run only open steps from task config as remediation."""
        open_steps = [s for s in raw_config.get("config", []) if s.get("type") == "open"]
        if not open_steps:
            return False
        self._last_setup_results = []
        all_ok = True
        for step in open_steps:
            py_command = self._config_entry_to_command(step)
            if py_command is None:
                continue
            try:
                resp = requests_module.post(
                    f"{self.config.server_url}/execute_windows",
                    json={"command": py_command},
                    timeout=60.0,
                )
                if resp.status_code == 200:
                    self._last_setup_results.append(
                        {"type": "open", "status": "ok"}
                    )
                else:
                    self._last_setup_results.append({
                        "type": "open",
                        "status": "error",
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    })
                    all_ok = False
            except Exception as e:
                self._last_setup_results.append({
                    "type": "open",
                    "status": "error",
                    "error": str(e),
                })
                all_ok = False
        if all_ok:
            logger.info("Open-step remediation succeeded (%d step(s))", len(open_steps))
        else:
            logger.warning("Open-step remediation had failures")
        return all_ok

    # --- App-name to window-title mapping for post-setup focus ---

    # Maps canonical app names (from related_apps / _normalize_app_name) to
    # substrings that should appear in the window title when the app is open.
    # The first match in the list wins.  These are case-insensitive substrings.
    _APP_WINDOW_PATTERNS: dict[str, list[str]] = {
        "libreoffice_calc": ["LibreOffice Calc", "Calc"],
        "libreoffice_writer": ["LibreOffice Writer", "Writer"],
        "libreoffice_impress": ["LibreOffice Impress", "Impress"],
        "libreoffice": ["LibreOffice"],
        "notepad": ["Notepad"],
        "chrome": ["Google Chrome", "Chrome"],
        "vs_code": ["Visual Studio Code", "VS Code"],
        "vlc": ["VLC media player", "VLC"],
        "file_explorer": ["File Explorer", "Explorer"],
        "paint": ["Paint"],
        "word": ["Word"],
        "excel": ["Excel"],
        "powerpoint": ["PowerPoint"],
    }

    @staticmethod
    def _normalize_app_name(name: str) -> str:
        """Canonicalize app names (mirrors evaluate_server._normalize_app_name)."""
        import re as _re

        key = _re.sub(r"[\s\-]+", "_", name.strip().lower())
        aliases = {
            "vscode": "vs_code",
            "vs_code": "vs_code",
            "libreoffice": "libreoffice_calc",
        }
        return aliases.get(key, key)

    def _get_expected_window_patterns(self, raw_config: dict) -> list[str]:
        """Determine window title substrings expected after task setup.

        Examines ``related_apps`` (primary signal) and the ``config`` array
        (looks for ``open`` steps whose file extension implies an app) to
        build a list of case-insensitive substrings that the foreground
        window title should contain.

        Args:
            raw_config: Task configuration dict.

        Returns:
            List of window-title substrings to search for, ordered by
            priority (most specific first).  Empty if nothing can be
            inferred.
        """
        patterns: list[str] = []

        # 1. related_apps is the most direct signal
        related_apps = raw_config.get("related_apps", [])
        for app in related_apps:
            canonical = self._normalize_app_name(app)
            app_patterns = self._APP_WINDOW_PATTERNS.get(canonical, [])
            patterns.extend(app_patterns)

        # 2. Infer from "open" steps in the config array
        config_steps = raw_config.get("config", [])
        for step in config_steps:
            if step.get("type") == "open":
                path = step.get("parameters", {}).get("path", "")
                if not path:
                    continue
                ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                ext_app_map = {
                    "xlsx": "libreoffice_calc",
                    "xls": "libreoffice_calc",
                    "ods": "libreoffice_calc",
                    "csv": "libreoffice_calc",
                    "docx": "libreoffice_writer",
                    "doc": "libreoffice_writer",
                    "odt": "libreoffice_writer",
                    "pptx": "libreoffice_impress",
                    "ppt": "libreoffice_impress",
                    "odp": "libreoffice_impress",
                    "txt": "notepad",
                    "html": "chrome",
                    "htm": "chrome",
                    "pdf": "chrome",
                }
                if ext in ext_app_map:
                    canonical = ext_app_map[ext]
                    app_patterns = self._APP_WINDOW_PATTERNS.get(canonical, [])
                    for p in app_patterns:
                        if p not in patterns:
                            patterns.extend(app_patterns)
                            break

                # Also add the filename itself as a fallback pattern,
                # since many apps put the filename in the title bar
                import os
                filename = os.path.basename(path)
                if filename and filename not in patterns:
                    patterns.append(filename)

        return patterns

    def _ensure_app_focused(self, raw_config: dict) -> bool:
        """Ensure target app is foregrounded after setup.

        Runs deterministic focus checks/remediation before step 0:
        - activate expected windows by title pattern
        - for LibreOffice tasks, clear recovery state and re-run open steps

        Returns:
            True if app focus was verified, False otherwise.
        """
        patterns = self._get_expected_window_patterns(raw_config)
        if not patterns:
            logger.info("No expected window patterns; skipping post-setup focus")
            return True

        logger.info(
            "Post-setup focus: looking for windows matching %r", patterns
        )

        import requests

        if self._try_activate_patterns(patterns, requests):
            return True

        # App-specific deterministic remediation path.
        if self._is_libreoffice_task(raw_config):
            logger.warning(
                "Post-setup focus failed for LibreOffice task. Applying recovery cleanup + reopen remediation."
            )
            self._prepare_libreoffice_clean_state(requests)
            self._rerun_open_setup_steps(raw_config, requests)
            time.sleep(2.0)
            if self._try_activate_patterns(patterns, requests):
                return True

        if self._is_chrome_task(raw_config):
            logger.warning(
                "Post-setup focus failed for Chrome task. Dismissing first-run dialogs + reopen remediation."
            )
            self._prepare_chrome_clean_state(requests)
            self._rerun_open_setup_steps(raw_config, requests)
            time.sleep(2.0)
            if self._try_activate_patterns(patterns, requests):
                return True

        logger.warning(
            "Post-setup focus: could not confirm target app is in foreground "
            "after deterministic remediation."
        )
        diagnostics = self._capture_focus_diagnostics(patterns)
        if diagnostics:
            logger.warning("Post-setup focus diagnostics: %s", diagnostics)
        return False

    def _capture_focus_diagnostics(self, patterns: list[str]) -> dict[str, Any]:
        """Capture foreground/window state and last open-step setup result."""
        diagnostics: dict[str, Any] = {
            "expected_patterns": patterns,
            "last_foreground_title": self._last_foreground_title,
        }

        open_results = [
            r for r in self._last_setup_results
            if isinstance(r, dict) and r.get("type") == "open"
        ]
        if open_results:
            diagnostics["last_open_setup_result"] = open_results[-1]

        script = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class OAWinApi {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  public static string GetForegroundTitle() {
    var sb = new StringBuilder(512);
    var h = GetForegroundWindow();
    GetWindowText(h, sb, sb.Capacity);
    return sb.ToString();
  }
}
"@
$fg = [OAWinApi]::GetForegroundTitle()
$wins = Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowTitle.Trim() -ne '' } | Select-Object -First 25 ProcessName, MainWindowTitle
[pscustomobject]@{
  foreground_title = $fg
  windows = $wins
} | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            output = self.run_powershell(script).strip()
            parsed_snapshot = False
            for line in reversed(output.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    diagnostics["window_snapshot"] = json.loads(line)
                    parsed_snapshot = True
                    break
            if not parsed_snapshot and output:
                diagnostics["window_snapshot_raw"] = output[-1000:]
        except Exception as e:
            diagnostics["window_snapshot_error"] = str(e)

        return diagnostics

    def _try_activate_patterns(
        self,
        patterns: list[str],
        requests_module: Any,
    ) -> bool:
        """Try to activate expected windows and verify active title."""
        max_retries = max(1, self.config.setup_readiness_retries)
        retry_delays = [1.5, 2.5, 4.0, 6.0]
        for attempt in range(max_retries):
            for pattern in patterns:
                entry = {
                    "type": "activate_window",
                    "parameters": {"window_name": pattern},
                }
                py_command = self._config_entry_to_command(entry)
                if py_command is None:
                    continue
                try:
                    resp = requests_module.post(
                        f"{self.config.server_url}/execute_windows",
                        json={"command": py_command},
                        timeout=15.0,
                    )
                    if resp.status_code == 200:
                        time.sleep(0.5)
                        if self._check_foreground_dispatch(patterns, requests_module):
                            logger.info(
                                "Post-setup focus: activated '%s' on attempt %d",
                                pattern,
                                attempt + 1,
                            )
                            return True
                except Exception as e:
                    logger.debug(
                        "Post-setup focus: activate_window('%s') failed: %s",
                        pattern,
                        e,
                    )

            if attempt < max_retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.info(
                    "Post-setup focus: window not found on attempt %d; "
                    "waiting %.1fs before retry...",
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
        return False

    # Known-bad foreground window titles that indicate the app is not ready.
    _BAD_FOREGROUND_TITLES = [
        "document recovery",
        "libreoffice start center",
    ]

    def _check_foreground_win32(self, patterns: list[str]) -> bool:
        """Check foreground window title using Win32 API (fast, reliable).

        Runs a minimal PowerShell script that calls ``GetForegroundWindow()``
        and ``GetWindowText()`` via P/Invoke to retrieve the current foreground
        window title, then checks whether it contains any of the expected
        patterns (case-insensitive).

        Args:
            patterns: Window title substrings to match (case-insensitive).

        Returns:
            True if the foreground window title contains any of the patterns.
        """
        script = r"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class FgWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  public static string GetTitle() {
    var sb = new StringBuilder(512);
    GetWindowText(GetForegroundWindow(), sb, sb.Capacity);
    return sb.ToString();
  }
}
"@
[FgWin]::GetTitle()
"""
        try:
            output = self.run_powershell(script).strip()
            # Take the last non-empty line (PowerShell may emit warnings before).
            title = ""
            for line in reversed(output.splitlines()):
                line = line.strip()
                if line:
                    title = line
                    break

            self._last_foreground_title = title

            # Detect known-bad foreground states.
            title_lower = title.lower()
            if not title:
                logger.warning(
                    "Win32 foreground check: window title is empty/blank"
                )
                return False
            for bad in self._BAD_FOREGROUND_TITLES:
                if bad in title_lower:
                    logger.warning(
                        "Win32 foreground check: detected known-bad title '%s'",
                        title[:120],
                    )
                    return False

            for pattern in patterns:
                if pattern.lower() in title_lower:
                    logger.debug(
                        "Win32 foreground check: matched '%s' in '%s'",
                        pattern,
                        title[:100],
                    )
                    return True
            logger.debug(
                "Win32 foreground check: no pattern matched in '%s'",
                title[:100],
            )
        except Exception as e:
            logger.debug("Win32 foreground check failed: %s", e)
        return False

    def _check_foreground_dispatch(
        self,
        patterns: list[str],
        requests_module: Any,
    ) -> bool:
        """Dispatch foreground check based on configured method.

        Args:
            patterns: Window title substrings to match (case-insensitive).
            requests_module: The ``requests`` module (needed for a11y fallback).

        Returns:
            True if the foreground window matches any pattern.
        """
        method = self.config.focus_check_method

        if method == "win32":
            return self._check_foreground_win32(patterns)
        elif method == "a11y":
            return self._check_foreground_matches(patterns, requests_module)
        elif method == "both":
            # Try fast Win32 first; fall back to a11y if it fails.
            result = self._check_foreground_win32(patterns)
            if result:
                return True
            logger.debug(
                "Win32 foreground check negative; falling back to a11y"
            )
            return self._check_foreground_matches(patterns, requests_module)
        else:
            logger.warning(
                "Unknown focus_check_method '%s'; defaulting to win32",
                method,
            )
            return self._check_foreground_win32(patterns)

    def _check_foreground_matches(
        self,
        patterns: list[str],
        requests_module: Any,
    ) -> bool:
        """Check whether the current foreground window matches any pattern.

        Uses the accessibility endpoint to infer the current top-level window
        title without injecting additional foreground commands into the VM.

        Args:
            patterns: Window title substrings to match (case-insensitive).
            requests_module: The ``requests`` module (avoids re-import).

        Returns:
            True if the foreground window title contains any of the patterns.
        """
        try:
            resp = requests_module.get(
                f"{self.config.server_url}/accessibility",
                params={"backend": self.config.a11y_backend},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return False
            result = resp.json()
            a11y_tree = result.get("AT", result) if isinstance(result, dict) else result
            if isinstance(a11y_tree, str):
                parsed = _parse_xml_a11y_tree(a11y_tree)
                if parsed:
                    a11y_tree = parsed
            title = (self._extract_window_title(a11y_tree) or "").strip()
            if not title:
                return False
            self._last_foreground_title = title
            title_lower = title.lower()
            for pattern in patterns:
                if pattern.lower() in title_lower:
                    logger.debug(
                        "Foreground check: matched '%s' in '%s'",
                        pattern,
                        title[:100],
                    )
                    return True
            logger.debug("Foreground check: no pattern matched in '%s'", title[:100])
        except Exception as e:
            logger.debug("Foreground check via accessibility failed: %s", e)
        return False

    def _clamp_pixel_coords(self, x: int, y: int) -> tuple[int, int]:
        """Validate pixel coordinates without moving the requested target.

        PyAutoGUI reserves the four exact screen corners for its fail-safe.
        Refuse those targets instead of shifting a valid target to a different
        control. Other edge coordinates remain unchanged.

        Returns:
            The unchanged validated ``(x, y)`` tuple.
        """
        screen_w, screen_h = self._require_pointer_context()
        if x < 0 or x >= screen_w or y < 0 or y >= screen_h:
            raise ActionExecutionError(
                f"WAA coordinates ({x}, {y}) are outside the "
                f"{screen_w}x{screen_h} viewport"
            )
        if (x, y) in {
            (0, 0),
            (0, screen_h - 1),
            (screen_w - 1, 0),
            (screen_w - 1, screen_h - 1),
        }:
            raise ActionExecutionError(
                f"WAA coordinates ({x}, {y}) are reserved for the input fail-safe"
            )
        return x, y

    def _translate_action(self, action: BenchmarkAction) -> str | None:
        """Translate BenchmarkAction to element-based command for WAA's Computer.

        Uses WAA's Computer class via /execute_windows endpoint. Click actions
        use computer.mouse.move_id(id) for element-based grounding - the actual
        coordinates are resolved by WAA's Computer class using the rects we
        POSTed to /update_computer.

        Args:
            action: The action to translate.

        Returns:
            Python command string to execute via /execute_windows endpoint,
            or None for actions that don't need execution.
        """
        if action.type in ("done", "error"):
            return None

        if action.type == "wait":
            return "import time; time.sleep(1)"

        if action.type == "click":
            return self._translate_click_action(action, "single_click")

        if action.type == "double_click":
            return self._translate_click_action(action, "double_click")

        if action.type == "right_click":
            return self._translate_click_action(action, "right_click")

        if action.type == "type":
            if action.text is None:
                raise ActionExecutionError("WAA 'type' action requires text")
            text = action.text
            type_body = _build_type_commands(text)
            # If target_node_id is set (from type_element), click element first to focus it
            if action.target_node_id is not None:
                elem_id = str(action.target_node_id)
                if elem_id in self._current_rects:
                    rect = self._current_rects[elem_id]
                    cx = (rect[0] + rect[2]) // 2
                    cy = (rect[1] + rect[3]) // 2
                    cx, cy = self._clamp_pixel_coords(cx, cy)
                else:
                    raise ActionExecutionError(
                        f"WAA element {elem_id!r} is stale; recorded coordinates "
                        "are not authorized as current type evidence"
                    )
                return (
                    f"import pyautogui; import time; "
                    f"pyautogui.click({cx}, {cy}); "
                    f"time.sleep(0.2); "
                    f"{type_body}"
                )
            return f"import pyautogui; {type_body}"

        if action.type == "key":
            if not action.key:
                raise ActionExecutionError("WAA 'key' action requires key")
            key = action.key
            modifiers = action.modifiers or []

            # Handle modifier+key combos (Ctrl+A, Alt+F4, etc.)
            if modifiers:
                all_keys = [m.lower() for m in modifiers] + [key.lower()]
                args = ", ".join(f"'{k}'" for k in all_keys)
                return f"import pyautogui; pyautogui.hotkey({args})"

            # Handle "ctrl+a" style strings (legacy format)
            if "+" in key:
                parts = [p.strip().lower() for p in key.split("+")]
                args = ", ".join(f"'{p}'" for p in parts)
                return f"import pyautogui; pyautogui.hotkey({args})"

            # Single key press
            return f"import pyautogui; pyautogui.press('{key.lower()}')"

        if action.type == "scroll":
            direction = action.scroll_direction
            if direction not in ("up", "down"):
                raise ActionExecutionError("WAA scroll direction must be 'up' or 'down'")
            # pyautogui.scroll(clicks) - positive is up, negative is down
            clicks = 3 if direction == "up" else -3
            return f"import pyautogui; pyautogui.scroll({clicks})"

        if action.type == "drag":
            # Get start position -- skip drag entirely if no coordinates
            start_x, start_y = None, None
            if action.target_node_id is not None:
                elem_id = str(action.target_node_id)
                if elem_id in self._current_rects:
                    rect = self._current_rects[elem_id]
                    start_x = (rect[0] + rect[2]) // 2
                    start_y = (rect[1] + rect[3]) // 2
                else:
                    raise ActionExecutionError(
                        f"WAA element {elem_id!r} is stale; recorded coordinates "
                        "are not authorized as current drag evidence"
                    )
            if start_x is None and action.x is not None and action.y is not None:
                screen_w, screen_h = self._actual_screen_size or (
                    self.config.screen_width,
                    self.config.screen_height,
                )
                start_x = (
                    action.x
                    if not isinstance(action.x, float) or action.x > 1
                    else _fraction_to_pixel(action.x, screen_w, "x")
                )
                start_y = (
                    action.y
                    if not isinstance(action.y, float) or action.y > 1
                    else _fraction_to_pixel(action.y, screen_h, "y")
                )

            # Get end position
            screen_w, screen_h = self._actual_screen_size or (
                self.config.screen_width,
                self.config.screen_height,
            )
            end_x = action.end_x
            end_y = action.end_y
            if end_x is not None and isinstance(end_x, float) and 0 <= end_x <= 1:
                end_x = _fraction_to_pixel(end_x, screen_w, "end_x")
            if end_y is not None and isinstance(end_y, float) and 0 <= end_y <= 1:
                end_y = _fraction_to_pixel(end_y, screen_h, "end_y")

            # Skip drag if coordinates are missing or both are at origin
            if start_x is None or end_x is None or end_y is None:
                raise ActionExecutionError("WAA drag action requires start and end coordinates")
            if int(start_x) == 0 and int(start_y) == 0 and int(end_x) == 0 and int(end_y) == 0:
                raise ActionExecutionError("WAA drag action cannot use all-zero coordinates")

            # Clamp to safe margin
            try:
                start_x, start_y = self._clamp_pixel_coords(
                    int(start_x), int(start_y)
                )
                end_x, end_y = self._clamp_pixel_coords(int(end_x), int(end_y))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ActionExecutionError(
                    "WAA drag action has invalid or out-of-viewport coordinates"
                ) from exc

            return (
                f"import pyautogui; import time; "
                f"pyautogui.moveTo({start_x}, {start_y}); "
                f"time.sleep(0.3); "
                f"pyautogui.mouseDown(button='left'); "
                f"time.sleep(0.1); "
                f"pyautogui.moveTo({end_x}, {end_y}, duration=1.0); "
                f"time.sleep(0.1); "
                f"pyautogui.mouseUp(button='left')"
            )

        raise ActionExecutionError(f"Unsupported WAA action type: {action.type!r}")

    def _translate_click_action(self, action: BenchmarkAction, click_method: str) -> str:
        """Translate click-type action to pyautogui command.

        Args:
            action: The click action.
            click_method: "single_click", "double_click", or "right_click".

        Returns:
            Python command string using pyautogui for direct control.
        """
        # Map click method to pyautogui function
        pyautogui_method = {
            "single_click": "click",
            "double_click": "doubleClick",
            "right_click": "rightClick",
        }.get(click_method, "click")

        # Prefer element ID for grounding (SoM mode)
        if action.target_node_id is not None:
            elem_id = str(action.target_node_id)
            if elem_id in self._current_rects:
                # Get center of element bounding box
                rect = self._current_rects[elem_id]
                cx = (rect[0] + rect[2]) // 2
                cy = (rect[1] + rect[3]) // 2
                cx, cy = self._clamp_pixel_coords(cx, cy)
                return f"import pyautogui; pyautogui.{pyautogui_method}({cx}, {cy})"
            else:
                raise ActionExecutionError(
                    f"WAA element {elem_id!r} is stale; recorded coordinates "
                    "are not authorized as current click evidence"
                )

        # Fallback: use coordinates if provided
        if action.x is None or action.y is None:
            raise ActionExecutionError("WAA click action requires x and y coordinates")
        x = action.x
        y = action.y

        # Convert normalized coordinates to pixels
        screen_w, screen_h = self._actual_screen_size or (
            self.config.screen_width,
            self.config.screen_height,
        )
        if isinstance(x, float) and 0 <= x <= 1:
            x = _fraction_to_pixel(x, screen_w, "x")
        if isinstance(y, float) and 0 <= y <= 1:
            y = _fraction_to_pixel(y, screen_h, "y")

        try:
            x, y = self._clamp_pixel_coords(int(x), int(y))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ActionExecutionError(
                "WAA click action has invalid or out-of-viewport coordinates"
            ) from exc
        return f"import pyautogui; pyautogui.{pyautogui_method}({x}, {y})"

    def _translate_key_action(self, action: BenchmarkAction) -> str:
        """Translate key press action using pyautogui (no grounding needed)."""
        key = action.key or ""

        # Map common key names to pyautogui names
        key_map = {
            "Enter": "enter",
            "Return": "enter",
            "Tab": "tab",
            "Escape": "escape",
            "Esc": "escape",
            "Backspace": "backspace",
            "Delete": "delete",
            "Del": "delete",
            "Space": "space",
            "Up": "up",
            "Down": "down",
            "Left": "left",
            "Right": "right",
            "Home": "home",
            "End": "end",
            "PageUp": "pageup",
            "PageDown": "pagedown",
            "F1": "f1",
            "F2": "f2",
            "F3": "f3",
            "F4": "f4",
            "F5": "f5",
            "F6": "f6",
            "F7": "f7",
            "F8": "f8",
            "F9": "f9",
            "F10": "f10",
            "F11": "f11",
            "F12": "f12",
        }
        key = key_map.get(key, key.lower())

        # Handle modifiers with hotkey
        if action.modifiers:
            mods = [m.lower() for m in action.modifiers]
            mod_map = {"control": "ctrl", "command": "win", "meta": "win"}
            mods = [mod_map.get(m, m) for m in mods]
            all_keys = mods + [key]
            keys_str = ", ".join(f"'{k}'" for k in all_keys)
            return f"import pyautogui; pyautogui.hotkey({keys_str})"

        return f"import pyautogui; pyautogui.press('{key}')"
