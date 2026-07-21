"""Tests for the openadapt-capture -> openadapt-flow recording adapter.

These build a *real* openadapt-capture session on disk — a SQLAlchemy
``recording.db`` written through capture's own db layer plus an
``oa_recording-*.mp4`` written through capture's public ``VideoWriter`` — and
then run the adapter over capture's public API
(``CaptureSession.load(dir).actions()``). This exercises capture's real
event-processing pipeline (raw mouse/keyboard streams -> merged
clicks/drags/typed text) and real frame extraction, so the test cannot silently
pass against a schema that no longer exists. The converted recording is fed to
the UNMODIFIED compiler — the format bridge, proven end to end.

openadapt-capture >=0.6.0 imports clean headless and exposes the exact
window-scoped producer contract, so this module runs for real in headless CI —
the `test` job installs the ``capture`` extra. It is skipped only when that
optional extra is not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

# openadapt-capture is an optional extra: skip only when it is not installed.
# Since 0.5.4 the import is headless-clean (no screenshot at import), so when
# the extra IS installed — as in CI's `test` job — an import failure is a real
# regression and must fail loudly here instead of silently skipping.
if importlib.util.find_spec("openadapt_capture") is None:
    pytest.skip(
        "openadapt-capture not installed (capture extra)", allow_module_level=True
    )

import openadapt_capture  # noqa: F401
from openadapt_capture.db import create_db
from openadapt_capture.db.models import ActionEvent, Recording, WindowEvent
from openadapt_capture.video import VideoWriter
from PIL import Image, ImageDraw

from openadapt_flow.adapters.capture import _reject_out_of_window, convert_capture

# Physical (video) pixels; logical screen is half that (pixel_ratio 2.0, the
# macOS Retina case where capture coords and frame pixels disagree).
FRAME_SIZE = (1280, 800)
PIXEL_RATIO = 2.0
FPS = 24
T0 = 100000.0  # wall-clock epoch (recording.timestamp)
VIDEO_T0 = T0 + 1.0  # first frame / video_start_time

BUTTON = (560, 400, 160, 48)  # physical px
BUTTON_CENTER_PHYSICAL = (BUTTON[0] + BUTTON[2] // 2, BUTTON[1] + BUTTON[3] // 2)
BUTTON_CENTER_LOGICAL = (
    BUTTON_CENTER_PHYSICAL[0] / PIXEL_RATIO,
    BUTTON_CENTER_PHYSICAL[1] / PIXEL_RATIO,
)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"


# -- fixture drawing (PIL, so frames go straight into capture's VideoWriter) ---


def blank() -> Image.Image:
    return Image.new("RGB", FRAME_SIZE, (245, 245, 245))


def draw_text(img: Image.Image, x: int, y: int, text: str) -> None:
    ImageDraw.Draw(img).text((x, y), text, fill=(0, 0, 0))


def draw_button(img: Image.Image, x: int, y: int, w: int, h: int, label: str) -> None:
    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + w, y + h], fill=(205, 205, 205), outline=(70, 70, 70))
    draw.text((x + 12, y + h // 2 - 4), label, fill=(0, 0, 0))


def app_screens() -> list[Image.Image]:
    s0 = blank()
    draw_text(s0, 520, 70, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")
    s1 = s0.copy()
    draw_text(s1, 420, 230, BANNER_LOADED)
    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)
    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    return [s0, s1, s2, s3]


def write_video(path: Path, states: list[Image.Image]) -> None:
    """Write a *dense* action-gated-style video via capture's public VideoWriter.

    State ``k`` is shown for the wall-clock window ``[VIDEO_T0 + k - 0.5,
    VIDEO_T0 + k + 0.5)`` (transitions on the half-second), so a frame sampled
    at whole-second offset ``k`` — where this fixture's actions land — resolves
    unambiguously to state ``k`` through capture's real timestamp-based frame
    extraction (a sparse video would collide on block boundaries).
    """
    import math

    writer = VideoWriter(
        str(path),
        width=FRAME_SIZE[0],
        height=FRAME_SIZE[1],
        fps=FPS,
        crf=23,
        preset="ultrafast",
    )
    last = len(states) - 1
    offset = 0.0
    end = last + 0.6
    while offset <= end:
        k = min(int(math.floor(offset + 0.5)), last)
        writer.write_frame(states[k], VIDEO_T0 + offset)
        offset += 1.0 / FPS
    writer.close()


def write_recording_db(
    path: Path,
    action_rows: list[dict],
    config: dict | None = None,
    window_event_rows: list[dict] | None = None,
) -> None:
    """Write a real capture recording.db via capture's SQLAlchemy models."""
    engine, Session = create_db(str(path))
    session = Session()
    try:
        recording = Recording(
            timestamp=T0,
            monitor_width=FRAME_SIZE[0],
            monitor_height=FRAME_SIZE[1],
            platform="darwin",
            task_description="add a note",
            video_start_time=VIDEO_T0,
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            # config-JSON pixel_ratio: the legacy (pre-0.5.4) persistence path,
            # which CaptureSession.pixel_ratio still honors as a fallback.
            config=config if config is not None else {"pixel_ratio": PIXEL_RATIO},
        )
        session.add(recording)
        session.flush()
        for row in action_rows:
            session.add(ActionEvent(recording_id=recording.id, **row))
        for row in window_event_rows or []:
            session.add(WindowEvent(recording_id=recording.id, **row))
        session.commit()
    finally:
        session.close()
        engine.dispose()


