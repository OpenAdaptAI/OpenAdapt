"""Deterministic perceptual hashes without SciPy.

The algorithms and serialized hexadecimal representation intentionally match
``ImageHash==4.3.2``.  OpenAdapt persisted those strings in compiled bundles,
so this module is a compatibility implementation rather than a new hash
format.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


def _binary_array_to_hex(bits: np.ndarray) -> str:
    """Serialize a boolean array in ImageHash-compatible row-major order."""
    flat = np.asarray(bits, dtype=np.bool_).ravel()
    bit_string = "".join("1" if bit else "0" for bit in flat)
    width = (len(bit_string) + 3) // 4
    return f"{int(bit_string, 2):0{width}x}"


def _unnormalized_dct2(pixels: np.ndarray) -> np.ndarray:
    """Return SciPy fftpack-compatible unnormalized type-II 2-D DCT.

    OpenCV computes an orthonormal DCT-II.  Scaling its frequency rows and
    columns converts it to the unnormalized transform used by
    ``scipy.fftpack.dct(..., type=2, norm=None)``.  Using OpenCV also preserves
    exact zero-frequency symmetry for constant images; a direct cosine-matrix
    implementation can leave tiny platform-dependent residuals that flip
    median-threshold bits.
    """
    height, width = pixels.shape
    if height != width:
        raise ValueError("perceptual hash DCT input must be square")
    normalized = cv2.dct(np.asarray(pixels, dtype=np.float64))
    scale = np.full(width, np.sqrt(2.0 * width), dtype=np.float64)
    scale[0] = 2.0 * np.sqrt(width)
    return normalized * scale[:, np.newaxis] * scale[np.newaxis, :]


def perceptual_hash(
    image: Image.Image,
    *,
    hash_size: int = 8,
    highfreq_factor: int = 4,
) -> str:
    """Return an ImageHash-compatible perceptual hash."""
    if hash_size < 2:
        raise ValueError("Hash size must be greater than or equal to 2")

    image_size = hash_size * highfreq_factor
    pixels = np.asarray(
        image.convert("L").resize(
            (image_size, image_size),
            Image.Resampling.LANCZOS,
        ),
        dtype=np.float64,
    )
    dct = _unnormalized_dct2(pixels)
    low_frequencies = dct[:hash_size, :hash_size]
    return _binary_array_to_hex(low_frequencies > np.median(low_frequencies))


def difference_hash(image: Image.Image, *, hash_size: int = 8) -> str:
    """Return an ImageHash-compatible horizontal difference hash."""
    if hash_size < 2:
        raise ValueError("Hash size must be greater than or equal to 2")

    pixels = np.asarray(
        image.convert("L").resize(
            (hash_size + 1, hash_size),
            Image.Resampling.LANCZOS,
        )
    )
    return _binary_array_to_hex(pixels[:, 1:] > pixels[:, :-1])


def hash_distance(a: str, b: str) -> int:
    """Return the Hamming distance between equal-width hexadecimal hashes."""
    if len(a) != len(b):
        raise TypeError(
            "Image hashes must have the same bit length.",
            len(a) * 4,
            len(b) * 4,
        )
    return (int(a, 16) ^ int(b, 16)).bit_count()
