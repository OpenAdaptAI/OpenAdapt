"""Action-time macOS Accessibility observations.

ApplicationServices is imported only when this observer is constructed. The
module therefore remains safe to import on other platforms and in headless
package checks.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from openadapt_capture.structural import (
    MAX_STRUCTURAL_ANCESTRY_DEPTH,
    MAX_STRUCTURAL_TEXT_LENGTH,
    MAX_STRUCTURAL_TREE_NODES,
    StructuralAncestor,
    StructuralBounds,
    StructuralElement,
    StructuralObservation,
    StructuralObservationRequest,
    StructuralProcessIdentity,
    StructuralTreeNode,
    StructuralWindowIdentity,
    omit_tree_value,
)

_logger = logging.getLogger(__name__)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_STRUCTURAL_TEXT_LENGTH:
        return None
    return value


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class _AXRuntime:
    """Small PyObjC adapter for the native AX API."""

    def __init__(self) -> None:
        import AppKit
        import ApplicationServices as ax

        if not bool(ax.AXIsProcessTrusted()):
            raise PermissionError(
                "macOS Accessibility permission is required for structural observation"
            )
        self.ax = ax
        self.system = ax.AXUIElementCreateSystemWide()
        self.frontmost_process_id = lambda: int(
            AppKit.NSWorkspace.sharedWorkspace()
            .frontmostApplication()
            .processIdentifier()
        )

    def attribute(self, element: Any, name: str) -> Any:
        error, value = self.ax.AXUIElementCopyAttributeValue(element, name, None)
        if error != self.ax.kAXErrorSuccess:
            return None
        return value

    def element_at_point(self, x: float, y: float) -> Any:
        error, element = self.ax.AXUIElementCopyElementAtPosition(
            self.system, float(x), float(y), None
        )
        if error != self.ax.kAXErrorSuccess:
            return None
        return element

    def focused_element(self) -> Any:
        element = self.attribute(self.system, "AXFocusedUIElement")
        if element is not None:
            return element
        try:
            process_id = self.frontmost_process_id()
        except Exception:
            return None
        if process_id <= 0:
            return None
        application = self.ax.AXUIElementCreateApplication(process_id)
        return self.attribute(application, "AXFocusedUIElement")

    def actions(self, element: Any) -> list[Any] | None:
        error, values = self.ax.AXUIElementCopyActionNames(element, None)
        if error != self.ax.kAXErrorSuccess or not isinstance(values, (list, tuple)):
            return None
        return list(values)

    def children(self, element: Any) -> list[Any]:
        value = self.attribute(element, "AXChildren")
        if isinstance(value, (list, tuple)):
            return list(value)
        return []

    def process_id(self, element: Any) -> int | None:
        result = self.ax.AXUIElementGetPid(element, None)
        if isinstance(result, tuple):
            error, pid = result
            if error != self.ax.kAXErrorSuccess:
                return None
            return _integer(pid)
        return _integer(result)

    def _geometry(self, value: Any, value_type: int) -> Any:
        if value is None:
            return None
        success, geometry = self.ax.AXValueGetValue(value, value_type, None)
        return geometry if success else None

    def bounds(self, element: Any) -> StructuralBounds | None:
        position = self._geometry(
            self.attribute(element, "AXPosition"),
            self.ax.kAXValueCGPointType,
        )
        size = self._geometry(
            self.attribute(element, "AXSize"),
            self.ax.kAXValueCGSizeType,
        )
        if position is None or size is None:
            return None
        try:
            left = float(position.x)
            top = float(position.y)
            width = float(size.width)
            height = float(size.height)
            return StructuralBounds(
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
            )
        except (AttributeError, TypeError, ValueError):
            return None


def _process_name(pid: int) -> str | None:
    try:
        import psutil

        return _text(psutil.Process(pid).name())
    except Exception:
        return None


def _element_fields(runtime: Any, element: Any) -> dict[str, Any]:
    role = _text(runtime.attribute(element, "AXRole"))
    title = _text(runtime.attribute(element, "AXTitle"))
    description = _text(runtime.attribute(element, "AXDescription"))
    action_reader = getattr(runtime, "actions", None)
    actions = action_reader(element) if callable(action_reader) else None
    supported_patterns = None
    if isinstance(actions, (list, tuple)):
        supported_patterns = [
            action for item in actions[:64] if (action := _text(str(item))) is not None
        ] or None
    return {
        "automation_id": _text(runtime.attribute(element, "AXIdentifier")),
        "role": role,
        "role_source": "macos_ax_role" if role else None,
        "control_type": _text(runtime.attribute(element, "AXSubrole")) or role,
        "name": title or description,
        "class_name": _text(runtime.attribute(element, "AXSubrole")),
        "native_window_handle": _integer(runtime.attribute(element, "AXWindowNumber")),
        "bounds": runtime.bounds(element),
        "supported_patterns": supported_patterns,
    }


def _ancestor(runtime: Any, element: Any) -> StructuralAncestor | None:
    fields = _element_fields(runtime, element)
    fields.pop("native_window_handle", None)
    fields.pop("supported_patterns", None)
    ancestor = StructuralAncestor(**fields)
    return ancestor if ancestor.model_dump(exclude_none=True) else None


def _ancestry(runtime: Any, element: Any, maximum_depth: int):
    result: list[StructuralAncestor] = []
    current = element
    for _ in range(maximum_depth):
        current = runtime.attribute(current, "AXParent")
        if current is None:
            break
        ancestor = _ancestor(runtime, current)
        if ancestor is not None:
            result.append(ancestor)
    return result or None


def _window(runtime: Any, element: Any) -> StructuralWindowIdentity | None:
    window = runtime.attribute(element, "AXWindow")
    if window is None:
        return None
    fields = _element_fields(runtime, window)
    identity = StructuralWindowIdentity(
        title=fields.get("name"),
        automation_id=fields.get("automation_id"),
        class_name=fields.get("class_name"),
        native_window_handle=fields.get("native_window_handle"),
        bounds=fields.get("bounds"),
    )
    return identity if identity.model_dump(exclude_none=True) else None


def _ax_children(runtime: Any, element: Any) -> list[Any]:
    reader = getattr(runtime, "children", None)
    if callable(reader):
        try:
            children = reader(element)
        except Exception:
            return []
        if isinstance(children, (list, tuple)):
            return list(children)
        return []
    value = runtime.attribute(element, "AXChildren")
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _ax_bool(runtime: Any, element: Any, name: str) -> bool | None:
    value = runtime.attribute(element, name)
    return value if isinstance(value, bool) else None


def _ax_value(runtime: Any, element: Any) -> str | None:
    value = runtime.attribute(element, "AXValue")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return _text(value)
    try:
        return _text(str(value))
    except Exception:
        return None


def _as_tree_node(
    runtime: Any,
    element: Any,
    state: dict[str, int | bool],
) -> StructuralTreeNode | None:
    remaining = int(state["remaining"])
    if remaining <= 0:
        state["truncated"] = True
        return None
    state["remaining"] = remaining - 1
    fields = _element_fields(runtime, element)
    children: list[StructuralTreeNode] = []
    for child in _ax_children(runtime, element):
        node = _as_tree_node(runtime, child, state)
        if node is not None:
            children.append(node)
        if state["truncated"]:
            break
    value = None
    if not omit_tree_value(fields.get("role"), fields.get("control_type")):
        value = _ax_value(runtime, element)
    return StructuralTreeNode(
        provider_runtime_id=fields.get("automation_id"),
        automation_id=fields.get("automation_id"),
        role=fields.get("role"),
        control_type=fields.get("control_type"),
        name=fields.get("name"),
        class_name=fields.get("class_name"),
        value=value,
        enabled=_ax_bool(runtime, element, "AXEnabled"),
        focused=_ax_bool(runtime, element, "AXFocused"),
        bounds=fields.get("bounds"),
        children=children or None,
    )


class MacOSAXStructuralObserver:
    """Read exact AX evidence for a pointer or focused action."""

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        runtime_factory: Callable[[], Any] = _AXRuntime,
        process_name_resolver: Callable[[int], str | None] = _process_name,
        clock: Callable[[], float] = time.time,
        maximum_ancestry_depth: int = 12,
    ) -> None:
        if not 0 < maximum_ancestry_depth <= MAX_STRUCTURAL_ANCESTRY_DEPTH:
            raise ValueError(
                f"maximum_ancestry_depth must be between 1 and {MAX_STRUCTURAL_ANCESTRY_DEPTH}"
            )
        self._runtime = runtime
        self._runtime_factory = runtime_factory
        self._thread_state = threading.local()
        self.process_name_resolver = process_name_resolver
        self.clock = clock
        self.maximum_ancestry_depth = maximum_ancestry_depth

    def open_current_thread(self) -> None:
        """Create the native AX runtime on the input delivery thread."""
        if self._runtime is not None:
            return
        if getattr(self._thread_state, "runtime", None) is not None:
            return
        if getattr(self._thread_state, "unavailable", False):
            return
        try:
            self._thread_state.runtime = self._runtime_factory()
        except Exception as exc:
            self._thread_state.unavailable = True
            _logger.warning("macOS AX observation is unavailable: %s", exc)

    def close_current_thread(self) -> None:
        """Release a thread-owned AX adapter when it exposes a close hook."""
        if self._runtime is not None:
            return
        runtime = getattr(self._thread_state, "runtime", None)
        self._thread_state.runtime = None
        close = getattr(runtime, "close", None)
        if callable(close):
            close()

    def observe(self, request: StructuralObservationRequest) -> StructuralObservation | None:
        runtime = self._runtime
        if runtime is None:
            self.open_current_thread()
            runtime = getattr(self._thread_state, "runtime", None)
        if runtime is None:
            return None
        window_tree = request.query_kind == "window_tree"
        if request.x is not None and request.y is not None:
            element = runtime.element_at_point(request.x, request.y)
            query_kind = "window_tree" if window_tree else "point"
        else:
            element = runtime.focused_element()
            query_kind = "window_tree" if window_tree else "focused"
        if element is None:
            return None
        pid = _integer(runtime.process_id(element))
        process = None
        if pid is not None and pid > 0:
            process = StructuralProcessIdentity(
                process_id=pid,
                process_name=self.process_name_resolver(pid),
            )
        if window_tree:
            root = runtime.attribute(element, "AXWindow") or element
            fields = _element_fields(runtime, root)
            observed_element = StructuralElement(**fields)
            if not observed_element.model_dump(exclude_none=True):
                return None
            tree_state: dict[str, int | bool] = {
                "remaining": MAX_STRUCTURAL_TREE_NODES,
                "truncated": False,
            }
            tree_root = _as_tree_node(runtime, root, tree_state)
            return StructuralObservation(
                provider="macos_ax",
                event_timestamp=request.event_timestamp,
                observed_at=self.clock(),
                query_kind="window_tree",
                element=observed_element,
                process=process,
                window=_window(runtime, element),
                tree=[tree_root] if tree_root is not None else None,
                tree_truncated=True if tree_state["truncated"] else None,
            )
        fields = _element_fields(runtime, element)
        observed_element = StructuralElement(**fields)
        if not observed_element.model_dump(exclude_none=True):
            return None
        return StructuralObservation(
            provider="macos_ax",
            event_timestamp=request.event_timestamp,
            observed_at=self.clock(),
            query_kind=query_kind,
            element=observed_element,
            process=process,
            window=_window(runtime, element),
            ancestry=_ancestry(
                runtime,
                element,
                self.maximum_ancestry_depth,
            ),
        )


__all__ = ["MacOSAXStructuralObserver"]
