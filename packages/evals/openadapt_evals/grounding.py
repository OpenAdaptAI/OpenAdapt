"""Grounding data model, state verification, and text anchoring for the
DemoExecutor cascade.

Defines GroundingTarget (stored per click step in demo) and
GroundingCandidate (produced by each tier during grounding).

Phase 4 — state-narrowing functions:
- ``check_state_preconditions``: verify the screen matches expectations
  before grounding a click.
- ``verify_transition``: verify the expected state change occurred after
  clicking.

Phase 5 — OCR text anchoring (Tier 1.5a):
- ``run_ocr``: extract text regions from a screenshot via pytesseract.
- ``ground_by_text``: match a GroundingTarget against OCR text with
  tiered scoring (exact > case-insensitive > substring > fuzzy) and
  nearby-text proximity boosting.

See docs/design/grounding_cascade_design_v3.md for the full architecture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class GroundingTarget:
    """Rich target representation stored per click step in a demo.

    This is the core data model that enables every tier in the cascade.
    Without it, each tier infers from a weak description string.
    """

    # What the target is
    description: str = ""
    target_type: str = ""  # "button" | "icon" | "checkbox" | "tab" | "text_field"

    # Visual evidence (from demo screenshot)
    crop_path: str = ""  # relative path to PNG crop of target element
    crop_bbox: tuple[int, int, int, int] | None = None  # [x1,y1,x2,y2] in demo frame
    click_offset: tuple[int, int] | None = None  # click point relative to crop top-left

    # Text context
    nearby_text: list[str] = field(default_factory=list)
    window_title: str = ""
    surrounding_labels: list[str] = field(default_factory=list)

    # Transition evidence (file references)
    screenshot_before_path: str = ""
    screenshot_after_path: str = ""

    # Structured transition expectations (machine-checkable)
    disappearance_text: list[str] = field(default_factory=list)
    appearance_text: list[str] = field(default_factory=list)
    window_title_change: str | None = None
    region_changed: tuple[int, int, int, int] | None = None
    modal_toggled: bool | None = None

    # Human-readable (logging/debugging)
    expected_change: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for demo JSON storage."""
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None and v != "" and v != [] and v != ():
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroundingTarget:
        """Deserialize from demo JSON."""
        kwargs = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                val = data[f]
                # Convert lists to tuples for bbox fields
                if f in ("crop_bbox", "click_offset", "region_changed") and isinstance(val, list):
                    val = tuple(val)
                kwargs[f] = val
        return cls(**kwargs)


@dataclass
class GroundingCandidate:
    """A candidate click location produced by a grounding tier.

    All tiers normalize their output into this type. Selection logic
    operates on a list of candidates, not on tier-specific return types.
    """

    source: str  # "a11y" | "ocr" | "clip" | "ui_venus" | "gpt54"
    point: tuple[int, int]  # (x, y) pixel coordinates
    bbox: tuple[int, int, int, int] | None = None
    local_score: float = 0.0  # tier-local confidence (not comparable across tiers)
    matched_text: str | None = None
    reasoning: str | None = None
    spatial_score: float | None = None  # consistency with demo position
    visual_verify_score: float | None = None  # crop resemblance to target
    accepted: bool = False


# ---------------------------------------------------------------------------
# Phase 4: State narrowing -- pre-click and post-click verification
# ---------------------------------------------------------------------------


def _text_present(
    query: str,
    ocr_results: list[dict],
    case_sensitive: bool = False,
) -> bool:
    """Check whether *query* appears in any OCR result text.

    Args:
        query: Text to search for.
        ocr_results: List of dicts with at least a ``"text"`` key.
        case_sensitive: Whether the comparison is case-sensitive.

    Returns:
        ``True`` if *query* is a substring of any OCR result text.
    """
    if not case_sensitive:
        query = query.lower()
    for item in ocr_results:
        text = item.get("text", "")
        if not case_sensitive:
            text = text.lower()
        if query in text:
            return True
    return False


