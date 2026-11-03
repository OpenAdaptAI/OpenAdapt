"""OCR utilities backed by RapidOCR or the operating system's own OCR.

The backend comes from ``OPENADAPT_FLOW_OCR`` (see
:mod:`openadapt_flow.vision.ocr_backends`). The engine is expensive to
construct, so a single module-level instance is created lazily on first use
and reused for the process lifetime.
All returned coordinates are in *screen* (full-frame) pixel space, even when
a ``region`` restricts the OCR input.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from pydantic import BaseModel

from openadapt_flow.ir import Region
from openadapt_flow.runtime.step_timing import timed_span
from openadapt_flow.vision import imgops
from openadapt_flow.vision.match import Match, _clamp_region

_engine: Any = None
_engine_lock = threading.Lock()


class OcrResolutionRefused(RuntimeError):
    """Base class for fail-closed OCR target-resolution refusals."""


class AmbiguousOcrMatchError(OcrResolutionRefused):
    """Raised when target resolution sees more than one qualifying OCR line.

    Generic OCR consumers retain the historical best-match behavior. The
    resolution ladder opts into this typed signal so it can distinguish
    ambiguity (halt) from absence (continue to the next independent evidence
    rung).
    """


class ContradictoryOcrEvidenceError(OcrResolutionRefused):
    """Raised when independently located OCR evidence disagrees on target."""


def _get_engine() -> Any:
    """Return the process-wide OCR engine, creating it on first use."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from openadapt_flow.vision.ocr_backends import (
                    active_backend_name,
                    create_native_engine,
                )

                if active_backend_name() == "native":
                    _engine = create_native_engine()
                else:
                    from rapidocr_onnxruntime import RapidOCR

                    # Parallel worklist workers share this engine; capping its
                    # threads stops N concurrent calls from each claiming
                    # every core. Unset keeps onnxruntime's default.
                    threads = os.environ.get("OPENADAPT_FLOW_OCR_THREADS")
                    _engine = (
                        RapidOCR(intra_op_num_threads=int(threads))
                        if threads
                        else RapidOCR()
                    )
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
    img = imgops.decode_bgr(png)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    return img


_OCR_MEMO_MAX = 16
_ocr_memo: OrderedDict[tuple[str, Region | None], tuple[Any, tuple[OcrLine, ...]]] = (
    OrderedDict()
)
_ocr_memo_lock = threading.Lock()


def clear_ocr_memo() -> None:
    """Drop every memoized OCR result."""

    with _ocr_memo_lock:
        _ocr_memo.clear()


# Frames derived from another frame: a display-scaled remote session resampled
# to the recorded geometry (runtime.render_scale). Resampling merges the gaps
# between words, so OCR reads the source frame, where the text has more
# pixels, and maps each line back. Keyed by the derived frame's SHA-256.
_SOURCE_MAX = 16
_frame_sources: OrderedDict[
    str,
    tuple[
        bytes, Callable[[Region], Region], Callable[[Region], Region], tuple[int, int]
    ],
] = OrderedDict()


def register_frame_source(
    derived_png: bytes,
    source_png: bytes,
    *,
    to_source: Callable[[Region], Region],
    from_source: Callable[[Region], Region],
    derived_size: tuple[int, int],
) -> None:
    """Have :func:`ocr` of ``derived_png`` read ``source_png`` instead.

    ``to_source`` maps a region of the derived frame into the source frame and
    ``from_source`` maps a recognized line's region back.
    """
    key = hashlib.sha256(derived_png).hexdigest()
    with _ocr_memo_lock:
        _frame_sources[key] = (source_png, to_source, from_source, derived_size)
        _frame_sources.move_to_end(key)
        while len(_frame_sources) > _SOURCE_MAX:
            _frame_sources.popitem(last=False)


def forget_frame_sources(derived_sha256: Iterable[str] | None = None) -> None:
    """Drop registered frame sources: the given derived-frame digests, or all."""

    with _ocr_memo_lock:
        if derived_sha256 is None:
            _frame_sources.clear()
            return
        for key in derived_sha256:
            _frame_sources.pop(key, None)


def _ocr_from_source(
    source: tuple[
        bytes, Callable[[Region], Region], Callable[[Region], Region], tuple[int, int]
    ],
    region: Region | None,
) -> list[OcrLine]:
    source_png, to_source, from_source, (width, height) = source
    lines = ocr(source_png, region=None if region is None else to_source(region))
    out: list[OcrLine] = []
    for line in lines:
        mapped = _clamp_region(from_source(line.region), width, height)
        if mapped is not None:
            out.append(line.model_copy(update={"region": mapped}))
    return out


