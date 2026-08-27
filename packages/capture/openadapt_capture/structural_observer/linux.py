"""Action-time Linux AT-SPI observations.

The system AT-SPI GI binding is imported only when this observer is
constructed. Native Wayland and X11 applications both expose structure through
AT-SPI when the desktop accessibility bus is available.
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
    StructuralAncestor,
    StructuralBounds,
    StructuralElement,
    StructuralObservation,
    StructuralObservationRequest,
    StructuralProcessIdentity,
    StructuralWindowIdentity,
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


def _call(element: Any, *names: str) -> Any:
    for name in names:
        method = getattr(element, name, None)
        if not callable(method):
            continue
        try:
            return method()
        except Exception:
            continue
    return None


class _GIAtspiRuntime:
    """Bounded adapter for the modern GObject AT-SPI binding."""

    _MAX_NODES = 4096

    def __init__(self) -> None:
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
        except Exception as exc:
            raise RuntimeError(
                "Linux structural observation requires PyGObject and the "
                "system AT-SPI typelib/runtime"
            ) from exc
        self.atspi = Atspi
        try:
            initialized = bool(Atspi.is_initialized())
            status = 0 if initialized else int(Atspi.init())
        except Exception as exc:
            raise RuntimeError("could not initialize the AT-SPI accessibility registry") from exc
        if status != 0:
            raise RuntimeError(f"AT-SPI accessibility registry initialization failed ({status})")
        try:
            self.desktop = Atspi.get_desktop(0)
        except Exception as exc:
            raise RuntimeError("AT-SPI did not expose the desktop") from exc
        if self.desktop is None:
            raise RuntimeError("AT-SPI did not expose a desktop accessibility tree")

    def _screen_coordinates(self) -> Any:
        return self.atspi.CoordType.SCREEN

    def _state_contains(self, element: Any, name: str) -> bool:
        state = getattr(self.atspi.StateType, name, None)
        state_set = _call(element, "get_state_set", "getState")
        if state is None or state_set is None:
            return False
        try:
            return bool(state_set.contains(state))
        except Exception:
            return False

    def children(self, element: Any) -> list[Any]:
        count = _integer(_call(element, "get_child_count", "getChildCount"))
        if count is None:
            return []
        if count > self._MAX_NODES:
            raise RuntimeError("AT-SPI child count exceeded the observation bound")
        children: list[Any] = []
        for index in range(count):
            try:
                child = element.get_child_at_index(index)
            except Exception:
                try:
                    child = element.getChildAtIndex(index)
                except Exception:
                    continue
            if child is not None:
                children.append(child)
        return children

    def component(self, element: Any) -> Any:
        return _call(
            element,
            "get_component_iface",
            "queryComponent",
            "get_component",
        )

    def bounds(self, element: Any) -> StructuralBounds | None:
        component = self.component(element)
        if component is None:
            return None
        try:
            extents = component.get_extents(self._screen_coordinates())
        except Exception:
            try:
                extents = component.getExtents(self._screen_coordinates())
            except Exception:
                return None
        try:
            left = float(extents.x)
            top = float(extents.y)
            width = float(extents.width)
            height = float(extents.height)
            return StructuralBounds(
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _child_at_point(self, element: Any, x: int, y: int) -> Any:
        component = self.component(element)
        if component is None:
            return None
        for method_name in ("get_accessible_at_point", "getAccessibleAtPoint"):
            method = getattr(component, method_name, None)
            if not callable(method):
                continue
            try:
                return method(x, y, self._screen_coordinates())
            except Exception:
                continue
        return None

    def _deepest_at_point(self, root: Any, x: int, y: int) -> Any:
        current = root
        seen = {id(current)}
        for _ in range(MAX_STRUCTURAL_ANCESTRY_DEPTH):
            child = self._child_at_point(current, x, y)
            if child is None or id(child) in seen:
                return current
            current = child
            seen.add(id(current))
        # A deeper direct child means the bounded query did not establish the
        # actual target. Omit the observation instead of returning an ancestor.
        child = self._child_at_point(current, x, y)
        return None if child is not None and id(child) not in seen else current

    def element_at_point(self, x: float, y: float) -> Any:
        point_x, point_y = int(round(x)), int(round(y))

        # The desktop Component, when available, owns cross-application
        # stacking and gives the only unambiguous top-most child. Descend from
        # that result because AT-SPI's point method returns one direct child.
        direct = self._child_at_point(self.desktop, point_x, point_y)
        if direct is not None:
            return self._deepest_at_point(direct, point_x, point_y)

        # Some registries do not expose Component on the desktop. In that
        # case, accept only one active visible top-level surface at the point.
        # Do not guess between overlapping applications.
        matches: list[Any] = []
        try:
            applications = self.children(self.desktop)
        except RuntimeError:
            return None
        visited = 0
        for application in applications:
            try:
                surfaces = self.children(application) or [application]
            except RuntimeError:
                return None
            for surface in surfaces:
                visited += 1
                if visited > self._MAX_NODES:
                    return None
                bounds = self.bounds(surface)
                if bounds is None:
                    continue
                if not (
                    bounds.left <= point_x < bounds.right and bounds.top <= point_y < bounds.bottom
                ):
                    continue
                if not all(
                    self._state_contains(surface, state)
                    for state in ("ACTIVE", "VISIBLE", "SHOWING")
                ):
                    continue
                match = self._deepest_at_point(surface, point_x, point_y)
                if match is not None:
                    matches.append(match)
        return matches[0] if len(matches) == 1 else None

    def focused_element(self) -> Any:
        try:
            stack = list(reversed(self.children(self.desktop)))
        except RuntimeError:
            return None
        visited = 0
        found: list[Any] = []
        while stack and visited < self._MAX_NODES:
            element = stack.pop()
            visited += 1
            if self._state_contains(element, "FOCUSED"):
                found.append(element)
                if len(found) > 1:
                    return None
            try:
                children = self.children(element)
            except RuntimeError:
                return None
            if visited + len(stack) + len(children) > self._MAX_NODES:
                return None
            stack.extend(reversed(children))
        if stack:
            return None
        # Multiple focused nodes make the structural binding ambiguous.
        return found[0] if len(found) == 1 else None

    def parent(self, element: Any) -> Any:
        value = getattr(element, "parent", None)
        return value if value is not None else _call(element, "get_parent", "getParent")

    def attributes(self, element: Any) -> dict[str, str]:
        raw = _call(element, "getAttributes", "get_attributes")
        if isinstance(raw, dict):
            return {str(key): str(value) for key, value in raw.items()}
        result: dict[str, str] = {}
        if isinstance(raw, (list, tuple)):
            for item in raw:
                key, separator, value = str(item).partition(":")
                if separator:
                    result[key] = value
        return result

    def role_name(self, element: Any) -> str | None:
        return _text(_call(element, "get_role_name", "getRoleName"))

    def process_id(self, element: Any) -> int | None:
        current = element
        for _ in range(MAX_STRUCTURAL_ANCESTRY_DEPTH):
            process_id = _integer(_call(current, "get_process_id", "getProcessId"))
            if process_id is not None and process_id > 0:
                return process_id
            parent = self.parent(current)
            if parent is None:
                break
            current = parent
        return None

    def action_names(self, element: Any) -> list[str] | None:
        action = _call(element, "get_action_iface", "queryAction", "get_action")
        if action is None:
            return None
        count = _integer(_call(action, "get_n_actions", "getNActions"))
        if count is None:
            return None
        names = []
        for index in range(min(count, 64)):
            try:
                name = action.get_action_name(index)
            except Exception:
                try:
                    name = action.getName(index)
                except Exception:
                    continue
            if (value := _text(name)) is not None:
                names.append(value)
        return names or None


def _process_name(pid: int) -> str | None:
    try:
        import psutil

        return _text(psutil.Process(pid).name())
    except Exception:
        return None


def _fields(runtime: Any, element: Any) -> dict[str, Any]:
    attributes = runtime.attributes(element)
    role = runtime.role_name(element)
    name = _text(getattr(element, "name", None)) or _text(_call(element, "get_name", "getName"))
    return {
        "automation_id": _text(
            _call(element, "get_accessible_id", "get_id")
            or attributes.get("accessible-id")
            or attributes.get("id")
        ),
        "role": role,
        "role_source": "linux_atspi_role" if role else None,
        "control_type": role,
        "name": name,
        "class_name": _text(attributes.get("class") or attributes.get("toolkit")),
        "bounds": runtime.bounds(element),
        "supported_patterns": runtime.action_names(element),
    }


def _ancestry(runtime: Any, element: Any, maximum_depth: int):
    result: list[StructuralAncestor] = []
    current = element
    for _ in range(maximum_depth):
        current = runtime.parent(current)
        if current is None:
            break
        fields = _fields(runtime, current)
        fields.pop("supported_patterns", None)
        ancestor = StructuralAncestor(**fields)
        if ancestor.model_dump(exclude_none=True):
            result.append(ancestor)
    return result or None


def _window(runtime: Any, element: Any) -> StructuralWindowIdentity | None:
    current = element
    candidate = None
    for _ in range(MAX_STRUCTURAL_ANCESTRY_DEPTH):
        if (runtime.role_name(current) or "").casefold() in {
            "alert",
            "dialog",
            "frame",
            "window",
        }:
            candidate = current
            break
        parent = runtime.parent(current)
        if parent is None:
            break
        current = parent
    if candidate is None:
        return None
    fields = _fields(runtime, candidate)
    identity = StructuralWindowIdentity(
        title=fields.get("name"),
        automation_id=fields.get("automation_id"),
        class_name=fields.get("class_name"),
        bounds=fields.get("bounds"),
    )
    return identity if identity.model_dump(exclude_none=True) else None


class LinuxATSpiStructuralObserver:
    """Read exact AT-SPI evidence for a pointer or focused action."""

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        runtime_factory: Callable[[], Any] = _GIAtspiRuntime,
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
        """Create the AT-SPI runtime on the input delivery thread."""
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
            _logger.warning("Linux AT-SPI observation is unavailable: %s", exc)

    def close_current_thread(self) -> None:
        """Release a thread-owned AT-SPI adapter when it exposes a close hook."""
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
        if request.x is not None and request.y is not None:
            element = runtime.element_at_point(request.x, request.y)
            query_kind = "point"
        else:
            element = runtime.focused_element()
            query_kind = "focused"
        if element is None:
            return None
        fields = _fields(runtime, element)
        observed_element = StructuralElement(**fields)
        if not observed_element.model_dump(exclude_none=True):
            return None
        pid = _integer(runtime.process_id(element))
        process = None
        if pid is not None and pid > 0:
            process = StructuralProcessIdentity(
                process_id=pid,
                process_name=self.process_name_resolver(pid),
            )
        return StructuralObservation(
            provider="linux_atspi",
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


__all__ = ["LinuxATSpiStructuralObserver"]