def check_state_preconditions(
    screenshot: bytes,
    target: GroundingTarget,
    ocr_fn: Callable[[bytes], list[dict]] | None = None,
) -> tuple[bool, str]:
    """Check if the current screen state matches the demo's expectations.

    This is the "state narrowing" step that runs *before* candidate
    generation.  It is cheaper to detect "wrong screen" than to ground
    on it -- see the Phase 4 rationale in
    ``docs/design/grounding_cascade_design_v3.md``.

    Args:
        screenshot: Current screenshot PNG bytes.
        target: :class:`GroundingTarget` with ``window_title``,
            ``nearby_text``, ``surrounding_labels``, etc.
        ocr_fn: Optional OCR function that accepts PNG bytes and returns
            ``list[dict]`` where each dict has at least a ``"text"`` key
            (and optionally ``"bbox"``).  When *None*, precondition
            checks that require OCR are skipped gracefully.

    Returns:
        ``(preconditions_met, reason)`` -- ``True`` if safe to proceed
        with grounding, ``False`` with a human-readable reason string if
        state recovery is needed.
    """
    has_expectations = bool(
        target.window_title
        or target.nearby_text
        or target.surrounding_labels
    )

    # No text expectations on this target -- nothing to check.
    if not has_expectations:
        return True, "no text preconditions defined on target"

    # OCR unavailable -- skip gracefully (Phase 5 adds real OCR).
    if ocr_fn is None:
        return True, "no OCR available, skipping precondition check"

    ocr_results = ocr_fn(screenshot)

    # 1. Window title check
    if target.window_title:
        if not _text_present(target.window_title, ocr_results):
            return (
                False,
                f"window title mismatch: expected {target.window_title!r}",
            )

    # 2. Nearby text -- require at least half to be present
    if target.nearby_text:
        found = sum(
            1 for t in target.nearby_text if _text_present(t, ocr_results)
        )
        threshold = max(1, len(target.nearby_text) // 2)
        if found < threshold:
            return (
                False,
                f"nearby text mismatch: found {found}/{len(target.nearby_text)}"
                f" (need >= {threshold})",
            )

    # 3. Surrounding labels -- require at least half to be present
    if target.surrounding_labels:
        found = sum(
            1
            for t in target.surrounding_labels
            if _text_present(t, ocr_results)
        )
        threshold = max(1, len(target.surrounding_labels) // 2)
        if found < threshold:
            return (
                False,
                f"surrounding labels mismatch: found "
                f"{found}/{len(target.surrounding_labels)}"
                f" (need >= {threshold})",
            )

    return True, "preconditions met"


def verify_transition(
    screenshot_after: bytes,
    target: GroundingTarget,
    ocr_fn: Callable[[bytes], list[dict]] | None = None,
) -> tuple[bool, str]:
    """Verify that the click produced the expected state change.

    Uses structured transition expectations from :class:`GroundingTarget`:

    - ``disappearance_text``: text that should *no longer* be visible.
    - ``appearance_text``: text that should *now* be visible.
    - ``window_title_change``: expected new window title.
    - ``modal_toggled``: whether a modal appeared/disappeared (deferred
      until a modal-detection backend is available).

    Args:
        screenshot_after: Screenshot PNG bytes taken after the click.
        target: :class:`GroundingTarget` with structured transition
            expectations.
        ocr_fn: Optional OCR function (same contract as
            :func:`check_state_preconditions`).  When *None*, checks
            that require OCR are skipped gracefully.

    Returns:
        ``(verified, reason)`` -- ``True`` if the transition looks
        correct, ``False`` with a human-readable reason if it looks
        wrong.
    """
    has_expectations = bool(
        target.disappearance_text
        or target.appearance_text
        or target.window_title_change is not None
        or target.modal_toggled is not None
    )

    # No structured transition expectations -- nothing to verify.
    if not has_expectations:
        return True, "no transition expectations defined on target"

    # OCR unavailable -- skip gracefully.
    if ocr_fn is None:
        return True, "no OCR available, skipping transition verification"

    ocr_results = ocr_fn(screenshot_after)

    # 1. Disappearance check -- text should have vanished.
    if target.disappearance_text:
        for text in target.disappearance_text:
            if _text_present(text, ocr_results):
                return (
                    False,
                    f"disappearance_text still present: {text!r}",
                )

    # 2. Appearance check -- text should now be visible.
    if target.appearance_text:
        for text in target.appearance_text:
            if not _text_present(text, ocr_results):
                return (
                    False,
                    f"appearance_text not found: {text!r}",
                )

    # 3. Window title change
    if target.window_title_change is not None:
        if not _text_present(target.window_title_change, ocr_results):
            return (
                False,
                f"window title change not detected: "
                f"expected {target.window_title_change!r}",
            )

    # 4. Modal toggled -- deferred (requires modal detection backend).
    #    Log for observability but do not fail.
    if target.modal_toggled is not None:
        logger.debug(
            "modal_toggled=%s expectation set but no modal detection "
            "backend available -- skipping",
            target.modal_toggled,
        )

    return True, "transition verified"


# ---------------------------------------------------------------------------
# Phase 5: OCR text anchoring (Tier 1.5a)
# ---------------------------------------------------------------------------


def _char_overlap_ratio(a: str, b: str) -> float:
    """Return the ratio of shared characters between *a* and *b*.

    Uses character-level intersection (multiset) divided by the length of
    the longer string.  This is *not* edit distance — it is deliberately
    cheap and order-insensitive.

    Returns:
        A float in ``[0.0, 1.0]``.
    """
    if not a or not b:
        return 0.0
    # Build character frequency maps
    from collections import Counter

    ca = Counter(a.lower())
    cb = Counter(b.lower())
    overlap = sum((ca & cb).values())
    return overlap / max(len(a), len(b))


def _bbox_center(bbox: list[int] | tuple[int, ...]) -> tuple[float, float]:
    """Return the center ``(cx, cy)`` of an ``[x1, y1, x2, y2]`` bbox."""
    x1, y1, x2, y2 = bbox[:4]
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _bbox_distance(
    a: list[int] | tuple[int, ...],
    b: list[int] | tuple[int, ...],
) -> float:
    """Euclidean distance between the centers of two bboxes."""
    import math

    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)


def run_ocr(screenshot: bytes) -> list[dict]:
    """Run OCR on a screenshot and return detected text regions.

    Tries backends in order:
    1. GLM-OCR (VLM-based, better accuracy on complex UIs, ``pip install glmocr``)
    2. pytesseract (traditional OCR, requires system Tesseract binary)
    3. Returns ``[]`` if neither is available (graceful degradation).

    Args:
        screenshot: PNG image bytes.

    Returns:
        List of dicts with keys ``"text"``, ``"bbox"`` (``[x1, y1, x2, y2]``),
        and ``"confidence"`` (``0.0``–``1.0``).
    """
    # --- Try GLM-OCR first (VLM-based, better accuracy) ---
    results = _run_glm_ocr(screenshot)
    if results:
        return results

    # --- Fallback to pytesseract ---
    results = _run_pytesseract(screenshot)
    if results:
        return results

    logger.debug("No OCR backend available (tried glmocr, pytesseract)")
    return []


def _run_glm_ocr(screenshot: bytes) -> list[dict]:
    """Run GLM-OCR on a screenshot.

    GLM-OCR uses a VLM (CogViT + GLM-0.5B) for semantic text extraction.
    Returns structured results with bounding boxes.

    Requires: ``pip install glmocr``
    """
    try:
        from glmocr import parse  # type: ignore[import-untyped]
    except ImportError:
        return []

    try:
        import tempfile
        from pathlib import Path

        # GLM-OCR expects a file path, not bytes
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(screenshot)
            tmp_path = f.name

        try:
            result = parse(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        # Convert GLM-OCR output to our standard format
        results: list[dict] = []
        # GLM-OCR returns structured blocks with text and coordinates
        for block in getattr(result, "blocks", []):
            text = getattr(block, "text", "").strip()
            if not text or len(text) < 2:
                continue
            bbox = getattr(block, "bbox", None)
            if bbox and len(bbox) >= 4:
                results.append({
                    "text": text,
                    "bbox": [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])],
                    "confidence": getattr(block, "confidence", 0.9),
                })
        if results:
            logger.info("GLM-OCR extracted %d text regions", len(results))
        return results

    except Exception as exc:
        logger.debug("GLM-OCR failed: %s", exc)
        return []


