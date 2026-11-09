"""Image operations on the replay path, with or without OpenCV.

OpenCV ships about 80 MB of native libraries, and on a fresh macOS install the
first ``import cv2`` can take 10 to 20 seconds while the system verifies them.
With the native OCR backend (see :mod:`openadapt_flow.vision.ocr_backends`),
nothing else on the record, compile, and replay path needs OpenCV, so these
functions use numpy and Pillow instead.

With RapidOCR, which loads OpenCV anyway, every function calls OpenCV exactly
as before, so that configuration is unchanged. The numpy versions are checked
against OpenCV in ``tests/test_imgops_equivalence.py``.
"""

from __future__ import annotations

import io
import os
from typing import Any

import numpy as np
from PIL import Image

#: ``cv2`` forces OpenCV, ``numpy`` forces the numpy versions, and ``auto``
#: (default) follows the OCR backend.
IMAGE_OPS_ENV = "OPENADAPT_FLOW_IMAGE_OPS"


class _LazyCv2:
    """Import OpenCV on first attribute access."""

    def __getattr__(self, name: str) -> Any:
        import cv2

        return getattr(cv2, name)


#: Drop-in for ``import cv2`` in modules whose import must not load OpenCV.
cv2: Any = _LazyCv2()


def use_opencv() -> bool:
    """Return whether these operations should call OpenCV."""
    choice = (os.environ.get(IMAGE_OPS_ENV) or "auto").strip().lower()
    if choice == "cv2":
        return True
    if choice == "numpy":
        return False
    from openadapt_flow.vision.ocr_backends import active_backend_name

    return active_backend_name() == "rapidocr"


# --------------------------------------------------------------------------
# Decode and encode
# --------------------------------------------------------------------------


def _pil_open(png: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(png))
        image.load()
    except Exception:
        return None
    return image


def _rgb_array(image: Image.Image) -> np.ndarray:
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        # OpenCV keeps the high byte of 16-bit samples.
        wide = np.asarray(image, dtype=np.uint32)
        gray8 = (wide >> 8).astype(np.uint8)
        return np.repeat(gray8[:, :, np.newaxis], 3, axis=2)
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def decode_bgr(png: bytes) -> np.ndarray | None:
    """Decode image bytes to a BGR ``uint8`` array, or None."""
    if use_opencv():
        return cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    image = _pil_open(png)
    if image is None:
        return None
    return np.ascontiguousarray(_rgb_array(image)[:, :, ::-1])


def bgr_to_gray(img: np.ndarray) -> np.ndarray:
    """``cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)``: OpenCV's 15-bit weights, rounded."""
    if use_opencv():
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.ndim == 2:
        return img
    b = img[:, :, 0].astype(np.uint32)
    g = img[:, :, 1].astype(np.uint32)
    r = img[:, :, 2].astype(np.uint32)
    return ((b * 3735 + g * 19235 + r * 9798 + (1 << 14)) >> 15).astype(np.uint8)


def decode_gray(png: bytes) -> np.ndarray | None:
    """Decode image bytes to a grayscale ``uint8`` array, or None."""
    if use_opencv():
        return cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    image = _pil_open(png)
    if image is None:
        return None
    if image.mode == "L":
        return np.asarray(image, dtype=np.uint8)
    # OpenCV's PNG reader asks libpng for gray, which truncates 0.299, 0.587,
    # and the remainder to 15-bit weights and truncates the sum.
    rgb = _rgb_array(image).astype(np.uint32)
    gray = (rgb[:, :, 0] * 9797 + rgb[:, :, 1] * 19234 + rgb[:, :, 2] * 3737) >> 15
    return gray.astype(np.uint8)


def encode_png(img: np.ndarray) -> bytes | None:
    """Encode a BGR or grayscale ``uint8`` array as PNG bytes, or None."""
    if use_opencv():
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes() if ok else None
    arr = np.ascontiguousarray(img)
    if arr.ndim == 3:
        arr = np.ascontiguousarray(arr[:, :, ::-1])
    out = io.BytesIO()
    try:
        Image.fromarray(arr).save(out, format="PNG")
    except Exception:
        return None
    return out.getvalue()


