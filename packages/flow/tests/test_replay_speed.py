"""Replay speed changes keep the settle and OCR contracts exact."""

from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image

import openadapt_flow.vision as vision
from openadapt_flow.vision.settle import (
    SettleSeed,
    wait_settled_result,
    wait_settled_seeded,
)


def _png(color: int) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (64, 64), (color, color, color))
    for x in range(0, 64, 8):
        for y in range(0, 64):
            img.putpixel((x, y), (255 - color, 0, color))
    img.save(buf, format="PNG")
    return buf.getvalue()


class _Frames:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.calls = 0

    def screenshot(self) -> bytes:
        self.calls += 1
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]


def _blank(color: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (color,) * 3).save(buf, format="PNG")
    return buf.getvalue()


def test_unseeded_settle_needs_two_polled_frames() -> None:
    backend = _Frames([_png(10)])
    result = wait_settled_seeded(backend, None, interval_s=0.0)
    assert result.settled and backend.calls == 2
    assert result.phash and result.captured_at > 0


def test_seed_from_a_settled_run_extends_it_with_one_fresh_frame() -> None:
    frame = _png(10)
    first = wait_settled_result(_Frames([frame]), interval_s=0.0)
    seed = SettleSeed(
        phash=first.phash,
        stable_frames=first.stable_frames,
        captured_at=first.captured_at,
    )
    backend = _Frames([frame])
    result = wait_settled_seeded(backend, seed, interval_s=0.0)
    assert result.settled
    assert backend.calls == 1  # one fresh capture, no extra poll
    assert result.png == frame


def test_seed_never_vouches_for_a_changed_frame() -> None:
    old = _png(10)
    new = _blank(200)
    first = wait_settled_result(_Frames([old]), interval_s=0.0)
    seed = SettleSeed(first.phash, first.stable_frames, first.captured_at)
    backend = _Frames([new])
    result = wait_settled_seeded(backend, seed, interval_s=0.0)
    # The changed frame restarts the run: two fresh identical frames needed.
    assert result.settled and backend.calls == 2
    assert result.png == new


def test_stale_or_unsettled_seed_is_ignored() -> None:
    frame = _png(10)
    first = wait_settled_result(_Frames([frame]), interval_s=0.0)
    stale = SettleSeed(first.phash, first.stable_frames, first.captured_at - 60)
    backend = _Frames([frame])
    wait_settled_seeded(backend, stale, interval_s=0.0)
    assert backend.calls == 2
    short = SettleSeed(first.phash, 1, first.captured_at)
    backend = _Frames([frame])
    wait_settled_seeded(backend, short, interval_s=0.0)
    assert backend.calls == 2


def test_timeout_still_reports_unsettled() -> None:
    backend = _Frames([_png(i * 20) for i in range(12)])
    result = wait_settled_seeded(backend, None, interval_s=0.0, timeout_s=0.0)
    assert result.settled is False


def test_ocr_memo_is_exact_and_returns_copies(monkeypatch) -> None:
    ocr_module = __import__("openadapt_flow.vision.ocr", fromlist=["_engine"])
    calls: list[int] = []

    def engine(img):
        calls.append(1)
        return ([[[0, 0], [5, 0], [5, 5], [0, 5]], "hi", 0.9],), None

    engine_ref = [engine]
    monkeypatch.setattr(ocr_module, "_get_engine", lambda: engine_ref[0])
    ocr_module.clear_ocr_memo()
    a = _png(10)
    b = _png(11)
    first = ocr_module.ocr(a)
    first[0].text = "mutated"
    again = ocr_module.ocr(a)
    assert again[0].text == "hi" and len(calls) == 1
    ocr_module.ocr(a, region=(0, 0, 32, 32))
    ocr_module.ocr(b)
    assert len(calls) == 3  # different region / different bytes: fresh OCR

    def other_engine(img):
        calls.append(2)
        return ([[[0, 0], [5, 0], [5, 5], [0, 5]], "yo", 0.9],), None

    engine_ref[0] = other_engine
    assert ocr_module.ocr(a)[0].text == "yo"  # engine swap invalidates
    ocr_module.clear_ocr_memo()


def test_replayer_seed_is_dropped_by_any_delivery() -> None:
    from openadapt_flow.runtime.replayer import Replayer

    frame = _png(10)
    backend = _Frames([frame])
    replayer = Replayer.__new__(Replayer)
    replayer.backend = backend
    replayer.vision = vision
    replayer._actuation_epoch = 0
    replayer._settle_seed = None
    replayer._active_delivery_acknowledgers = ()

    replayer._settle_seeded()
    assert replayer._settle_seed is not None
    calls = backend.calls
    replayer._settle_seeded()  # seeded: one fresh frame
    assert backend.calls == calls + 1

    result = SimpleNamespace(delivery_attempted=False)
    try:
        replayer._deliver_backend_call(result, lambda: True)
    except Exception:
        pass
    assert replayer._settle_seed is None
    calls = backend.calls
    replayer._settle_seeded()  # after input: full settle again
    assert backend.calls == calls + 2


def test_custom_vision_without_seeded_settle_still_works() -> None:
    from openadapt_flow.runtime.replayer import Replayer

    seen: list[str] = []
    replayer = Replayer.__new__(Replayer)
    replayer.backend = object()
    replayer.vision = SimpleNamespace(
        wait_settled=lambda backend: seen.append("plain") or b"png"
    )
    replayer._actuation_epoch = 0
    replayer._settle_seed = None
    assert replayer._settle_seeded() == b"png" and seen == ["plain"]