def _run_pytesseract(screenshot: bytes) -> list[dict]:
    """Run pytesseract OCR on a screenshot.

    Requires: ``pip install pytesseract`` + system Tesseract binary.
    """
    try:
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return []

    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(screenshot))
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception as exc:
        logger.debug("pytesseract failed: %s", exc)
        return []

    results: list[dict] = []
    n_boxes = len(data.get("text", []))
    for i in range(n_boxes):
        text = data["text"][i].strip()
        if not text:
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])
        results.append({
            "text": text,
            "bbox": [x, y, x + w, y + h],
            "confidence": conf / 100.0,
        })
    if results:
        logger.info("pytesseract extracted %d text regions", len(results))
    return results


def ground_by_text(
    screenshot: bytes,
    target: GroundingTarget,
    ocr_results: list[dict] | None = None,
) -> list[GroundingCandidate]:
    """Ground a target by matching its description against OCR text.

    This is **Tier 1.5a** in the grounding cascade — faster and cheaper
    than a VLM call, but only works when the target contains readable
    text.

    Scoring tiers (from highest to lowest):

    - **Exact match** (``0.95``): OCR text equals the target description.
    - **Case-insensitive match** (``0.90``): Matches after lowercasing.
    - **Substring match** (``0.70``): Target description is a substring of
      the OCR text (or vice-versa), case-insensitive.
    - **Fuzzy match** (``0.50``): Character-level overlap ratio > 80%.

    Candidates near ``target.nearby_text`` locations receive a ``+0.05``
    proximity boost (capped at ``1.0``).

    Args:
        screenshot: PNG image bytes (used for OCR if *ocr_results* not
            provided).
        target: :class:`GroundingTarget` with at least a ``description``.
        ocr_results: Pre-computed OCR results.  When ``None``,
            :func:`run_ocr` is called on *screenshot*.

    Returns:
        Up to 5 :class:`GroundingCandidate` objects sorted by score
        (highest first).  Empty list if no matches found.
    """
    if not target.description:
        return []

    if ocr_results is None:
        ocr_results = run_ocr(screenshot)

    if not ocr_results:
        return []

    query = target.description
    query_lower = query.lower()

    candidates: list[GroundingCandidate] = []

    for item in ocr_results:
        text = item.get("text", "")
        bbox = item.get("bbox")
        if not text or not bbox:
            continue

        text_lower = text.lower()
        score = 0.0

        # Tiered scoring
        if text == query:
            score = 0.95
        elif text_lower == query_lower:
            score = 0.90
        elif query_lower in text_lower or text_lower in query_lower:
            score = 0.70
        elif _char_overlap_ratio(query, text) > 0.80:
            score = 0.50
        else:
            continue  # No match

        cx, cy = _bbox_center(bbox)
        candidates.append(
            GroundingCandidate(
                source="ocr",
                point=(int(cx), int(cy)),
                bbox=tuple(bbox[:4]),  # type: ignore[arg-type]
                local_score=score,
                matched_text=text,
                reasoning=f"OCR text match: {text!r} (score={score:.2f})",
            )
        )

    # Proximity boost: +0.05 for candidates near nearby_text locations
    if target.nearby_text and candidates:
        # Find bboxes for nearby_text items
        nearby_bboxes: list[list[int]] = []
        for nearby in target.nearby_text:
            nearby_lower = nearby.lower()
            for item in ocr_results:
                item_text = item.get("text", "").lower()
                if nearby_lower in item_text and item.get("bbox"):
                    nearby_bboxes.append(item["bbox"])

        if nearby_bboxes:
            proximity_threshold = 300.0  # pixels
            for candidate in candidates:
                if candidate.bbox is None:
                    continue
                for nb_bbox in nearby_bboxes:
                    dist = _bbox_distance(list(candidate.bbox), nb_bbox)
                    if dist < proximity_threshold:
                        candidate.local_score = min(
                            1.0, candidate.local_score + 0.05
                        )
                        candidate.reasoning = (
                            f"{candidate.reasoning}, "
                            f"nearby boost (+0.05)"
                        )
                        break  # One boost per candidate

    # Sort by score (descending), return top 5
    candidates.sort(key=lambda c: c.local_score, reverse=True)
    return candidates[:5]