def png_size(png: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` of decodable image bytes, or None."""
    if use_opencv():
        frame = decode_bgr(png)
        return None if frame is None else (int(frame.shape[1]), int(frame.shape[0]))
    image = _pil_open(png)
    return None if image is None else (int(image.width), int(image.height))


# --------------------------------------------------------------------------
# Pixel arithmetic
# --------------------------------------------------------------------------


def absdiff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``cv2.absdiff`` for ``uint8`` arrays."""
    if use_opencv():
        return cv2.absdiff(a, b)
    return np.abs(a.astype(np.int16) - b.astype(np.int16)).astype(np.uint8)


def threshold_binary(gray: np.ndarray, thresh: float, maxval: int = 255) -> np.ndarray:
    """``cv2.threshold(gray, thresh, maxval, THRESH_BINARY)[1]`` for ``uint8``."""
    if use_opencv():
        return cv2.threshold(gray, thresh, maxval, cv2.THRESH_BINARY)[1]
    # OpenCV floors a fractional threshold for 8-bit input.
    limit = int(np.floor(thresh))
    return np.where(gray > limit, np.uint8(maxval), np.uint8(0))


def _max_filter_1d(arr: np.ndarray, size: int, axis: int) -> np.ndarray:
    radius = size // 2
    pad = [(0, 0)] * arr.ndim
    pad[axis] = (radius, size - 1 - radius)
    padded = np.pad(arr, pad, mode="constant", constant_values=0)
    out = np.zeros_like(arr)
    length = arr.shape[axis]
    for offset in range(size):
        window = np.take(padded, range(offset, offset + length), axis=axis)
        np.maximum(out, window, out=out)
    return out


def dilate_rect(mask: np.ndarray, size: int, iterations: int = 1) -> np.ndarray:
    """``cv2.dilate`` with an all-ones ``size`` x ``size`` kernel, non-negative input."""
    if use_opencv():
        kernel = np.ones((size, size), dtype=np.uint8)
        return cv2.dilate(mask, kernel, iterations=iterations)
    out = mask
    for _ in range(iterations):
        out = _max_filter_1d(_max_filter_1d(out, size, axis=1), size, axis=0)
    return out


# --------------------------------------------------------------------------
# Connected regions (largest external contour)
# --------------------------------------------------------------------------


def _row_runs(row: np.ndarray) -> list[tuple[int, int]]:
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


def _label_runs(
    mask: np.ndarray, *, eight: bool
) -> tuple[list[tuple[int, int, int, int]], list[int]]:
    """Label connected runs. Returns runs ``(y, x0, x1, label)`` and parents."""
    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs: list[tuple[int, int, int, int]] = []
    previous: list[tuple[int, int, int]] = []
    reach = 1 if eight else 0
    for y in range(mask.shape[0]):
        current: list[tuple[int, int, int]] = []
        prev_index = 0
        for x0, x1 in _row_runs(mask[y]):
            label = -1
            while prev_index < len(previous) and previous[prev_index][1] + reach <= x0:
                prev_index += 1
            j = prev_index
            while j < len(previous) and previous[j][0] < x1 + reach:
                other = find(previous[j][2])
                if label < 0:
                    label = other
                elif other != label:
                    parent[other] = label
                j += 1
            if label < 0:
                label = len(parent)
                parent.append(label)
            current.append((x0, x1, label))
            runs.append((y, x0, x1, label))
        previous = current
    return runs, [find(i) for i in range(len(parent))]


def largest_external_region(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding rect of the largest external contour of a binary mask.

    Matches ``cv2.findContours(mask, RETR_EXTERNAL, ...)`` followed by
    ``max(contours, key=cv2.contourArea)`` and ``cv2.boundingRect``. A region
    that sits inside a hole of another region is not external. The contour
    area of a region is the area of the polygon through its boundary pixel
    centers (Pick's theorem on the filled region).
    """
    if use_opencv():
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        x, y, w, h = cv2.boundingRect(max(contours, key=cv2.contourArea))
        return (int(x), int(y), int(w), int(h))

    fg = mask > 0
    if not fg.any():
        return None
    height, width = fg.shape
    runs, roots = _label_runs(fg, eight=True)
    labels = np.full(fg.shape, -1, dtype=np.int64)
    for y, x0, x1, label in runs:
        labels[y, x0:x1] = roots[label]

    # Background is 4-connected; the part reachable from outside the image is
    # the outer background. A region is external when it touches it.
    bg = np.pad(~fg, 1, constant_values=True)
    bg_runs, bg_roots = _label_runs(bg, eight=False)
    outer_label = bg_roots[bg_runs[0][3]]
    outer = np.zeros(bg.shape, dtype=bool)
    for y, x0, x1, label in bg_runs:
        if bg_roots[label] == outer_label:
            outer[y, x0:x1] = True
    touches = np.zeros(fg.shape, dtype=bool)
    touches |= outer[:-2, 1:-1] | outer[2:, 1:-1] | outer[1:-1, :-2] | outer[1:-1, 2:]
    touches &= fg
    external = set(np.unique(labels[touches]).tolist())

    best: tuple[float, int, tuple[int, int, int, int]] | None = None
    for label in sorted(external):
        region = labels == label
        ys, xs = np.nonzero(region)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        filled = _filled(region[y0 : y1 + 1, x0 : x1 + 1])
        pad = np.pad(filled, 1, constant_values=False)
        boundary = filled & ~(
            pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
        )
        total = int(filled.sum())
        b = int(boundary.sum())
        area = max(0.0, (total - b) + b / 2.0 - 1.0)
        # OpenCV lists contours in reverse raster order of their first pixel,
        # and max() keeps the first of equal areas: the later start wins.
        start = y0 * width + int(xs[ys == y0].min())
        key = (area, start)
        if best is None or key > (best[0], best[1]):
            best = (area, start, (x0, y0, x1 - x0 + 1, y1 - y0 + 1))
    return None if best is None else best[2]


def _filled(region: np.ndarray) -> np.ndarray:
    """``region`` plus its holes: what the outside can't reach 4-connected."""
    free = np.pad(~region, 1, constant_values=True)
    runs, roots = _label_runs(free, eight=False)
    outside = roots[runs[0][3]]
    reach = np.zeros(free.shape, dtype=bool)
    for y, x0, x1, label in runs:
        if roots[label] == outside:
            reach[y, x0:x1] = True
    return ~reach[1:-1, 1:-1]


# --------------------------------------------------------------------------
# Resize and template matching
# --------------------------------------------------------------------------


def _area_weights(src: int, dst: int) -> np.ndarray:
    """``dst x src`` weights of OpenCV's INTER_AREA for a downscale."""
    scale = src / dst
    weights = np.zeros((dst, src), dtype=np.float64)
    for d in range(dst):
        f0, f1 = d * scale, (d + 1) * scale
        s0, s1 = int(np.floor(f0)), int(np.ceil(f1))
        for s in range(s0, min(s1, src)):
            overlap = min(f1, s + 1) - max(f0, s)
            if overlap > 1e-12:
                weights[d, s] = overlap / scale
    return weights


def _area_upscale_weights(src: int, dst: int) -> np.ndarray:
    """``dst x src`` weights of OpenCV's INTER_AREA for an upscale (linear form)."""
    scale = src / dst
    inv = dst / src
    weights = np.zeros((dst, src), dtype=np.float64)
    for d in range(dst):
        s = int(np.floor(d * scale))
        f = (d + 1) - (s + 1) * inv
        f = 0.0 if f <= 0 else f - np.floor(f)
        if s < 0:
            s, f = 0, 0.0
        if s >= src - 1:
            s, f = src - 1, 0.0
        weights[d, s] += 1.0 - f
        if f:
            weights[d, s + 1] += f
    return weights


def resize_area(img: np.ndarray, width: int, height: int) -> np.ndarray:
    """``cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)``, 2-D ``uint8``.

    The numpy version agrees with OpenCV to within one gray level; OpenCV's
    fixed-point rounding is not reproduced bit for bit.
    """
    if use_opencv():
        return cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
    src_h, src_w = img.shape[:2]
    wy = (
        _area_weights(src_h, height)
        if height <= src_h
        else _area_upscale_weights(src_h, height)
    )
    wx = (
        _area_weights(src_w, width)
        if width <= src_w
        else _area_upscale_weights(src_w, width)
    )
    out = wy @ img.astype(np.float64) @ wx.T
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def match_template_ccoeff_normed(image: np.ndarray, templ: np.ndarray) -> np.ndarray:
    """``cv2.matchTemplate(image, templ, cv2.TM_CCOEFF_NORMED)`` for 2-D ``uint8``.

    Uses OpenCV's normalization and its rule for near-flat windows. OpenCV
    correlates in float32, so its scores can differ by up to about 0.01 on
    nearly flat windows and by less than 5e-4 where a match could be
    accepted. A flat template scores 0 everywhere; see below.
    """
    if use_opencv():
        return cv2.matchTemplate(image, templ, cv2.TM_CCOEFF_NORMED)
    img = image.astype(np.float64)
    tpl = templ.astype(np.float64)
    ih, iw = img.shape
    th, tw = tpl.shape
    rh, rw = ih - th + 1, iw - tw + 1
    n = float(th * tw)
    tpl_zero = tpl - tpl.mean()
    if int(templ.max()) == int(templ.min()):
        # A flat template carries nothing to match. OpenCV returns all ones or
        # all zeros here depending on how its build rounds the template
        # variance, so the answer differs across platforms. This path always
        # returns zeros: a flat template matches nowhere.
        return np.zeros((rh, rw), dtype=np.float32)
    templ_norm = float(np.sqrt((tpl_zero**2).sum()))

    # Cross-correlation through the FFT.
    fh, fw = ih + th - 1, iw + tw - 1
    spectrum = np.fft.rfft2(img, s=(fh, fw)) * np.conj(
        np.fft.rfft2(tpl_zero, s=(fh, fw))
    )
    corr = np.fft.irfft2(spectrum, s=(fh, fw))[:rh, :rw]

    integral = np.zeros((ih + 1, iw + 1), dtype=np.float64)
    integral[1:, 1:] = img.cumsum(0).cumsum(1)
    integral_sq = np.zeros((ih + 1, iw + 1), dtype=np.float64)
    integral_sq[1:, 1:] = (img * img).cumsum(0).cumsum(1)

    def window(table: np.ndarray) -> np.ndarray:
        return table[th:, tw:] - table[:-th, tw:] - table[th:, :-tw] + table[:-th, :-tw]

    win_sum = window(integral)[:rh, :rw]
    win_sum2 = window(integral_sq)[:rh, :rw]
    t = np.sqrt(np.maximum(win_sum2 - win_sum * win_sum / n, 0.0)) * templ_norm
    num = corr
    out = np.zeros_like(num)
    absnum = np.abs(num)
    inside = absnum < t
    out[inside] = num[inside] / t[inside]
    near = ~inside & (absnum < t * 1.125)
    out[near] = np.sign(num[near])
    return out.astype(np.float32)


def min_max_loc(
    arr: np.ndarray,
) -> tuple[float, float, tuple[int, int], tuple[int, int]]:
    """``cv2.minMaxLoc`` for a 2-D array: ``(min, max, (x, y) of min, (x, y) of max)``."""
    if use_opencv():
        lo, hi, lo_loc, hi_loc = cv2.minMaxLoc(arr)
        return float(lo), float(hi), tuple(lo_loc), tuple(hi_loc)
    lo_i = int(np.argmin(arr))
    hi_i = int(np.argmax(arr))
    width = arr.shape[1]
    return (
        float(arr.flat[lo_i]),
        float(arr.flat[hi_i]),
        (lo_i % width, lo_i // width),
        (hi_i % width, hi_i // width),
    )


def resize_cubic(img: np.ndarray, factor: int) -> np.ndarray:
    """``cv2.resize(img, None, fx=factor, fy=factor, interpolation=INTER_CUBIC)``.

    The numpy path uses Pillow's bicubic filter, which is close to but not the
    same as OpenCV's. It only feeds OCR retries on small text.
    """
    if use_opencv():
        return cv2.resize(
            img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC
        )
    h, w = img.shape[:2]
    if img.ndim == 3:
        rgb = Image.fromarray(np.ascontiguousarray(img[:, :, ::-1]))
        up = rgb.resize((w * factor, h * factor), Image.Resampling.BICUBIC)
        return np.ascontiguousarray(np.asarray(up)[:, :, ::-1])
    up = Image.fromarray(img).resize((w * factor, h * factor), Image.Resampling.BICUBIC)
    return np.asarray(up)


def dct2_orthonormal(pixels: np.ndarray) -> np.ndarray:
    """``cv2.dct`` of a square float64 array (orthonormal 2-D DCT-II).

    The numpy version multiplies by the DCT matrix and rounds to 1e-9, so a
    constant input gives exact zeros, as OpenCV does.
    """
    if use_opencv():
        return cv2.dct(np.asarray(pixels, dtype=np.float64))
    n = pixels.shape[0]
    k = np.arange(n)[:, np.newaxis]
    i = np.arange(n)[np.newaxis, :]
    basis = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    basis[0, :] = np.sqrt(1.0 / n)
    out = basis @ np.asarray(pixels, dtype=np.float64) @ basis.T
    return np.round(out, 9)
