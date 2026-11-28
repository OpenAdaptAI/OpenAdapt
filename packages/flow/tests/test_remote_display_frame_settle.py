"""Tests for AVC444/H.264 frame-settle polling and hover-jitter tolerance."""

from __future__ import annotations

import io
from typing import Optional

import pytest
from PIL import Image

from openadapt_flow.backends.remote_display import (
    DEFAULT_FRAME_SETTLE_MAX_MEAN_DELTA,
    RemoteDisplayBackend,
    RemoteDisplayError,
    WindowInfo,
    frame_mean_abs_delta,
    points_within_hover_jitter,
    resolve_mac_key,
    wait_for_frame_settle,
)


def _solid_png(rgb: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


def test_frame_mean_abs_delta_exact_and_resize() -> None:
    a = _solid_png((100, 100, 100))
    b = _solid_png((101, 101, 101))
    c = _solid_png((100, 100, 100), size=(40, 32))
    assert frame_mean_abs_delta(a, a) == pytest.approx(0.0)
    assert frame_mean_abs_delta(a, b) == pytest.approx(1.0)
    assert frame_mean_abs_delta(a, c) == float("inf")


def test_wait_for_frame_settle_returns_immediately_on_stable_frames() -> None:
    stable = _solid_png((120, 120, 120))
    sleeps: list[float] = []
    calls = 0

    def capture() -> bytes:
        nonlocal calls
        calls += 1
        return stable

    result = wait_for_frame_settle(
        capture,
        max_wait_s=0.6,
        poll_interval_s=0.05,
        sleep_fn=sleeps.append,
    )
    assert result.settled is True
    assert result.frames_checked == 2
    assert result.last_mean_delta == pytest.approx(0.0)
    assert result.frame_png == stable
    assert sleeps == []
    assert calls == 2


def test_wait_for_frame_settle_waits_through_progressive_codec_refinement() -> None:
    # Simulate progressive AVC444 refinement: large delta -> moderate delta -> settled.
    frames = [
        _solid_png((80, 80, 80)),
        _solid_png((100, 100, 100)),
        _solid_png((106, 106, 106)),
        _solid_png((107, 107, 107)),  # delta 1.0 <= 1.5 default threshold
    ]
    idx = 0
    now = 0.0
    sleeps: list[float] = []

    def capture() -> bytes:
        nonlocal idx
        frame = frames[min(idx, len(frames) - 1)]
        idx += 1
        return frame

    def fake_sleep(dt: float) -> None:
        nonlocal now
        sleeps.append(dt)
        now += dt

    result = wait_for_frame_settle(
        capture,
        max_wait_s=0.6,
        poll_interval_s=0.05,
        max_mean_delta=DEFAULT_FRAME_SETTLE_MAX_MEAN_DELTA,
        sleep_fn=fake_sleep,
        monotonic_fn=lambda: now,
    )
    assert result.settled is True
    assert result.frames_checked == 4
    assert result.last_mean_delta == pytest.approx(1.0)
    assert result.frame_png == frames[-1]
    assert len(sleeps) == 2


def test_wait_for_frame_settle_returns_cleanly_on_timeout() -> None:
    step = 0
    now = 0.0

    def capture() -> bytes:
        nonlocal step
        step += 1
        val = (step * 25) % 256
        return _solid_png((val, val, val))

    def fake_sleep(dt: float) -> None:
        nonlocal now
        now += dt

    result = wait_for_frame_settle(
        capture,
        max_wait_s=0.2,
        poll_interval_s=0.05,
        max_mean_delta=1.5,
        sleep_fn=fake_sleep,
        monotonic_fn=lambda: now,
    )
    assert result.settled is False
    assert result.frames_checked >= 2
    assert result.last_mean_delta > 1.5
    assert result.elapsed_s >= 0.2


def test_points_within_hover_jitter_tolerates_1_to_2px_and_refuses_drift() -> None:
    assert points_within_hover_jitter((100, 200), (100, 200)) is True
    assert points_within_hover_jitter((100, 200), (101, 199)) is True
    assert points_within_hover_jitter((100, 200), (102, 198)) is True
    assert points_within_hover_jitter((100, 200), (103, 200)) is False
    assert points_within_hover_jitter((100, 200), (100, 203)) is False
    assert points_within_hover_jitter((100, 200), (101, 200), max_jitter_px=0) is False


class _MinimalWindowClient:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self._idx = 0
        self._win = WindowInfo(
            window_id=7,
            owner="Citrix Viewer",
            title="Session",
            pid=42,
            bounds=(0.0, 0.0, 32.0, 32.0),
            on_screen=True,
        )
        self.clicks: list[tuple[float, float]] = []

    def input_trusted(self) -> bool:
        return True

    def frontmost_pid(self) -> Optional[int]:
        return 42

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        return [self._win]

    def key_window_id(self, pid: int) -> Optional[int]:
        return 7

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        return 7

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        frame = self._frames[min(self._idx, len(self._frames) - 1)]
        self._idx += 1
        return frame, 32, 32

    def activate(self, pid: int) -> None:
        return None

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        if down:
            self.clicks.append((x, y))

    def mouse_move(self, x: float, y: float) -> None:
        return None

    def type_chars(self, text: str) -> None:
        return None

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        return None

    def scroll(self, dx: int, dy: int) -> None:
        return None

    def resolve_key(self, token: str):
        return resolve_mac_key(token)


def test_remote_display_backend_wait_for_frame_settle_and_hover_jitter() -> None:
    stable = _solid_png((90, 90, 90))
    client = _MinimalWindowClient([stable])
    backend = RemoteDisplayBackend(
        client,
        owner_substr="Citrix Viewer",
        settle_s=0.0,
        pointer_settle_stable_frames=2,
        hover_jitter_tolerance_px=2,
    )

    settle = backend.wait_for_frame_settle(max_wait_s=0.2, poll_interval_s=0.0)
    assert settle.settled is True
    assert settle.frames_checked == 2

    # 2px jitter after pointer prepare is accepted; 3px drift is refused.
    backend.prepare_pointer_actuation(10, 10)
    backend.acquire_actuation_frame()
    backend.click(12, 11)
    assert client.clicks == [(12.0, 11.0)]

    backend.prepare_pointer_actuation(10, 10)
    backend.acquire_actuation_frame()
    with pytest.raises(RemoteDisplayError, match="not pre-positioned"):
        backend.click(13, 10)
