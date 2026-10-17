"""OCR backends: RapidOCR everywhere, and the operating system's own OCR.

``OPENADAPT_FLOW_OCR`` selects the backend:

* ``rapidocr`` -- RapidOCR on ONNX Runtime. Works on every platform; the
  identity and OCR safety evidence in ``docs/validation`` was measured with it.
* ``native`` -- Apple Vision on macOS, ``Windows.Media.Ocr`` on Windows. Needs
  the ``native-ocr`` extra. Refuses to start on any other platform or when the
  extra is missing, instead of silently using another engine.
* ``auto`` (default) -- see :data:`AUTO_PREFERS_NATIVE`.

Every backend returns lines as ``(x, y, w, h, text, confidence)`` in the
coordinates of the image it was given. Backends never import OpenCV.
"""

from __future__ import annotations

import io
import os
import platform
import sys
import threading
from typing import Any, Optional, Protocol

OCR_ENV = "OPENADAPT_FLOW_OCR"

#: Whether ``auto`` picks the native backend when it is available. It stays
#: False until the native backend matches RapidOCR on the identity and OCR
#: safety evidence (no new false accepts); until then ``native`` is opt-in.
AUTO_PREFERS_NATIVE = False

RawLine = tuple[int, int, int, int, str, float]


class OcrBackendUnavailable(RuntimeError):
    """The requested OCR backend can't run on this machine."""


class NativeOcrEngine(Protocol):
    """An OCR engine that reads PNG bytes directly."""

    name: str

    def recognize_png(self, png: bytes) -> list[RawLine]: ...

    def contract(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def _choice() -> str:
    choice = (os.environ.get(OCR_ENV) or "auto").strip().lower()
    if choice not in ("auto", "native", "rapidocr"):
        raise OcrBackendUnavailable(
            f"{OCR_ENV}={choice!r} is not one of auto, native, rapidocr"
        )
    return choice


def native_backend_available() -> bool:
    """Return whether this platform's native OCR can be imported, cheaply."""
    import importlib.util

    try:
        if sys.platform == "darwin":
            return importlib.util.find_spec("Vision") is not None
        if sys.platform.startswith("win"):
            return importlib.util.find_spec("winrt.windows.media.ocr") is not None
    except (ImportError, ValueError):
        return False
    return False


_resolved: Optional[str] = None
_resolved_for: Optional[str] = None


def active_backend_name() -> str:
    """Return ``"native"`` or ``"rapidocr"`` for the current setting.

    Resolved once per value of the setting, so one run never mixes engines.
    """
    global _resolved, _resolved_for
    raw = os.environ.get(OCR_ENV)
    if _resolved is not None and _resolved_for == raw:
        return _resolved
    choice = _choice()
    if choice == "native":
        if not native_backend_available():
            raise OcrBackendUnavailable(_missing_native_message())
        name = "native"
    elif choice == "auto" and AUTO_PREFERS_NATIVE and native_backend_available():
        name = "native"
    else:
        name = "rapidocr"
    _resolved, _resolved_for = name, raw
    return name


def _missing_native_message() -> str:
    if sys.platform == "darwin":
        return (
            f"{OCR_ENV}=native needs Apple Vision through PyObjC. Install it with "
            "pip install 'openadapt-flow[native-ocr]'."
        )
    if sys.platform.startswith("win"):
        return (
            f"{OCR_ENV}=native needs Windows OCR through the winrt packages. "
            "Install them with pip install 'openadapt-flow[native-ocr]'."
        )
    return (
        f"{OCR_ENV}=native is available on macOS and Windows only. Use "
        f"{OCR_ENV}=rapidocr on this platform."
    )


def create_native_engine() -> NativeOcrEngine:
    """Build this platform's native engine."""
    if sys.platform == "darwin":
        return AppleVisionEngine()
    if sys.platform.startswith("win"):
        return WindowsOcrEngine()
    raise OcrBackendUnavailable(_missing_native_message())


# --------------------------------------------------------------------------
# Apple Vision
# --------------------------------------------------------------------------


class AppleVisionEngine:
    """``VNRecognizeTextRequest`` at the accurate level, no language correction.

    Language correction is off so identifiers such as MRNs are read as drawn,
    not corrected toward dictionary words.
    """

    name = "apple-vision"
    languages = ("en-US",)

    def __init__(self) -> None:
        import Quartz  # noqa: F401 - fail here, not mid-run
        import Vision

        self._vision = Vision
        self._lock = threading.Lock()
        probe = Vision.VNRecognizeTextRequest.alloc().init()
        self._revision = int(probe.revision())

    def _request(self) -> Any:
        vision = self._vision
        request = vision.VNRecognizeTextRequest.alloc().init()
        request.setRevision_(self._revision)
        request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(list(self.languages))
        return request

    def recognize_png(self, png: bytes) -> list[RawLine]:
        import Quartz
        from Foundation import NSData

        data = NSData.dataWithBytes_length_(png, len(png))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        if source is None:
            raise ValueError("could not decode PNG bytes")
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            raise ValueError("could not decode PNG bytes")
        width = Quartz.CGImageGetWidth(image)
        height = Quartz.CGImageGetHeight(image)
        request = self._request()
        handler = self._vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            image, None
        )
        with self._lock:
            ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(f"Apple Vision text recognition failed: {error}")
        lines: list[RawLine] = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            box = observation.boundingBox()
            x0 = box.origin.x * width
            x1 = (box.origin.x + box.size.width) * width
            # Vision's origin is the bottom-left corner.
            y0 = (1.0 - box.origin.y - box.size.height) * height
            y1 = (1.0 - box.origin.y) * height
            ix0, iy0 = int(max(0.0, x0)), int(max(0.0, y0))
            ix1, iy1 = int(min(float(width), x1)), int(min(float(height), y1))
            lines.append(
                (
                    ix0,
                    iy0,
                    max(0, ix1 - ix0),
                    max(0, iy1 - iy0),
                    str(candidate.string()),
                    float(candidate.confidence()),
                )
            )
        return lines

    def contract(self) -> dict[str, Any]:
        import objc

        return {
            "engine": self.name,
            "macos": platform.mac_ver()[0],
            "machine": platform.machine(),
            "request_revision": self._revision,
            "recognition_level": "accurate",
            "language_correction": False,
            "languages": list(self.languages),
            "pyobjc": str(getattr(objc, "__version__", "")),
        }


