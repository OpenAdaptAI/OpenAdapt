"""Vision utilities: template matching, OCR, perceptual hashing, settling.

Public API (see DESIGN.md "Vision API"):

- :class:`Match`, :func:`find_template`, :func:`find_structural_template`
- :class:`OcrLine`, :func:`ocr`, :func:`find_text`, :func:`text_present`,
  :func:`upscale_png`
- :func:`phash_png`, :func:`phash_distance`
- :func:`pixels_changed`
- :func:`wait_settled`, :func:`wait_settled_result`, :class:`SettleResult`
"""

from openadapt_flow.vision.hashing import phash_distance, phash_png
from openadapt_flow.vision.match import (
    Match,
    find_structural_template,
    find_template,
    pixels_changed,
)
from openadapt_flow.vision.ocr import (
    OcrLine,
    find_text,
    ocr,
    text_present,
    upscale_png,
)
from openadapt_flow.vision.settle import (
    SettleResult,
    wait_settled,
    wait_settled_result,
)

__all__ = [
    "Match",
    "OcrLine",
    "SettleResult",
    "find_structural_template",
    "find_template",
    "find_text",
    "ocr",
    "phash_distance",
    "phash_png",
    "pixels_changed",
    "text_present",
    "upscale_png",
    "wait_settled",
    "wait_settled_result",
]