@timed_span("ocr")
def ocr(screen_png: bytes, *, region: Region | None = None) -> list[OcrLine]:
    """Run OCR on a screenshot (optionally restricted to a region).

    Memoized on the SHA-256 of the exact PNG bytes, the region, and the
    engine object: OCR is a pure function of those, and replay asks it about
    the same frame several times per step (identity, landmark, postcondition).
    One changed pixel is a different key, so a memo hit is never stale.
    Callers always receive fresh copies. A frame registered with
    :func:`register_frame_source` is read from its source frame.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        region: Optional ``(x, y, w, h)`` sub-region to OCR (clamped to the
            frame). Returned line regions are offset back into full-screen
            coordinates.

    Returns:
        Recognized lines in engine order; empty list if nothing was found.
    """

    engine = _get_engine()
    key = (hashlib.sha256(screen_png).hexdigest(), region)
    with _ocr_memo_lock:
        hit = _ocr_memo.get(key)
        if hit is not None and hit[0] is engine:
            _ocr_memo.move_to_end(key)
            return [line.model_copy() for line in hit[1]]
    with _ocr_memo_lock:
        source = _frame_sources.get(key[0])
    if source is not None:
        lines = _ocr_from_source(source, region)
    else:
        lines = _ocr_uncached(engine, screen_png, region=region)
    with _ocr_memo_lock:
        _ocr_memo[key] = (engine, tuple(line.model_copy() for line in lines))
        _ocr_memo.move_to_end(key)
        while len(_ocr_memo) > _OCR_MEMO_MAX:
            _ocr_memo.popitem(last=False)
    return lines


def _ocr_uncached(
    engine: Any, screen_png: bytes, *, region: Region | None = None
) -> list[OcrLine]:
    """Run OCR on a screenshot (optionally restricted to a region).

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        region: Optional ``(x, y, w, h)`` sub-region to OCR (clamped to the
            frame). Returned line regions are offset back into full-screen
            coordinates.

    Returns:
        Recognized lines in engine order; empty list if nothing was found.
    """
    if hasattr(engine, "recognize_png"):
        return _native_ocr_uncached(engine, screen_png, region=region)
    img = _decode_bgr(screen_png)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region is not None:
        clamped = _clamp_region(region, w, h)
        if clamped is None:
            return []
        off_x, off_y, rw, rh = clamped
        img = img[off_y : off_y + rh, off_x : off_x + rw]

    result, _elapse = engine(img)
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


def _native_ocr_uncached(
    engine: Any, screen_png: bytes, *, region: Region | None = None
) -> list[OcrLine]:
    """Run a native engine on the full frame or on a cropped region."""
    off_x, off_y = 0, 0
    png = screen_png
    if region is not None:
        img = _decode_bgr(screen_png)
        h, w = img.shape[:2]
        clamped = _clamp_region(region, w, h)
        if clamped is None:
            return []
        off_x, off_y, rw, rh = clamped
        cropped = imgops.encode_png(img[off_y : off_y + rh, off_x : off_x + rw])
        if cropped is None:
            raise ValueError("could not encode OCR region")
        png = cropped
    return [
        OcrLine(
            text=text,
            region=(off_x + x, off_y + y, lw, lh),
            confidence=confidence,
        )
        for x, y, lw, lh, text, confidence in engine.recognize_png(png)
        if text
    ]


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
    img = imgops.decode_bgr(screen_png)
    if img is None:
        return screen_png
    encoded = imgops.encode_png(imgops.resize_cubic(img, factor))
    return encoded if encoded is not None else screen_png


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


def text_key(text: str) -> str:
    """Letters and digits only, lowercased: case, spaces and punctuation drop."""

    return "".join(ch for ch in text.lower() if ch.isalnum())


