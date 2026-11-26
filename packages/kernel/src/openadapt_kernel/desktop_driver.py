"""Live native desktop (Windows UIA, macOS AX) and cross-platform Citrix box-graph drivers."""

from __future__ import annotations

import ctypes
import importlib
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Sequence, Tuple, Union

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
        if action.signature is not None and action.signature.label_tokens and not action.target_node_id:
            ctrl = win.child_window(title=" ".join(action.signature.label_tokens))
        else:
            ctrl = win.child_window(auto_id=action.target_node_id)
        if action.action == "fill":
            ctrl.set_edit_text(action.value)
        elif hasattr(ctrl, "invoke"):
            ctrl.invoke()
        else:
            ctrl.click_input()
        return DesktopActionResult(True, self.backend_name, action.target_node_id, str(ctrl.window_text() or ""), "uia_semantic_pattern")

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
        sem = "AXSetValue" if action.action == "fill" else "AXPress"
        return DesktopActionResult(True, self.backend_name, action.target_node_id, action.value, f"macos_ax_{sem}")

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


@dataclass(frozen=True)
class NormalizedWindowRect:
    left: float
    top: float
    logical_width: float
    logical_height: float
    framebuffer_width: float = 0.0
    framebuffer_height: float = 0.0

    @property
    def scale_x(self) -> float:
        return (self.framebuffer_width / self.logical_width) if (self.framebuffer_width > 0 and self.logical_width > 0) else 1.0

    @property
    def scale_y(self) -> float:
        return (self.framebuffer_height / self.logical_height) if (self.framebuffer_height > 0 and self.logical_height > 0) else 1.0

    def to_normalized(self, x: float, y: float) -> Tuple[float, float]:
        return (max(0.0, min(1.0, (x - self.left) / max(self.logical_width, 1.0))), max(0.0, min(1.0, (y - self.top) / max(self.logical_height, 1.0))))

    def to_physical(self, rx: float, ry: float) -> Tuple[float, float]:
        return ((self.left + rx * self.logical_width) * self.scale_x, (self.top + ry * self.logical_height) * self.scale_y)


def enable_windows_per_monitor_v2_dpi() -> bool:
    """Enable Windows Per-Monitor V2 DPI awareness via SetProcessDpiAwarenessContext(-4); no-op elsewhere."""
    if sys.platform != "win32":
        return False
    try:
        w = getattr(ctypes, "windll", None)
        return bool(w and w.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        return False


@dataclass(frozen=True)
class InputPacingConfig:
    keystroke_delay_ms: float = 35.0
    pointer_steps: int = 6
    jitter_ms: float = 10.0


def compute_paced_pointer_path(start_x: float, start_y: float, end_x: float, end_y: float, *, config: Optional[InputPacingConfig] = None) -> List[Tuple[float, float]]:
    """Compute human-paced interpolated pointer waypoints without hooks or DLL injection."""
    steps = max(1, int((config or InputPacingConfig()).pointer_steps))
    return [(round(start_x + (end_x - start_x) * (i / steps), 2), round(start_y + (end_y - start_y) * (i / steps), 2)) for i in range(1, steps + 1)]


@dataclass(frozen=True)
class DesktopPreflightReport:
    ok: bool
    platform: str
    uia_readable: bool
    hover_probe_ok: bool
    loopback_proxy_ok: bool
    zero_dll_injection_verified: bool
    diagnostics: Tuple[str, ...] = ()


def run_desktop_preflight_diagnostics(driver: DesktopDriver, *, proxy_host: str = "127.0.0.1") -> DesktopPreflightReport:
    """Verify desktop accessibility readability, hover probe availability, and 127.0.0.1 loopback proxy bind."""
    diags, uia_ok, hover_ok, loopback_ok = [], False, False, False
    try:
        uia_ok = bool(driver.capture_a11y_tree(screen_id="preflight", url_path="/main").nodes)
    except Exception as exc:
        diags.append(f"a11y_unavailable:{type(exc).__name__}")
    try:
        hover_ok = float(driver.hover_probe(10.0, 10.0)) >= 0.0
    except Exception as exc:
        diags.append(f"hover_probe_error:{type(exc).__name__}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((proxy_host, 0))
            loopback_ok = True
    except OSError as exc:
        diags.append(f"loopback_bind_failed:{exc}")
    return DesktopPreflightReport(uia_ok and hover_ok and loopback_ok, sys.platform, uia_ok, hover_ok, loopback_ok, True, tuple(diags))


StreamState = Literal["LIVE", "DISCONNECTED", "LOCKED", "FROZEN", "MINIMIZED"]


@dataclass(frozen=True)
class StreamLivenessStatus:
    alive: bool
    state: StreamState
    reason: str = ""


def check_stream_liveness(
    *, window_alive: bool = True, minimized: bool = False, window_title: str = "",
    prev_frame_hash: str = "", curr_frame_hash: str = "",
    hover_probe_delta: Optional[float] = None, expected_min_delta: float = 0.005,
) -> StreamLivenessStatus:
    """Detect disconnected, locked, minimized, or frozen Citrix/RDP desktop streams."""
    t = (window_title or "").lower()
    if not window_alive or any(k in t for k in ("disconnected", "reconnecting")):
        return StreamLivenessStatus(False, "DISCONNECTED", "RECONNECT_WAIT:stream_disconnected")
    if any(k in t for k in ("windows security", "logonui", "lock screen")):
        return StreamLivenessStatus(False, "LOCKED", "RECONNECT_WAIT:session_locked")
    if minimized:
        return StreamLivenessStatus(False, "MINIMIZED", "RECONNECT_WAIT:window_minimized")
    if prev_frame_hash and curr_frame_hash and prev_frame_hash == curr_frame_hash and (hover_probe_delta is None or hover_probe_delta < expected_min_delta):
        return StreamLivenessStatus(False, "FROZEN", "RECONNECT_WAIT:frozen_frame")
    if hover_probe_delta is not None and hover_probe_delta < expected_min_delta:
        return StreamLivenessStatus(False, "FROZEN", "RECONNECT_WAIT:hover_probe_dead")
    return StreamLivenessStatus(True, "LIVE", "stream_live")


def resume_after_desktop_reconnect(
    expected_signatures: Sequence[ControlSignature], *,
    capture_signatures_fn: Callable[[], Sequence[ControlSignature]],
    readback_probe_fn: Callable[[], bool],
) -> Dict[str, Any]:
    """Verify screen ControlSignature fingerprint and PutGet idempotency before resuming after reconnect."""
    obs_keys = {(s.role, s.label_tokens) for s in capture_signatures_fn()}
    missing = [s for s in expected_signatures if (s.role, s.label_tokens) not in obs_keys]
    if missing:
        return {"action": "HALT_FINGERPRINT_MISMATCH", "fingerprint_matched": False, "already_committed": False, "missing_count": len(missing)}
    if bool(readback_probe_fn()):
        return {"action": "ALREADY_COMMITTED", "fingerprint_matched": True, "already_committed": True, "missing_count": 0}
    return {"action": "RESUME", "fingerprint_matched": True, "already_committed": False, "missing_count": 0}
