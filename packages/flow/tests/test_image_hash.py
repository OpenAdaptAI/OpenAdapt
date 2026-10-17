"""Compatibility tests for the dependency-free ImageHash implementation."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from openadapt_flow.image_hash import (
    difference_hash,
    hash_distance,
    perceptual_hash,
)
from openadapt_flow.recorder import _phash
from openadapt_flow.validation import pixel_identity_probe
from openadapt_flow.vision.hashing import phash_png


@pytest.mark.parametrize(
    ("shade", "expected"),
    [
        (0, "0000000000000000"),
        (1, "8000000000000000"),
        (127, "8000000000000000"),
        (255, "8000000000000000"),
    ],
)
def test_perceptual_hash_matches_imagehash_constant_vectors(
    shade: int, expected: str
) -> None:
    """Constants cover the DCT zero-frequency/tie corner."""
    assert perceptual_hash(Image.new("L", (32, 32), shade)) == expected


def test_seeded_random_vectors_match_imagehash_4_3_2() -> None:
    """Fixed vectors prevent a silent persisted-bundle hash migration."""
    expected = [
        ("f0b4856b51ccaba6", "a22c35940959e3ca"),
        ("fe9f404085fd53d0", "966d6d57a415a3ec"),
        ("dcc1f3204c826bfb", "c2d6553434a73c30"),
        ("a189bdc43e450bfc", "32bb53c82dcaa41b"),
        ("8d9974e98c908bf6", "325248b26643ad66"),
        ("e67ad3b56d02d02c", "4515ad55369acd56"),
        ("812050cebbe15def", "bb999a9b69d5ca9a"),
        ("b5bae2511305933f", "8c175dce149926cd"),
        ("f8b9d8161cc59e19", "b48a9b979974d3b5"),
        ("c1d9772cc3b1ae09", "93b25992d324d64a"),
        ("dc43610d6fa4da4d", "c288b9ac6231b629"),
        ("c8ba36909939f95a", "ad25625aaba1e446"),
    ]
    rng = np.random.default_rng(20260723)
    for expected_pair in expected:
        image = Image.fromarray(rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8))
        assert (
            perceptual_hash(image),
            difference_hash(image),
        ) == expected_pair


def _geometric_png() -> bytes:
    array = np.full((96, 144, 3), 255, np.uint8)
    array[10:60, 7:90] = (20, 40, 180)
    array[14:56, 11:86] = (245, 245, 245)
    array[25:45, 20:35] = (0, 0, 0)
    array[25:45, 45:60] = (80, 80, 80)
    array[20:50, 100:130] = (220, 20, 20)
    output = io.BytesIO()
    Image.fromarray(array).save(output, format="PNG")
    return output.getvalue()


def test_recorder_and_edge_hashes_match_persisted_imagehash_vectors() -> None:
    png = _geometric_png()
    assert _phash(png) == "aa0ab5b4b4db1fc0"
    assert phash_png(png) == "fbd38c49c522ca36"


def test_pixel_probe_phash_and_dhash_keep_imagehash_16_bit_contract() -> None:
    before = np.full((40, 200, 3), 255, np.uint8)
    for x in (20, 60, 100, 140):
        before[10:30, x : x + 16] = 0
    after = before.copy()
    after[14:26, 104:112] = 255

    assert pixel_identity_probe.m_phash(before, after) == 31.0
    assert pixel_identity_probe.m_dhash(before, after) == 0.0


def test_hex_hash_distance_matches_imagehash_semantics() -> None:
    assert hash_distance("0f", "f0") == 8
    assert hash_distance("0000000000000000", "8000000000000000") == 1
    with pytest.raises(TypeError):
        hash_distance("00", "0000")
    with pytest.raises(ValueError):
        hash_distance("zz", "00")
