"""Contracts for binding each action to its exact retained screen frame.

An action's evidence frame must be THE frame the recorder retained when the
action completed, not a nearest-frame guess. These tests pin:

- the video timing box retaining an exact capture-timestamp binding per
  encoded frame (and rejecting malformed bindings),
- fail-closed exact extraction (no nearest-frame substitute),
- propagation of the bound timestamp through event merging,
- Action.screenshot resolving the bound frame or raising, never substituting.
"""

from __future__ import annotations

import json
import struct
from fractions import Fraction

import pytest
from PIL import Image

from openadapt_capture import video
from openadapt_capture.capture import Action
from openadapt_capture.events import (
    KeyDownEvent,
    KeyTypeEvent,
    MouseClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseUpEvent,
)
from openadapt_capture.processing import (
    detect_drag_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
)

_TIMING_BOX_UUID = video._TIMING_BOX_UUID


# ---------------------------------------------------------------------------
# Timing box: exact capture bindings
# ---------------------------------------------------------------------------


def test_timing_box_round_trips_exact_capture_bindings(tmp_path):
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    frames = [(0, 0.0), (3, 3 / 24), (4, 4 / 24)]
    captures = [(0, 1000.5), (3, 1001.25)]
    video._append_timing_box(
        path,
        fps=Fraction(24),
        frames=frames,
        captures=captures,
    )

    fps, read_frames, read_captures = video._read_timing_box(path)
    assert fps == Fraction(24)
    assert read_frames == frames
    # JSON float round-trip preserves the double exactly.
    assert read_captures == captures
    assert read_captures[1][1] == 1001.25


def test_timing_box_without_bindings_reads_as_none(tmp_path):
    """Media recorded before exact binding stays readable (captures=None)."""
    path = tmp_path / "legacy.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(path, fps=Fraction(24), frames=[(0, 0.0)])

    _, frames, captures = video._read_timing_box(path)
    assert frames == [(0, 0.0)]
    assert captures is None


