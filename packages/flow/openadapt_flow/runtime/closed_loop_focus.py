"""Closed-loop focus-before-write verification and self-correction.

On pixel-only surfaces (Citrix, RDP, legacy desktop apps) and dynamic web
forms, clicking a candidate point and typing blindly causes silent wrong-field
writes whenever a row shifts, a click is dropped, or a modal popup covers the
target input.

This module enforces a non-destructive focus check before typing any text:

1. Capture ``before_png``, click a candidate input point (focus only, never
   type yet), and capture ``after_png``.
2. Isolate the newly highlighted focus ring or caret box with
   :func:`diff_focus_region`.
3. Verify with :func:`verify_focus_target` that a focus highlight appeared,
   is not an unexpected popup overlay, and sits immediately next to (left of
   or directly above) an OCR label matching ``expected_label``.
4. Only when focus verifies, type the value and verify it with tight-crop OCR
   read-back (retrying once if characters were dropped in transit).
5. If focus lands on a different field, no focus change occurs, or a popup
   swallows the click, type zero characters into the wrong field, retry or
   try the next candidate (up to ``max_attempts``), and halt cleanly if none
   verify.

Commit and destructive buttons (``is_commit=True``) are never probed by
clicking; passing ``is_commit=True`` raises :class:`CommitProbingRefusedError`.
"""

from __future__ import annotations

import difflib
import io
import math
import re
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from openadapt_flow.ir import Point, Region
from openadapt_flow.pixel_prepare_card import OcrWord

_BLOCK_SIZE = 8
_DEFAULT_MIN_LABEL_SIMILARITY = 0.80
_DEFAULT_MIN_WORD_SIMILARITY = 0.75
_DEFAULT_MIN_READBACK_SIMILARITY = 0.92
_MAX_FOCUS_VIEWPORT_FRACTION = 0.20
_MAX_FOCUS_HEIGHT_FACTOR = 5.5
_MAX_FOCUS_HEIGHT_FLOOR_PX = 120.0
_IMMEDIATE_PHRASE_GAP_NORM = 2.2
_IMMEDIATE_MAX_WORDS = 6

OcrFn = Callable[[bytes], Sequence[Any]]
RegroundFn = Callable[[bytes, str, Sequence[Point]], Point | None]


class CommitProbingRefusedError(ValueError):
    """Raised when focus-probing is requested on a commit or destructive control."""


@dataclass(frozen=True)
class _WordToken:
    text: str
    norm_text: str
    cx: float
    cy: float
    w: float
    h: float
    confidence: float = 1.0


@dataclass(frozen=True)
class FocusVerdict:
    """Result of checking whether a post-click focus change hit ``expected_label``."""

    verified: bool
    reason: str
    expected_label: str
    observed_label: str | None = None
    focus_box: Region | None = None
    similarity: float = 0.0
    label_position: str = "none"


