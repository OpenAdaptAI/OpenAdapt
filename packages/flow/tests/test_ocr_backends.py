"""OCR backend selection, the native engines, and the OpenCV-free path."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.vision import ocr_backends as ob


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ob, "_resolved", None)
    monkeypatch.setattr(ob, "_resolved_for", None)


def test_auto_is_rapidocr_until_native_is_qualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ob.OCR_ENV, raising=False)
    monkeypatch.setattr(ob, "native_backend_available", lambda: True)
    expected = "native" if ob.AUTO_PREFERS_NATIVE else "rapidocr"
    assert ob.active_backend_name() == expected


def test_native_without_engine_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ob.OCR_ENV, "native")
    monkeypatch.setattr(ob, "native_backend_available", lambda: False)
    with pytest.raises(ob.OcrBackendUnavailable):
        ob.active_backend_name()


def test_unknown_choice_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ob.OCR_ENV, "tesseract")
    with pytest.raises(ob.OcrBackendUnavailable, match="not one of"):
        ob.active_backend_name()


def test_rapidocr_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ob.OCR_ENV, "rapidocr")
    monkeypatch.setattr(ob, "native_backend_available", lambda: True)
    assert ob.active_backend_name() == "rapidocr"


class _FakeEngine:
    name = "fake-native"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def recognize_png(self, png: bytes) -> list[ob.RawLine]:
        with Image.open(io.BytesIO(png)) as image:
            self.calls.append(image.size)
        return [(2, 3, 10, 5, "Jane Sample", 0.5), (0, 0, 1, 1, "", 1.0)]

    def contract(self) -> dict[str, Any]:
        return {"engine": self.name, "version": 1}


def _frame() -> bytes:
    image = Image.new("RGB", (200, 100), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_native_region_offsets_and_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from openadapt_flow import vision

    ocr_module = importlib.import_module("openadapt_flow.vision.ocr")

    engine = _FakeEngine()
    monkeypatch.setattr(ocr_module, "_engine", engine)
    ocr_module.clear_ocr_memo()
    lines = ocr_module.ocr(_frame(), region=(50, 40, 60, 30))
    assert engine.calls == [(60, 30)]
    assert [(line.text, line.region, line.confidence) for line in lines] == [
        ("Jane Sample", (52, 43, 10, 5), 0.5)
    ]
    assert vision.program_predicate_contract() == {
        "ocr_backend": "fake-native",
        "ocr_engine": {"engine": "fake-native", "version": 1},
    }
    ocr_module.clear_ocr_memo()


def test_runtime_imports_do_not_load_opencv() -> None:
    code = (
        "import sys\n"
        "import openadapt_flow.__main__, openadapt_flow.tutorial\n"
        "import openadapt_flow.vision, openadapt_flow.compiler\n"
        "import openadapt_flow.runtime, openadapt_flow.recorder\n"
        "import openadapt_flow.backends.playwright_backend\n"
        "print('cv2' in sys.modules, 'rapidocr_onnxruntime' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["False", "False"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS-only")
def test_apple_vision_reads_rendered_text() -> None:
    pytest.importorskip("objc")
    engine = ob.AppleVisionEngine()
    image = Image.new("RGB", (480, 120), "white")
    draw = ImageDraw.Draw(image)
    try:
        font: Any = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 40), "MRN 100512 Save Encounter", fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    lines = engine.recognize_png(out.getvalue())
    text = " ".join(line[4] for line in lines)
    assert "100512" in text
    x, y, w, h = lines[0][:4]
    assert 0 <= x < 100 and 20 <= y < 80 and w > 100 and 10 < h < 60
    contract = engine.contract()
    assert contract["engine"] == "apple-vision"
    assert contract["language_correction"] is False


def test_prewarm_image_is_a_valid_png() -> None:
    """The prewarm image must decode, or the native engine rejects it and
    the warm-up silently does nothing (the earlier 1x1 constant did)."""
    from openadapt_flow import _prewarm
    from openadapt_flow.vision.imgops import png_size

    assert png_size(_prewarm._blank_png()) == (64, 16)
    import io

    from PIL import Image

    with Image.open(io.BytesIO(_prewarm._blank_png())) as image:
        image.load()
        assert image.size == (64, 16)


@pytest.mark.parametrize("flag", ["-h", "--help", "--version"])
@pytest.mark.parametrize("command", ["demo", "tutorial"])
def test_help_does_not_start_the_prewarm(
    monkeypatch: pytest.MonkeyPatch, command: str, flag: str
) -> None:
    """`tutorial --help` exited while onnxruntime was still loading on the
    prewarm thread, and the native runtime aborted the process (exit -6)."""
    from openadapt_flow import __main__ as cli
    from openadapt_flow import _prewarm

    started: list[bool] = []
    monkeypatch.setattr(_prewarm, "start", lambda: started.append(True))
    with pytest.raises(SystemExit):
        cli.main([command, flag])
    assert started == []


def test_exit_waits_for_an_ocr_load_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from openadapt_flow import _prewarm

    release = threading.Event()
    registered: list[Any] = []
    monkeypatch.setattr(_prewarm, "_started", False)
    monkeypatch.setattr(_prewarm, "_load", release.wait)
    monkeypatch.setattr(_prewarm, "_touch_driver", lambda: None)
    monkeypatch.setattr(
        _prewarm.atexit, "register", lambda fn, *args: registered.append((fn, args))
    )
    _prewarm.start()

    assert len(registered) == 1
    fn, (thread,) = registered[0]
    assert fn is _prewarm._wait_for_load and thread.name == "ocr-prewarm"
    assert thread.is_alive()
    release.set()
    fn(thread)
    assert not thread.is_alive()
