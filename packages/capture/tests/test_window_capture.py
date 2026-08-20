"""Tests for window-scoped recording (openadapt_capture/window_capture.py).

Unit tests run everywhere with NO display: the platform resolver/capturer are
injected fakes, so coordinate translation, bounds-timeline tracking, config
plumbing, and persistence are all exercised headless.

The live smoke test (TestWindowCaptureLive) captures a REAL window and is
gated like the input-injection tests in tests/test_performance.py: marked
'slow', skipped on unsupported platforms and when
OPENADAPT_CI_NO_INPUT_INJECTION=1 (hosted-runner session limitation). Run it
on an interactive macOS/Windows desktop (e.g. the Parallels rig):

    OPENADAPT_WINDOW_SMOKE_OWNER=Parallels pytest tests/test_window_capture.py -m slow
"""

import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.recorder import Recorder, read_screen_events
from openadapt_capture.window_capture import (
    TargetWindow,
    WindowCaptureError,
    WindowCaptureScope,
    WindowTarget,
    build_window_scope,
    translate_point,
)

# ---------------------------------------------------------------------------
# translate_point: exact inverse of flow's replay mapping
# ---------------------------------------------------------------------------


class TestTranslatePoint:
    """Coordinate translation: global screen points -> window pixels."""

    def test_identity_at_origin_scale_1(self):
        assert translate_point(10.0, 20.0, (0.0, 0.0, 100.0, 100.0), 1.0) == (
            10.0,
            20.0,
        )

    def test_offset_window(self):
        # Window at (300, 150): a global point inside it becomes relative.
        assert translate_point(310.0, 170.0, (300.0, 150.0, 800.0, 600.0), 1.0) == (
            10.0,
            20.0,
        )

    def test_retina_scale(self):
        # 2x backing scale: window points map to twice the pixels.
        assert translate_point(310.0, 170.0, (300.0, 150.0, 800.0, 600.0), 2.0) == (
            20.0,
            40.0,
        )

    def test_inverse_of_flow_replay_mapping(self):
        """Round-trip through flow's ``_to_screen``: screen = origin + px/scale."""
        bounds = (37.0, 59.0, 1024.0, 768.0)
        scale = 2.0
        for sx, sy in [(37.0, 59.0), (549.0, 443.0), (1061.0, 827.0)]:
            px, py = translate_point(sx, sy, bounds, scale)
            # flow replay maps the pixel back to a screen point:
            rx, ry = bounds[0] + px / scale, bounds[1] + py / scale
            assert (rx, ry) == (sx, sy)

    def test_out_of_window_points_not_clamped(self):
        """Input outside the window records out-of-range (not clamped) pixels."""
        px, py = translate_point(100.0, 100.0, (300.0, 150.0, 800.0, 600.0), 2.0)
        assert px == -400.0 and py == -100.0


# ---------------------------------------------------------------------------
# WindowTarget spec parsing
# ---------------------------------------------------------------------------


class TestWindowTarget:
    """WindowTarget.from_spec validation (the Recorder(window=...) shape)."""

    def test_none_spec_is_none(self):
        assert WindowTarget.from_spec(None) is None

    def test_dict_spec(self):
        target = WindowTarget.from_spec({"owner": "Parallels", "title": None})
        assert target == WindowTarget(owner="Parallels", title=None)

    def test_title_only(self):
        target = WindowTarget.from_spec({"title": "Accuro"})
        assert target.owner is None and target.title == "Accuro"

    def test_passthrough(self):
        target = WindowTarget(owner="Citrix")
        assert WindowTarget.from_spec(target) is target

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError, match="unknown window spec keys"):
            WindowTarget.from_spec({"owner": "Parallels", "app": "x"})

    def test_empty_spec_rejected(self):
        with pytest.raises(ValueError, match="owner.*title"):
            WindowTarget.from_spec({"owner": None, "title": None})

    def test_wrong_type_rejected(self):
        with pytest.raises(TypeError):
            WindowTarget.from_spec("Parallels")

    def test_build_window_scope_none_when_unconfigured(self):
        assert build_window_scope(None, None) is None

    def test_build_window_scope_with_owner(self):
        scope = build_window_scope("Parallels", None)
        assert isinstance(scope, WindowCaptureScope)
        assert scope.target.owner == "Parallels"


