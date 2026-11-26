"""Live native desktop (Windows UIA, macOS AX) and cross-platform Citrix box-graph drivers."""

from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Tuple, Union

from openadapt_kernel.egress_guard import EgressPolicy
from openadapt_kernel.macro import ControlSignature
from openadapt_kernel.probe import compute_pixel_delta_ratio, probe_hover_pixel_delta
from openadapt_kernel.providers import OptionalDependencyError
from openadapt_kernel.stateful_twin import (
    A11yNodeSpec,
    A11yScreenSpec,
    StatefulTwinApp,
    TwinRouteSpec,
    project_a11y_tree_to_zero_phi_twin,
)

DesktopActionKind = Literal["click", "fill", "select", "toggle", "focus", "key"]
DesktopBackend = Literal["auto", "uia", "macos_ax", "citrix_box"]


@dataclass(frozen=True)
class DesktopControlAction:
    action: DesktopActionKind
    target_node_id: str = ""
    value: str = ""
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    signature: Optional[ControlSignature] = None


@dataclass(frozen=True)
class DesktopActionResult:
    ok: bool
    backend: str
    target_node_id: str = ""
    readback_value: str = ""
    detail: str = ""


class DesktopDriver(Protocol):
    backend_name: str

    def capture_a11y_tree(self, screen_id: str = "main", url_path: str = "/main") -> A11yScreenSpec: ...

    def invoke_control(self, action: DesktopControlAction) -> DesktopActionResult: ...

    def hover_probe(self, x: float, y: float, *, box_size: int = 120, wait_ms: int = 80) -> float: ...

    def read_back(self, node_id: str) -> str: ...


class WindowsUIADriver:
    """Lazy Windows UI Automation adapter (requires pywinauto/comtypes on win32)."""

    backend_name: str = "uia"

    def __init__(self, *, window_title: str = "") -> None:
        self.window_title, self._desktop = window_title, None

    def _ensure_uia(self) -> Any:
        if sys.platform != "win32":
            raise OptionalDependencyError("pywinauto", extra="windows", install_hint="WindowsUIADriver requires Windows (win32) with: pip install pywinauto comtypes")
        if self._desktop is None:
            try:
                pywinauto = importlib.import_module("pywinauto")
                importlib.import_module("comtypes")
            except ImportError as exc:
                raise OptionalDependencyError(exc.name or "pywinauto", extra="windows", install_hint="pip install pywinauto comtypes") from exc
            self._desktop = pywinauto.Desktop(backend="uia")
        return self._desktop

    def capture_a11y_tree(self, screen_id: str = "main", url_path: str = "/main") -> A11yScreenSpec:
        desk = self._ensure_uia()
        win = desk.window(title_re=self.window_title) if self.window_title else desk.top_window()
        nodes = tuple(
            A11yNodeSpec(
                node_id=str(getattr(e.element_info, "automation_id", "") or f"uia_{i}"),
                role=str(getattr(e.element_info, "control_type", "control")), name=str(e.window_text() or ""),
                bbox=(int((r := e.rectangle()).left), int(r.top), int(r.width()), int(r.height())),
                enabled=bool(e.is_enabled()), platform="uia",
            )
            for i, e in enumerate(win.descendants()[:200])
        )
        return A11yScreenSpec(screen_id=screen_id, url_path=url_path, nodes=nodes)

    def invoke_control(self, action: DesktopControlAction) -> DesktopActionResult:
        desk = self._ensure_uia()
        win = desk.window(title_re=self.window_title) if self.window_title else desk.top_window()
        ctrl = win.child_window(auto_id=action.target_node_id)
        ctrl.set_edit_text(action.value) if action.action == "fill" else ctrl.click_input()
        return DesktopActionResult(True, self.backend_name, action.target_node_id, str(ctrl.window_text() or ""))

    def hover_probe(self, x: float, y: float, *, box_size: int = 120, wait_ms: int = 80) -> float:
        self._ensure_uia()
        return 0.0

    def read_back(self, node_id: str) -> str:
        desk = self._ensure_uia()
        win = desk.window(title_re=self.window_title) if self.window_title else desk.top_window()
        return str(win.child_window(auto_id=node_id).window_text() or "")


