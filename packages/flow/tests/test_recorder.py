"""Tests for the Recorder (unit, fake backend) and the demo driver (live)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Iterator

import pytest
from PIL import Image

from openadapt_flow.demo_driver import record_triage_demo
from openadapt_flow.mockmed.server import serve
from openadapt_flow.recorder import Recorder

NOTE = "Patient reports mild headache for two days, advise rest and fluids"


def _png(size: tuple[int, int] = (1280, 800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


class FakeBackend:
    """Minimal in-memory Backend: constant frame, records calls."""

    def __init__(self) -> None:
        self._png = _png()
        self.calls: list[tuple] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.calls.append(("click", x, y, double))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press(self, key: str) -> None:
        self.calls.append(("key", key))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))


def _read_events(rec_dir: Path) -> list[dict]:
    lines = (rec_dir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# -- unit: Recorder with a fake backend --------------------------------------


def test_recorder_writes_recording_format(tmp_path: Path) -> None:
    backend = FakeBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.click(10, 20)
    rec.double_click(30, 40)
    rec.type_text("hello")
    rec.type_text("world", param="note")
    rec.press("Enter")
    rec_dir = rec.finish()

    assert rec_dir == tmp_path / "rec"

    # meta.json
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["viewport"] == [1280, 800]
    assert meta["app_url"] == "http://fake.local/"
    assert meta["params"] == {"note": "world"}
    assert meta["id"]
    assert meta["created_at"]

    # events.jsonl
    events = _read_events(rec_dir)
    assert [e["kind"] for e in events] == [
        "click",
        "double_click",
        "type",
        "type",
        "key",
    ]
    assert [e["i"] for e in events] == [0, 1, 2, 3, 4]
    assert events[0]["x"] == 10 and events[0]["y"] == 20
    assert events[1]["x"] == 30 and events[1]["y"] == 40
    assert events[2]["text"] == "hello"
    assert "param" not in events[2]  # param present iff parameterized
    assert events[3]["text"] == "world"
    assert events[3]["param"] == "note"
    assert events[4]["key"] == "Enter"
    times = [e["t"] for e in events]
    assert times == sorted(times)
    assert all(t >= 0 for t in times)

    # frames
    for i in range(5):
        for suffix in ("before", "after"):
            frame = rec_dir / "frames" / f"{i:04d}_{suffix}.png"
            assert frame.exists(), frame
    with Image.open(rec_dir / "frames" / "0000_before.png") as img:
        assert img.size == (1280, 800)
        assert img.format == "PNG"

    # actions were forwarded to the backend in order
    assert backend.calls == [
        ("click", 10, 20, False),
        ("click", 30, 40, True),
        ("type", "hello"),
        ("type", "world"),
        ("key", "Enter"),
    ]


def test_recorder_scroll_event(tmp_path: Path) -> None:
    """scroll() records a {"kind": "scroll", "dx", "dy"} event with frames."""
    backend = FakeBackend()
    rec = Recorder(backend, tmp_path / "rec")
    rec.scroll(0, 400)
    rec.scroll(-30, -120)
    rec_dir = rec.finish()

    events = _read_events(rec_dir)
    assert [e["kind"] for e in events] == ["scroll", "scroll"]
    assert events[0]["dx"] == 0 and events[0]["dy"] == 400
    assert events[1]["dx"] == -30 and events[1]["dy"] == -120
    for i in range(2):
        for suffix in ("before", "after"):
            assert (rec_dir / "frames" / f"{i:04d}_{suffix}.png").exists()
    assert backend.calls == [("scroll", 0, 400), ("scroll", -30, -120)]


def test_recorder_settle_timeout_on_unstable_screen(tmp_path: Path) -> None:
    """A constantly-changing screen must not hang the recorder."""

    class UnstableBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self._n = 0

        def screenshot(self) -> bytes:
            self._n += 1
            shade = 255 if self._n % 2 else 0
            buf = io.BytesIO()
            img = Image.new("RGB", (1280, 800), (250, 250, 250))
            for px in range(0, 1280, 2):
                for py in range(0, 800, 8):
                    img.putpixel((px, py), (shade, 0, 255 - shade))
            img.save(buf, format="PNG")
            return buf.getvalue()

    rec = Recorder(
        UnstableBackend(),
        tmp_path / "rec",
        settle_timeout_s=0.5,
        settle_interval_s=0.05,
    )
    rec.click(1, 1)  # returns despite never settling
    rec_dir = rec.finish()
    assert (rec_dir / "frames" / "0000_after.png").exists()


# -- integration: demo driver against a live MockMed -------------------------


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    url, stop = serve(port=0)
    yield url
    stop()


def test_record_triage_demo_produces_valid_recording(
    tmp_path: Path, server_url: str
) -> None:
    rec_dir = record_triage_demo(
        server_url, tmp_path / "rec", note_text=NOTE, param_name="note"
    )
    assert rec_dir == tmp_path / "rec"

    # meta.json
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["viewport"] == [1280, 800]
    assert meta["app_url"] == server_url
    assert meta["params"] == {"note": NOTE}

    # events: the canonical 11-action demo.
    events = _read_events(rec_dir)
    assert [e["kind"] for e in events] == [
        "click",  # username field
        "type",  # username
        "click",  # password field
        "type",  # password
        "click",  # Sign In
        "click",  # Open first referral
        "click",  # New Encounter
        "click",  # Triage
        "click",  # Note field
        "type",  # note (parameterized)
        "click",  # Save Encounter
    ]
    assert [e["i"] for e in events] == list(range(11))

    param_events = [e for e in events if "param" in e]
    assert len(param_events) == 1
    assert param_events[0]["param"] == "note"
    assert param_events[0]["text"] == NOTE

    for e in events:
        if e["kind"] in ("click", "double_click"):
            assert 0 <= e["x"] < 1280
            assert 0 <= e["y"] < 800
    times = [e["t"] for e in events]
    assert times == sorted(times)

    # frames: before/after per event, all 1280x800 PNGs.
    for i in range(len(events)):
        for suffix in ("before", "after"):
            frame = rec_dir / "frames" / f"{i:04d}_{suffix}.png"
            assert frame.exists(), frame
            with Image.open(frame) as img:
                assert img.format == "PNG"
                assert img.size == (1280, 800)


def test_recorder_captures_structural_state_when_backend_exposes_it(
    tmp_path: Path,
) -> None:
    """A backend with StructuralBackend observations gets url/title/pages
    _before/_after keys on every event; a click that opens a new tab shows
    up as pages 1 -> 2 even though the frame never changed."""

    class StructuralFake(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self._url = "http://app/"
            self._title = "Inbox"
            self._pages = 1

        @property
        def url(self) -> str:
            return self._url

        @property
        def page_title(self) -> str:
            return self._title

        @property
        def page_count(self) -> int:
            return self._pages

        def click(self, x: int, y: int, *, double: bool = False) -> None:
            super().click(x, y, double=double)
            self._pages = 2  # the click opened a tab

    backend = StructuralFake()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://app/")
    rec.click(10, 20)
    rec.finish()
    (event,) = _read_events(tmp_path / "rec")
    assert event["url_before"] == "http://app/"
    assert event["url_after"] == "http://app/"
    assert event["title_before"] == "Inbox"
    assert event["pages_before"] == 1
    assert event["pages_after"] == 2


def test_recorder_omits_structural_keys_on_plain_backend(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    rec = Recorder(backend, tmp_path / "rec")
    rec.click(1, 2)
    rec.finish()
    (event,) = _read_events(tmp_path / "rec")
    for key in (
        "url_before",
        "url_after",
        "title_before",
        "title_after",
        "pages_before",
        "pages_after",
    ):
        assert key not in event