def _continues(upper: OcrLine, lower: OcrLine) -> bool:
    """Whether ``lower`` is the wrapped continuation of ``upper``."""

    ux, uy, _uw, uh = upper.region
    lx, ly, _lw, _lh = lower.region
    gap = ly - (uy + uh)
    return abs(lx - ux) <= max(6, uh // 2) and -2 <= gap <= max(4, (uh * 4) // 5)


def wrapped_readings(
    lines: Sequence[OcrLine], *, max_lines: int = 3, min_confidence: float = 0.0
) -> list[list[str]]:
    """For each OCR line, its text and its wrapped continuations, joined.

    ``result[i]`` holds the readings that start at the i-th confident line:
    the line alone, then joined with the line wrapped under it (same left
    edge, directly below), up to ``max_lines`` lines.
    """

    confident = [line for line in lines if line.confidence >= min_confidence]
    out: list[list[str]] = []
    for start in confident:
        chain = [start]
        readings = [start.text.strip()]
        while len(chain) < max_lines:
            following = [
                line
                for line in confident
                if line is not chain[-1] and _continues(chain[-1], line)
            ]
            if len(following) != 1:
                break
            chain.append(following[0])
            readings.append(" ".join(line.text.strip() for line in chain))
        out.append(readings)
    return out


def exact_text_count(
    screen_png: bytes,
    text: str,
    *,
    min_confidence: float = 0.8,
) -> int:
    """How many times ``text`` is read exactly, as a whole line or wrapped lines.

    Exact means equal after case, spaces and punctuation drop
    (:func:`text_key`), with no fuzzy ratio: "2 matches were found" is not
    "No matches were found.". Retried once at 2x when the raw frame reads
    none (rapidocr drops dense lines at common screen resolutions).
    """

    key = text_key(text)
    if not key:
        return 0
    for factor in (1, 2):
        png = screen_png if factor == 1 else upscale_png(screen_png, factor)
        count = sum(
            1
            for readings in wrapped_readings(ocr(png), min_confidence=min_confidence)
            if any(text_key(reading) == key for reading in readings)
        )
        if count:
            return count
    return 0


def find_text(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None = None,
    min_ratio: float = 0.8,
    min_ocr_confidence: float = 0.0,
    raise_on_ambiguity: bool = False,
) -> Match | None:
    """Locate a text label on screen via OCR plus fuzzy matching.

    Each OCR line is compared to ``text`` with
    ``difflib.SequenceMatcher.ratio()`` over normalized (lowercased,
    whitespace-collapsed) strings. Generic callers receive the best qualifying
    line, preserving the historical presence/readiness behavior. Targeting
    callers set ``raise_on_ambiguity`` so repeated labels become a typed refusal
    instead of silently selecting the first or highest-scoring control.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        text: Target text to find.
        region: Optional ``(x, y, w, h)`` sub-region to search within.
        min_ratio: Minimum similarity ratio in ``[0, 1]`` to accept.
        min_ocr_confidence: Minimum OCR-engine confidence to accept. The
            compatibility default keeps historical generic lookup behavior;
            compiler-qualified exact labels opt into a stricter floor.
        raise_on_ambiguity: Raise :class:`AmbiguousOcrMatchError` instead of
            selecting the best line when multiple lines qualify. Target
            resolution enables this so ambiguity cannot be mistaken for a miss
            and fall through to weaker evidence.

    Returns:
        A :class:`Match` centered on the best qualifying line's bounding box,
        or ``None`` if no line is similar enough.
    """
    exact_confidence_contract = (
        raise_on_ambiguity and min_ratio == 1.0 and min_ocr_confidence > 0.0
    )
    qualifying = _qualifying_text_lines(
        screen_png,
        text,
        region=region,
        min_ratio=min_ratio,
        min_ocr_confidence=(0.0 if exact_confidence_contract else min_ocr_confidence),
    )
    if len(qualifying) > 1:
        if raise_on_ambiguity:
            raise AmbiguousOcrMatchError(
                f"{len(qualifying)} OCR lines qualify for target text"
            )
    if exact_confidence_contract:
        qualifying = [
            item for item in qualifying if item[1].confidence >= min_ocr_confidence
        ]
    if not qualifying:
        return None
    ratio, line = max(qualifying, key=lambda item: item[0])
    x, y, w, h = line.region
    return Match(point=(x + w // 2, y + h // 2), region=line.region, confidence=ratio)


def find_text_candidates(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None = None,
    min_ratio: float = 0.8,
    min_ocr_confidence: float = 0.0,
) -> list[Match]:
    """Return every OCR line that qualifies for ``text``.

    Target resolution needs the complete candidate set so it can distinguish
    repeated labels with independent retained evidence (for example the
    recorded target region or landmark relations).  This API deliberately
    makes no selection: candidate order is not evidence, and callers must
    prove uniqueness before acting.

    Generic callers should continue to use :func:`find_text`, whose default
    best-match behavior remains backward compatible.
    """
    matches: list[Match] = []
    for ratio, line in _qualifying_text_lines(
        screen_png,
        text,
        region=region,
        min_ratio=min_ratio,
        min_ocr_confidence=min_ocr_confidence,
    ):
        x, y, w, h = line.region
        matches.append(
            Match(
                point=(x + w // 2, y + h // 2),
                region=line.region,
                confidence=ratio,
            )
        )
    return matches


def _qualifying_text_lines(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None,
    min_ratio: float,
    min_ocr_confidence: float,
) -> list[tuple[float, OcrLine]]:
    """Return qualifying ``(similarity, line)`` pairs without selecting one."""
    target = normalize_text(text)
    if not target:
        return []
    qualifying: list[tuple[float, OcrLine]] = []
    for line in ocr(screen_png, region=region):
        if line.confidence < min_ocr_confidence:
            continue
        ratio = difflib.SequenceMatcher(None, normalize_text(line.text), target).ratio()
        if ratio >= min_ratio:
            qualifying.append((ratio, line))
    return qualifying