def make_capture(
    tmp_path: Path,
    action_rows: list[dict],
    screens=None,
    config: dict | None = None,
    window_event_rows: list[dict] | None = None,
) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    write_recording_db(
        capture_dir / "recording.db",
        action_rows,
        config=config,
        window_event_rows=window_event_rows,
    )
    screens = screens if screens is not None else app_screens()
    write_video(capture_dir / f"oa_recording-{T0}.mp4", screens)
    return capture_dir


# -- raw action_event rows (capture's schema; processing merges them) ---------


def _click_rows(ts: float, x: float, y: float, button: str = "left") -> list[dict]:
    """A press+release pair -> capture merges into one mouse.singleclick."""
    return [
        {
            "name": "click",
            "timestamp": ts,
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": True,
        },
        {
            "name": "click",
            "timestamp": ts + 0.01,
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": False,
        },
    ]


def _type_rows(start_ts: float, text: str) -> list[dict]:
    """One press+release per character -> a run of key.type actions."""
    rows: list[dict] = []
    ts = start_ts
    for ch in text:
        rows.append({"name": "press", "timestamp": ts, "key_char": ch})
        rows.append({"name": "release", "timestamp": ts + 0.005, "key_char": ch})
        ts += 0.02
    return rows


def _named_key_rows(ts: float, key_name: str) -> list[dict]:
    """A named special key (no char) -> a key.type with empty text."""
    return [
        {"name": "press", "timestamp": ts, "key_name": key_name},
        {"name": "release", "timestamp": ts + 0.01, "key_name": key_name},
    ]


def demo_rows() -> list[dict]:
    """click -> type NOTE_VALUE -> Enter, at t = 1s, 2s, 3s (relative to T0)."""
    x, y = BUTTON_CENTER_LOGICAL
    rows: list[dict] = []
    rows += _click_rows(T0 + 1.0, x, y)
    rows += _type_rows(T0 + 2.0, NOTE_VALUE)
    rows += _named_key_rows(T0 + 3.0, "enter")
    return rows


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("adapter")
    capture_dir = make_capture(tmp_path, demo_rows())
    recording_dir = tmp_path / "recording"
    convert_capture(
        capture_dir, recording_dir, params={"note": NOTE_VALUE}, settle_s=1.0
    )
    return recording_dir


