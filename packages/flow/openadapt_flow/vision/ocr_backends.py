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
#: False while the native backend halts correct runs more often than RapidOCR
#: in the identity and OCR safety evidence (no false accepts on either engine,
#: but more false aborts with Apple Vision); until then ``native`` is opt-in.
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


_VISION_FRAMEWORK = "/System/Library/Frameworks/Vision.framework"


def native_backend_available() -> bool:
    """Return whether this platform's native OCR can be imported, cheaply."""
    import importlib.util

    try:
        if sys.platform == "darwin":
            return (
                importlib.util.find_spec("objc") is not None
                and importlib.util.find_spec("Foundation") is not None
                and os.path.isdir(_VISION_FRAMEWORK)
            )
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

    Three measured fixes for screen captures (see
    ``scripts/native_ocr_tuning/RESEARCH.md``):

    * Tall images are read in overlapping horizontal strips. On a full
      2240x3702 frame Vision skipped whole table columns that it read in
      every strip.
    * Short images (identity bands, row crops) are upscaled before reading.
    * A read with any upside-down line is redone with request revision 2.
      Revision 3 sometimes reads a crop that slices through the rows above
      and below rotated 180 degrees; revision 2 has no orientation step, and
      a screen capture is always upright.

    Vision.framework is loaded straight from the system with
    ``objc.loadBundle``, and Vision decodes the PNG itself. That skips
    PyObjC's Vision and Quartz wrapper packages, which take about two seconds
    to load the first time on a new install.
    """

    name = "apple-vision"
    languages = ("en-US",)
    #: ``VNRequestTextRecognitionLevelAccurate``.
    _LEVEL_ACCURATE = 0
    #: Images taller than ``STRIP_HEIGHT * 1.25`` are read in strips of about
    #: this height, each overlapping its neighbours by ``STRIP_OVERLAP``.
    STRIP_HEIGHT = 768
    STRIP_OVERLAP = 192
    #: Images shorter than this are upscaled by ``SHORT_IMAGE_SCALE``.
    SHORT_IMAGE_HEIGHT = 300
    SHORT_IMAGE_SCALE = 2
    #: Request revision used to re-read an image that came back upside down.
    UPRIGHT_REVISION = 2

    def __init__(self) -> None:
        import objc
        from Foundation import NSData

        classes: dict[str, Any] = {}
        objc.loadBundle("Vision", classes, bundle_path=_VISION_FRAMEWORK)
        self._request_class = classes["VNRecognizeTextRequest"]
        self._handler_class = classes["VNImageRequestHandler"]
        self._nsdata = NSData
        self._lock = threading.Lock()
        probe = self._request_class.alloc().init()
        self._revision = int(probe.revision())
        supported = self._request_class.supportedRevisions()
        self._upright_revision: Optional[int] = (
            self.UPRIGHT_REVISION
            if self._revision != self.UPRIGHT_REVISION
            and bool(supported.containsIndex_(self.UPRIGHT_REVISION))
            else None
        )

    def _request(self, revision: Optional[int] = None) -> Any:
        request = self._request_class.alloc().init()
        request.setRevision_(self._revision if revision is None else revision)
        request.setRecognitionLevel_(self._LEVEL_ACCURATE)
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(list(self.languages))
        return request

    def recognize_png(self, png: bytes) -> list[RawLine]:
        from openadapt_flow.vision.imgops import png_size

        size = png_size(png)
        if size is None:
            raise ValueError("could not decode PNG bytes")
        width, height = size
        if height > self.STRIP_HEIGHT * 1.25:
            return self._recognize_strips(png, width, height)
        if height < self.SHORT_IMAGE_HEIGHT:
            return self._recognize_upscaled(png, self.SHORT_IMAGE_SCALE)
        return self._recognize(png, width, height)

    def _recognize_strips(self, png: bytes, width: int, height: int) -> list[RawLine]:
        """Read a tall image strip by strip.

        Each line is kept from the one strip whose core (the strip without
        its overlap) holds the line's vertical center; the overlap keeps that
        line whole inside the strip.
        """
        from PIL import Image

        count = -(-height // self.STRIP_HEIGHT)
        step = -(-height // count)
        lines: list[RawLine] = []
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            for k in range(count):
                core0, core1 = k * step, min(height, (k + 1) * step)
                y0 = max(0, core0 - self.STRIP_OVERLAP)
                y1 = min(height, core1 + self.STRIP_OVERLAP)
                strip = _encode_png(image.crop((0, y0, width, y1)))
                for x, y, w, h, text, confidence in self._recognize(
                    strip, width, y1 - y0
                ):
                    if core0 <= y + y0 + h / 2 < core1:
                        lines.append((x, y + y0, w, h, text, confidence))
        return lines

    def _recognize_upscaled(self, png: bytes, scale: int) -> list[RawLine]:
        """Read an image upscaled by ``scale`` (Lanczos); boxes map back."""
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
            big = image.convert("RGB").resize(
                (width * scale, height * scale), Image.Resampling.LANCZOS
            )
        lines: list[RawLine] = []
        for x, y, w, h, text, confidence in self._recognize(
            _encode_png(big), width * scale, height * scale
        ):
            x0, y0 = x // scale, y // scale
            x1 = min(width, -(-(x + w) // scale))
            y1 = min(height, -(-(y + h) // scale))
            lines.append((x0, y0, max(0, x1 - x0), max(0, y1 - y0), text, confidence))
        return lines

    def _recognize(self, png: bytes, width: int, height: int) -> list[RawLine]:
        """One read; redone at the upright revision if any line is flipped."""
        observed = self._observe(png, width, height, None)
        if self._upright_revision is not None and any(f for _, f in observed):
            observed = self._observe(png, width, height, self._upright_revision)
        return [line for line, _flipped in observed]

    def _observe(
        self, png: bytes, width: int, height: int, revision: Optional[int]
    ) -> list[tuple[RawLine, bool]]:
        """Run Vision once; each line with whether it was read upside down."""
        data = self._nsdata.dataWithBytes_length_(png, len(png))
        request = self._request(revision)
        handler = self._handler_class.alloc().initWithData_options_(data, None)
        with self._lock:
            result = handler.performRequests_error_([request], None)
        # Without the wrapper's metadata PyObjC returns only the BOOL.
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if not ok:
            raise RuntimeError("Apple Vision text recognition failed")
        lines: list[tuple[RawLine, bool]] = []
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
            # The quad's top edge runs right to left on an upside-down read.
            flipped = bool(observation.topLeft().x > observation.topRight().x)
            lines.append(
                (
                    (
                        ix0,
                        iy0,
                        max(0, ix1 - ix0),
                        max(0, iy1 - iy0),
                        str(candidate.string()),
                        float(candidate.confidence()),
                    ),
                    flipped,
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
            "upright_revision": self._upright_revision,
            "recognition_level": "accurate",
            "language_correction": False,
            "languages": list(self.languages),
            "image_input": "png-data",
            "strip_height": self.STRIP_HEIGHT,
            "strip_overlap": self.STRIP_OVERLAP,
            "short_image_height": self.SHORT_IMAGE_HEIGHT,
            "short_image_scale": self.SHORT_IMAGE_SCALE,
            "pyobjc": str(getattr(objc, "__version__", "")),
        }


def _encode_png(image: Any) -> bytes:
    """Encode a PIL image as PNG, favouring speed over size."""
    out = io.BytesIO()
    image.save(out, format="PNG", compress_level=1)
    return out.getvalue()


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
