"""Fast contracts for the no-DOM canvas backend's remote actuation lease."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.backend import (
    PreparedPointerActuationBackend,
    RemoteActuationBackend,
)

HARNESS = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "canvas_ladder"
    / "run_canvas_ladder_qualification.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("canvas_ladder_backend", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (1280, 800), color)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class _Mouse:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def move(self, x, y) -> None:
        self.events.append(("move", x, y))

    def click(self, x, y, *, click_count=1) -> None:
        self.events.append(("click", x, y, click_count))

    def down(self, *, button, click_count) -> None:
        self.events.append(("down", button, click_count))

    def up(self, *, button, click_count) -> None:
        self.events.append(("up", button, click_count))


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def type(self, text, *, delay) -> None:
        self.events.append(("type", text, delay))

    def press(self, key) -> None:
        self.events.append(("press", key))


class _Canvas:
    def __init__(self) -> None:
        self.png = _png((245, 246, 248))

    def bounding_box(self):
        return {"x": 12.0, "y": 18.0, "width": 1280.0, "height": 800.0}

    def screenshot(self):
        return self.png


class _Locator:
    def __init__(self, canvas) -> None:
        self.first = canvas


class _Page:
    def __init__(self) -> None:
        self.canvas = _Canvas()
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()

    def locator(self, selector):
        assert selector == "canvas"
        return _Locator(self.canvas)

    def wait_for_timeout(self, milliseconds) -> None:
        del milliseconds


def test_canvas_backend_binds_fresh_frame_to_prepared_click():
    module = _module()
    page = _Page()
    backend = module.CanvasBrowserBackend(page)

    assert isinstance(backend, RemoteActuationBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)

    backend.prepare_pointer_actuation(*module.SAVE_BUTTON)
    backend.acquire_actuation_frame()
    backend.click(*module.SAVE_BUTTON)

    assert page.mouse.events == [
        ("move", 922.0, 666.0),
        ("down", "left", 1),
        ("up", "left", 1),
    ]


def test_canvas_backend_refuses_changed_frame_before_input():
    module = _module()
    page = _Page()
    backend = module.CanvasBrowserBackend(page)

    backend.prepare_pointer_actuation(*module.SAVE_BUTTON)
    backend.acquire_actuation_frame()
    page.canvas.png = _png((20, 30, 40))

    with pytest.raises(RuntimeError, match="content changed"):
        backend.click(*module.SAVE_BUTTON)

    assert not any(event[0] in {"down", "up", "click"} for event in page.mouse.events)


def test_drift_wrapper_preserves_remote_actuation_protocol():
    module = _module()
    backend = module._DriftBackend.moderate(module.CanvasBrowserBackend(_Page()))

    assert isinstance(backend, RemoteActuationBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)


def test_canvas_backend_maps_portable_select_all_to_remote_control():
    module = _module()
    page = _Page()

    module.CanvasBrowserBackend(page).press("ControlOrMeta+a")

    assert page.keyboard.events == [("press", "Control+a")]