@pytest.mark.parametrize(
    "captures",
    [
        [(1, 1.0), (0, 2.0)],  # indexes not increasing
        [(0, 2.0), (1, 1.0)],  # timestamps decreasing
        [(0, "later")],  # non-numeric timestamp
        [("zero", 1.0)],  # non-integer index
        [(0, None)],  # null timestamp
    ],
)
def test_timing_box_rejects_malformed_capture_bindings(tmp_path, captures):
    path = tmp_path / "capture.mp4"
    payload = json.dumps(
        {
            "schema": video._TIMING_SCHEMA,
            "fps": "24/1",
            "frames": [[0, 0.0]],
            "captures": captures,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    box_size = 24 + len(payload)
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    with path.open("ab") as output:
        output.write(struct.pack(">I4s16s", box_size, b"uuid", _TIMING_BOX_UUID))
        output.write(payload)

    with pytest.raises(video.FFmpegEncodingError, match="capture"):
        video._read_timing_box(path)


def test_timing_box_accepts_duplicated_first_frame_binding(tmp_path):
    """The first frame is written twice; one capture ts may hold two indexes."""
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(
        path,
        fps=Fraction(24),
        frames=[(0, 0.0), (1, 1 / 24)],
        captures=[(0, 500.0), (1, 500.0)],
    )
    _, _, captures = video._read_timing_box(path)
    assert captures == [(0, 500.0), (1, 500.0)]


# ---------------------------------------------------------------------------
# extract_exact_frame: fail closed, never substitute
# ---------------------------------------------------------------------------


def test_extract_exact_frame_fails_closed_without_timing_metadata(tmp_path):
    path = tmp_path / "bare.mp4"
    path.write_bytes(b"not an openadapt recording")
    with pytest.raises(LookupError, match="no OpenAdapt timing metadata"):
        video.extract_exact_frame(path, 123.0)


def test_extract_exact_frame_fails_closed_on_legacy_bindings(tmp_path):
    path = tmp_path / "legacy.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(path, fps=Fraction(24), frames=[(0, 0.0)])
    with pytest.raises(LookupError, match="predates exact frame binding"):
        video.extract_exact_frame(path, 123.0)


def test_extract_exact_frame_fails_closed_when_timestamp_unbound(tmp_path):
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(
        path,
        fps=Fraction(24),
        frames=[(0, 0.0)],
        captures=[(0, 900.0)],
    )
    with pytest.raises(LookupError, match="no retained frame bound to") as exc_info:
        video.extract_exact_frame(path, 901.0)
    assert "nearest binding" in str(exc_info.value)


def test_extract_exact_frame_decodes_the_bound_index(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(
        path,
        fps=Fraction(24),
        frames=[(0, 0.0), (1, 1 / 24)],
        captures=[(0, 777.0), (1, 777.25)],
    )

    decoded = {}

    def fake_extract(video_path, frame_index, provision):
        decoded["index"] = frame_index
        return Image.new("RGB", (2, 1), "blue")

    monkeypatch.setattr(video, "_extract_frame_index_png", fake_extract)
    frame = video.extract_exact_frame(path, 777.25, ffmpeg_path=executable)
    assert decoded["index"] == 1
    assert frame.size == (2, 1)


def test_extract_exact_frame_prefers_the_first_duplicate_binding(
    tmp_path, monkeypatch
):
    """The duplicated first frame binds twice; decode its first index."""
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"\x00\x00\x00\x08ftyp")
    video._append_timing_box(
        path,
        fps=Fraction(24),
        frames=[(0, 0.0), (1, 1 / 24)],
        captures=[(0, 500.0), (1, 500.0)],
    )
    decoded = {}
    monkeypatch.setattr(
        video,
        "_extract_frame_index_png",
        lambda _path, index, _provision: decoded.setdefault("index", index),
    )
    video.extract_exact_frame(path, 500.0, ffmpeg_path=executable)
    assert decoded["index"] == 0


# ---------------------------------------------------------------------------
# Encoder-side retention of capture bindings
# ---------------------------------------------------------------------------


class _FakeInput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, payload: bytes) -> int:
        self.data.extend(payload)
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.pipe = _FakeInput()
        self.stdin = self.pipe
        self.stderr_payload = b""
        self.returncode: int | None = None
        self._killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = -9 if self._killed else 0
        if self.returncode == 0:
            from pathlib import Path

            Path(self.command[-1]).write_bytes(b"\x00\x00\x00\x08ftyp")
        return self.returncode

    def kill(self) -> None:
        self._killed = True


def _small_stream() -> video.FFmpegVideoStream:
    return video.FFmpegVideoStream(
        width=2,
        height=1,
        average_rate=Fraction(24),
        pix_fmt="yuv420p",
        codec="mpeg4",
        muxer="mp4",
    )


def _install_fake_popen(monkeypatch, process: _FakeProcess) -> None:
    def popen(command, **kwargs):
        process.command = list(command)
        kwargs["stderr"].write(process.stderr_payload)
        return process

    monkeypatch.setattr(video.subprocess, "Popen", popen)


def _png_bytes(color: str = "black") -> bytes:
    import io

    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def _stage_with_fake_encoder(tmp_path, monkeypatch):
    from pathlib import Path

    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        video.FFmpegProvision(str(executable)),
    )
    process = _FakeProcess(stage._encode_command())
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_a, **_k: _png_bytes())
    return stage, Path(output)


def test_stage_frame_binds_only_newly_captured_frames(tmp_path, monkeypatch):
    stage, output = _stage_with_fake_encoder(tmp_path, monkeypatch)
    red = Image.new("RGB", (2, 1), "red")
    blue = Image.new("RGB", (2, 1), "blue")

    # Frame at pts 27 fills three PTS slots (two gap fillers + itself).
    stage.stage_frame(red, 24, capture_timestamp=111.0)
    stage.stage_frame(blue, 27, capture_timestamp=113.5)
    stage.close()

    _, logical, captures = video._read_timing_box(output)
    assert [index for index, _ in logical] == [0, 3]
    assert captures == [(0, 111.0), (3, 113.5)]