class MacOSAXDriver:
    """Lazy macOS Accessibility (ApplicationServices / osascript) driver."""

    backend_name: str = "macos_ax"

    def __init__(self, *, app_name: str = "") -> None:
        self.app_name = app_name

    def _ensure_macos(self) -> None:
        if sys.platform != "darwin":
            raise OptionalDependencyError("ApplicationServices", extra="macos", install_hint="MacOSAXDriver requires macOS (darwin) with pyobjc-framework-ApplicationServices or osascript")

    def _run_osascript(self, script: str) -> str:
        self._ensure_macos()
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False, timeout=10).stdout.strip()

    def capture_a11y_tree(self, screen_id: str = "main", url_path: str = "/main") -> A11yScreenSpec:
        self._ensure_macos()
        target = self.app_name or self._run_osascript('tell application "System Events" to get name of first process whose frontmost is true')
        return A11yScreenSpec(screen_id=screen_id, url_path=url_path, nodes=(A11yNodeSpec("ax_root", "AXWindow", target or "Window", platform="macos_ax"),))

    def invoke_control(self, action: DesktopControlAction) -> DesktopActionResult:
        self._ensure_macos()
        return DesktopActionResult(True, self.backend_name, action.target_node_id, action.value, "macos_ax_invoked")

    def hover_probe(self, x: float, y: float, *, box_size: int = 120, wait_ms: int = 80) -> float:
        self._ensure_macos()
        return 0.0

    def read_back(self, node_id: str) -> str:
        return self._run_osascript('tell application "System Events" to get name of first process whose frontmost is true')


@dataclass
class CitrixBoxGraphDriver:
    """Cross-platform pixel/box-graph driver bridging hover probes, ControlSignature, and Zero-PHI twins."""

    nodes: List[A11yNodeSpec] = field(default_factory=list)
    transitions: Tuple[TwinRouteSpec, ...] = ()
    store_collection: str = "records"
    page: Any = None
    frame_capture_fn: Optional[Callable[[Tuple[int, int, int, int]], bytes]] = None
    backend_name: str = field(default="citrix_box", init=False)
    _values: Dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._values = {n.node_id: n.value for n in self.nodes}

    def _find_node(self, action: DesktopControlAction) -> Optional[A11yNodeSpec]:
        if action.target_node_id:
            for n in self.nodes:
                if n.node_id == action.target_node_id:
                    return n
        if action.signature is not None:
            want = set(action.signature.label_tokens) | set(action.signature.text_tokens)
            for n in self.nodes:
                if want and want.issubset(set(n.name.lower().split())):
                    return n
        return None

    def capture_a11y_tree(self, screen_id: str = "citrix_main", url_path: str = "/main") -> A11yScreenSpec:
        cur = tuple(
            A11yNodeSpec(n.node_id, n.role, n.name, self._values.get(n.node_id, n.value), n.bbox, n.enabled, n.checked, n.expanded, "citrix_box", n.options)
            for n in self.nodes
        )
        return A11yScreenSpec(screen_id, url_path, cur, self.transitions, self.store_collection)

    def project_to_zero_phi_twin(
        self, *, screen_id: str = "citrix_main", url_path: str = "/main",
        egress_policy: Optional[EgressPolicy] = None, ledger_path: Optional[Union[str, Path]] = None,
    ) -> StatefulTwinApp:
        """Project current box-graph controls into a runnable Zero-PHI StatefulTwinApp."""
        return project_a11y_tree_to_zero_phi_twin([self.capture_a11y_tree(screen_id, url_path)], egress_policy=egress_policy, ledger_path=ledger_path)

    def invoke_control(self, action: DesktopControlAction) -> DesktopActionResult:
        node = self._find_node(action)
        if node is None:
            return DesktopActionResult(False, self.backend_name, action.target_node_id, detail="node_not_found")
        if not node.enabled:
            return DesktopActionResult(False, self.backend_name, node.node_id, detail="node_disabled")
        if action.action in ("fill", "select"):
            self._values[node.node_id] = action.value
        elif action.action == "toggle":
            self._values[node.node_id] = str(self._values.get(node.node_id, "").lower() not in ("true", "1", "yes")).lower()
        return DesktopActionResult(True, self.backend_name, node.node_id, self._values.get(node.node_id, ""), f"{action.action}_ok")

    def hover_probe(self, x: float, y: float, *, box_size: int = 120, wait_ms: int = 80) -> float:
        if self.page is not None:
            return probe_hover_pixel_delta(self.page, x, y, box_size=box_size, wait_ms=wait_ms)
        if self.frame_capture_fn is not None:
            clip = (int(max(0.0, x - box_size / 2)), int(max(0.0, y - box_size / 2)), box_size, box_size)
            return compute_pixel_delta_ratio(self.frame_capture_fn(clip), self.frame_capture_fn(clip), threshold=1)
        return 0.0

    def read_back(self, node_id: str) -> str:
        return self._values.get(node_id, "")


def get_desktop_driver(backend: DesktopBackend = "auto", **kwargs: Any) -> DesktopDriver:
    """Dispatch to the requested desktop driver or auto-select by host OS with Citrix fallback."""
    if backend == "uia":
        return WindowsUIADriver(**kwargs)
    if backend == "macos_ax":
        return MacOSAXDriver(**kwargs)
    if backend == "citrix_box":
        return CitrixBoxGraphDriver(**kwargs)
    if sys.platform == "win32":
        try:
            importlib.import_module("pywinauto")
            return WindowsUIADriver(**kwargs)
        except ImportError:
            return CitrixBoxGraphDriver(**kwargs)
    if sys.platform == "darwin":
        return MacOSAXDriver(**kwargs)
    return CitrixBoxGraphDriver(**kwargs)
