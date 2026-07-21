"""OCR utilities backed by rapidocr-onnxruntime.

The RapidOCR engine is expensive to construct, so a single module-level
instance is created lazily on first use and reused for the process lifetime.
All returned coordinates are in *screen* (full-frame) pixel space, even when
a ``region`` restricts the OCR input.
"""

from __future__ import annotations

import difflib
import threading
from typing import Any, Optional

import cv2
import numpy as np
from pydantic import BaseModel

from openadapt_flow.ir import Region
from openadapt_flow.vision.match import Match, _clamp_region

_engine: Any = None
_engine_lock = threading.Lock()


def _get_engine() -> Any:
    """Return the process-wide RapidOCR engine, creating it on first use."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


class OcrLine(BaseModel):
    """One recognized text line.

    Attributes:
        text: Recognized text.
        region: Bounding box ``(x, y, w, h)`` in screen coordinates.
        confidence: Recognition confidence in ``[0, 1]``.
    """

    text: str
    region: Region
    confidence: float


def _decode_bgr(png: bytes) -> np.ndarray:
    """Decode PNG bytes to a BGR ``uint8`` image."""
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    return img


def ocr(screen_png: bytes, *, region: Region | None = None) -> list[OcrLine]:
    """Run OCR on a screenshot (optionally restricted to a region).

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        region: Optional ``(x, y, w, h)`` sub-region to OCR (clamped to the
            frame). Returned line regions are offset back into full-screen
            coordinates.

    Returns:
        Recognized lines in engine order; empty list if nothing was found.
    """
    img = _decode_bgr(screen_png)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region is not None:
        clamped = _clamp_region(region, w, h)
        if clamped is None:
            return []
        off_x, off_y, rw, rh = clamped
        img = img[off_y : off_y + rh, off_x : off_x + rw]

    result, _elapse = _get_engine()(img)
    lines: list[OcrLine] = []
    if not result:
        return lines
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = int(min(xs)), int(min(ys))
        x1, y1 = int(max(xs)), int(max(ys))
        lines.append(
            OcrLine(
                text=str(text),
                region=(off_x + x0, off_y + y0, x1 - x0, y1 - y0),
                confidence=float(score),
            )
        )
    return lines


def normalize_text(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


def upscale_png(screen_png: bytes, factor: int = 2) -> bytes:
    """Upscale a PNG (cubic) so OCR can read dense or small text.

    Args:
        screen_png: PNG bytes.
        factor: Integer upscale factor.

    Returns:
        The upscaled PNG bytes (the input unchanged if decoding fails).
    """
    img = cv2.imdecode(np.frombuffer(screen_png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return screen_png
    up = cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", up)
    return buf.tobytes() if ok else screen_png


def text_present(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None = None,
    min_ratio: float = 0.8,
) -> bool:
    """OCR-segmentation-tolerant presence check for a text snippet.

    :func:`find_text` fuzzy-matches whole OCR lines, which makes a bare
    presence check segmentation-dependent: the engine sometimes merges a
    short target into one long box together with neighboring text (the
    whole-line similarity then falls below ``min_ratio`` even though the
    text is plainly on screen) and sometimes splits one rendered line into
    several boxes. Presence must not depend on that coin flip, so this
    check passes when EITHER

    - some OCR line whole-line fuzzy-matches ``text`` at ``min_ratio``
      (exactly :func:`find_text`'s criterion), or
    - a contiguous run of at least ``min_ratio * len(target)`` squashed
      (lowercased, whitespace-stripped) target characters appears in the
      squashed concatenation of all OCR lines in engine reading order —
      tolerant of the target being embedded in a longer box or split
      across boxes, while scattered per-character coincidences (which
      accumulate on dense screens) still fail because the matched run
      must be contiguous.

    When the raw frame misses, the frame is retried once at 2x resolution
    (rapidocr drops dense lines at common screen resolutions).

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        text: Target text snippet.
        region: Optional ``(x, y, w, h)`` sub-region to search within.
        min_ratio: Minimum whole-line similarity ratio / contiguous-run
            fraction of the target to accept.

    Returns:
        Whether the text is considered present.
    """
    target = normalize_text(text)
    squashed_target = "".join(target.split())
    if not squashed_target:
        return False
    for factor in (1, 2):
        png = screen_png if factor == 1 else upscale_png(screen_png, factor)
        scaled_region = (
            None
            if region is None
            else (
                region[0] * factor,
                region[1] * factor,
                region[2] * factor,
                region[3] * factor,
            )
        )
        lines = ocr(png, region=scaled_region)
        if not lines:
            continue
        for line in lines:
            ratio = difflib.SequenceMatcher(
                None, normalize_text(line.text), target
            ).ratio()
            if ratio >= min_ratio:
                return True
        hay = "".join(normalize_text(" ".join(line.text for line in lines)).split())
        # autojunk=False: the default heuristic marks frequent characters
        # of a long OCR haystack as junk, silently dropping real matches.
        matcher = difflib.SequenceMatcher(None, squashed_target, hay, autojunk=False)
        longest = max(
            (block.size for block in matcher.get_matching_blocks()),
            default=0,
        )
        if longest >= min_ratio * len(squashed_target):
            return True
    return False


def find_text(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None = None,
    min_ratio: float = 0.8,
) -> Match | None:
    """Locate a text label on screen via OCR plus fuzzy matching.

    Each OCR line is compared to ``text`` with
    ``difflib.SequenceMatcher.ratio()`` over normalized (lowercased,
    whitespace-collapsed) strings; the best line at or above ``min_ratio``
    wins.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        text: Target text to find.
        region: Optional ``(x, y, w, h)`` sub-region to search within.
        min_ratio: Minimum similarity ratio in ``[0, 1]`` to accept.

    Returns:
        A :class:`Match` centered on the best-matching line's bounding box,
        or ``None`` if no line is similar enough.
    """
    target = normalize_text(text)
    if not target:
        return None
    best: Optional[tuple[float, OcrLine]] = None
    for line in ocr(screen_png, region=region):
        ratio = difflib.SequenceMatcher(None, normalize_text(line.text), target).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, line)
    if best is None or best[0] < min_ratio:
        return None
    ratio, line = best
    x, y, w, h = line.region
    return Match(point=(x + w // 2, y + h // 2), region=line.region, confidence=ratio)
