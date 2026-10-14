"""Local desktop adapter for benchmark evaluation.

This adapter runs on the LOCAL machine using `mss` for screenshots and
`pynput` for mouse/keyboard input. No VM or remote server is required.

Platform notes:
- macOS: Requires Accessibility permission for pynput. mss returns
  physical (Retina) pixels; pynput uses logical points. The adapter
  handles the coordinate scaling automatically.
- Linux/Windows: Coordinates are typically 1:1 between mss and pynput.

Example:
    ```python
    from openadapt_evals.adapters.local import LocalAdapter

    with LocalAdapter(action_delay=0.5) as adapter:
        task = adapter.list_tasks()[0]
        obs = adapter.reset(task)
        obs, done, info = adapter.step(BenchmarkAction(type="click", x=100, y=200))
    ```
"""

from __future__ import annotations

import io
import logging
import math
import platform
import time
from typing import Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.errors import (
    ActionDeliveredObservationError,
    ActionDeliveryUncertainError,
    ActionExecutionError,
)

logger = logging.getLogger(__name__)


def _get_input_monitor_geometry(
    monitor_index: int,
    capture_monitor: dict[str, int],
) -> dict[str, float]:
    """Return the selected monitor bounds in the input controller's space."""
    if platform.system() != "Darwin":
        return {
            name: float(capture_monitor[name])
            for name in ("left", "top", "width", "height")
        }

    if monitor_index <= 0:
        raise ActionExecutionError(
            "Local pointer actions cannot safely use the combined macOS monitor capture"
        )

    try:
        import Quartz  # type: ignore[import-not-found]

        error, display_ids, display_count = Quartz.CGGetActiveDisplayList(
            32, None, None
        )
        if error != Quartz.kCGErrorSuccess:
            raise RuntimeError(f"CGGetActiveDisplayList returned {error}")
        active_displays = list(display_ids)[: int(display_count)]
        display_id = active_displays[monitor_index - 1]
        rotation = float(Quartz.CGDisplayRotation(display_id)) % 360.0
        if not math.isclose(rotation, 0.0, abs_tol=1e-6):
            raise RuntimeError(
                f"rotated macOS display ({rotation:g} degrees) is not supported"
            )
        bounds = Quartz.CGDisplayBounds(display_id)
        logical = {
            "left": float(bounds.origin.x),
            "top": float(bounds.origin.y),
            "width": float(bounds.size.width),
            "height": float(bounds.size.height),
        }

        # MSS and Quartz both enumerate CGGetActiveDisplayList in this order.
        # Check that the selected capture still resembles either the logical
        # or backing-pixel size. This rejects a stale or incompatible mapping.
        capture_size = (
            int(capture_monitor["width"]),
            int(capture_monitor["height"]),
        )
        logical_size = (round(logical["width"]), round(logical["height"]))
        backing_size = (
            int(Quartz.CGDisplayPixelsWide(display_id)),
            int(Quartz.CGDisplayPixelsHigh(display_id)),
        )
        valid_sizes = {
            logical_size,
            backing_size,
        }
        if capture_size not in valid_sizes:
            raise RuntimeError(
                f"capture size {capture_size!r} does not match selected display "
                f"logical/backing sizes {logical_size!r}/{backing_size!r}"
            )
        return logical
    except Exception as exc:
        raise ActionExecutionError(
            "Local capture could not bind the selected macOS display to input geometry"
        ) from exc


