"""Unit tests for promoted CanvasBrowserBackend, factory registration, and paste_and_verify."""

from __future__ import annotations

import io
from typing import Optional

import pytest
from PIL import Image

from openadapt_flow.backend import (
    PreparedPointerActuationBackend,
    RemoteActuationBackend,
)
from openadapt_flow.backends.canvas_backend import (
    CanvasBrowserBackend,
    MixedCanvasDOMBackend,
)
from openadapt_flow.backends.clipboard_paste import paste_and_verify
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.deployment import BackendConfig


def _png(
    size: tuple[int, int] = (1280, 800), color: tuple[int, int, int] = (240, 240, 240)
) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeMouse:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", round(x, 2), round(y, 2)))

    def click(self, x: float, y: float, *, click_count: int = 1) -> None:
        self.events.append(("click", round(x, 2), round(y, 2), click_count))

    def down(self, *, button: str, click_count: int) -> None:
        self.events.append(("down", button, click_count))

    def up(self, *, button: str, click_count: int) -> None:
        self.events.append(("up", button, click_count))

    def wheel(self, dx: int, dy: int) -> None:
        self.events.append(("wheel", dx, dy))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def type(self, text: str, *, delay: int = 0) -> None:
        self.events.append(("type", text, delay))

    def press(self, key: str) -> None:
        self.events.append(("press", key))


class _FakeCanvas:
    def __init__(
        self,
        bbox: Optional[dict[str, float]] = None,
        png: Optional[bytes] = None,
    ) -> None:
        self._bbox = bbox or {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 800.0}
        self.png = png or _png((1280, 800))

    def bounding_box(self) -> Optional[dict[str, float]]:
        return self._bbox

    def screenshot(self) -> bytes:
        return self.png


class _FakeLocator:
    def __init__(self, element: _FakeCanvas) -> None:
        self.first = element


