"""Windows UI Automation observation without input injection.

``pywinauto`` is imported only when the Windows observer is instantiated.
Importing this module on macOS, Linux, or a headless host is side-effect free.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from openadapt_capture.structural import (
    StructuralAncestor,
    StructuralBounds,
    StructuralCandidateContext,
    StructuralElement,
    StructuralObservation,
    StructuralObservationRequest,
    StructuralProcessIdentity,
    StructuralWindowIdentity,
)

_logger = logging.getLogger(__name__)

_SUPPORTED_PATTERN_ATTRIBUTES = (
    ("ExpandCollapse", "iface_expand_collapse"),
    ("Selection", "iface_selection"),
    ("SelectionItem", "iface_selection_item"),
    ("Invoke", "iface_invoke"),
    ("Toggle", "iface_toggle"),
    ("Text", "iface_text"),
    ("Value", "iface_value"),
    ("RangeValue", "iface_range_value"),
    ("Grid", "iface_grid"),
    ("GridItem", "iface_grid_item"),
    ("Table", "iface_table"),
    ("TableItem", "iface_table_item"),
    ("ScrollItem", "iface_scroll_item"),
    ("Scroll", "iface_scroll"),
    ("Transform", "iface_transform"),
    ("Window", "iface_window"),
)

_COINIT_APARTMENTTHREADED = 0x2
def _uia_point_coordinate(value: float) -> int:
    """Quantize a native pixel coordinate for UIA's integer ``POINT`` ABI.

    Native Windows hook coordinates arrive as integer pixels but the public
    cross-platform event contract represents them as floats. Half values are
    rounded away from zero so negative-monitor coordinates are deterministic.
    """

    coordinate = float(value)
    if not math.isfinite(coordinate):
        raise ValueError("UIA point coordinates must be finite")
    if coordinate >= 0:
        return int(math.floor(coordinate + 0.5))
    return int(math.ceil(coordinate - 0.5))


class _PywinautoRuntime:
    """Exact pywinauto UIA adapter, created and used on one COM thread."""

    def __init__(self) -> None:
        from pywinauto import Desktop
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        self._desktop = Desktop(backend="uia", allow_magic_lookup=False)
        self._iui = IUIA()
        self._element_info_type = UIAElementInfo

    def from_point(self, x: float, y: float) -> Any:
        """Return the element under the integer Windows screen point."""

        return self._desktop.from_point(
            _uia_point_coordinate(x),
            _uia_point_coordinate(y),
        )

    def focused_element(self) -> Any:
        """Return the exact focused UIA element, not merely the active window."""

        element_info = self._element_info_type(self._iui.get_focused_element())
        return self._desktop.backend.generic_wrapper_class(element_info)

    def close(self) -> None:
        """Release the service-owned pywinauto singleton on its COM thread."""

        from pywinauto.uia_defines import IUIA

        singleton_instances = getattr(type(IUIA), "_instances", {})
        if singleton_instances.get(IUIA) is self._iui:
            singleton_instances.pop(IUIA, None)
        self._desktop = None
        self._iui = None
        self._element_info_type = None


class _WindowsCOMApartment:
    """Balanced COM apartment owned by the UIA service thread."""

    def __init__(self) -> None:
        self._ole32: Any | None = None
        self._comtypes_import_initialized = False

    def __enter__(self) -> "_WindowsCOMApartment":
        # Importing comtypes initializes COM on the importing thread. Initialize
        # explicitly first so this adapter owns a balanced lifecycle even when
        # comtypes was already imported by the host process.
        comtypes_was_loaded = "comtypes" in sys.modules
        existing_uia_module = sys.modules.get("pywinauto.uia_defines")
        if existing_uia_module is not None:
            iui_type = getattr(existing_uia_module, "IUIA", None)
            singleton_instances = getattr(type(iui_type), "_instances", {})
            if iui_type is not None and singleton_instances.get(iui_type) is not None:
                raise RuntimeError(
                    "pywinauto UIA was initialized before the Capture UIA service; "
                    "refusing to reuse an apartment-bound singleton"
                )
        ole32 = ctypes.OleDLL("ole32")
        co_initialize_ex = ole32.CoInitializeEx
        co_initialize_ex.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        co_initialize_ex.restype = ctypes.c_long
        result = int(co_initialize_ex(None, _COINIT_APARTMENTTHREADED))
        if result < 0:
            raise OSError(f"CoInitializeEx failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")
        self._ole32 = ole32

        self._comtypes_import_initialized = not comtypes_was_loaded
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        ole32 = self._ole32
        self._ole32 = None
        if ole32 is None:
            return
        uia_module = sys.modules.get("pywinauto.uia_defines")
        if uia_module is not None:
            iui_type = getattr(uia_module, "IUIA", None)
            singleton_instances = getattr(type(iui_type), "_instances", {})
            if iui_type is not None:
                singleton_instances.pop(iui_type, None)
        gc.collect()
        # If this service thread performed comtypes' first import, balance the
        # initialization that comtypes performs at module import as well as our
        # explicit initialization.
        if self._comtypes_import_initialized:
            import atexit as _atexit

            from comtypes import CoUninitialize
            from comtypes._post_coinit import _shutdown

            # comtypes assumes its first import happens on the process main
            # thread and registers a process-exit uninitializer. This adapter
            # imports it on its owned UIA thread, balances it here, and removes
            # that mismatched process-exit callback.
            _atexit.unregister(_shutdown)

            CoUninitialize()
        co_uninitialize = ole32.CoUninitialize
        co_uninitialize.argtypes = []
        co_uninitialize.restype = None
        co_uninitialize()


def _present_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _present_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_value(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _safe_call(obj: Any, name: str) -> Any:
    value = _safe_value(obj, name)
    if not callable(value):
        return None
    try:
        return value()
    except Exception:
        return None


def _bounds(value: Any) -> StructuralBounds | None:
    if value is None:
        return None

    def read(name: str) -> Any:
        if isinstance(value, dict):
            return value.get(name)
        return _safe_value(value, name)

    raw = {name: read(name) for name in ("left", "top", "right", "bottom")}
    if any(item is None for item in raw.values()):
        return None
    try:
        return StructuralBounds(**raw)
    except (TypeError, ValueError):
        return None


def _element_info(wrapper: Any) -> Any:
    return _safe_value(wrapper, "element_info")


def _element_fields(wrapper: Any) -> dict[str, Any]:
    info = _element_info(wrapper)
    if info is None:
        return {}
    role = _present_string(_safe_call(wrapper, "friendly_class_name"))
    return {
        "automation_id": _present_string(
            _safe_value(info, "automation_id")
            or _safe_value(info, "auto_id")
        ),
        "role": role,
        "role_source": "pywinauto_friendly_class" if role else None,
        "control_type": _present_string(_safe_value(info, "control_type")),
        "name": _present_string(_safe_value(info, "name")),
        "class_name": _present_string(_safe_value(info, "class_name")),
        "framework_id": _present_string(_safe_value(info, "framework_id")),
        "native_window_handle": _present_int(
            _safe_value(info, "handle") or _safe_value(wrapper, "handle")
        ),
        "bounds": _bounds(_safe_value(info, "rectangle")),
    }


def _supported_patterns(wrapper: Any) -> list[str]:
    supported: list[str] = []
    for name, attribute in _SUPPORTED_PATTERN_ATTRIBUTES:
        try:
            if getattr(wrapper, attribute) is not None:
                supported.append(name)
        except Exception:
            continue
    return supported


def _as_element(wrapper: Any) -> StructuralElement:
    return StructuralElement(
        **_element_fields(wrapper),
        supported_patterns=_supported_patterns(wrapper),
    )


def _as_ancestor(wrapper: Any) -> StructuralAncestor | None:
    fields = _element_fields(wrapper)
    fields.pop("framework_id", None)
    fields.pop("native_window_handle", None)
    ancestor = StructuralAncestor(**fields)
    if not ancestor.model_dump(exclude_none=True):
        return None
    return ancestor


def _ancestry(wrapper: Any, *, maximum_depth: int) -> list[StructuralAncestor] | None:
    ancestors: list[StructuralAncestor] = []
    current = wrapper
    for _ in range(maximum_depth):
        parent = _safe_call(current, "parent")
        if parent is None:
            break
        ancestor = _as_ancestor(parent)
        if ancestor is not None:
            ancestors.append(ancestor)
        current = parent
    return ancestors or None


def _window_identity(wrapper: Any) -> StructuralWindowIdentity | None:
    top = _safe_call(wrapper, "top_level_parent")
    if top is None:
        return None
    fields = _element_fields(top)
    title = _present_string(_safe_call(top, "window_text")) or fields.get("name")
    identity = StructuralWindowIdentity(
        title=title,
        automation_id=fields.get("automation_id"),
        class_name=fields.get("class_name"),
        native_window_handle=fields.get("native_window_handle"),
        bounds=fields.get("bounds"),
    )
    if not identity.model_dump(exclude_none=True):
        return None
    return identity


def _candidate_cardinality(
    target: Any,
) -> tuple[int | None, StructuralCandidateContext | None]:
    target_fields = _element_fields(target)
    automation_id = target_fields.get("automation_id")
    control_type = target_fields.get("control_type")
    name = target_fields.get("name")

    search_kwargs: dict[str, str] = {}
    matched_fields: list[str] = []
    if automation_id:
        # pywinauto 0.6.9's UIA descendants() cannot filter by auto_id.
        # Narrow with its supported control-type condition, then compare the
        # stable AutomationId exactly below.
        matched_fields.append("automation_id")
        if control_type:
            search_kwargs["control_type"] = control_type
            matched_fields.append("control_type")
    elif control_type and name:
        search_kwargs["control_type"] = control_type
        search_kwargs["title"] = name
        matched_fields.extend(("control_type", "name"))
    else:
        return None, None

    top = _safe_call(target, "top_level_parent")
    if top is None:
        return None, None
    try:
        candidates = list(top.descendants(**search_kwargs))
    except Exception:
        return None, None

    def exact_match(candidate: Any) -> bool:
        fields = _element_fields(candidate)
        return all(
            {
                "automation_id": fields.get("automation_id") == automation_id,
                "control_type": fields.get("control_type") == control_type,
                "name": fields.get("name") == name,
            }[field]
            for field in matched_fields
        )

    count = sum(1 for candidate in candidates if exact_match(candidate))
    if exact_match(top):
        count += 1
    return count, StructuralCandidateContext(
        scope="top_level_window",
        matched_fields=matched_fields,
    )


def _resolve_process_name(process_id: int) -> str | None:
    try:
        import psutil

        return _present_string(psutil.Process(process_id).name())
    except Exception:
        return None


def _observe_with_runtime(
    runtime: Any,
    request: StructuralObservationRequest,
    *,
    process_name_resolver: Callable[[int], str | None],
    clock: Callable[[], float],
    maximum_ancestry_depth: int,
) -> StructuralObservation | None:
    """Build one observation while every UIA wrapper stays on its owner thread."""

    if request.x is not None and request.y is not None:
        target = runtime.from_point(request.x, request.y)
        query_kind = "point"
    else:
        target = runtime.focused_element()
        query_kind = "focused"
    if target is None:
        return None

    element = _as_element(target)
    info = _element_info(target)
    process_id = _present_int(_safe_value(info, "process_id"))
    process = None
    if process_id is not None:
        process = StructuralProcessIdentity(
            process_id=process_id,
            process_name=process_name_resolver(process_id),
        )
    candidate_count, candidate_context = _candidate_cardinality(target)
    return StructuralObservation(
        provider="windows_uia",
        event_timestamp=request.event_timestamp,
        observed_at=clock(),
        query_kind=query_kind,
        element=element,
        process=process,
        window=_window_identity(target),
        ancestry=_ancestry(
            target,
            maximum_depth=maximum_ancestry_depth,
        ),
        candidate_count=candidate_count,
        candidate_context=candidate_context,
    )


class WindowsUIAStructuralObserver:
    """Read Windows UIA evidence at a pointer or focused-element action."""

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        runtime_factory: Callable[[], Any] = _PywinautoRuntime,
        apartment_factory: Callable[[], Any] = _WindowsCOMApartment,
        process_name_resolver: Callable[[int], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        maximum_ancestry_depth: int = 12,
    ) -> None:
        self._runtime = runtime
        self._runtime_factory = runtime_factory
        self._apartment_factory = apartment_factory
        self._thread_state = threading.local()
        self._process_name_resolver = process_name_resolver or _resolve_process_name
        self._clock = clock
        self._maximum_ancestry_depth = maximum_ancestry_depth

    def open_current_thread(self) -> None:
        """Initialize the observing thread's COM apartment and UIA runtime."""

        if self._runtime is not None:
            return
        if getattr(self._thread_state, "unavailable", False):
            return
        if getattr(self._thread_state, "runtime", None) is not None:
            return
        apartment = self._apartment_factory()
        opened = False
        try:
            apartment.__enter__()
            opened = True
            runtime = self._runtime_factory()
        except Exception as exc:
            if opened:
                apartment.__exit__(*sys.exc_info())
            self._thread_state.unavailable = True
            _logger.warning("Windows UIA observation is unavailable: %s", exc)
            return
        self._thread_state.apartment = apartment
        self._thread_state.runtime = runtime

    def close_current_thread(self) -> None:
        """Release UIA and COM on the same thread that initialized them."""

        if self._runtime is not None:
            return
        runtime = getattr(self._thread_state, "runtime", None)
        apartment = getattr(self._thread_state, "apartment", None)
        if runtime is None or apartment is None:
            return
        try:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
        finally:
            self._thread_state.runtime = None
            self._thread_state.apartment = None
            apartment.__exit__(None, None, None)

    def observe(
        self,
        request: StructuralObservationRequest,
    ) -> StructuralObservation | None:
        runtime = self._runtime
        if runtime is None:
            self.open_current_thread()
            runtime = getattr(self._thread_state, "runtime", None)
        if runtime is None:
            return None
        return _observe_with_runtime(
            runtime,
            request,
            process_name_resolver=self._process_name_resolver,
            clock=self._clock,
            maximum_ancestry_depth=self._maximum_ancestry_depth,
        )


__all__ = ["WindowsUIAStructuralObserver"]