# ---------------------------------------------------------------------------
# WindowCaptureScope with injected fakes (no display)
# ---------------------------------------------------------------------------


class FakePlatform:
    """Injectable resolver/capturer simulating a movable Retina window."""

    def __init__(self, bounds=(300.0, 150.0, 800.0, 600.0), scale=2.0):
        self.bounds = bounds
        self.scale = scale
        self.window_id = 42
        self.title = "Fake Window"
        self.missing = False

    def resolver(self, target: WindowTarget):
        if self.missing:
            return None
        return TargetWindow(
            window_id=self.window_id,
            owner="FakeApp",
            title=self.title,
            pid=1234,
            bounds=self.bounds,
        )

    def capturer(self, window: TargetWindow) -> Image.Image:
        w = int(window.bounds[2] * self.scale)
        h = int(window.bounds[3] * self.scale)
        return Image.new("RGB", (w, h), color=(1, 2, 3))


@pytest.fixture
def fake():
    return FakePlatform()


@pytest.fixture
def scope(fake):
    return WindowCaptureScope(
        WindowTarget(owner="FakeApp"),
        resolver=fake.resolver,
        capturer=fake.capturer,
    )


class TestWindowCaptureScope:
    """Bounds tracking, scale computation, and translation via fakes."""

    def test_capture_frame_returns_window_pixels(self, scope):
        image, changed = scope.capture_frame()
        assert changed is True  # first frame always establishes the timeline
        assert image.size == (1600, 1200)  # 800x600 points at 2x

    def test_scale_computed_from_frame_and_bounds(self, scope):
        scope.capture_frame()
        assert scope.snapshot()["scale"] == 2.0

    def test_unchanged_window_not_reannounced(self, scope):
        scope.capture_frame()
        _, changed = scope.capture_frame()
        assert changed is False

    def test_moved_window_flagged_changed(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (500.0, 250.0, 800.0, 600.0)  # window moved
        _, changed = scope.capture_frame()
        assert changed is True

    def test_resized_window_flagged_changed(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (300.0, 150.0, 900.0, 700.0)
        image, changed = scope.capture_frame()
        assert changed is True
        assert image.size == (1600, 1200)
        state = scope.window_event_data()["state"]
        assert state["viewport"] == [1600, 1200]
        assert state["source_viewport"] == [1800, 1400]

    def test_resize_letterboxes_and_translates_into_fixed_viewport(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (300.0, 150.0, 400.0, 600.0)

        image, changed = scope.capture_frame()

        assert changed is True
        assert image.size == (1600, 1200)
        state = scope.window_event_data()["state"]
        assert state["source_viewport"] == [800, 1200]
        assert state["content_rect"] == [400, 0, 800, 1200]
        assert state["fit_scale"] == 1.0
        assert scope.translate(300.0, 150.0) == (400.0, 0.0)
        assert scope.translate(500.0, 450.0) == (800.0, 600.0)

    def test_resize_uses_exact_axis_scales_after_integer_rounding(self, fake):
        fake.bounds = (0.0, 0.0, 3.0, 3.0)
        images = iter(
            [
                Image.new("RGB", (5, 5)),
                Image.new("RGB", (4, 3)),
            ]
        )
        scope = WindowCaptureScope(
            WindowTarget(owner="Parallels"),
            resolver=fake.resolver,
            capturer=lambda _window: next(images),
        )
        scope.capture_frame()
        scope.capture_frame()

        state = scope.window_event_data()["state"]
        assert state["content_rect"] == [0, 0, 5, 4]
        assert state["scale_x"] == pytest.approx(5 / 3)
        assert state["scale_y"] == pytest.approx(4 / 3)
        assert scope.translate(1.5, 1.5) == pytest.approx((2.5, 2.0))

    def test_translate_before_first_frame_raises(self, scope):
        with pytest.raises(WindowCaptureError, match="before the first"):
            scope.translate(400.0, 300.0)

    def test_translate_uses_latest_bounds(self, scope, fake):
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (20.0, 40.0)
        # Window moves; after the next frame the SAME global point maps
        # relative to the new origin.
        fake.bounds = (100.0, 50.0, 800.0, 600.0)
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (420.0, 240.0)

    def test_resolve_does_not_mix_new_bounds_with_previous_frame(self, scope, fake):
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (20.0, 40.0)

        fake.bounds = (100.0, 50.0, 800.0, 600.0)
        scope.resolve()

        # A resolver poll alone cannot commit geometry. Translation changes
        # only after the corresponding frame has been captured.
        assert scope.translate(310.0, 170.0) == (20.0, 40.0)
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (420.0, 240.0)

    def test_window_identity_change_terminates_scope(self, scope, fake):
        scope.capture_frame()
        fake.window_id = 99

        with pytest.raises(WindowCaptureError, match="changed window identity"):
            scope.capture_frame()

    def test_missing_window_raises_loudly(self, scope, fake):
        fake.missing = True
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()

    def test_screen_reader_propagates_capture_failure_without_retry(self, scope, fake):
        fake.missing = True

        with pytest.raises(WindowCaptureError, match="no window matching"):
            read_screen_events(
                queue.Queue(),
                threading.Event(),
                SimpleNamespace(timestamp=time.time()),
                threading.Event(),
                window_scope=scope,
            )

    def test_window_event_data_matches_window_event_columns(self, scope):
        scope.capture_frame()
        data = scope.window_event_data()
        # Keys are exactly the WindowEvent insert payload.
        assert set(data) == {
            "title",
            "left",
            "top",
            "width",
            "height",
            "window_id",
            "state",
        }
        assert data["left"] == 300 and data["top"] == 150
        assert data["width"] == 800 and data["height"] == 600
        assert data["window_id"] == "42"
        state = data["state"]
        assert state["window_capture"] is True
        assert state["scale"] == 2.0
        assert state["bounds"] == [300.0, 150.0, 800.0, 600.0]
        assert state["viewport"] == [1600, 1200]
        assert state["source_viewport"] == [1600, 1200]
        assert state["content_rect"] == [0, 0, 1600, 1200]
        assert state["fit_scale"] == 1.0

    def test_window_event_data_before_frame_raises(self, scope):
        with pytest.raises(WindowCaptureError):
            scope.window_event_data()

    def test_snapshot_shape(self, scope):
        scope.capture_frame()
        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert snap["target"] == {"owner": "FakeApp", "title": None}
        assert snap["window_id"] == 42
        assert snap["initial_bounds"] == [300.0, 150.0, 800.0, 600.0]
        assert snap["viewport"] == [1600, 1200]
        assert snap["source_viewport"] == [1600, 1200]
        assert snap["content_rect"] == [0, 0, 1600, 1200]

    def test_snapshot_before_frame_has_target_only(self, scope):
        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert "window_id" not in snap


# ---------------------------------------------------------------------------
# Config plumbing (Recorder(window=...) -> RecordingConfig -> Settings)
# ---------------------------------------------------------------------------


class TestWindowConfigPlumbing:
    """The window spec flows through config the same way other options do."""

    def test_config_override_window_fields(self):
        from openadapt_capture.config import (
            RecordingConfig,
            config,
            config_override,
        )

        assert config.RECORD_WINDOW_OWNER is None  # full-screen by default
        rc = RecordingConfig(window_owner="Parallels", window_title="Accuro")
        with config_override(rc):
            assert config.RECORD_WINDOW_OWNER == "Parallels"
            assert config.RECORD_WINDOW_TITLE == "Accuro"
        assert config.RECORD_WINDOW_OWNER is None
        assert config.RECORD_WINDOW_TITLE is None

    def test_recorder_accepts_window_param(self):
        rec = Recorder(
            "/tmp/test_never_created",
            task_description="test",
            window={"owner": "Parallels", "title": None},
        )
        assert rec._recording_config.window_owner == "Parallels"
        assert rec._recording_config.window_title is None

    def test_recorder_without_window_param_records_fullscreen(self):
        rec = Recorder("/tmp/test_never_created")
        assert rec._recording_config.window_owner is None
        assert rec._recording_config.window_title is None

    def test_recorder_rejects_bad_window_spec(self):
        with pytest.raises(ValueError):
            Recorder("/tmp/test_never_created", window={"app": "Parallels"})


# ---------------------------------------------------------------------------
# Coordinate translation through the recorder's action path (no display)
# ---------------------------------------------------------------------------


class TestActionTranslation:
    """trigger_action_event translates coordinates in window mode."""

    def _drain(self, q):
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def test_mouse_action_translated(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        scope.capture_frame()
        q = queue.Queue()
        trigger_action_event(q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0}, scope)
        (event,) = self._drain(q)
        assert event.data["mouse_x"] == 20.0
        assert event.data["mouse_y"] == 40.0

    def test_mouse_action_before_first_frame_fails_session(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        with pytest.raises(WindowCaptureError, match="before the first"):
            trigger_action_event(
                q,
                {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0},
                scope,
            )
        assert self._drain(q) == []

    def test_key_action_unaffected(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        trigger_action_event(q, {"name": "press", "key_char": "a"}, scope)
        (event,) = self._drain(q)
        assert event.data["key_char"] == "a"

    def test_no_scope_leaves_globals(self):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        trigger_action_event(q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0})
        (event,) = self._drain(q)
        assert event.data["mouse_x"] == 310.0


# ---------------------------------------------------------------------------
# Persistence: capture_window config JSON round-trips through CaptureSession
# ---------------------------------------------------------------------------


class TestWindowCapturePersistence:
    """Recording.config['capture_window'] is exposed to converters."""

    def _insert_recording(self, capture_path, config_json=None):
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        recording_data = {
            "timestamp": time.time(),
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": sys.platform,
            "task_description": "window test",
        }
        if config_json is not None:
            recording_data["config"] = config_json
        crud.insert_recording(session, recording_data)
        session.close()
        engine.dispose()
        return capture_path

    def test_window_capture_round_trips(self, tmp_path):
        scope_info = {
            "target": {"owner": "Parallels", "title": None},
            "coordinate_space": "window_pixels",
            "window_id": 42,
            "initial_bounds": [300.0, 150.0, 800.0, 600.0],
            "scale": 2.0,
            "viewport": [1600, 1200],
        }
        capture_path = self._insert_recording(str(tmp_path / "cap"), {"capture_window": scope_info})
        with CaptureSession.load(capture_path) as capture:
            assert capture.window_capture == scope_info
            assert capture.window_capture["coordinate_space"] == "window_pixels"

    def test_fullscreen_recording_has_no_window_capture(self, tmp_path):
        capture_path = self._insert_recording(str(tmp_path / "cap"))
        with CaptureSession.load(capture_path) as capture:
            assert capture.window_capture is None


# ---------------------------------------------------------------------------
# Live smoke test: capture a REAL window (interactive desktop only)
# ---------------------------------------------------------------------------

_ON_SUPPORTED_PLATFORM = sys.platform in ("darwin", "win32")
_PLATFORM_SKIP_REASON = (
    "window-scoped capture supports macOS (CGWindowListCreateImage) and "
    "Windows (Win32 + mss region grab) only; no Linux implementation"
)
# Same gate as the input-injection tests in tests/test_performance.py: on
# hosted CI runners the job executes in a non-interactive session, so there
# is no guarantee a resolvable/capturable application window exists. Run on
# an interactive desktop (developer machine or the Parallels rig).
_NO_INPUT_INJECTION = os.environ.get("OPENADAPT_CI_NO_INPUT_INJECTION") == "1"
_PRODUCTION_QUALIFICATION = os.environ.get("OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION") == "1"
_SESSION_SKIP_REASON = (
    "OPENADAPT_CI_NO_INPUT_INJECTION=1: non-interactive hosted-runner session "
    "has no guaranteed capturable application window (hosted CI limitation, "
    "not a window-capture bug); run on an interactive macOS/Windows desktop"
)
# Default smoke target: a window that exists on any logged-in desktop.
# Override on the rig: OPENADAPT_WINDOW_SMOKE_OWNER=Parallels
_SMOKE_OWNER = os.environ.get(
    "OPENADAPT_WINDOW_SMOKE_OWNER",
    "Finder" if sys.platform == "darwin" else "explorer",
)
_SMOKE_TITLE = os.environ.get("OPENADAPT_WINDOW_SMOKE_TITLE") or None


def _geometry_changed(
    current: tuple[float, float, float, float],
    original: tuple[float, float, float, float],
) -> bool:
    """Return true only after both the position and the size change."""
    moved = abs(current[0] - original[0]) >= 1 or abs(current[1] - original[1]) >= 1
    resized = abs(current[2] - original[2]) >= 1 or abs(current[3] - original[3]) >= 1
    return moved and resized


def _capture_until_bounds(
    scope: WindowCaptureScope,
    predicate,
    *,
    timeout: float = 10.0,
):
    """Capture until the live target has bounds accepted by ``predicate``."""
    deadline = time.monotonic() + timeout
    last_bounds = None
    saw_changed = False
    while time.monotonic() < deadline:
        image, changed = scope.capture_frame()
        saw_changed = saw_changed or changed
        data = scope.window_event_data()
        last_bounds = tuple(data["state"]["bounds"])
        if predicate(last_bounds):
            return image, saw_changed, data
        time.sleep(0.1)
    raise AssertionError(
        f"window bounds did not reach the required state within {timeout}s; "
        f"last bounds were {last_bounds!r}"
    )


@contextmanager
def _temporary_windows_geometry(window: TargetWindow):
    """Move and resize a normal Win32 window, then restore its exact rectangle."""
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    hwnd = wintypes.HWND(window.window_id)
    assert user32.IsWindow(hwnd), f"Win32 window {window.window_id} no longer exists"
    assert not user32.IsIconic(hwnd), "qualification target must not be minimized"
    assert not user32.IsZoomed(hwnd), "qualification target must not be maximized"

    original = wintypes.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(original)), (
        f"GetWindowRect failed for Win32 window {window.window_id}"
    )
    original_width = original.right - original.left
    original_height = original.bottom - original.top
    target_width = max(320, original_width - 137)
    target_height = max(240, original_height - 83)
    if target_width == original_width:
        target_width += 137
    if target_height == original_height:
        target_height += 83

    mutated = False
    try:
        mutated = bool(
            user32.MoveWindow(
                hwnd,
                original.left + 37,
                original.top + 29,
                target_width,
                target_height,
                True,
            )
        )
        assert mutated, f"MoveWindow failed for Win32 window {window.window_id}"
        yield
    finally:
        if mutated:
            restored = user32.MoveWindow(
                hwnd,
                original.left,
                original.top,
                original_width,
                original_height,
                True,
            )
            assert restored, f"could not restore Win32 window {window.window_id} geometry"


def _macos_ax_attribute(application_services, element, name: str):
    """Read one AX attribute and return ``None`` when it is unavailable."""
    error, value = application_services.AXUIElementCopyAttributeValue(
        element,
        name,
        None,
    )
    if error != application_services.kAXErrorSuccess:
        return None
    return value


def _macos_ax_geometry(application_services, value, value_type):
    """Read a CGPoint or CGSize from an AXValue."""
    success, geometry = application_services.AXValueGetValue(
        value,
        value_type,
        None,
    )
    assert success, "could not decode macOS accessibility geometry"
    return geometry


@contextmanager
def _temporary_macos_geometry(window: TargetWindow):
    """Move and resize one AX window, then restore its exact AX geometry."""
    import ApplicationServices

    app = ApplicationServices.AXUIElementCreateApplication(window.pid)
    ax_windows = _macos_ax_attribute(ApplicationServices, app, "AXWindows") or []
    id_matches = []
    title_matches = []
    for candidate in ax_windows:
        candidate_number = _macos_ax_attribute(
            ApplicationServices,
            candidate,
            "AXWindowNumber",
        )
        if candidate_number is not None and int(candidate_number) == window.window_id:
            id_matches.append(candidate)
        candidate_title = _macos_ax_attribute(
            ApplicationServices,
            candidate,
            "AXTitle",
        )
        if candidate_title is not None and str(candidate_title) == window.title:
            title_matches.append(candidate)

    if id_matches:
        ax_window = id_matches[0]
    else:
        assert len(title_matches) == 1, (
            "the macOS qualification target must expose a unique Accessibility "
            f"window title; found {len(title_matches)} matches for {window.title!r}"
        )
        ax_window = title_matches[0]

    fullscreen = _macos_ax_attribute(
        ApplicationServices,
        ax_window,
        "AXFullScreen",
    )
    movable = _macos_ax_attribute(ApplicationServices, ax_window, "AXMovable")
    resizable = _macos_ax_attribute(ApplicationServices, ax_window, "AXResizable")
    assert not fullscreen, "qualification target must not be full screen"
    assert movable is not False, "qualification target must be movable"
    assert resizable is not False, "qualification target must be resizable"

    original_position = _macos_ax_attribute(
        ApplicationServices,
        ax_window,
        "AXPosition",
    )
    original_size = _macos_ax_attribute(ApplicationServices, ax_window, "AXSize")
    assert original_position is not None and original_size is not None, (
        "qualification target does not expose mutable Accessibility geometry"
    )
    point = _macos_ax_geometry(
        ApplicationServices,
        original_position,
        ApplicationServices.kAXValueCGPointType,
    )
    size = _macos_ax_geometry(
        ApplicationServices,
        original_size,
        ApplicationServices.kAXValueCGSizeType,
    )
    target_width = max(320.0, float(size.width) - 137.0)
    target_height = max(240.0, float(size.height) - 83.0)
    if target_width == float(size.width):
        target_width += 137.0
    if target_height == float(size.height):
        target_height += 83.0

    target_position_value = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGPointType,
        (float(point.x) + 37.0, float(point.y) + 29.0),
    )
    target_size_value = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGSizeType,
        (target_width, target_height),
    )

    mutated = False
    try:
        size_error = ApplicationServices.AXUIElementSetAttributeValue(
            ax_window,
            "AXSize",
            target_size_value,
        )
        mutated = size_error == ApplicationServices.kAXErrorSuccess
        assert mutated, f"could not resize macOS window (AX error {size_error})"
        position_error = ApplicationServices.AXUIElementSetAttributeValue(
            ax_window,
            "AXPosition",
            target_position_value,
        )
        assert position_error == ApplicationServices.kAXErrorSuccess, (
            f"could not move macOS window (AX error {position_error})"
        )
        yield
    finally:
        if mutated:
            size_error = ApplicationServices.AXUIElementSetAttributeValue(
                ax_window,
                "AXSize",
                original_size,
            )
            position_error = ApplicationServices.AXUIElementSetAttributeValue(
                ax_window,
                "AXPosition",
                original_position,
            )
            assert size_error == ApplicationServices.kAXErrorSuccess, (
                f"could not restore macOS window size (AX error {size_error})"
            )
            assert position_error == ApplicationServices.kAXErrorSuccess, (
                f"could not restore macOS window position (AX error {position_error})"
            )


@contextmanager
def _temporary_window_geometry(window: TargetWindow):
    """Dispatch a reversible live geometry change to the current platform."""
    if sys.platform == "win32":
        with _temporary_windows_geometry(window):
            yield
        return
    if sys.platform == "darwin":
        with _temporary_macos_geometry(window):
            yield
        return
    raise AssertionError(f"no live window geometry controller for {sys.platform}")


@pytest.mark.slow
@pytest.mark.skipif(not _ON_SUPPORTED_PLATFORM, reason=_PLATFORM_SKIP_REASON)
@pytest.mark.skipif(_NO_INPUT_INJECTION, reason=_SESSION_SKIP_REASON)
class TestWindowCaptureLive:
    """Capture a real window end to end (resolve -> frame -> translate)."""

    def _scope(self) -> WindowCaptureScope:
        scope = WindowCaptureScope(WindowTarget(owner=_SMOKE_OWNER, title=_SMOKE_TITLE))
        try:
            scope.resolve()
        except WindowCaptureError as exc:
            if _PRODUCTION_QUALIFICATION:
                raise AssertionError(
                    f"production qualification requires an on-screen window "
                    f"matching owner {_SMOKE_OWNER!r} title {_SMOKE_TITLE!r}"
                ) from exc
            pytest.skip(
                f"no on-screen window matching owner {_SMOKE_OWNER!r} "
                f"title {_SMOKE_TITLE!r} on this desktop; open one (or set "
                "OPENADAPT_WINDOW_SMOKE_OWNER) to run the live smoke test"
            )
        return scope

    def test_live_window_frame_and_translation(self):
        scope = self._scope()
        image, changed = scope.capture_frame()
        assert changed is True
        assert image.width > 0 and image.height > 0

        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert snap["scale"] > 0
        x, y, w, h = snap["initial_bounds"]
        assert w > 0 and h > 0

        # The window's center (global points) must translate to the center
        # of the captured frame (pixels), within a pixel of rounding.
        cx, cy = scope.translate(x + w / 2, y + h / 2)
        assert abs(cx - image.width / 2) <= max(2.0, snap["scale"])
        assert abs(cy - image.height / 2) <= max(2.0, snap["scale"])

        # Bounds-timeline payload is writable as a WindowEvent.
        data = scope.window_event_data()
        assert data["state"]["viewport"] == [image.width, image.height]

    @pytest.mark.skipif(
        not _PRODUCTION_QUALIFICATION,
        reason=(
            "live window move/resize changes are reserved for explicit "
            "OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION=1 runs"
        ),
    )
    def test_live_move_resize_preserves_fixed_viewport_and_restores_window(self):
        """Prove live move/resize normalization without changing final app state."""
        discovery_scope = self._scope()
        target = discovery_scope.resolve()
        assert target.title.strip(), (
            "production qualification requires a target with a stable window title"
        )

        # Bind this test to the exact resolved application/title. This prevents
        # an owner-only selector from switching to another large window after
        # the target changes size.
        scope = WindowCaptureScope(WindowTarget(owner=target.owner, title=target.title))
        initial_image, initial_changed = scope.capture_frame()
        assert initial_changed is True
        initial_data = scope.window_event_data()
        initial_state = initial_data["state"]
        initial_bounds = tuple(initial_state["bounds"])
        initial_viewport = initial_state["viewport"]
        initial_source_viewport = initial_state["source_viewport"]

        with _temporary_window_geometry(target):
            moved_image, moved_changed, moved_data = _capture_until_bounds(
                scope,
                lambda bounds: _geometry_changed(bounds, initial_bounds),
            )

            assert moved_changed is True
            assert moved_data["window_id"] == str(target.window_id)
            moved_state = moved_data["state"]
            assert moved_image.size == tuple(initial_viewport)
            assert moved_state["viewport"] == initial_viewport
            assert moved_state["source_viewport"] != initial_source_viewport

            # A changed aspect ratio must be represented by a content rectangle
            # inside the fixed output viewport, not by a dropped or stretched
            # frame.
            content_x, content_y, content_width, content_height = moved_state["content_rect"]
            assert 0 <= content_x < initial_viewport[0]
            assert 0 <= content_y < initial_viewport[1]
            assert 0 < content_width <= initial_viewport[0]
            assert 0 < content_height <= initial_viewport[1]
            assert [content_x, content_y, content_width, content_height] != [
                0,
                0,
                *initial_viewport,
            ]

            # Input at the live window center must map to the center of the
            # non-letterboxed content, even after the move and resize.
            x, y, width, height = moved_state["bounds"]
            px, py = scope.translate(x + width / 2, y + height / 2)
            tolerance = max(3.0, float(moved_state["scale"]))
            assert px == pytest.approx(content_x + content_width / 2, abs=tolerance)
            assert py == pytest.approx(content_y + content_height / 2, abs=tolerance)

        restored_image, restored_changed, restored_data = _capture_until_bounds(
            scope,
            lambda bounds: all(
                abs(current - original) <= 4 for current, original in zip(bounds, initial_bounds)
            ),
        )
        assert restored_changed is True
        assert restored_image.size == tuple(initial_viewport)
        assert restored_data["window_id"] == str(target.window_id)

    def test_live_missing_window_fails_loud(self):
        scope = WindowCaptureScope(WindowTarget(owner="no-such-app-obviously-not-running-xyz"))
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()
