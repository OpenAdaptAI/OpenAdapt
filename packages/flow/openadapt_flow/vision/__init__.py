"""Vision utilities: template matching, OCR, perceptual hashing, settling.

Public API (see DESIGN.md "Vision API"):

- :class:`Match`, :func:`find_template`, :func:`find_structural_template`
- :class:`OcrLine`, :func:`ocr`, :func:`find_text`,
  :func:`find_text_candidates`, :func:`text_present`, :func:`upscale_png`
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
    AmbiguousOcrMatchError,
    ContradictoryOcrEvidenceError,
    OcrLine,
    OcrResolutionRefused,
    find_text,
    find_text_candidates,
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
    "AmbiguousOcrMatchError",
    "ContradictoryOcrEvidenceError",
    "Match",
    "OcrLine",
    "OcrResolutionRefused",
    "SettleResult",
    "find_structural_template",
    "find_template",
    "find_text",
    "find_text_candidates",
    "ocr",
    "phash_distance",
    "phash_png",
    "pixels_changed",
    "text_present",
    "upscale_png",
    "wait_settled",
    "wait_settled_result",
]