# --------------------------------------------------------------------------
# Windows.Media.Ocr
# --------------------------------------------------------------------------


class WindowsOcrEngine:
    """``Windows.Media.Ocr`` for the English (United States) language pack.

    Windows OCR reports no per-line confidence. Lines it returns are given
    confidence 1.0, so the caller's other checks (exact text, identity
    quorum) still decide.
    """

    name = "windows-media-ocr"
    language_tag = "en-US"

    def __init__(self) -> None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        language = Language(self.language_tag)
        if not OcrEngine.is_language_supported(language):
            raise OcrBackendUnavailable(
                "Windows OCR has no English (United States) language pack. Add "
                "it in Settings > Time & language, or set "
                f"{OCR_ENV}=rapidocr."
            )
        self._engine = OcrEngine.try_create_from_language(language)
        if self._engine is None:
            raise OcrBackendUnavailable("Windows OCR could not start")
        self._lock = threading.Lock()

    def recognize_png(self, png: bytes) -> list[RawLine]:
        import asyncio

        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            rgba = image.convert("RGBA")
        width, height = rgba.size
        # SoftwareBitmap takes BGRA8 pixels.
        r, g, b, a = rgba.split()
        bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()

        async def run() -> Any:
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode,
                BitmapPixelFormat,
                SoftwareBitmap,
            )
            from winrt.windows.storage.streams import DataWriter

            writer = DataWriter()
            writer.write_bytes(bgra)
            buffer = writer.detach_buffer()
            bitmap = SoftwareBitmap.create_copy_from_buffer(
                buffer,
                BitmapPixelFormat.BGRA8,
                width,
                height,
                BitmapAlphaMode.IGNORE,
            )
            return await self._engine.recognize_async(bitmap)

        with self._lock:
            result = asyncio.run(run())
        lines: list[RawLine] = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            xs0 = [w.bounding_rect.x for w in words]
            ys0 = [w.bounding_rect.y for w in words]
            xs1 = [w.bounding_rect.x + w.bounding_rect.width for w in words]
            ys1 = [w.bounding_rect.y + w.bounding_rect.height for w in words]
            x0, y0 = int(min(xs0)), int(min(ys0))
            x1, y1 = int(max(xs1)), int(max(ys1))
            lines.append((x0, y0, x1 - x0, y1 - y0, str(line.text), 1.0))
        return lines

    def contract(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "windows": platform.version(),
            "machine": platform.machine(),
            "language": self.language_tag,
        }