class LocalAdapter(BenchmarkAdapter):
    """Adapter for local desktop automation. No VM required.

    Uses ``mss`` for screen capture and ``pynput`` for mouse/keyboard
    control. Coordinates passed to :meth:`step` are expected in **pixel**
    units matching the screenshot resolution (physical pixels). The adapter
    converts to logical points internally when needed (macOS Retina).

    Args:
        action_delay: Seconds to wait after each action (default 0.5).
        monitor_index: Which monitor to capture (1 = primary).
    """

    def __init__(
        self,
        action_delay: float = 0.5,
        monitor_index: int = 1,
    ):
        self._action_delay = action_delay
        self._monitor_index = monitor_index
        self._current_task: BenchmarkTask | None = None
        self._step_count = 0
        self._last_viewport: tuple[int, int] | None = None
        self._capture_origin: tuple[float, float] | None = None
        self._capture_scale: tuple[float, float] | None = None
        self._capture_pixel_size: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    # BenchmarkAdapter properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "local"

    @property
    def benchmark_type(self) -> str:
        return "interactive"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_capture_geometry(
        self,
        capture_monitor: dict[str, int],
        pixel_size: tuple[int, int],
        input_monitor: dict[str, float] | None = None,
    ) -> None:
        """Bind screenshot pixels to the captured monitor's global coordinates.

        Screenshot bounds and input-controller bounds are independent on
        platforms such as macOS Retina. The scale comes from the exact image
        and the selected display's OS input bounds, never from the primary
        display or from the MSS dimensions alone.
        """
        monitor = input_monitor or {
            name: float(capture_monitor[name])
            for name in ("left", "top", "width", "height")
        }
        try:
            left = float(monitor["left"])
            top = float(monitor["top"])
            logical_width = float(monitor["width"])
            logical_height = float(monitor["height"])
            pixel_width = int(pixel_size[0])
            pixel_height = int(pixel_size[1])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ActionExecutionError(
                "Local capture did not provide usable monitor geometry"
            ) from exc

        values = (left, top, logical_width, logical_height)
        if not all(math.isfinite(value) for value in values):
            raise ActionExecutionError(
                "Local capture provided non-finite monitor geometry"
            )
        if logical_width <= 0 or logical_height <= 0:
            raise ActionExecutionError(
                "Local capture provided non-positive monitor dimensions"
            )
        if pixel_width <= 0 or pixel_height <= 0:
            raise ActionExecutionError(
                "Local capture provided non-positive screenshot dimensions"
            )

        scale_x = pixel_width / logical_width
        scale_y = pixel_height / logical_height
        if not math.isfinite(scale_x) or not math.isfinite(scale_y):
            raise ActionExecutionError("Local capture scale is not finite")

        self._capture_origin = (left, top)
        self._capture_scale = (scale_x, scale_y)
        self._capture_pixel_size = (pixel_width, pixel_height)
        self._last_viewport = self._capture_pixel_size

    def _require_capture_geometry(self) -> None:
        if (
            self._capture_origin is None
            or self._capture_scale is None
            or self._capture_pixel_size is None
            or self._last_viewport != self._capture_pixel_size
        ):
            raise ActionExecutionError(
                "Local pointer action requires fresh, matching capture geometry"
            )

    def _to_logical(self, x: float, y: float) -> tuple[float, float]:
        """Map monitor-relative screenshot pixels to global input coordinates."""
        self._require_capture_geometry()
        assert self._capture_origin is not None
        assert self._capture_scale is not None
        origin_x, origin_y = self._capture_origin
        scale_x, scale_y = self._capture_scale
        return origin_x + x / scale_x, origin_y + y / scale_y

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(self) -> BenchmarkObservation:
        """Capture a screenshot of the local screen.

        Returns:
            :class:`BenchmarkObservation` with PNG screenshot bytes and
            viewport dimensions in physical pixels.
        """
        import mss  # type: ignore[import-untyped]

        with mss.mss() as sct:
            monitor = sct.monitors[self._monitor_index]
            img = sct.grab(monitor)

            # Convert BGRA raw data to PNG via Pillow
            from PIL import Image  # type: ignore[import-untyped]

            pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            pixel_size = (int(img.size.width), int(img.size.height))
            input_monitor = _get_input_monitor_geometry(
                self._monitor_index,
                monitor,
            )
            self._record_capture_geometry(monitor, pixel_size, input_monitor)
            return BenchmarkObservation(
                screenshot=png_bytes,
                viewport=self._last_viewport,
            )

    # ------------------------------------------------------------------
    # BenchmarkAdapter interface
    # ------------------------------------------------------------------

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        """Return an empty task list (local adapter has no predefined tasks)."""
        return []

    def load_task(self, task_id: str) -> BenchmarkTask:
        """Load a task by ID.

        For the local adapter, callers construct tasks directly; this
        method exists only for interface compliance.

        Raises:
            KeyError: Always, since local adapter has no task registry.
        """
        raise KeyError(
            f"LocalAdapter has no task registry. "
            f"Construct a BenchmarkTask directly instead of loading '{task_id}'."
        )

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Reset for a new task.

        On the local machine the user is responsible for setting up the
        initial application state. This method simply records the task
        and takes a fresh screenshot.

        Args:
            task: The task to run.

        Returns:
            Initial observation (screenshot of current screen).
        """
        # A failed fresh observation must invalidate the prior task and its
        # coordinate transform before any later input can be delivered.
        self._current_task = None
        self._step_count = 0
        self._last_viewport = None
        self._capture_origin = None
        self._capture_scale = None
        self._capture_pixel_size = None
        logger.info("LocalAdapter reset for task: %s", task.task_id)
        try:
            observation = self.observe()
        except Exception:
            self._last_viewport = None
            self._capture_origin = None
            self._capture_scale = None
            self._capture_pixel_size = None
            raise
        self._current_task = task
        return observation

    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute an action on the local machine and return a new observation.

        Supported action types:
        - ``click``: Move mouse to (x, y) and click.
        - ``double_click``: Move mouse to (x, y) and double-click.
        - ``right_click``: Move mouse to (x, y) and right-click.
        - ``type``: Type text string.
        - ``key``: Press a single key, optionally with modifiers.
        - ``scroll``: Scroll in a direction.
        - ``drag``: Drag from (x, y) to (end_x, end_y).
        - ``done``: Signal task completion (no-op).

        Args:
            action: The action to execute.

        Returns:
            Tuple of (observation, done, info).
        """
        if self._current_task is None and action.type not in {"done", "error"}:
            raise ActionExecutionError("Call reset() before a local action")

        self._validate_action(action)
        done = action.type in ("done", "error")

        try:
            self._execute_action(action)
        except ActionExecutionError:
            raise
        except Exception as e:
            logger.error("Failed to execute action %s: %s", action.type, e)
            raise ActionDeliveryUncertainError(
                f"Failed to execute local action {action.type!r}: {e}"
            ) from e

        self._step_count += 1
        if self._action_delay > 0 and not done:
            time.sleep(self._action_delay)

        try:
            obs = self.observe()
        except Exception as exc:
            raise ActionDeliveredObservationError(
                f"Local action {action.type!r} was delivered, but observation failed: {exc}"
            ) from exc
        return obs, done, {"step": self._step_count}

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Placeholder evaluation.

        Local adapter does not have built-in evaluation logic. Use
        task-specific evaluators or a VLM judge externally.

        Returns:
            :class:`BenchmarkResult` with score 0.0 and a note that
            evaluation is not implemented for the local adapter.
        """
        return BenchmarkResult(
            task_id=task.task_id,
            success=False,
            score=0.0,
            num_steps=self._step_count,
            error_type="evaluation",
            reason="LocalAdapter does not implement built-in evaluation. "
            "Use an external evaluator or VLM judge.",
        )

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _validate_coordinate(
        self, value: float | None, field: str, action_type: str
    ) -> None:
        if value is None:
            raise ActionExecutionError(
                f"Local {action_type!r} action requires {field}"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ActionExecutionError(
                f"Local {action_type!r} action requires numeric {field}"
            )
        if not math.isfinite(float(value)):
            raise ActionExecutionError(
                f"Local {action_type!r} action requires finite {field}"
            )
        if value < 0:
            raise ActionExecutionError(
                f"Local {action_type!r} action requires non-negative {field}"
            )
        self._require_capture_geometry()
        assert self._last_viewport is not None
        width, height = self._last_viewport
        limit = width if field in {"x", "end_x"} else height
        if value >= limit:
            raise ActionExecutionError(
                f"Local {action_type!r} action {field} is outside the captured viewport"
            )

    def _validate_action(self, action: BenchmarkAction) -> None:
        """Reject incomplete actions before input delivery or step accounting."""
        supported = {
            "click",
            "double_click",
            "right_click",
            "type",
            "key",
            "scroll",
            "drag",
            "done",
            "wait",
            "error",
        }
        if action.type not in supported:
            raise ActionExecutionError(f"Unsupported local action type: {action.type!r}")

        if action.type in {"click", "double_click", "right_click"}:
            self._validate_coordinate(action.x, "x", action.type)
            self._validate_coordinate(action.y, "y", action.type)
        elif action.type == "type" and action.text is None:
            raise ActionExecutionError("Local 'type' action requires text")
        elif action.type == "key" and not action.key:
            raise ActionExecutionError("Local 'key' action requires key")
        elif action.type == "scroll":
            if action.scroll_direction not in {"up", "down", "left", "right"}:
                raise ActionExecutionError(
                    "Local 'scroll' action requires direction up, down, left, or right"
                )
            if action.scroll_amount is not None:
                if (
                    isinstance(action.scroll_amount, bool)
                    or not isinstance(action.scroll_amount, (int, float))
                    or not math.isfinite(float(action.scroll_amount))
                    or action.scroll_amount <= 0
                ):
                    raise ActionExecutionError(
                        "Local 'scroll' action requires a positive finite amount"
                    )
        elif action.type == "drag":
            self._validate_coordinate(action.x, "x", action.type)
            self._validate_coordinate(action.y, "y", action.type)
            self._validate_coordinate(action.end_x, "end_x", action.type)
            self._validate_coordinate(action.end_y, "end_y", action.type)

    def _execute_action(self, action: BenchmarkAction) -> None:
        """Dispatch and execute a single action via pynput."""
        action_type = action.type

        if action_type in ("click", "double_click", "right_click"):
            self._do_click(action)
        elif action_type == "type":
            self._do_type(action)
        elif action_type == "key":
            self._do_key(action)
        elif action_type == "scroll":
            self._do_scroll(action)
        elif action_type == "drag":
            self._do_drag(action)
        elif action_type in ("done", "wait", "error"):
            pass  # No-op
        else:
            raise ActionExecutionError(f"Unsupported local action type: {action_type!r}")

    def _do_click(self, action: BenchmarkAction) -> None:
        """Execute a mouse click action."""
        from pynput.mouse import Button  # type: ignore[import-untyped]
        from pynput.mouse import Controller as MouseController

        mouse = MouseController()
        x = action.x if action.x is not None else 0
        y = action.y if action.y is not None else 0
        lx, ly = self._to_logical(x, y)
        mouse.position = (lx, ly)

        if action.type == "double_click":
            mouse.click(Button.left, 2)
        elif action.type == "right_click":
            mouse.click(Button.right, 1)
        else:
            mouse.click(Button.left, 1)

    def _do_type(self, action: BenchmarkAction) -> None:
        """Execute a text typing action."""
        from pynput.keyboard import Controller as KbdController  # type: ignore[import-untyped]

        kbd = KbdController()
        text = action.text or ""
        kbd.type(text)

    def _do_key(self, action: BenchmarkAction) -> None:
        """Execute a key press action, optionally with modifiers."""
        from pynput.keyboard import Controller as KbdController  # type: ignore[import-untyped]

        kbd = KbdController()
        key_name = action.key or ""
        modifiers = action.modifiers or []

        # Resolve the key
        resolved_key = self._resolve_key(key_name)

        # Press modifiers
        held_modifiers = []
        for mod in modifiers:
            mod_key = self._resolve_key(mod)
            kbd.press(mod_key)
            held_modifiers.append(mod_key)

        # Press and release the main key
        kbd.press(resolved_key)
        kbd.release(resolved_key)

        # Release modifiers in reverse order
        for mod_key in reversed(held_modifiers):
            kbd.release(mod_key)

    def _do_scroll(self, action: BenchmarkAction) -> None:
        """Execute a scroll action."""
        from pynput.mouse import Controller as MouseController  # type: ignore[import-untyped]

        mouse = MouseController()
        amount = int(action.scroll_amount or 3)
        direction = action.scroll_direction or "down"

        # pynput scroll: positive dy = scroll up, negative = scroll down
        if direction == "up":
            mouse.scroll(0, amount)
        elif direction == "down":
            mouse.scroll(0, -amount)
        elif direction == "left":
            mouse.scroll(-amount, 0)
        elif direction == "right":
            mouse.scroll(amount, 0)

    def _do_drag(self, action: BenchmarkAction) -> None:
        """Execute a drag action."""
        from pynput.mouse import Button  # type: ignore[import-untyped]
        from pynput.mouse import Controller as MouseController

        mouse = MouseController()

        start_x = action.x if action.x is not None else 0
        start_y = action.y if action.y is not None else 0
        end_x = action.end_x if action.end_x is not None else start_x
        end_y = action.end_y if action.end_y is not None else start_y

        sx, sy = self._to_logical(start_x, start_y)
        ex, ey = self._to_logical(end_x, end_y)

        mouse.position = (sx, sy)
        mouse.press(Button.left)
        # Smooth drag with small steps
        steps = 20
        for i in range(1, steps + 1):
            t = i / steps
            ix = sx + (ex - sx) * t
            iy = sy + (ey - sy) * t
            mouse.position = (ix, iy)
            time.sleep(0.01)
        mouse.release(Button.left)

    @staticmethod
    def _resolve_key(key_name: str):
        """Resolve a key name string to a pynput Key enum or character.

        Handles common key names like "enter", "tab", "ctrl", "shift",
        "alt", "cmd", "space", "backspace", "delete", "escape", etc.
        Single characters are returned as-is.
        """
        from pynput.keyboard import Key  # type: ignore[import-untyped]

        key_map = {
            "enter": Key.enter,
            "return": Key.enter,
            "tab": Key.tab,
            "space": Key.space,
            "backspace": Key.backspace,
            "delete": Key.delete,
            "escape": Key.esc,
            "esc": Key.esc,
            "up": Key.up,
            "down": Key.down,
            "left": Key.left,
            "right": Key.right,
            "home": Key.home,
            "end": Key.end,
            "page_up": Key.page_up,
            "page_down": Key.page_down,
            "ctrl": Key.ctrl_l,
            "ctrl_l": Key.ctrl_l,
            "ctrl_r": Key.ctrl_r,
            "shift": Key.shift_l,
            "shift_l": Key.shift_l,
            "shift_r": Key.shift_r,
            "alt": Key.alt_l,
            "alt_l": Key.alt_l,
            "alt_r": Key.alt_r,
            "cmd": Key.cmd,
            "command": Key.cmd,
            "super": Key.cmd,
            "f1": Key.f1,
            "f2": Key.f2,
            "f3": Key.f3,
            "f4": Key.f4,
            "f5": Key.f5,
            "f6": Key.f6,
            "f7": Key.f7,
            "f8": Key.f8,
            "f9": Key.f9,
            "f10": Key.f10,
            "f11": Key.f11,
            "f12": Key.f12,
            "caps_lock": Key.caps_lock,
        }

        # Key.insert is not available on all platforms (e.g. macOS)
        if hasattr(Key, "insert"):
            key_map["insert"] = Key.insert

        normalized = key_name.strip().lower()
        if normalized in key_map:
            return key_map[normalized]

        # Single character keys
        if len(key_name) == 1:
            return key_name

        logger.warning("Unrecognized key name '%s', passing as literal", key_name)
        return key_name