@dataclass(frozen=True)
class WriteAttempt:
    """Record of one non-destructive focus probe attempt."""

    attempt_index: int
    point: Point
    verdict: FocusVerdict
    elapsed_ms: float


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a closed-loop field write with focus check and read-back."""

    succeeded: bool
    reason: str
    expected_label: str
    value: str
    selected_point: Point | None = None
    focus_box: Region | None = None
    observed_label: str | None = None
    readback_text: str | None = None
    readback_verified: bool = False
    attempts: tuple[WriteAttempt, ...] = ()
    typing_attempts: int = 0
    elapsed_ms: float = 0.0


def _load_pil_and_numpy() -> tuple[Any, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow and numpy are required for focus pixel diffing. "
            "Install the 'vision' extra (openadapt-flow[vision])."
        ) from exc
    return Image, np


def _default_ocr_fn(
    png_bytes: bytes,
    *,
    region: Region | None = None,
) -> list[OcrWord]:
    try:
        from openadapt_flow.vision.ocr import ocr as local_ocr
    except ImportError as exc:
        raise RuntimeError(
            "Local OCR backend is unavailable. Install openadapt-flow[vision] "
            "or pass a custom ocr_fn."
        ) from exc

    lines = local_ocr(png_bytes, region=region)
    words: list[OcrWord] = []
    for line in lines:
        rx, ry, rw, rh = line.region
        words.append(
            OcrWord(
                text=line.text,
                x=int(rx),
                y=int(ry),
                width=int(rw),
                height=int(rh),
                confidence=float(line.confidence),
            )
        )
    return words


def _normalize_label_phrase(text: str) -> str:
    cleaned = re.sub(r"^[^0-9a-zA-Z]+|[^0-9a-zA-Z]+$", "", text.strip())
    cleaned = re.sub(r"[:*]+", " ", cleaned)
    return " ".join(cleaned.lower().split())


def _normalize_word(text: str) -> str:
    cleaned = re.sub(r"^[^0-9a-zA-Z]+|[^0-9a-zA-Z]+$", "", text.strip())
    return cleaned.lower()


def _digits_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _alnum_squash(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _coerce_ocr_words(raw_items: Sequence[Any]) -> list[OcrWord]:
    words: list[OcrWord] = []
    for item in raw_items:
        if isinstance(item, OcrWord):
            if item.text.strip():
                words.append(item)
            continue
        if isinstance(item, Mapping):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            words.append(
                OcrWord(
                    text=text,
                    x=int(item.get("x", 0)),
                    y=int(item.get("y", 0)),
                    width=int(item.get("width", 0)),
                    height=int(item.get("height", 0)),
                    confidence=float(item.get("confidence", 1.0)),
                )
            )
            continue
        if hasattr(item, "text") and hasattr(item, "region"):
            text = str(getattr(item, "text") or "").strip()
            region = getattr(item, "region")
            if not text or region is None:
                continue
            rx, ry, rw, rh = region
            words.append(
                OcrWord(
                    text=text,
                    x=int(rx),
                    y=int(ry),
                    width=int(rw),
                    height=int(rh),
                    confidence=float(getattr(item, "confidence", 1.0) or 1.0),
                )
            )
            continue
        raise TypeError(f"Unsupported OCR item type: {type(item).__name__}")
    return words


def _split_to_word_tokens(words: Sequence[OcrWord]) -> list[_WordToken]:
    tokens: list[_WordToken] = []
    for item in words:
        raw_text = item.text
        if not raw_text.strip() or item.width <= 0 or item.height <= 0:
            continue
        x = float(item.x)
        y = float(item.y)
        w = float(item.width)
        h = float(item.height)
        line_len = len(raw_text)
        for match in re.finditer(r"\S+", raw_text):
            word = match.group(0)
            parts = re.split(r"(?<=[a-z])(?=[A-Z])", word)
            cursor = match.start()
            for part in parts:
                end = cursor + len(part)
                norm = _normalize_word(part)
                if norm:
                    start_frac = cursor / line_len
                    end_frac = end / line_len
                    wx0 = x + w * start_frac
                    wx1 = x + w * end_frac
                    ww = max(1.0, wx1 - wx0)
                    tokens.append(
                        _WordToken(
                            text=part,
                            norm_text=norm,
                            cx=(wx0 + wx1) / 2.0,
                            cy=y + h / 2.0,
                            w=ww,
                            h=h,
                            confidence=item.confidence,
                        )
                    )
                cursor = end
    return tokens


def _median_token_height(tokens: Sequence[_WordToken]) -> float:
    heights = [t.h for t in tokens if t.h > 0]
    if not heights:
        return 16.0
    return float(statistics.median(heights))


def _point_in_region(px: float, py: float, region: Region, margin: float = 1.0) -> bool:
    x, y, w, h = region
    return (x - margin) <= px <= (x + w + margin) and (y - margin) <= py <= (
        y + h + margin
    )


def _point_to_box_distance(point: Point, box: tuple[int, int, int, int]) -> float:
    px, py = point
    x0, y0, x1, y1 = box
    dx = max(x0 - px, 0, px - x1)
    dy = max(y0 - py, 0, py - y1)
    return math.hypot(float(dx), float(dy))


def diff_focus_region(
    before_png: bytes,
    after_png: bytes,
    *,
    min_area_px: int = 80,
    pixel_threshold: int = 18,
    pad_px: int = 2,
    click_point: Point | None = None,
) -> Region | None:
    """Isolate the newly highlighted focus ring or caret box between two frames.

    Compares ``before_png`` and ``after_png`` using Pillow and numpy, clusters
    changed pixels into 8-connected components on an 8x8 block grid, filters
    out tiny noise below ``min_area_px``, and selects the newly focused target
    component.

    When focus moves from an old field to a new field, both fields change
    pixels. Passing ``click_point`` selects the changed component containing or
    nearest to the click; without ``click_point``, components whose color
    saturation increased in ``after_png`` (a newly drawn focus ring) take
    priority over components whose saturation decreased (a blurred old ring).

    Returns ``(x, y, w, h)`` in pixel coordinates, or ``None`` when dimensions
    differ, frames cannot be decoded, nothing changed, or every changed
    component has bounding-box area below ``min_area_px``.
    """
    if min_area_px < 1:
        raise ValueError("min_area_px must be at least 1.")
    if pixel_threshold < 1:
        raise ValueError("pixel_threshold must be at least 1.")
    if pad_px < 0:
        raise ValueError("pad_px must be non-negative.")
    if not before_png or not after_png:
        return None

    Image, np = _load_pil_and_numpy()

    try:
        with Image.open(io.BytesIO(before_png)) as before_img:
            before_arr = np.asarray(before_img.convert("RGB"), dtype=np.int16)
        with Image.open(io.BytesIO(after_png)) as after_img:
            after_arr = np.asarray(after_img.convert("RGB"), dtype=np.int16)
    except Exception:  # noqa: BLE001 - safe abstention on undecodable frames
        return None

    if before_arr.shape != after_arr.shape:
        return None

    img_h, img_w = after_arr.shape[:2]
    diff = np.abs(after_arr - before_arr).max(axis=2)
    mask = diff >= pixel_threshold
    if not mask.any():
        return None

    before_chroma = before_arr.max(axis=2) - before_arr.min(axis=2)
    after_chroma = after_arr.max(axis=2) - after_arr.min(axis=2)
    chroma_delta = after_chroma - before_chroma

    grid_h = (img_h + _BLOCK_SIZE - 1) // _BLOCK_SIZE
    grid_w = (img_w + _BLOCK_SIZE - 1) // _BLOCK_SIZE
    pad_h = grid_h * _BLOCK_SIZE - img_h
    pad_w = grid_w * _BLOCK_SIZE - img_w
    if pad_h or pad_w:
        padded = np.pad(mask, ((0, pad_h), (0, pad_w)), constant_values=False)
    else:
        padded = mask
    block_counts = padded.reshape(grid_h, _BLOCK_SIZE, grid_w, _BLOCK_SIZE).sum(
        axis=(1, 3)
    )
    active = block_counts > 0

    visited = np.zeros((grid_h, grid_w), dtype=bool)
    components: list[tuple[tuple[int, int, int, int], int, float]] = []

    active_ys, active_xs = np.nonzero(active)
    for start_y_np, start_x_np in zip(active_ys, active_xs):
        sy, sx = int(start_y_np), int(start_x_np)
        if visited[sy, sx]:
            continue
        queue: deque[tuple[int, int]] = deque([(sy, sx)])
        visited[sy, sx] = True
        comp_blocks: list[tuple[int, int]] = []
        while queue:
            cy, cx = queue.popleft()
            comp_blocks.append((cy, cx))
            for dy in (-1, 0, 1):
                ny = cy + dy
                if ny < 0 or ny >= grid_h:
                    continue
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    nx = cx + dx
                    if 0 <= nx < grid_w and active[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))

        by_min = min(by for by, _ in comp_blocks)
        by_max = max(by for by, _ in comp_blocks)
        bx_min = min(bx for _, bx in comp_blocks)
        bx_max = max(bx for _, bx in comp_blocks)

        roi_y0 = by_min * _BLOCK_SIZE
        roi_y1 = min(img_h, (by_max + 1) * _BLOCK_SIZE)
        roi_x0 = bx_min * _BLOCK_SIZE
        roi_x1 = min(img_w, (bx_max + 1) * _BLOCK_SIZE)

        comp_mask = np.zeros((roi_y1 - roi_y0, roi_x1 - roi_x0), dtype=bool)
        for by, bx in comp_blocks:
            py0 = by * _BLOCK_SIZE - roi_y0
            py1 = min(img_h, (by + 1) * _BLOCK_SIZE) - roi_y0
            px0 = bx * _BLOCK_SIZE - roi_x0
            px1 = min(img_w, (bx + 1) * _BLOCK_SIZE) - roi_x0
            comp_mask[py0:py1, px0:px1] = mask[
                roi_y0 + py0 : roi_y0 + py1,
                roi_x0 + px0 : roi_x0 + px1,
            ]

        ys, xs = np.nonzero(comp_mask)
        if ys.size == 0:
            continue

        x0 = roi_x0 + int(xs.min())
        x1 = roi_x0 + int(xs.max()) + 1
        y0 = roi_y0 + int(ys.min())
        y1 = roi_y0 + int(ys.max()) + 1

        if (x1 - x0) * (y1 - y0) < min_area_px:
            continue

        comp_chroma_vals = chroma_delta[roi_y0:roi_y1, roi_x0:roi_x1][comp_mask]
        mean_chroma_gain = (
            float(comp_chroma_vals.mean()) if comp_chroma_vals.size else 0.0
        )
        components.append(((x0, y0, x1, y1), int(ys.size), mean_chroma_gain))

    if not components:
        return None

    if click_point is not None:
        near_click = [
            (box, count, chroma_gain, _point_to_box_distance(click_point, box))
            for box, count, chroma_gain in components
            if _point_to_box_distance(click_point, box) <= 16.0
        ]
        if near_click:
            best_box, _, _, _ = max(
                near_click,
                key=lambda item: (
                    item[3] == 0.0,
                    item[2] > 5.0,
                    item[1],
                    -item[3],
                ),
            )
        else:
            best_box, _, _ = max(
                components,
                key=lambda item: (item[2] > 5.0, item[2] >= -5.0, item[1]),
            )
    else:
        best_box, _, _ = max(
            components,
            key=lambda item: (item[2] > 5.0, item[2] >= -5.0, item[1]),
        )

    x0, y0, x1, y1 = best_box
    cx0 = max(0, x0 - pad_px)
    cy0 = max(0, y0 - pad_px)
    cx1 = min(img_w, x1 + pad_px)
    cy1 = min(img_h, y1 + pad_px)
    return (cx0, cy0, cx1 - cx0, cy1 - cy0)


def _count_inner_rows(
    tokens: Sequence[_WordToken],
    focus_box: Region,
    med_h: float,
) -> int:
    inner = [
        tok for tok in tokens if _point_in_region(tok.cx, tok.cy, focus_box, margin=0.0)
    ]
    if not inner:
        return 0
    row_centers: list[float] = []
    for tok in sorted(inner, key=lambda t: t.cy):
        placed = False
        for idx, rcy in enumerate(row_centers):
            if abs(tok.cy - rcy) <= 0.65 * med_h:
                row_centers[idx] = (rcy + tok.cy) / 2.0
                placed = True
                break
        if not placed:
            row_centers.append(tok.cy)
    return len(row_centers)


def _extract_left_label(
    outside_tokens: Sequence[_WordToken],
    focus_box: Region,
    med_h: float,
    *,
    max_dx_factor: float,
    max_dy_factor: float,
) -> tuple[str, float] | None:
    fx, fy, _fw, fh = focus_box
    x0 = float(fx)
    y0 = float(fy)
    y1 = float(fy + fh)
    focus_cy = y0 + float(fh) / 2.0

    candidates: list[tuple[float, float, int, _WordToken]] = []
    for idx, tok in enumerate(outside_tokens):
        if abs(tok.cy - focus_cy) > max_dy_factor * med_h:
            continue
        if tok.cy < y0 - 0.8 * med_h or tok.cy > y1 + 0.8 * med_h:
            continue
        tok_right = tok.cx + tok.w / 2.0
        if tok.cx >= x0 or tok_right > x0 + 0.5 * med_h:
            continue
        gap_left = max(0.0, x0 - tok_right)
        if gap_left <= max_dx_factor * med_h:
            candidates.append((gap_left, abs(tok.cy - focus_cy), idx, tok))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    anchor_gap, _, anchor_idx, anchor = candidates[0]
    selected: list[_WordToken] = [anchor]
    cur_left = anchor.cx - anchor.w / 2.0

    same_line_left = [
        tok
        for idx, tok in enumerate(outside_tokens)
        if idx != anchor_idx
        and abs(tok.cy - anchor.cy) <= 0.65 * med_h
        and (tok.cx + tok.w / 2.0) <= cur_left + 0.3 * med_h
    ]
    same_line_left.sort(key=lambda tok: -(tok.cx + tok.w / 2.0))
    for tok in same_line_left:
        if len(selected) >= _IMMEDIATE_MAX_WORDS:
            break
        tok_right = tok.cx + tok.w / 2.0
        if cur_left - tok_right <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
            selected.append(tok)
            cur_left = min(cur_left, tok.cx - tok.w / 2.0)
        else:
            break

    selected.sort(key=lambda tok: tok.cx)
    phrase = " ".join(tok.text for tok in selected).strip()
    return (phrase, anchor_gap / med_h) if phrase else None


def _extract_above_label(
    outside_tokens: Sequence[_WordToken],
    focus_box: Region,
    med_h: float,
    *,
    max_dy_factor: float,
) -> tuple[str, float] | None:
    fx, fy, fw, _fh = focus_box
    x0 = float(fx)
    y0 = float(fy)
    x1 = float(fx + fw)
    target_cx = (x0 + x1) / 2.0

    candidates: list[tuple[float, float, int, _WordToken]] = []
    for idx, tok in enumerate(outside_tokens):
        tok_left = tok.cx - tok.w / 2.0
        tok_right = tok.cx + tok.w / 2.0
        tok_bottom = tok.cy + tok.h / 2.0
        if tok.cy >= y0 or tok_bottom > y0 + 0.5 * med_h:
            continue
        gap_above = max(0.0, y0 - tok_bottom)
        if gap_above > max_dy_factor * med_h:
            continue
        # Require real horizontal overlap with the focused control so a label
        # belonging to a left-hand column on the row above never qualifies.
        if tok_right < x0 or tok_left > x1:
            continue
        candidates.append((gap_above, abs(tok.cx - target_cx), idx, tok))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    anchor_gap, _, anchor_idx, anchor = candidates[0]
    selected: list[_WordToken] = [anchor]
    same_line = [
        tok
        for idx, tok in enumerate(outside_tokens)
        if idx != anchor_idx
        and abs(tok.cy - anchor.cy) <= 0.65 * med_h
        and (tok.cx + tok.w / 2.0) >= x0 - 0.5 * med_h
        and (tok.cx - tok.w / 2.0) <= x1 + 0.5 * med_h
    ]

    cur_left = anchor.cx - anchor.w / 2.0
    for tok in sorted(
        (t for t in same_line if t.cx < anchor.cx),
        key=lambda t: -(t.cx + t.w / 2.0),
    ):
        if len(selected) >= _IMMEDIATE_MAX_WORDS:
            break
        if cur_left - (tok.cx + tok.w / 2.0) <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
            selected.append(tok)
            cur_left = min(cur_left, tok.cx - tok.w / 2.0)
        else:
            break

    cur_right = anchor.cx + anchor.w / 2.0
    for tok in sorted(
        (t for t in same_line if t.cx > anchor.cx),
        key=lambda t: t.cx - t.w / 2.0,
    ):
        if len(selected) >= _IMMEDIATE_MAX_WORDS:
            break
        if (tok.cx - tok.w / 2.0) - cur_right <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
            selected.append(tok)
            cur_right = max(cur_right, tok.cx + tok.w / 2.0)
        else:
            break

    selected.sort(key=lambda tok: tok.cx)
    phrase = " ".join(tok.text for tok in selected).strip()
    return (phrase, anchor_gap / med_h) if phrase else None


def _label_similarity(expected_label: str, observed_label: str) -> float:
    """Compute strict label similarity in [0, 1].

    Tolerates punctuation differences (trailing colons/asterisks), merged OCR
    spaces, and single-character OCR typos while rejecting substituted
    differentiating words (such as "Member ID" vs "Group ID" or "Mailing
    Street Address" vs "Billing Street Address") and mismatched digits.
    """
    exp_norm = _normalize_label_phrase(expected_label)
    obs_norm = _normalize_label_phrase(observed_label)
    if not exp_norm or not obs_norm:
        return 0.0
    if _digits_only(exp_norm) != _digits_only(obs_norm):
        return 0.0
    if exp_norm == obs_norm:
        return 1.0

    exp_squash = _alnum_squash(exp_norm)
    obs_squash = _alnum_squash(obs_norm)
    if exp_squash == obs_squash:
        return 0.98

    phrase_ratio = difflib.SequenceMatcher(None, exp_norm, obs_norm).ratio()
    squash_ratio = difflib.SequenceMatcher(None, exp_squash, obs_squash).ratio()

    exp_words = exp_norm.split()
    obs_words = obs_norm.split()
    if len(exp_words) == len(obs_words) and len(exp_words) >= 2:
        word_ratios = [
            difflib.SequenceMatcher(None, ew, ow).ratio()
            for ew, ow in zip(exp_words, obs_words)
        ]
        if min(word_ratios) < _DEFAULT_MIN_WORD_SIMILARITY:
            return min(phrase_ratio, min(word_ratios))

    if len(exp_words) >= 2 and len(obs_words) >= 2:
        for ew in exp_words:
            if len(ew) <= 2:
                continue
            best_word = max(
                difflib.SequenceMatcher(None, ew, ow).ratio() for ow in obs_words
            )
            if best_word < _DEFAULT_MIN_WORD_SIMILARITY and ew not in obs_squash:
                return min(phrase_ratio, best_word)

    return max(phrase_ratio, min(0.95, squash_ratio))


def _image_dimensions(png_bytes: bytes) -> tuple[int, int] | None:
    Image, _ = _load_pil_and_numpy()
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            return int(img.size[0]), int(img.size[1])
    except Exception:  # noqa: BLE001
        return None


def verify_focus_target(
    *,
    before_png: bytes,
    after_png: bytes,
    expected_label: str,
    ocr_fn: OcrFn | None = None,
    max_dx_factor: float = 8.0,
    max_dy_factor: float = 2.0,
    min_area_px: int = 80,
    min_label_similarity: float = _DEFAULT_MIN_LABEL_SIMILARITY,
    click_point: Point | None = None,
) -> FocusVerdict:
    """Verify that a click focused the field labeled ``expected_label``.

    Checks three gates in order:

    1. A focus ring or caret region appeared between ``before_png`` and
       ``after_png`` (:func:`diff_focus_region`).
    2. The changed region is consistent with a form input focus highlight
       rather than a newly opened popup modal or multi-row overlay.
    3. The immediate OCR label directly to the left of or above ``focus_box``
       matches ``expected_label`` strictly.
    """
    clean_expected = expected_label.strip()
    if not clean_expected:
        raise ValueError("expected_label must be a non-empty string.")

    focus_box = diff_focus_region(
        before_png,
        after_png,
        min_area_px=min_area_px,
        click_point=click_point,
    )
    if focus_box is None:
        return FocusVerdict(
            verified=False,
            reason="no_focus_change",
            expected_label=clean_expected,
            focus_box=None,
        )

    dims = _image_dimensions(after_png)
    if dims is None:
        return FocusVerdict(
            verified=False,
            reason="invalid_after_frame",
            expected_label=clean_expected,
            focus_box=focus_box,
        )
    img_w, img_h = dims
    fx, fy, fw, fh = focus_box

    if (fw * fh) > _MAX_FOCUS_VIEWPORT_FRACTION * float(img_w * img_h):
        return FocusVerdict(
            verified=False,
            reason="unexpected_popup_size",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    try:
        if ocr_fn is not None:
            raw_ocr = ocr_fn(after_png)
        else:
            est_h = max(20, min(fh, 40))
            roi_x0 = max(
                0, fx - max(320, int(math.ceil((max_dx_factor + 8.0) * est_h)))
            )
            roi_y0 = max(0, fy - max(96, int(math.ceil((max_dy_factor + 2.0) * est_h))))
            roi_x1 = min(img_w, fx + fw + 32)
            roi_y1 = min(img_h, fy + fh + max(40, est_h))
            local_roi: Region = (roi_x0, roi_y0, roi_x1 - roi_x0, roi_y1 - roi_y0)
            raw_ocr = _default_ocr_fn(after_png, region=local_roi)
    except Exception:  # noqa: BLE001 - refuse safely when OCR fails
        return FocusVerdict(
            verified=False,
            reason="ocr_error",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    words = _coerce_ocr_words(raw_ocr)
    tokens = _split_to_word_tokens(words)
    if not tokens:
        return FocusVerdict(
            verified=False,
            reason="no_ocr_tokens",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    med_h = _median_token_height(tokens)

    if float(fh) > max(_MAX_FOCUS_HEIGHT_FLOOR_PX, _MAX_FOCUS_HEIGHT_FACTOR * med_h):
        return FocusVerdict(
            verified=False,
            reason="unexpected_popup_size",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    inner_rows = _count_inner_rows(tokens, focus_box, med_h)
    if inner_rows >= 2 and float(fh) > 2.8 * med_h:
        return FocusVerdict(
            verified=False,
            reason="unexpected_popup_content",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    outside_tokens = [
        tok
        for tok in tokens
        if not _point_in_region(tok.cx, tok.cy, focus_box, margin=0.0)
    ]

    left_match = _extract_left_label(
        outside_tokens,
        focus_box,
        med_h,
        max_dx_factor=max_dx_factor,
        max_dy_factor=max_dy_factor,
    )
    above_match = _extract_above_label(
        outside_tokens,
        focus_box,
        med_h,
        max_dy_factor=max_dy_factor,
    )

    label_candidates: list[tuple[str, str, float, float]] = []
    if left_match is not None:
        phrase, gap_norm = left_match
        sim = _label_similarity(clean_expected, phrase)
        label_candidates.append(("left", phrase, sim, gap_norm))
    if above_match is not None:
        phrase, gap_norm = above_match
        sim = _label_similarity(clean_expected, phrase)
        label_candidates.append(("above", phrase, sim, gap_norm))

    if not label_candidates:
        return FocusVerdict(
            verified=False,
            reason="no_immediate_label",
            expected_label=clean_expected,
            focus_box=focus_box,
        )

    passing = [item for item in label_candidates if item[2] >= min_label_similarity]
    if passing:
        best_pos, best_phrase, best_sim, _ = max(
            passing, key=lambda item: (item[2], -item[3])
        )
        return FocusVerdict(
            verified=True,
            reason="verified",
            expected_label=clean_expected,
            observed_label=best_phrase,
            focus_box=focus_box,
            similarity=round(best_sim, 4),
            label_position=best_pos,
        )

    # Report the closest observed label when refusing.
    closest_pos, closest_phrase, closest_sim, _ = min(
        label_candidates, key=lambda item: item[3]
    )
    return FocusVerdict(
        verified=False,
        reason="label_mismatch",
        expected_label=clean_expected,
        observed_label=closest_phrase,
        focus_box=focus_box,
        similarity=round(closest_sim, 4),
        label_position=closest_pos,
    )


def _crop_focus_for_readback(
    screen_png: bytes,
    focus_box: Region,
    *,
    pad_px: int = 4,
    upscale_below_height: int = 48,
) -> bytes:
    Image, _ = _load_pil_and_numpy()
    x, y, w, h = focus_box
    with Image.open(io.BytesIO(screen_png)) as img:
        img_w, img_h = img.size
        x0 = max(0, x - pad_px)
        y0 = max(0, y - pad_px)
        x1 = min(img_w, x + w + pad_px)
        y1 = min(img_h, y + h + pad_px)
        crop = img.crop((x0, y0, x1, y1))
        if crop.size[1] < upscale_below_height:
            resample = getattr(Image, "Resampling", Image).BICUBIC
            crop = crop.resize((crop.size[0] * 2, crop.size[1] * 2), resample=resample)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()


def _readback_matches(expected_value: str, observed_text: str) -> bool:
    exp_clean = " ".join(expected_value.strip().split())
    obs_clean = " ".join(observed_text.strip().split())
    if not exp_clean:
        return True
    if not obs_clean:
        return False
    # Strip a single trailing cursor bar character if OCR picks up the caret.
    obs_stripped = (
        obs_clean.rstrip("|Il").strip() if obs_clean.endswith("|") else obs_clean
    )
    if (
        exp_clean.lower() == obs_clean.lower()
        or exp_clean.lower() == obs_stripped.lower()
    ):
        return True

    # Digits must match exactly so dropped or transposed digits always trigger retry.
    exp_digits = _digits_only(exp_clean)
    if exp_digits:
        obs_digits = _digits_only(obs_stripped)
        if exp_digits != obs_digits:
            return False

    exp_squash = _alnum_squash(exp_clean)
    obs_squash = _alnum_squash(obs_stripped)
    if exp_squash == obs_squash:
        return True
    ratio = difflib.SequenceMatcher(None, exp_squash, obs_squash).ratio()
    return ratio >= _DEFAULT_MIN_READBACK_SIMILARITY


def _verify_typed_readback(
    after_type_png: bytes,
    focus_box: Region,
    expected_value: str,
    *,
    ocr_fn: OcrFn | None = None,
) -> tuple[bool, str]:
    crop_png = _crop_focus_for_readback(after_type_png, focus_box)
    runner = ocr_fn if ocr_fn is not None else _default_ocr_fn
    try:
        raw_ocr = runner(crop_png)
    except Exception:  # noqa: BLE001
        return False, ""
    words = _coerce_ocr_words(raw_ocr)
    ordered = sorted(words, key=lambda w: (w.y, w.x))
    observed = " ".join(w.text.strip() for w in ordered if w.text.strip()).strip()
    return _readback_matches(expected_value, observed), observed


class ClosedLoopFieldWriter:
    """Write text into a GUI input field only after verifying post-click focus.

    Args:
        screenshot_fn: Zero-argument callable returning the current frame as PNG bytes.
        click_fn: Callable ``(x, y) -> None`` performing a single non-destructive click.
        type_fn: Callable ``(text) -> None`` typing into the currently focused field.
        clear_fn: Optional zero-argument callable clearing the focused field before
            retrying dropped characters.
        ocr_fn: Optional OCR callable ``(png_bytes) -> Sequence[OcrWord]``. Defaults
            to OpenAdapt's local OCR backend.
        max_attempts: Maximum number of focus-probe clicks before halting (default 3).
        max_type_retries: Maximum number of typing retries when tight-crop OCR
            read-back detects dropped characters (default 1).
        min_area_px: Minimum focus diff bounding-box area in pixels.
        max_dx_factor: Maximum horizontal label distance in median token heights.
        max_dy_factor: Maximum vertical label offset in median token heights.
        min_label_similarity: Minimum similarity score to accept an adjacent label.
    """

    def __init__(
        self,
        *,
        screenshot_fn: Callable[[], bytes],
        click_fn: Callable[[int, int], None],
        type_fn: Callable[[str], None],
        clear_fn: Callable[[], None] | None = None,
        ocr_fn: OcrFn | None = None,
        max_attempts: int = 3,
        max_type_retries: int = 1,
        min_area_px: int = 80,
        max_dx_factor: float = 8.0,
        max_dy_factor: float = 2.0,
        min_label_similarity: float = _DEFAULT_MIN_LABEL_SIMILARITY,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if max_type_retries < 0:
            raise ValueError("max_type_retries must be non-negative.")
        self._screenshot_fn = screenshot_fn
        self._click_fn = click_fn
        self._type_fn = type_fn
        self._clear_fn = clear_fn
        self._ocr_fn = ocr_fn
        self._max_attempts = max_attempts
        self._max_type_retries = max_type_retries
        self._min_area_px = min_area_px
        self._max_dx_factor = max_dx_factor
        self._max_dy_factor = max_dy_factor
        self._min_label_similarity = min_label_similarity

    def write_field(
        self,
        *,
        expected_label: str,
        value: str,
        candidates: Sequence[Point],
        is_commit: bool = False,
        reground_fn: RegroundFn | None = None,
    ) -> WriteResult:
        """Probe candidates until focus verifies, then type and read back.

        Never types a single character before :func:`verify_focus_target`
        confirms the focused input matches ``expected_label``. Raises
        :class:`CommitProbingRefusedError` when ``is_commit=True``.
        """
        if is_commit:
            raise CommitProbingRefusedError(
                f"Refused focus probing on commit or destructive control {expected_label!r}."
            )
        clean_expected = expected_label.strip()
        if not clean_expected:
            raise ValueError("expected_label must be a non-empty string.")

        t0 = time.perf_counter()
        queue: deque[Point] = deque((int(p[0]), int(p[1])) for p in candidates)
        attempts: list[WriteAttempt] = []
        tried_wrong_points: list[Point] = []
        retried_no_focus: set[Point] = set()

        selected_point: Point | None = None
        verified_verdict: FocusVerdict | None = None
        last_frame: bytes | None = None

        while len(attempts) < self._max_attempts:
            if not queue and reground_fn is not None:
                frame_for_reground = (
                    last_frame if last_frame is not None else self._screenshot_fn()
                )
                proposed = reground_fn(
                    frame_for_reground, clean_expected, tuple(tried_wrong_points)
                )
                if proposed is not None:
                    queue.append((int(proposed[0]), int(proposed[1])))
            if not queue:
                break

            point = queue.popleft()
            attempt_start = time.perf_counter()
            before_png = self._screenshot_fn()
            self._click_fn(point[0], point[1])
            after_png = self._screenshot_fn()
            last_frame = after_png

            verdict = verify_focus_target(
                before_png=before_png,
                after_png=after_png,
                expected_label=clean_expected,
                ocr_fn=self._ocr_fn,
                max_dx_factor=self._max_dx_factor,
                max_dy_factor=self._max_dy_factor,
                min_area_px=self._min_area_px,
                min_label_similarity=self._min_label_similarity,
                click_point=point,
            )
            attempt_ms = (time.perf_counter() - attempt_start) * 1000.0
            attempts.append(
                WriteAttempt(
                    attempt_index=len(attempts),
                    point=point,
                    verdict=verdict,
                    elapsed_ms=round(attempt_ms, 2),
                )
            )

            if verdict.verified and verdict.focus_box is not None:
                selected_point = point
                verified_verdict = verdict
                break

            if verdict.reason == "no_focus_change" and point not in retried_no_focus:
                # Retry once on the same coordinate to recover from dropped clicks.
                retried_no_focus.add(point)
                queue.appendleft(point)
            else:
                tried_wrong_points.append(point)

        if (
            selected_point is None
            or verified_verdict is None
            or verified_verdict.focus_box is None
        ):
            halt_reason = attempts[-1].verdict.reason if attempts else "no_candidates"
            return WriteResult(
                succeeded=False,
                reason=f"focus_unverified:{halt_reason}",
                expected_label=clean_expected,
                value=value,
                observed_label=attempts[-1].verdict.observed_label
                if attempts
                else None,
                focus_box=attempts[-1].verdict.focus_box if attempts else None,
                attempts=tuple(attempts),
                typing_attempts=0,
                elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        focus_box = verified_verdict.focus_box
        readback_ok = False
        readback_text = ""
        typing_attempts = 0
        total_type_tries = 1 + self._max_type_retries

        for try_idx in range(total_type_tries):
            if try_idx > 0 and self._clear_fn is not None:
                self._clear_fn()
            typing_attempts += 1
            self._type_fn(value)
            after_type_png = self._screenshot_fn()
            readback_ok, readback_text = _verify_typed_readback(
                after_type_png,
                focus_box,
                value,
                ocr_fn=self._ocr_fn,
            )
            if readback_ok:
                break

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return WriteResult(
            succeeded=readback_ok,
            reason="verified_write" if readback_ok else "readback_mismatch",
            expected_label=clean_expected,
            value=value,
            selected_point=selected_point,
            focus_box=focus_box,
            observed_label=verified_verdict.observed_label,
            readback_text=readback_text,
            readback_verified=readback_ok,
            attempts=tuple(attempts),
            typing_attempts=typing_attempts,
            elapsed_ms=elapsed_ms,
        )


__all__ = [
    "ClosedLoopFieldWriter",
    "CommitProbingRefusedError",
    "FocusVerdict",
    "OcrWord",
    "WriteAttempt",
    "WriteResult",
    "diff_focus_region",
    "verify_focus_target",
]
