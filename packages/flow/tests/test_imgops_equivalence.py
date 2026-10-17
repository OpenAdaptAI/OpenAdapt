"""The numpy image operations agree with OpenCV.

``openadapt_flow.vision.imgops`` uses OpenCV when RapidOCR is active and
numpy/Pillow otherwise. These tests run both paths on the same inputs.
"""

from __future__ import annotations

import io
from typing import Iterator

import numpy as np
import pytest
from PIL import Image, ImageDraw

cv2 = pytest.importorskip("cv2")

from openadapt_flow.image_hash import perceptual_hash  # noqa: E402
from openadapt_flow.vision import imgops  # noqa: E402


@pytest.fixture
def numpy_ops(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(imgops.IMAGE_OPS_ENV, "numpy")
    yield


def _both(monkeypatch: pytest.MonkeyPatch, fn, *args):  # type: ignore[no-untyped-def]
    monkeypatch.setenv(imgops.IMAGE_OPS_ENV, "cv2")
    expected = fn(*args)
    monkeypatch.setenv(imgops.IMAGE_OPS_ENV, "numpy")
    actual = fn(*args)
    return expected, actual


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _form(seed: int, size: tuple[int, int] = (640, 400)) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(img)
    for i in range(12):
        x = int(rng.integers(0, size[0] - 120))
        y = int(rng.integers(0, size[1] - 30))
        color = tuple(int(c) for c in rng.integers(0, 200, 3))
        draw.rectangle([x, y, x + int(rng.integers(20, 120)), y + 24], outline=color)
        draw.text((x + 4, y + 6), f"Field {seed}-{i} MRN 1O0{i}", fill=color)
    return img


FRAMES = [_form(seed) for seed in range(4)]


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
def test_decode_matches(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    png = _png(FRAMES[0].convert(mode))
    expected, actual = _both(monkeypatch, imgops.decode_bgr, png)
    np.testing.assert_array_equal(actual, expected)
    expected, actual = _both(monkeypatch, imgops.decode_gray, png)
    np.testing.assert_array_equal(actual, expected)


def test_gray_absdiff_threshold_dilate_match(monkeypatch: pytest.MonkeyPatch) -> None:
    a = imgops.decode_bgr(_png(FRAMES[0]))
    b = imgops.decode_bgr(_png(FRAMES[1]))
    assert a is not None and b is not None
    e, n = _both(monkeypatch, imgops.bgr_to_gray, a)
    np.testing.assert_array_equal(n, e)
    e, n = _both(monkeypatch, imgops.absdiff, a, b)
    np.testing.assert_array_equal(n, e)
    gray = cv2.cvtColor(cv2.absdiff(a, b), cv2.COLOR_BGR2GRAY)
    for thresh in (0, 24, 24.5, 254):
        e, n = _both(monkeypatch, imgops.threshold_binary, gray, thresh, 255)
        np.testing.assert_array_equal(n, e)
    mask = cv2.threshold(gray, 24, 255, cv2.THRESH_BINARY)[1]
    for size, iterations in ((9, 2), (3, 1), (4, 1)):
        e, n = _both(monkeypatch, imgops.dilate_rect, mask, size, iterations)
        np.testing.assert_array_equal(n, e)


def _blob_masks() -> Iterator[np.ndarray]:
    rng = np.random.default_rng(7)
    for _ in range(40):
        mask = np.zeros((90, 120), dtype=np.uint8)
        for _ in range(int(rng.integers(1, 6))):
            x, y = int(rng.integers(0, 110)), int(rng.integers(0, 80))
            w, h = int(rng.integers(1, 40)), int(rng.integers(1, 30))
            mask[y : y + h, x : x + w] = 255
        if rng.random() < 0.3:
            # A ring with a blob inside its hole: the inner blob is not external.
            mask[10:60, 10:70] = 255
            mask[20:50, 20:60] = 0
            mask[30:40, 30:40] = 255
        yield (
            cv2.dilate(mask, np.ones((3, 3), np.uint8)) if rng.random() < 0.5 else mask
        )


def test_largest_external_region(monkeypatch: pytest.MonkeyPatch) -> None:
    for mask in _blob_masks():
        e, n = _both(monkeypatch, imgops.largest_external_region, mask)
        assert n == e
    empty = np.zeros((10, 10), dtype=np.uint8)
    e, n = _both(monkeypatch, imgops.largest_external_region, empty)
    assert n is None and e is None


@pytest.mark.parametrize("scale", [0.85, 1.18, 0.5, 2.0])
def test_resize_area_within_one_level(
    monkeypatch: pytest.MonkeyPatch, scale: float
) -> None:
    gray = cv2.cvtColor(np.asarray(FRAMES[2])[:, :, ::-1].copy(), cv2.COLOR_BGR2GRAY)
    tpl = gray[100:160, 200:330]
    th, tw = tpl.shape
    size = (max(1, round(tw * scale)), max(1, round(th * scale)))
    e, n = _both(monkeypatch, imgops.resize_area, tpl, *size)
    assert n.shape == e.shape
    assert int(np.abs(n.astype(int) - e.astype(int)).max()) <= 1


def test_match_template_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    for frame in FRAMES:
        gray = cv2.cvtColor(np.asarray(frame)[:, :, ::-1].copy(), cv2.COLOR_BGR2GRAY)
        for y, x, h, w in ((40, 60, 30, 90), (200, 300, 24, 24), (0, 0, 50, 50)):
            tpl = gray[y : y + h, x : x + w]
            e, n = _both(monkeypatch, imgops.match_template_ccoeff_normed, gray, tpl)
            assert n.shape == e.shape and n.dtype == e.dtype
            # OpenCV correlates in float32; the numpy path in float64. On a
            # nearly flat window (standard deviation about 0.1) OpenCV's score
            # is off by up to about 0.01. Where a match could be accepted,
            # they agree to within 5e-4; the numpy score is the one that
            # equals a float64 Pearson correlation of the window.
            assert float(np.abs(n - e).max()) < 2e-2
            strong = e > 0.5
            if strong.any():
                assert float(np.abs(n - e)[strong].max()) < 5e-4
            _, e_max, _, e_loc = cv2.minMaxLoc(e)
            monkeypatch.setenv(imgops.IMAGE_OPS_ENV, "numpy")
            _, n_max, _, n_loc = imgops.min_max_loc(n)
            assert abs(n_max - e_max) < 5e-4
            if int((e > e_max - 1e-3).sum()) == 1:
                # A unique best match is found at the same place. Ties (a
                # flat patch matches a flat area everywhere) may resolve to
                # different equal-scoring places.
                assert n_loc == tuple(e_loc)
    # A flat template matches everywhere, as in OpenCV.
    flat = np.full((10, 10), 128, np.uint8)
    e, n = _both(monkeypatch, imgops.match_template_ccoeff_normed, gray, flat)
    np.testing.assert_array_equal(n, e)


def test_perceptual_hash_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    images = [*FRAMES, Image.new("RGB", (64, 64), (120, 120, 120))]
    gradient = np.tile(np.arange(256, dtype=np.uint8), (64, 1))
    images.append(Image.fromarray(gradient))
    rng = np.random.default_rng(3)
    images += [
        Image.fromarray(rng.integers(0, 256, (48, 48, 3), dtype=np.uint8))
        for _ in range(30)
    ]
    for image in images:
        e, n = _both(monkeypatch, perceptual_hash, image)
        assert n == e


def test_encode_roundtrip(numpy_ops: None) -> None:
    img = imgops.decode_bgr(_png(FRAMES[3]))
    assert img is not None
    png = imgops.encode_png(img)
    assert png is not None
    np.testing.assert_array_equal(imgops.decode_bgr(png), img)
    assert imgops.png_size(png) == (640, 400)
