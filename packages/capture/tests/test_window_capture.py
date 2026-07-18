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
import sys
import time

import pytest
from PIL import Image

from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.window_capture import (
    TargetWindow,
    WindowCaptureError,
    WindowCaptureScope,
    WindowTarget,
    build_window_scope,
    translate_point,
)

# Recorder requires pynput which needs a display server
try:
    from openadapt_capture.recorder import Recorder
except ImportError:
    Recorder = None


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
        _, changed = scope.capture_frame()
        assert changed is True

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

    def test_missing_window_raises_loudly(self, scope, fake):
        fake.missing = True
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()

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

    @pytest.mark.skipif(Recorder is None, reason="pynput unavailable (headless)")
    def test_recorder_accepts_window_param(self):
        rec = Recorder(
            "/tmp/test_never_created",
            task_description="test",
            window={"owner": "Parallels", "title": None},
        )
        assert rec._recording_config.window_owner == "Parallels"
        assert rec._recording_config.window_title is None

    @pytest.mark.skipif(Recorder is None, reason="pynput unavailable (headless)")
    def test_recorder_without_window_param_records_fullscreen(self):
        rec = Recorder("/tmp/test_never_created")
        assert rec._recording_config.window_owner is None
        assert rec._recording_config.window_title is None

    @pytest.mark.skipif(Recorder is None, reason="pynput unavailable (headless)")
    def test_recorder_rejects_bad_window_spec(self):
        with pytest.raises(ValueError):
            Recorder("/tmp/test_never_created", window={"app": "Parallels"})


# ---------------------------------------------------------------------------
# Coordinate translation through the recorder's action path (no display)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(Recorder is None, reason="pynput unavailable (headless)")
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
        trigger_action_event(
            q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0}, scope
        )
        (event,) = self._drain(q)
        assert event.data["mouse_x"] == 20.0
        assert event.data["mouse_y"] == 40.0

    def test_mouse_action_before_first_frame_discarded(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        trigger_action_event(
            q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0}, scope
        )
        assert self._drain(q) == []  # discarded loudly, not mis-recorded

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
        capture_path = self._insert_recording(
            str(tmp_path / "cap"), {"capture_window": scope_info}
        )
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


@pytest.mark.slow
@pytest.mark.skipif(not _ON_SUPPORTED_PLATFORM, reason=_PLATFORM_SKIP_REASON)
@pytest.mark.skipif(_NO_INPUT_INJECTION, reason=_SESSION_SKIP_REASON)
class TestWindowCaptureLive:
    """Capture a real window end to end (resolve -> frame -> translate)."""

    def _scope(self) -> WindowCaptureScope:
        scope = WindowCaptureScope(
            WindowTarget(owner=_SMOKE_OWNER, title=_SMOKE_TITLE)
        )
        try:
            scope.resolve()
        except WindowCaptureError:
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

    def test_live_missing_window_fails_loud(self):
        scope = WindowCaptureScope(
            WindowTarget(owner="no-such-app-obviously-not-running-xyz")
        )
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()