class _FakePage:
    def __init__(
        self,
        *,
        bbox: Optional[dict[str, float]] = None,
        png: Optional[bytes] = None,
        expected_selector: str = "canvas",
        has_canvas: bool = True,
    ) -> None:
        self.canvas = _FakeCanvas(bbox=bbox, png=png) if has_canvas else None
        self.expected_selector = expected_selector
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()

    def locator(self, selector: str) -> _FakeLocator:
        if self.canvas is None:
            return _FakeLocator(
                _FakeCanvas(bbox={"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0})
            )
        assert selector == self.expected_selector
        return _FakeLocator(self.canvas)

    def wait_for_timeout(self, ms: int) -> None:
        del ms


class _FakeClipboard:
    def __init__(self, initial: Optional[str] = "prior-host-clipboard") -> None:
        self.value: Optional[str] = initial
        self.history: list[tuple[str, Optional[str]]] = []

    def read_text(self) -> Optional[str]:
        self.history.append(("read", self.value))
        return self.value

    def write_text(self, text: str) -> None:
        self.value = text
        self.history.append(("write", text))

    def clear(self) -> None:
        self.value = ""
        self.history.append(("clear", ""))


class _SimulatedAppBackend:
    """Backend simulating a target field that accepts Ctrl+V and Ctrl+A/Ctrl+C."""

    def __init__(
        self,
        clipboard: _FakeClipboard,
        *,
        corrupt_paste: Optional[str] = None,
        fail_on_press: bool = False,
    ) -> None:
        self._clip = clipboard
        self.field_text = ""
        self._corrupt_paste = corrupt_paste
        self._fail_on_press = fail_on_press
        self.chords: list[str] = []

    def press(self, chord: str) -> None:
        self.chords.append(chord)
        if self._fail_on_press:
            raise RuntimeError("remote transport disconnected")
        low = chord.lower()
        if low.endswith("+v"):
            pasted = self._clip.value or ""
            self.field_text = (
                self._corrupt_paste if self._corrupt_paste is not None else pasted
            )
        elif low.endswith("+c"):
            self._clip.value = self.field_text

    def screenshot(self) -> bytes:
        return _png((320, 80))


def test_canvas_backend_letterbox_and_dpi_scaling():
    # Container is 1920x800 (wider than 1280x800): letterbox pads 320px on left/right.
    page = _FakePage(
        bbox={"x": 100.0, "y": 50.0, "width": 1920.0, "height": 800.0},
        png=_png((2560, 1600)),  # 2x DPI retina capture
        expected_selector="#vdi-canvas",
    )
    backend = CanvasBrowserBackend(
        page,
        width=1280,
        height=800,
        canvas_selector="#vdi-canvas",
        letterbox=True,
    )

    assert isinstance(backend, RemoteActuationBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)

    # Screenshot normalizes 2x retina PNG back to exact (1280, 800) framebuffer.
    frame = Image.open(io.BytesIO(backend.screenshot()))
    assert frame.size == (1280, 800)

    # Content rect starts at x = 100 + (1920 - 1280)/2 = 420, y = 50, scale = 1.0.
    backend.click(10, 20)
    assert page.mouse.events == [("click", 430.0, 70.0, 1)]


def test_canvas_backend_stretch_scaling_without_letterbox():
    page = _FakePage(bbox={"x": 10.0, "y": 20.0, "width": 640.0, "height": 400.0})
    backend = CanvasBrowserBackend(page, width=1280, height=800, letterbox=False)
    backend.click(640, 400)
    assert page.mouse.events == [("click", 330.0, 220.0, 1)]


def test_canvas_backend_readiness_probe_refuses_unready_frame():
    ready = {"ok": False}
    page = _FakePage()
    backend = CanvasBrowserBackend(
        page,
        readiness_probe=lambda _png: ready["ok"],
        application_marker="ClinicalPortal",
    )
    with pytest.raises(RuntimeError, match="readiness probe failed"):
        backend.acquire_actuation_frame()

    ready["ok"] = True
    assert backend.acquire_actuation_frame()


def test_factory_registers_canvas_kind_and_aliases():
    page = _FakePage(expected_selector="canvas#remote")
    for kind in ("canvas", "canvas-browser", "novnc"):
        cfg = BackendConfig(
            kind=kind,
            canvas_selector="canvas#remote",
            canvas_width=1024,
            canvas_height=768,
        )
        backend = build_backend(cfg, page=page)
        assert isinstance(backend, CanvasBrowserBackend)
        assert backend.viewport == (1024, 768)
        assert backend.canvas_selector == "canvas#remote"

    with pytest.raises(ValueError, match="needs a live Playwright page"):
        build_backend(BackendConfig(kind="canvas"))


def test_mixed_canvas_dom_backend_switches_tabs():
    class _FakeDOMBackend:
        viewport = (1440, 900)

        def __init__(self) -> None:
            self.clicks: list[tuple[int, int]] = []

        def screenshot(self) -> bytes:
            return _png((1440, 900))

        def click(self, x: int, y: int, *, double: bool = False) -> None:
            del double
            self.clicks.append((x, y))

        def type_text(self, text: str) -> None:
            del text

        def press(self, key: str) -> None:
            del key

        def scroll(self, dx: int, dy: int) -> None:
            del dx, dy

    dom_backend = _FakeDOMBackend()
    canvas_page = _FakePage()
    dom_page = _FakePage(has_canvas=False)
    canvas_backend = CanvasBrowserBackend(canvas_page)

    mixed = MixedCanvasDOMBackend(
        dom_backend=dom_backend, canvas_backend=canvas_backend
    )
    assert mixed.active_mode == "dom"
    mixed.click(50, 60)
    assert dom_backend.clicks == [(50, 60)]

    assert mixed.select_tab(canvas_page) == "canvas"
    mixed.click(100, 200)
    assert canvas_page.mouse.events == [("click", 100.0, 200.0, 1)]

    assert mixed.select_tab(dom_page) == "dom"
    assert mixed.viewport == (1440, 900)


def test_paste_and_verify_select_all_copy_and_clipboard_restore():
    clip = _FakeClipboard("original-user-clip")
    backend = _SimulatedAppBackend(clip)

    res = paste_and_verify(backend, "MRN-24682", clipboard=clip)
    assert res.verified is True
    assert res.observed == "MRN-24682"
    assert res.clipboard_zeroed is True
    assert res.clipboard_restored is True
    assert clip.value == "original-user-clip"
    # Verify clear happened before restore in finally.
    assert ("clear", "") in clip.history


def test_paste_and_verify_detects_mismatch_and_scrubs_clipboard():
    clip = _FakeClipboard("keep-me")
    backend = _SimulatedAppBackend(clip, corrupt_paste="MRN-24683")

    res = paste_and_verify(backend, "MRN-24682", clipboard=clip)
    assert res.verified is False
    assert res.observed == "MRN-24683"
    assert res.clipboard_zeroed is True
    assert res.clipboard_restored is True
    assert clip.value == "keep-me"


def test_paste_and_verify_ocr_mode_exact_match():
    clip = _FakeClipboard("before")
    backend = _SimulatedAppBackend(clip)

    res = paste_and_verify(
        backend,
        "OD",
        clipboard=clip,
        verify_mode="ocr",
        ocr_reader=lambda _png, _reg: "OD",
    )
    assert res.verified is True
    assert clip.value == "before"

    res_bad = paste_and_verify(
        backend,
        "OD",
        clipboard=clip,
        verify_mode="ocr",
        ocr_reader=lambda _png, _reg: "OS",
    )
    assert res_bad.verified is False
    assert clip.value == "before"


def test_paste_and_verify_scrubs_clipboard_even_on_crash():
    clip = _FakeClipboard("sensitive-prior-clip")
    backend = _SimulatedAppBackend(clip, fail_on_press=True)

    with pytest.raises(RuntimeError, match="remote transport disconnected"):
        paste_and_verify(backend, "SSN-123-45-6789", clipboard=clip)

    # Even though press() crashed, finally zeroed and restored the host clipboard.
    assert ("clear", "") in clip.history
    assert clip.value == "sensitive-prior-clip"