def events_of(recording_dir: Path) -> list[dict]:
    import json

    lines = (recording_dir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_event_mapping_and_order(converted: Path) -> None:
    events = events_of(converted)
    # The typed run coalesced into ONE type event; Enter is a separate key.
    assert [e["kind"] for e in events] == ["click", "type", "key"]
    assert events[1]["text"] == NOTE_VALUE
    assert events[1]["param"] == "note"
    assert events[2]["key"] == "Enter"
    # Recorder line-format parity: {"i", ...event fields..., "t"}.
    assert list(events[0].keys()) == ["i", "kind", "x", "y", "t"]
    assert [e["i"] for e in events] == [0, 1, 2]
    assert [e["t"] for e in events] == [1.0, 2.0, 3.0]


def test_coordinates_scaled_to_frame_pixels(converted: Path) -> None:
    # Capture points are logical (Retina /2); frames are physical pixels.
    click = events_of(converted)[0]
    assert (click["x"], click["y"]) == BUTTON_CENTER_PHYSICAL


def test_meta_matches_recorder_contract(converted: Path) -> None:
    import json

    meta = json.loads((converted / "meta.json").read_text())
    assert meta["viewport"] == list(FRAME_SIZE)
    assert meta["params"] == {"note": NOTE_VALUE}
    assert meta["app_url"] is None
    assert meta["source"] == "openadapt-capture"
    assert meta["task_description"] == "add a note"
    # Regression: a NON-window session's meta carries exactly the recorder
    # contract keys — no window-mode fields may leak into it.
    assert set(meta.keys()) == {
        "id",
        "created_at",
        "viewport",
        "app_url",
        "params",
        "source",
        "task_description",
    }


def test_frames_selected_from_video(converted: Path) -> None:
    """Before frames precede each action; after frames show its effect."""
    import cv2

    screens = [np.array(s)[:, :, ::-1] for s in app_screens()]  # RGB->BGR

    def state_of(path: Path) -> int:
        """Nearest app state (mp4 is lossy, so classify, don't compare)."""
        actual = cv2.imdecode(
            np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR
        ).astype(np.int16)
        diffs = [float(np.abs(actual - s.astype(np.int16)).mean()) for s in screens]
        return int(np.argmin(diffs))

    frames = converted / "frames"
    assert state_of(frames / "0000_before.png") == 0
    assert state_of(frames / "0000_after.png") == 1
    assert state_of(frames / "0001_before.png") == 1
    assert state_of(frames / "0001_after.png") == 2
    assert state_of(frames / "0002_before.png") == 2
    assert state_of(frames / "0002_after.png") == 3


@pytest.mark.timeout(300)
def test_converted_recording_compiles(converted: Path, tmp_path: Path) -> None:
    """The unmodified compiler accepts the adapted desktop recording."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind

    workflow = compile_recording(converted, tmp_path / "bundle", name="capture-bridge")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]
    type_step = workflow.steps[1]
    assert type_step.param == "note"


# -- scroll conversion -------------------------------------------------------


def test_scroll_notches_to_pixels_and_sign(tmp_path: Path) -> None:
    # pynput: +dy = scroll up (view up); flow: +dy = view down.
    rows = [
        {
            "name": "scroll",
            "timestamp": T0 + 1.0,
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_dx": 0.0,
            "mouse_dy": -3.0,
        },
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:2])
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir)
    (scroll,) = events_of(recording_dir)
    assert scroll["kind"] == "scroll"
    assert (scroll["dx"], scroll["dy"]) == (0, 300)


# -- loud rejection of silently-lossy inputs ----------------------------------


def test_no_actions_rejected(tmp_path: Path) -> None:
    # Mouse moves are filtered by actions(include_moves=False) -> nothing to do.
    rows = [
        {"name": "move", "timestamp": T0 + 1.0, "mouse_x": 5.0, "mouse_y": 5.0},
        {"name": "move", "timestamp": T0 + 1.5, "mouse_x": 9.0, "mouse_y": 9.0},
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="no convertible actions"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_drag_rejected_not_dropped(tmp_path: Path) -> None:
    # down -> moves -> up at a far position -> capture emits a mouse.drag.
    rows = [
        {
            "name": "click",
            "timestamp": T0 + 1.0,
            "mouse_x": 10.0,
            "mouse_y": 10.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        },
        {"name": "move", "timestamp": T0 + 1.1, "mouse_x": 60.0, "mouse_y": 60.0},
        {"name": "move", "timestamp": T0 + 1.2, "mouse_x": 120.0, "mouse_y": 120.0},
        {
            "name": "click",
            "timestamp": T0 + 1.3,
            "mouse_x": 120.0,
            "mouse_y": 120.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        },
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="mouse.drag"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_right_click_rejected(tmp_path: Path) -> None:
    rows = _click_rows(T0 + 1.0, 10.0, 10.0, button="right")
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="button='right'"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_unknown_named_key_rejected(tmp_path: Path) -> None:
    rows = _named_key_rows(T0 + 1.0, "f13")
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="f13"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_shortcut_rejected(tmp_path: Path) -> None:
    # ctrl down, c down, c up, ctrl up -> one key.type with keys=[ctrl, c].
    rows = [
        {"name": "press", "timestamp": T0 + 1.0, "key_name": "ctrl"},
        {"name": "press", "timestamp": T0 + 1.01, "key_char": "c"},
        {"name": "release", "timestamp": T0 + 1.02, "key_char": "c"},
        {"name": "release", "timestamp": T0 + 1.03, "key_name": "ctrl"},
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="shortcut"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_ambiguous_params_rejected(tmp_path: Path) -> None:
    capture_dir = make_capture(tmp_path, demo_rows())
    with pytest.raises(ValueError, match="same value"):
        convert_capture(
            capture_dir,
            tmp_path / "recording",
            params={"a": NOTE_VALUE, "b": NOTE_VALUE},
        )


def test_missing_recording_db_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        convert_capture(empty, tmp_path / "recording")


# -- window-scoped sessions (capture's window recording mode) -----------------
#
# These build a real capture session whose recording config carries the
# ``capture_window`` scoping dict that capture's window mode persists
# (window_capture.WindowFrameSource.snapshot()), with action coordinates
# ALREADY in the captured frame's pixel space. Against the PyPI 0.5.4 package
# (what CI installs) exposes this dict through CaptureSession.window_capture;
# the adapter's defensive config-JSON fallback reads the same persisted source.

WINDOW_OWNER = "MockMedRemote"
WINDOW_TITLE = "MockMed - Ward A"


def window_capture_config(**overrides) -> dict:
    """Recording config for a window-scoped session (window pixels = frame)."""
    capture_window = {
        "target": {"owner": WINDOW_OWNER, "title": None},
        "coordinate_space": "window_pixels",
        "window_id": "42",
        "owner": WINDOW_OWNER,
        "title": WINDOW_TITLE,
        "pid": 4242,
        "initial_bounds": [100.0, 50.0, FRAME_SIZE[0] / 2, FRAME_SIZE[1] / 2],
        "scale": 2.0,
        "viewport": list(FRAME_SIZE),
    }
    capture_window.update(overrides)
    # pixel_ratio deliberately present AND non-1.0: window mode must IGNORE it.
    return {"pixel_ratio": PIXEL_RATIO, "capture_window": capture_window}


def window_demo_rows() -> list[dict]:
    """Same demo as demo_rows(), but the click is in captured-frame pixels."""
    x, y = BUTTON_CENTER_PHYSICAL
    rows: list[dict] = []
    rows += _click_rows(T0 + 1.0, float(x), float(y))
    rows += _type_rows(T0 + 2.0, NOTE_VALUE)
    rows += _named_key_rows(T0 + 3.0, "enter")
    return rows


@pytest.fixture(scope="module")
def window_converted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("window_adapter")
    capture_dir = make_capture(
        tmp_path, window_demo_rows(), config=window_capture_config()
    )
    recording_dir = tmp_path / "recording"
    convert_capture(
        capture_dir, recording_dir, params={"note": NOTE_VALUE}, settle_s=1.0
    )
    return recording_dir


def test_window_mode_coordinates_not_rescaled(window_converted: Path) -> None:
    """Double-scale regression: window-space coordinates pass through EXACTLY.

    The session's pixel_ratio is 2.0; a regression that applies it in window
    mode would double every coordinate (and land this click off-frame).
    """
    events = events_of(window_converted)
    assert [e["kind"] for e in events] == ["click", "type", "key"]
    click = events[0]
    assert (click["x"], click["y"]) == BUTTON_CENTER_PHYSICAL
    assert events[1]["text"] == NOTE_VALUE
    assert events[2]["key"] == "Enter"


def test_window_mode_frames_taken_as_is(window_converted: Path) -> None:
    """Frames pass through at the captured (window) size, untouched."""
    import json

    meta = json.loads((window_converted / "meta.json").read_text())
    assert meta["viewport"] == list(FRAME_SIZE)
    assert (window_converted / "frames" / "0000_before.png").is_file()


def test_window_mode_meta_stamps_backend_hints(window_converted: Path) -> None:
    """meta.json carries the scoping provenance + rdp replay hints."""
    import json

    meta = json.loads((window_converted / "meta.json").read_text())
    assert meta["window_capture"] == {
        "coordinate_space": "window_pixels",
        "target_owner": WINDOW_OWNER,
        "target_title": None,
        "resolved_owner": WINDOW_OWNER,
        "resolved_title": WINDOW_TITLE,
        # Resolved-window identity (OS handle + owning pid) — provenance for the
        # local recording; window_id "42" coerced from the persisted string.
        "resolved_pid": 4242,
        "resolved_window_id": 42,
    }
    # The recorded TARGET had owner only (title=None): hints carry exactly the
    # user's proven-to-resolve substrings, not the volatile resolved title.
    assert meta["backend_hints"] == {"backend": "rdp", "rdp_window": WINDOW_OWNER}


def test_window_mode_identity_omitted_when_absent(tmp_path: Path) -> None:
    """A session without a resolved pid/window id simply omits those keys.

    A missing OS handle (older capture, or a resolver that could not read it)
    must not fail conversion nor serialize a null identity; the other scoping
    provenance is unaffected.
    """
    import json

    config = window_capture_config(window_id=None, pid=None)
    capture_dir = make_capture(tmp_path, window_demo_rows(), config=config)
    recording_dir = tmp_path / "recording"
    convert_capture(
        capture_dir, recording_dir, params={"note": NOTE_VALUE}, settle_s=1.0
    )
    wc = json.loads((recording_dir / "meta.json").read_text())["window_capture"]
    assert "resolved_pid" not in wc
    assert "resolved_window_id" not in wc
    # Core scoping provenance still present.
    assert wc["coordinate_space"] == "window_pixels"
    assert wc["resolved_owner"] == WINDOW_OWNER


def test_window_mode_out_of_window_click_rejected(tmp_path: Path) -> None:
    """Out-of-range coordinates (input aimed at another window) refuse loudly."""
    rows = _click_rows(T0 + 1.0, float(FRAME_SIZE[0] + 50), 100.0)
    capture_dir = make_capture(
        tmp_path, rows, screens=app_screens()[:1], config=window_capture_config()
    )
    with pytest.raises(ValueError, match="out-of-window input"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_out_of_window_scroll_rejected(tmp_path: Path) -> None:
    """A scroll at a negative (off-window) position is screened too."""
    rows = [
        {
            "name": "scroll",
            "timestamp": T0 + 1.0,
            "mouse_x": -30.0,
            "mouse_y": 100.0,
            "mouse_dx": 0.0,
            "mouse_dy": -3.0,
        },
    ]
    capture_dir = make_capture(
        tmp_path, rows, screens=app_screens()[:1], config=window_capture_config()
    )
    with pytest.raises(ValueError, match="out-of-window input"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_bounds_timeline_honored(tmp_path: Path) -> None:
    """A mid-recording resize is refused even when actions remain in bounds.

    Capture's fixed-size MP4 skips resized frames and Flow has one viewport.
    Accepting the resize could pair new-space coordinates with an old frame.
    """
    x, y = 100.0, 100.0  # deliberately inside both 1280x800 and 640x400
    rows = _click_rows(T0 + 1.0, x, y) + _click_rows(T0 + 2.0, x, y)
    small = (640, 400)
    window_event_rows = [
        {
            "timestamp": T0,
            "title": WINDOW_TITLE,
            "left": 100,
            "top": 50,
            "width": FRAME_SIZE[0] // 2,
            "height": FRAME_SIZE[1] // 2,
            "window_id": "42",
            "state": {
                "window_capture": True,
                "owner": WINDOW_OWNER,
                "viewport": list(FRAME_SIZE),
            },
        },
        {
            "timestamp": T0 + 1.5,
            "title": WINDOW_TITLE,
            "left": 100,
            "top": 50,
            "width": small[0] // 2,
            "height": small[1] // 2,
            "window_id": "42",
            "state": {
                "window_capture": True,
                "owner": WINDOW_OWNER,
                "viewport": list(small),
            },
        },
    ]
    capture_dir = make_capture(
        tmp_path,
        rows,
        screens=app_screens()[:1],
        config=window_capture_config(),
        window_event_rows=window_event_rows,
    )
    with pytest.raises(ValueError, match=r"changed viewport.*640x400"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_unknown_coordinate_space_rejected(tmp_path: Path) -> None:
    """A declared coordinate space the adapter doesn't know refuses loudly."""
    config = window_capture_config(coordinate_space="screen_points")
    capture_dir = make_capture(
        tmp_path, window_demo_rows(), screens=app_screens()[:1], config=config
    )
    with pytest.raises(ValueError, match="coordinate_space='screen_points'"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_malformed_window_capture_marker_rejected(tmp_path: Path) -> None:
    """A corrupt marker cannot silently fall back to full-screen scaling."""
    capture_dir = make_capture(
        tmp_path,
        window_demo_rows(),
        screens=app_screens()[:1],
        config={"pixel_ratio": PIXEL_RATIO, "capture_window": ["corrupt"]},
    )
    with pytest.raises(ValueError, match="malformed window-capture metadata"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_missing_viewport_rejected(tmp_path: Path) -> None:
    """The pre-listener static viewport is mandatory, even with no timeline."""
    config = window_capture_config(viewport=None)
    capture_dir = make_capture(
        tmp_path, window_demo_rows(), screens=app_screens()[:1], config=config
    )
    with pytest.raises(ValueError, match="no valid initial"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_action_before_every_timeline_sample_refused() -> None:
    """Never borrow a future viewport for an earlier action."""
    action = SimpleNamespace(
        type="mouse.singleclick",
        x=10.0,
        y=10.0,
        timestamp=T0,
    )
    with pytest.raises(ValueError, match="precedes every known viewport sample"):
        _reject_out_of_window(
            [action],
            [(T0 + 1.0, FRAME_SIZE)],
        )


def test_window_mode_rounding_outside_viewport_rejected() -> None:
    """A raw in-range float may not round into an out-of-range Flow click."""
    action = SimpleNamespace(
        type="mouse.singleclick",
        x=FRAME_SIZE[0] - 0.25,
        y=100.0,
        timestamp=T0,
    )
    with pytest.raises(ValueError, match=r"rounds outside it as \(1280, 100\)"):
        _reject_out_of_window(
            [action],
            [(float("-inf"), FRAME_SIZE)],
        )


def test_window_mode_missing_pointer_coordinate_rejected() -> None:
    """Window-scoped pointer actions cannot be verified without x and y."""
    action = SimpleNamespace(
        type="mouse.scroll",
        x=None,
        y=100.0,
        timestamp=T0,
    )
    with pytest.raises(ValueError, match="no complete pointer coordinates"):
        _reject_out_of_window(
            [action],
            [(float("-inf"), FRAME_SIZE)],
        )


@pytest.mark.parametrize(
    "bad_viewport",
    [
        [True, FRAME_SIZE[1]],
        [float("inf"), FRAME_SIZE[1]],
        [FRAME_SIZE[0] + 0.5, FRAME_SIZE[1]],
    ],
)
def test_window_mode_malformed_initial_viewport_rejected(
    tmp_path: Path, bad_viewport: list
) -> None:
    """Viewport dimensions must be finite positive integer pixels."""
    config = window_capture_config(viewport=bad_viewport)
    capture_dir = make_capture(
        tmp_path, window_demo_rows(), screens=app_screens()[:1], config=config
    )
    with pytest.raises(ValueError, match="no valid initial"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_malformed_timeline_row_rejected(tmp_path: Path) -> None:
    """A corrupt resize row cannot be silently skipped in favor of metadata."""
    window_event_rows = [
        {
            "timestamp": T0,
            "title": WINDOW_TITLE,
            "left": 100,
            "top": 50,
            "width": FRAME_SIZE[0] // 2,
            "height": FRAME_SIZE[1] // 2,
            "window_id": "42",
            "state": {
                "window_capture": True,
                "owner": WINDOW_OWNER,
                "viewport": None,
            },
        }
    ]
    capture_dir = make_capture(
        tmp_path,
        window_demo_rows(),
        screens=app_screens()[:1],
        config=window_capture_config(),
        window_event_rows=window_event_rows,
    )
    with pytest.raises(ValueError, match="malformed bounds-timeline"):
        convert_capture(capture_dir, tmp_path / "recording")


@pytest.mark.parametrize(
    "target",
    [
        ["not", "an", "object"],
        {"owner": 42, "title": None},
        {"owner": "   ", "title": None},
    ],
)
def test_window_mode_malformed_target_metadata_rejected(
    tmp_path: Path, target: object
) -> None:
    """Replay selector hints must not contain arbitrary JSON values."""
    config = window_capture_config(target=target)
    capture_dir = make_capture(
        tmp_path, window_demo_rows(), screens=app_screens()[:1], config=config
    )
    with pytest.raises(ValueError, match="metadata"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_window_mode_frame_size_must_match_recorded_viewport(
    tmp_path: Path,
) -> None:
    """Coordinates and visual evidence must share one exact pixel space."""
    viewport = [640, 400]
    rows = _click_rows(T0 + 1.0, 100.0, 100.0)
    capture_dir = make_capture(
        tmp_path,
        rows,
        screens=app_screens()[:1],  # encoded at 1280x800
        config=window_capture_config(viewport=viewport),
    )
    with pytest.raises(ValueError, match=r"frame.*1280x800.*viewport.*640x400"):
        convert_capture(capture_dir, tmp_path / "recording")
