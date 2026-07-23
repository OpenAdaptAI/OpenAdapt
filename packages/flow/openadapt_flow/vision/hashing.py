"""Perceptual hashing helpers.

Used for change detection (settle polling) and REGION_STABLE postconditions.

Hashes are *structural*: the image is reduced to its edge map (PIL
``FIND_EDGES``) before perceptual hashing, so the hash captures layout and
text placement rather than palette. Identical frames still hash identically
(distance 0), a dark-theme re-render of the same screen stays within a small
distance, while a genuinely different screen state (e.g. an unexpected
modal) remains far away. Measured on MockMed: same-screen theme drift
distances are <= 12 versus >= 30 for a blocking modal.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter

from openadapt_flow.image_hash import hash_distance, perceptual_hash
from openadapt_flow.ir import Region


def phash_png(png: bytes, region: Region | None = None) -> str:
    """Compute the structural perceptual hash of a PNG (or a sub-region).

    The (optionally cropped) image is converted to grayscale, reduced to its
    edge map, and perceptually hashed — see the module docstring for why.

    Args:
        png: PNG-encoded image bytes.
        region: Optional ``(x, y, w, h)`` crop (clamped to image bounds)
            hashed instead of the full image.

    Returns:
        Hex-string perceptual hash suitable for :func:`phash_distance`.

    Raises:
        ValueError: If ``region`` is empty after clamping.
    """
    img = Image.open(io.BytesIO(png))
    if region is not None:
        x, y, w, h = region
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(img.width, x + w), min(img.height, y + h)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"region {region} is empty after clamping")
        img = img.crop((x0, y0, x1, y1))
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    return perceptual_hash(edges)


def phash_distance(a: str, b: str) -> int:
    """Return the Hamming distance between two hex phash strings."""
    return hash_distance(a, b)