def test_stage_frame_refuses_a_non_finite_capture_timestamp(tmp_path, monkeypatch):
    stage, _ = _stage_with_fake_encoder(tmp_path, monkeypatch)
    red = Image.new("RGB", (2, 1), "red")
    with pytest.raises(video.FFmpegEncodingError, match="finite wall-clock"):
        stage.stage_frame(red, 24, capture_timestamp=float("nan"))


# ---------------------------------------------------------------------------
# Merge propagation of the bound frame
# ---------------------------------------------------------------------------


def _click_pair(down_ts=10.0, up_ts=10.05, down_bound=9.9, up_bound=10.0):
    down = MouseDownEvent(
        timestamp=down_ts, x=1.0, y=2.0, button="left", screenshot_timestamp=down_bound
    )
    up = MouseUpEvent(
        timestamp=up_ts, x=1.0, y=2.0, button="left", screenshot_timestamp=up_bound
    )
    return down, up


def test_click_merge_keeps_the_up_childs_binding():
    down, up = _click_pair()
    (merged,) = merge_consecutive_mouse_click_events([down, up])
    assert isinstance(merged, MouseClickEvent)
    assert merged.screenshot_timestamp == 10.0


def test_drag_merge_keeps_the_final_bindings():
    down = MouseDownEvent(timestamp=1.0, x=1.0, y=2.0, button="left", screenshot_timestamp=0.9)
    move = MouseMoveEvent(timestamp=1.5, x=30.0, y=30.0, screenshot_timestamp=1.6)
    up = MouseUpEvent(timestamp=2.0, x=30.0, y=30.0, button="left", screenshot_timestamp=2.0)
    (drag,) = detect_drag_events([down, move, up])
    assert isinstance(drag, MouseDragEvent)
    assert drag.screenshot_timestamp == 2.0


def test_type_merge_keeps_the_last_key_binding():
    first = KeyDownEvent(timestamp=20.0, key_char="a", screenshot_timestamp=19.5)
    second = KeyDownEvent(timestamp=21.0, key_char="b", screenshot_timestamp=20.75)
    (merged,) = merge_consecutive_keyboard_events([first, second])
    assert isinstance(merged, KeyTypeEvent)
    assert merged.text == "ab"
    assert merged.screenshot_timestamp == 20.75


def test_merge_leaves_legacy_children_unbound():
    down = MouseDownEvent(timestamp=1.0, x=0.0, y=0.0, button="left")
    up = MouseUpEvent(timestamp=1.1, x=0.0, y=0.0, button="left")
    (merged,) = merge_consecutive_mouse_click_events([down, up])
    assert merged.screenshot_timestamp is None


# ---------------------------------------------------------------------------
# Action.screenshot resolves the bound frame exactly
# ---------------------------------------------------------------------------


class _StubCapture:
    def __init__(self):
        self.exact_calls: list[float] = []
        self.lenient_calls: list[float] = []

    def get_exact_frame(self, capture_timestamp: float) -> Image.Image:
        self.exact_calls.append(capture_timestamp)
        return Image.new("RGB", (1, 1), "red")

    def get_frame_at(self, timestamp: float) -> Image.Image:
        self.lenient_calls.append(timestamp)
        return Image.new("RGB", (1, 1), "blue")


def test_action_screenshot_uses_exact_binding():
    stub = _StubCapture()
    _, up = _click_pair()
    action = Action(event=up, _capture=stub)
    image = action.screenshot
    assert stub.exact_calls == [10.0]
    assert stub.lenient_calls == []
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_action_screenshot_falls_back_for_legacy_events():
    stub = _StubCapture()
    legacy = MouseUpEvent(timestamp=42.0, x=0.0, y=0.0, button="left")
    action = Action(event=legacy, _capture=stub)
    image = action.screenshot
    assert stub.exact_calls == []
    assert stub.lenient_calls == [42.0]
    assert image is not None
