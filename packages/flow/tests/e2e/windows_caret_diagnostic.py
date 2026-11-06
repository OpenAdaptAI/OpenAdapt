"""Test-only owned-control observation; never an input-admission predicate."""

from __future__ import annotations

import hashlib
import io
import math
import time
from typing import Any, Callable

from PIL import Image

from tests.e2e.win_agent_capture_telemetry import compare_captured_rasters

SAMPLE_COUNT = 8
_OBSERVATION_KEYS = {
    "owner",
    "focus",
    "caret",
    "visible",
    "rect",
    "origin",
    "virtual_size",
    "anchor_matches",
    "dpi",
    "blink_ms",
    "capture_origin",
    "capture_size",
    "single_monitor",
}


def _rect(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(v) is int for v in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def _valid_observation(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _OBSERVATION_KEYS
        and all(
            type(value[k]) is bool
            for k in ("owner", "focus", "anchor_matches", "single_monitor")
        )
        and isinstance(value["caret"], str)
        and value["caret"] in {"owned", "absent", "unavailable", "foreign"}
        and (value["visible"] is None or type(value["visible"]) is bool)
        and (value["rect"] is None or _rect(value["rect"]))
        and isinstance(value["origin"], list)
        and len(value["origin"]) == 2
        and all(type(v) is int and -32768 <= v <= 32768 for v in value["origin"])
        and isinstance(value["capture_origin"], list)
        and all(type(v) is int for v in value["capture_origin"])
        and value["capture_origin"] == value["origin"]
        and isinstance(value["capture_size"], list)
        and all(type(v) is int for v in value["capture_size"])
        and value["capture_size"] == value["virtual_size"]
        and isinstance(value["virtual_size"], list)
        and len(value["virtual_size"]) == 2
        and all(type(v) is int and 0 < v <= 16384 for v in value["virtual_size"])
        and isinstance(value["dpi"], list)
        and len(value["dpi"]) == 2
        and all(type(v) is int for v in value["dpi"])
        and 0 <= value["dpi"][0] <= 500
        and 1 <= value["dpi"][1] <= 960
        and type(value["blink_ms"]) is int
    )


def project(samples: list[dict]) -> dict:
    """Project only bounded facts; malformed observations disclose no values."""
    output = {"status": "UNKNOWN", "code": "invalid_observation", "captures": 0}
    if not isinstance(samples, list) or len(samples) != SAMPLE_COUNT:
        return {**output, "code": "sample_count"}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "before",
            "after",
            "started_ns",
            "ended_ns",
            "scheduled_ns",
            "sha256",
            "size",
            "difference",
        }:
            return output
        if not all(_valid_observation(sample[k]) for k in ("before", "after")):
            return output
        if (
            any(
                type(sample[k]) is not int or not 0 <= sample[k] < 2**63
                for k in ("started_ns", "ended_ns", "scheduled_ns")
            )
            or sample["ended_ns"] < sample["started_ns"]
            or not isinstance(sample["sha256"], str)
            or len(sample["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in sample["sha256"])
            or not isinstance(sample["size"], list)
            or any(type(v) is not int for v in sample["size"])
            or sample["size"] != sample["before"]["virtual_size"]
        ):
            return output
    output = {
        **output,
        "captures": SAMPLE_COUNT,
        "dimensions": samples[0]["size"],
        "frame_sha256": [s["sha256"] for s in samples],
        "capture_duration_ms": [
            round((s["ended_ns"] - s["started_ns"]) / 1e6, 3) for s in samples
        ],
        "lateness_ms": [
            round(max(0, s["started_ns"] - s["scheduled_ns"]) / 1e6, 3) for s in samples
        ],
        "visibility": [s["before"]["visible"] for s in samples],
        "changed_pixel_count": [],
        "changed_bbox": [],
        "transition_pairs": 0,
    }
    first = samples[0]["before"]
    for s in samples:
        a, b = s["before"], s["after"]
        if not (a["owner"] and b["owner"] and a["focus"] and b["focus"]):
            return {**output, "code": "ownership_unavailable"}
        if a["caret"] != "owned" or b["caret"] != "owned":
            return {**output, "code": "caret_unavailable"}
        period = a["blink_ms"]
        if not (1 <= period <= 2000 and period == b["blink_ms"] == first["blink_ms"]):
            return {**output, "code": "blink_unavailable"}
        if (
            a != b
            or type(a["visible"]) is not bool
            or s["ended_ns"] - s["started_ns"] >= period * 1_000_000
        ):
            return {**output, "code": "capture_bracket_ambiguous"}
        if (
            not a["anchor_matches"]
            or not a["single_monitor"]
            or a["dpi"] != [100, 96]
            or a["origin"] != [0, 0]
            or not _rect(a["rect"])
            or a["origin"] != first["origin"]
            or a["dpi"] != first["dpi"]
            or a["virtual_size"] != first["virtual_size"]
            or a["rect"] != first["rect"]
            or s["size"] != output["dimensions"]
        ):
            return {**output, "code": "mapping_unavailable"}
        x1, y1, x2, y2 = a["rect"]
        width, height = s["size"]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            return {**output, "code": "mapping_unavailable"}
    transitions = 0
    for left, right in zip(samples, samples[1:]):
        diff = right["difference"]
        if (
            not isinstance(diff, dict)
            or diff.get("status") != "available"
            or diff.get("code") != "raster_compared"
            or diff.get("expected_dimensions") != output["dimensions"]
            or diff.get("current_dimensions") != output["dimensions"]
            or type(diff.get("changed_pixel_count")) is not int
        ):
            return {**output, "code": "difference_unavailable"}
        count, bbox = diff["changed_pixel_count"], diff.get("changed_bbox")
        if not (0 <= count <= math.prod(output["dimensions"])) or (
            (count == 0 and bbox is not None)
            or (
                count > 0
                and (
                    not _rect(bbox)
                    or not (
                        0 <= bbox[0] < bbox[2] <= output["dimensions"][0]
                        and 0 <= bbox[1] < bbox[3] <= output["dimensions"][1]
                    )
                    or count > (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                )
            )
        ):
            return {**output, "code": "difference_unavailable"}
        output["changed_pixel_count"].append(count)
        output["changed_bbox"].append(bbox)
        transition = left["before"]["visible"] != right["before"]["visible"]
        if transition:
            transitions += 1
        if count:
            x1, y1, x2, y2 = first["rect"]
            if not transition or not (
                x1 <= bbox[0] < bbox[2] <= x2 and y1 <= bbox[1] < bbox[3] <= y2
            ):
                return {**output, "code": "unexplained_changes"}
        elif transition:
            return {**output, "code": "unexplained_changes"}
    output["transition_pairs"] = transitions
    return {
        **output,
        "status": "CARET_CORRELATED" if transitions else "UNKNOWN",
        "code": "visibility_raster_correlation"
        if transitions
        else "no_visibility_transition",
    }


def collect(
    capture: Callable[[], bytes],
    observe: Callable[[], dict],
    *,
    clock: Callable[[], int] = time.monotonic_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Exactly eight scheduled captures; do not retry an exception or outcome."""
    initial = observe()
    period = initial.get("blink_ms")
    if type(period) is not int or not 1 <= period <= 2000:
        raise RuntimeError("caret blink period unavailable")
    start = clock()
    previous = None
    samples = []
    for index in range(SAMPLE_COUNT):
        scheduled = start + index * period * 500_000
        delay = scheduled - clock()
        if delay > 0:
            sleep(delay / 1e9)
        began = clock()
        before = observe()
        png = capture()  # Exceptions retain their original identity; no resampling.
        after = observe()
        ended = clock()
        with Image.open(io.BytesIO(png)) as image:
            if (
                image.width > 16384
                or image.height > 16384
                or image.width * image.height > 8_294_400
            ):
                raise RuntimeError("caret capture exceeds fixed size limit")
            rgb = image.convert("RGB")
            current = (rgb.tobytes(), rgb.width, rgb.height)
        difference = compare_captured_rasters(previous, current) if previous else None
        samples.append(
            {
                "before": before,
                "after": after,
                "started_ns": began,
                "ended_ns": ended,
                "scheduled_ns": scheduled,
                "sha256": hashlib.sha256(png).hexdigest(),
                "size": list(current[1:]),
                "difference": difference,
            }
        )
        previous = current
    return samples


def close_owned(
    processes: dict, cleanup: dict, primary: Exception | None
) -> Exception | None:
    """Independently close and really wait each exact fixture Popen object."""
    for name, process in processes.items():
        cleanup[name + "_started"] = process is not None
        if process is None:
            continue
        try:
            if process.poll() is None:
                process.kill()
        except Exception as exc:  # noqa: BLE001 - still wait this child and close the other
            if primary is None:
                primary = exc
        try:
            process.wait(timeout=10)
            cleanup[name + "_waited"] = True
        except Exception as exc:  # noqa: BLE001 - preserve first error; attempt both children
            if primary is None:
                primary = exc
    return primary


class OwnedCaret:
    """Read a static fixture's owned handles, not a general HWND-generation proof."""

    def __init__(self, app) -> None:
        import ctypes

        import uiautomation as auto

        from openadapt_flow.backends.win32_window_client import Win32WindowClient

        self.c = ctypes
        self.app = app
        self.client = Win32WindowClient()
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.scale_api = ctypes.WinDLL("shcore", use_last_error=True)
        c = ctypes
        self.Rect = type(
            "Rect",
            (c.Structure,),
            {"_fields_": [(n, c.c_int32) for n in ("left", "top", "right", "bottom")]},
        )
        self.Point = type(
            "Point", (c.Structure,), {"_fields_": [("x", c.c_int32), ("y", c.c_int32)]}
        )
        self.Gui = type(
            "Gui",
            (c.Structure,),
            {
                "_fields_": [
                    ("size", c.c_uint32),
                    ("flags", c.c_uint32),
                    *[
                        (n, c.c_void_p)
                        for n in ("active", "focus", "capture", "menu", "move", "caret")
                    ],
                    ("rect", self.Rect),
                ]
            },
        )
        for dll, name, result, args in (
            (
                self.u,
                "GetWindowThreadProcessId",
                c.c_uint32,
                [c.c_void_p, c.POINTER(c.c_uint32)],
            ),
            (self.u, "GetGUIThreadInfo", c.c_int, [c.c_uint32, c.POINTER(self.Gui)]),
            (self.u, "IsWindow", c.c_int, [c.c_void_p]),
            (self.u, "IsChild", c.c_int, [c.c_void_p, c.c_void_p]),
            (self.u, "GetCaretBlinkTime", c.c_uint32, []),
            (self.u, "SetThreadDpiAwarenessContext", c.c_void_p, [c.c_void_p]),
            (self.u, "GetDpiForWindow", c.c_uint32, [c.c_void_p]),
            (self.u, "GetWindowRect", c.c_int, [c.c_void_p, c.POINTER(self.Rect)]),
            (self.u, "ClientToScreen", c.c_int, [c.c_void_p, c.POINTER(self.Point)]),
            (self.u, "GetSystemMetrics", c.c_int, [c.c_int]),
            (self.u, "MonitorFromWindow", c.c_void_p, [c.c_void_p, c.c_uint32]),
            (
                self.scale_api,
                "GetScaleFactorForMonitor",
                c.c_int32,
                [c.c_void_p, c.POINTER(c.c_int)],
            ),
            (self.k, "GetProcessId", c.c_uint32, [c.c_void_p]),
            (
                self.k,
                "GetProcessTimes",
                c.c_int,
                [c.c_void_p, *([c.POINTER(c.c_uint64)] * 4)],
            ),
        ):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, args
        self.process_handle = int(app._handle)
        self.creation = self._creation()
        windows = [
            w for w in self.client.find_windows("powershell", None) if w.pid == app.pid
        ]
        if len(windows) != 1:
            raise RuntimeError("owned fixture root is not unique")
        self.root = windows[0].window_id
        self.client.activate(app.pid)
        root_control = auto.ControlFromHandle(self.root)
        pending = [(root_control, 0)]
        found = []
        visited = 0
        while pending:
            node, depth = pending.pop()
            visited += 1
            if visited > 1024:
                raise RuntimeError("owned fixture tree exceeds fixed bound")
            if node.AutomationId == "noteBox" and node.ProcessId == app.pid:
                found.append(node)
            if depth < 8:
                pending.extend((child, depth + 1) for child in node.GetChildren())
            elif node.GetChildren():
                raise RuntimeError("owned fixture tree is incomplete")
        if len(found) != 1:
            raise RuntimeError("owned note control is not unique")
        self.note = found[0]
        self.hwnd = int(self.note.NativeWindowHandle)
        self.runtime_id = tuple(self.note.GetRuntimeId())
        if not self.runtime_id or self.note.SetFocus() is False:
            raise RuntimeError("owned note control did not accept focus")
        first = self.observe()
        if not first["owner"] or not first["focus"]:
            raise RuntimeError("owned note binding is unavailable")

    def _creation(self) -> int:
        c = self.c
        times = [c.c_uint64() for _ in range(4)]
        if not self.k.GetProcessTimes(
            self.process_handle, *[c.byref(v) for v in times]
        ):
            raise RuntimeError("owned process lifetime unavailable")
        return times[0].value

    def _thread(self, hwnd: int) -> tuple[int, int]:
        pid = self.c.c_uint32()
        thread = self.u.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
        return int(thread), pid.value

    def _mapping(self, gui) -> dict:
        """Support only a verified 100%, 96-DPI, origin-zero single monitor."""
        import mss

        c, u = self.c, self.u
        old = u.SetThreadDpiAwarenessContext(c.c_void_p(-4))
        if not old:
            raise RuntimeError("physical observation context unavailable")
        try:
            origin = [u.GetSystemMetrics(76), u.GetSystemMetrics(77)]
            size = [u.GetSystemMetrics(78), u.GetSystemMetrics(79)]
            with mss.mss() as screen:
                monitors = screen.monitors
                capture_origin = [monitors[0]["left"], monitors[0]["top"]]
                capture_size = [monitors[0]["width"], monitors[0]["height"]]
                single = len(monitors) == 2 and u.GetSystemMetrics(80) == 1
            scale = c.c_int()
            monitor = u.MonitorFromWindow(self.hwnd, 0)  # No default-monitor fallback.
            scale_ok = bool(
                monitor
                and self.scale_api.GetScaleFactorForMonitor(monitor, c.byref(scale))
                == 0
            )
            dpi = [scale.value if scale_ok else 0, int(u.GetDpiForWindow(self.hwnd))]
            result = {
                "rect": None,
                "origin": origin,
                "virtual_size": size,
                "capture_origin": capture_origin,
                "capture_size": capture_size,
                "single_monitor": bool(single),
                "dpi": dpi,
                "anchor_matches": False,
            }
            if not (
                single
                and dpi == [100, 96]
                and origin == capture_origin == [0, 0]
                and size == capture_size
            ):
                return result
            # At independently verified 100% scaling, caret client units and
            # physical screen pixels share the same scale; do not guess others.
            outer = self.Rect()
            points = [
                self.Point(gui.rect.left, gui.rect.top),
                self.Point(gui.rect.right, gui.rect.bottom),
            ]
            if not u.GetWindowRect(self.hwnd, c.byref(outer)) or not all(
                u.ClientToScreen(self.hwnd, c.byref(point)) for point in points
            ):
                return result
            bounds = self.note.BoundingRectangle
            expected = [bounds.left, bounds.top, bounds.right, bounds.bottom]
            result["anchor_matches"] = [
                outer.left,
                outer.top,
                outer.right,
                outer.bottom,
            ] == expected
            result["rect"] = [points[0].x, points[0].y, points[1].x, points[1].y]
            return result
        finally:
            if not u.SetThreadDpiAwarenessContext(old):
                raise RuntimeError("observation context restoration failed")

    def observe(self) -> dict:
        u, c = self.u, self.c
        thread, pid = self._thread(self.hwnd)
        root_thread, root_pid = self._thread(self.root)
        owner = bool(
            self.app.poll() is None
            and self.k.GetProcessId(self.process_handle) == self.app.pid
            and self._creation() == self.creation
            and u.IsWindow(self.root)
            and u.IsWindow(self.hwnd)
            and u.IsChild(self.root, self.hwnd)
            and pid == root_pid == self.app.pid
            and thread == root_thread
            and thread > 0
            and self.note.NativeWindowHandle == self.hwnd
            and self.note.ProcessId == self.app.pid
            and self.note.AutomationId == "noteBox"
            and tuple(self.note.GetRuntimeId()) == self.runtime_id
            and self.client.key_window_id(self.app.pid) == self.root
        )
        if not owner:
            raise RuntimeError("owned static fixture handle checks failed")
        gui = self.Gui()
        gui.size = c.sizeof(self.Gui)
        available = bool(owner and u.GetGUIThreadInfo(thread, c.byref(gui)))
        return {
            "owner": owner,
            "focus": bool(available and gui.focus == self.hwnd),
            "caret": "unavailable"
            if not available
            else "absent"
            if not gui.caret
            else "owned"
            if gui.caret == self.hwnd
            else "foreign",
            "visible": bool(gui.flags & 1)
            if available and gui.caret == self.hwnd
            else None,
            **self._mapping(gui),
            "blink_ms": int(u.GetCaretBlinkTime()),
        }
