"""The video start time is anchored to the first encoded frame.

The encoded stream begins at its first frame, and frame lookups turn a
wall-clock time into video time by subtracting ``video_start_time``. When the
first frame arrives long after the writer opened (macOS ScreenCaptureKit only
delivers a frame when the screen changes), the writer-open time put every
lookup that many seconds past the end of the video.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from PIL import Image

from openadapt_capture import db, recorder, utils, video
from openadapt_capture.db import crud
from openadapt_capture.db.models import Recording
from openadapt_capture.extensions import synchronized_queue as sq


@pytest.fixture(autouse=True)
def _clock():
    utils.set_start_time()


def _screen_event(timestamp: float, color: str, ordinal: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="screen/video",
        data=Image.new("RGB", (64, 48), color),
        timestamp=timestamp,
        source_ordinal=ordinal,
    )


def test_first_frame_anchors_the_video_start(monkeypatch):
    writes = []
    updates = []
    monkeypatch.setattr(
        recorder.video,
        "write_video_frame",
        lambda _c, _s, _img, ts, start, last_pts, *_a, **_k: (
            writes.append((ts, start)) or last_pts + 1
        ),
    )
    monkeypatch.setattr(
        recorder.crud,
        "update_video_start_time",
        lambda _db, _rec, start: updates.append(start),
    )
    perf_q = sq.SynchronizedQueue()
    state = {
        "video_container": None,
        "video_stream": SimpleNamespace(width=64, height=48),
        "video_start_timestamp": 100.0,
        "last_pts": 0,
        "video_file_path": "unused.mp4",
    }
    rec = SimpleNamespace(id=1, timestamp=99.0)

    state = recorder.write_video_event(None, rec, _screen_event(115.0, "red", 1), perf_q, **state)
    assert updates == [115.0]
    assert state["video_start_timestamp"] == 115.0
    assert {start for _, start in writes} == {115.0}

    state = recorder.write_video_event(None, rec, _screen_event(115.5, "blue", 2), perf_q, **state)
    assert updates == [115.0]
    assert writes[-1] == (115.5, 115.0)
    assert state["video_start_timestamp"] == 115.0


def _real_ffmpeg_mpeg4() -> str | None:
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    try:
        provision = video.FFmpegProvision(executable, source="test PATH")
        if "mpeg4" not in video.available_encoders(provision):
            return None
    except (video.FFmpegEncodingError, OSError):
        return None
    return executable


@pytest.mark.skipif(
    _real_ffmpeg_mpeg4() is None,
    reason="opt-in real FFmpeg/mpeg4 executable is not available",
)
def test_late_first_frame_is_found_at_its_wall_clock_time(tmp_path):
    executable = _real_ffmpeg_mpeg4()
    db_path = tmp_path / "recording.db"
    engine, Session = db.create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(
        session,
        {
            "timestamp": 4.0,
            "monitor_width": 64,
            "monitor_height": 48,
            "platform": "test",
            "task_description": "late first frame",
        },
    )
    detached = SimpleNamespace(id=recording.id, timestamp=recording.timestamp)
    provision = video.FFmpegProvision(
        executable,
        codec="mpeg4",
        pixel_format="yuv420p",
        muxer="mp4",
        source="test PATH",
    )
    state = recorder.video_pre_callback(
        session,
        detached,
        video_dir=str(tmp_path),
        frame_size=(64, 48),
        provision=provision,
        timeout_seconds=60.0,
    )
    opened = state["video_start_timestamp"]
    perf_q = sq.SynchronizedQueue()
    red_ts = opened + 15.0
    blue_ts = opened + 16.0
    state = recorder.write_video_event(
        session, detached, _screen_event(red_ts, "red", 1), perf_q, **state
    )
    state = recorder.write_video_event(
        session, detached, _screen_event(blue_ts, "blue", 2), perf_q, **state
    )
    recorder.video_post_callback(state)

    stored = session.execute(
        sa.select(Recording.video_start_time).where(Recording.id == recording.id)
    ).scalar_one()
    assert stored == red_ts
    for ts, channel in ((red_ts, 0), (blue_ts, 2)):
        frame = video.extract_frame(state["video_file_path"], ts - stored, ffmpeg_path=executable)
        pixel = frame.getpixel((10, 10))
        assert pixel[channel] > 200, (ts - opened, pixel)
    while not perf_q.empty():
        perf_q.get()
    session.close()
    engine.dispose()
