"""Vision utilities: template matching, OCR, perceptual hashing, settling.

Public API (see DESIGN.md "Vision API"):

- :class:`Match`, :func:`find_template`, :func:`find_structural_template`
- :class:`OcrLine`, :func:`ocr`, :func:`find_text`,
  :func:`find_text_candidates`, :func:`normalize_text`,
  :func:`text_present`, :func:`upscale_png`
- :func:`phash_png`, :func:`phash_distance`
- :func:`pixels_changed`
- :func:`wait_settled`, :func:`wait_settled_result`, :class:`SettleResult`
"""

import hashlib
import json
from functools import lru_cache
from typing import Any

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
    normalize_text,
    ocr,
    text_present,
    upscale_png,
)
from openadapt_flow.vision.settle import (
    SettleResult,
    wait_settled,
    wait_settled_result,
)


def _contract_digest(value: Any) -> str:
    """Hash one explicit JSON semantic value."""

    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _array_contract(value: Any) -> dict[str, Any]:
    """Bind one explicit numeric preprocessing value by shape and bytes."""

    payload = value.tobytes()
    return {
        "shape": list(value.shape),
        "dtype": str(value.dtype),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _inference_contract(value: Any) -> dict[str, Any]:
    """Bind provider selection without binding lazy runtime sessions/loggers."""

    return {
        "cfg_use_cuda": bool(value.cfg_use_cuda),
        "cfg_use_dml": bool(value.cfg_use_dml),
        "had_providers": list(value.had_providers),
        "use_cuda": bool(value.use_cuda),
        "use_directml": bool(value.use_directml),
    }


def _rapidocr_contract(engine: Any) -> dict[str, Any]:
    """Return the reviewed, behavior-affecting RapidOCR configuration."""

    detector = engine.text_det
    classifier = engine.text_cls
    recognizer = engine.text_rec
    detector_post = detector.postprocess_op
    classifier_post = classifier.postprocess_op
    recognizer_post = recognizer.postprocess_op
    return {
        "type": f"{type(engine).__module__}.{type(engine).__qualname__}",
        "text_score": float(engine.text_score),
        "min_height": int(engine.min_height),
        "width_height_ratio": int(engine.width_height_ratio),
        "use_det": bool(engine.use_det),
        "use_cls": bool(engine.use_cls),
        "use_rec": bool(engine.use_rec),
        "max_side_len": int(engine.max_side_len),
        "min_side_len": int(engine.min_side_len),
        "detector": {
            "limit_side_len": int(detector.limit_side_len),
            "limit_type": str(detector.limit_type),
            "mean": list(detector.mean),
            "std": list(detector.std),
            "postprocess": {
                "thresh": float(detector_post.thresh),
                "box_thresh": float(detector_post.box_thresh),
                "max_candidates": int(detector_post.max_candidates),
                "unclip_ratio": float(detector_post.unclip_ratio),
                "min_size": int(detector_post.min_size),
                "score_mode": str(detector_post.score_mode),
                "dilation_kernel": _array_contract(detector_post.dilation_kernel),
            },
            "inference": _inference_contract(detector.infer),
        },
        "classifier": {
            "image_shape": list(classifier.cls_image_shape),
            "batch_num": int(classifier.cls_batch_num),
            "threshold": float(classifier.cls_thresh),
            "labels_sha256": _contract_digest(classifier_post.label_list),
            "inference": _inference_contract(classifier.infer),
        },
        "recognizer": {
            "image_shape": list(recognizer.rec_image_shape),
            "batch_num": int(recognizer.rec_batch_num),
            "characters_sha256": _contract_digest(recognizer_post.character),
            "character_index_sha256": _contract_digest(recognizer_post.dict),
            "inference": _inference_contract(recognizer.session),
        },
    }


@lru_cache(maxsize=1)
def _default_ocr_contract_state() -> Any:
    """Build the lazy default OCR contract once per runtime process."""

    from rapidocr_onnxruntime import RapidOCR

    return _rapidocr_contract(RapidOCR())


def program_predicate_contract() -> dict[str, Any]:
    """Describe the exact active built-in visual-predicate configuration."""

    from openadapt_flow.vision import ocr as _ocr_callable

    ocr_module = __import__(_ocr_callable.__module__, fromlist=["_engine"])
    engine = getattr(ocr_module, "_engine", None)
    if engine is None:
        # Contract the default lazy engine before the first OCR call. This
        # produces the same semantic configuration before and after lazy
        # initialization, while the dependency artifact digest binds its exact
        # models and native runtime.
        engine_contract = _default_ocr_contract_state()
    else:
        engine_contract = _rapidocr_contract(engine)
    return {
        "ocr_backend": "rapidocr-onnxruntime",
        "ocr_engine": engine_contract,
    }


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
    "normalize_text",
    "ocr",
    "phash_distance",
    "phash_png",
    "pixels_changed",
    "program_predicate_contract",
    "text_present",
    "upscale_png",
    "wait_settled",
    "wait_settled_result",
]
