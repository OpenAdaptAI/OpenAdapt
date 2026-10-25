"""Compile a recorded demonstration into a workflow bundle.

Input is a recording directory (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``); output is a bundle directory
(``workflow.json`` + ``templates/*.png`` + a generated, human-reviewable
``workflow.py`` rendering).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    cast,
)

if TYPE_CHECKING:
    from openadapt_flow.compiler.annotate import StepAnnotator

import numpy as np

from openadapt_flow import sealed_set as _sealed
from openadapt_flow import volatility
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    BackendHints,
    BundleManifest,
    BundleProvenance,
    DialogResponse,
    ExecutionMode,
    ExecutionTargetKind,
    HoverEvidence,
    Landmark,
    ParamKind,
    ParamSpec,
    Point,
    Postcondition,
    PostconditionKind,
    Region,
    Step,
    StructuralLocator,
    SurfaceSpec,
    Workflow,
    structural_locator_is_locatable,
)
from openadapt_flow.risk import (
    PRESUMED_COMMIT_EXPLANATION,
    confirmation_opener_step_ids,
    infer_step_risk,
    is_meaningful_label,
    presumed_commit_step_ids,
)
from openadapt_flow.runtime.identity import (
    band_region,
    context_from_lines,
    context_region_from_lines,
    coverage,
    identifier_text_from_lines,
)
from openadapt_flow.vision import imgops
from openadapt_flow.vision.hashing import phash_distance, phash_png
from openadapt_flow.vision.ocr import OcrLine, normalize_text, ocr

# Template crop size around a click target (clamped to the frame).
TEMPLATE_W = 160
TEMPLATE_H = 64

# Crops with almost no pixel variance (e.g. the middle of an empty textarea)
# cannot be template-matched; the crop is grown through this ladder until its
# grayscale standard deviation clears MIN_TEMPLATE_STD, so every template
# contains at least some structure (a border, a label, ...).
CROP_GROWTH_LADDER: tuple[tuple[int, int], ...] = (
    (TEMPLATE_W, TEMPLATE_H),
    (240, 96),
    (320, 128),
    (480, 192),
    (640, 256),
)
MIN_TEMPLATE_STD = 5.0

# Minimum OCR confidence for a line to be used as ocr_text / landmark text.
MIN_OCR_CONFIDENCE = 0.5


def _exact_ocr_value_present(png: bytes, text: str, region: Region) -> bool:
    """Require one complete normalized OCR line to equal the option value.

    Fuzzy substring evidence is unsafe for enum selection: ``Virginia`` must
    not certify a committed ``West Virginia`` value. False negatives halt
    compilation of the richer action and preserve the explicit click/type/key
    sequence; false positives could delete the demonstrated commit step.
    """

    expected = normalize_text(text)
    return any(
        line.confidence >= MIN_OCR_CONFIDENCE and normalize_text(line.text) == expected
        for line in ocr(png, region=region)
    )


def _selection_readback_region(png: bytes, target_region: Region) -> Region:
    """Return a bounded horizontal readback band around a resolved target.

    Visual target templates are intentionally compact and centered on the
    demonstrated click. A click near a native select's arrow can therefore
    crop the first glyphs of its value. Selection verification keeps the exact
    target region for resolution and stores this separate, deterministically
    expanded band for complete-value OCR. Runtime maps the band from the
    recorded target to the freshly re-resolved live target; its absolute
    recording coordinates are never replayed directly.
    """

    frame = imgops.decode_bgr(png)
    if frame is None:
        return target_region
    frame_h, frame_w = frame.shape[:2]
    x, y, width, height = target_region
    pad = max(16, width // 4)
    left = max(0, x - pad)
    right = min(frame_w, x + width + pad)
    top = max(0, y)
    bottom = min(frame_h, y + height)
    return (left, top, right - left, bottom - top)


# Before/after diff parameters (see DESIGN.md "Compiler").
DIFF_THRESHOLD = 25
# Tolerance for REGION_STABLE postconditions. The structural (edge-based)
# phash distance is ~0 for identical screens, <= 12 for cosmetic drift
# (theme palettes, relocated buttons elsewhere in the region) and >= 30 for
# semantic drift (e.g. a blocking modal), measured on MockMed; 16 splits
# those populations with margin.
REGION_STABLE_TOLERANCE = 16

# The changed region is padded by this many pixels (clamped to the frame)
# before hashing: tiny text-only regions (e.g. a freshly typed value inside
# an input) are dominated by glyph antialiasing, which shifts with palette
# drift; padding pulls stable structure (field borders, surrounding
# whitespace) into the hashed region.
REGION_STABLE_PAD = 24

# ``_param_free_band`` blocks each parameter-bearing text row with this much
# vertical margin (glyph antialiasing plus OCR bounding-box slop), and discards
# any surviving slice shorter than CARVE_MIN_HEIGHT as glyph noise rather than
# structure.
CARVE_ROW_PAD = 6
CARVE_MIN_HEIGHT = 2 * REGION_STABLE_PAD

# Minimum characters for a TEXT_PRESENT candidate (filters OCR noise).
MIN_TEXT_PRESENT_LEN = 3

# A TEXT_PRESENT candidate is dropped when at least this fraction of an
# excluded (parameterized) value's characters fuzzily appears inside it.
# Matching is whitespace-insensitive because OCR frequently drops or moves
# spaces (e.g. "Encounter saved — my note" -> "Encountersaved—mynote").
EXCLUDE_CONTAINMENT_RATIO = 0.8

# A TEXT_PRESENT candidate counts as already visible in the before frame
# when its squashed text matches a before line at or above this ratio. OCR
# reads the same static text slightly differently across frames (whitespace
# jitter, the odd glyph), so exact comparison would misclassify permanently
# visible chrome (e.g. an app header) as "new" — a postcondition that can
# never discriminate anything.
SEEN_BEFORE_RATIO = 0.9

# A TEXT_PRESENT candidate is dropped when it whole-line matches a click
# target's label (any anchor's ocr_text) at or above this ratio. Target
# labels are mutable evidence by design — rename drift changes them and the
# resolution ladder heals through it — so asserting one as a postcondition
# invariant would turn cosmetic label drift into a false semantic-drift
# abort.
LABEL_MATCH_RATIO = 0.85

# TEXT_PRESENT candidates are classified for volatility (see
# openadapt_flow.volatility): clock times, dates near the recording date,
# digit/punctuation-dominated fragments, and low-entropy noise all name a
# moment or a count, not an invariant screen state, and cannot survive
# replay against live data. Observed on OpenEMR: a stray ':01' clock-minute
# fragment was mined and false-halted every later replay. Dates FAR from
# the recording date (a DOB in an identity banner) are deliberately kept —
# they are identity data, not chronology.

# Empirical stability: a candidate that appeared in a step's after frame
# but is already gone (or has changed) by the NEXT step's before frame —
# two captures of the SAME screen state, moments apart — is volatile by
# demonstration (fading toasts, spinners, ticking counters). A candidate's
# squashed text must be covered at this ratio in the next frame's OCR to
# survive. The same rule drops REGION_STABLE postconditions whose region
# self-mutates between the two captures (animations, clocks).
PERSISTENCE_RATIO = 0.8

# Semantic-tie preference: among stable candidates, the one with the most
# alphabetic content wins, but any candidate within this fraction of the
# best is competitive — and the competitive candidate CLOSEST to the click
# target is preferred (text near the action is likelier to describe its
# effect than a far-away data row).
PROXIMITY_POOL_RATIO = 0.6

# Excluded (parameterized) values shorter than this (squashed) are not used
# for exclusion or leak-linting: 1-2 char examples fuzzily match everything.
MIN_EXCLUDE_CHARS = 3

logger = logging.getLogger(__name__)


def _load_events(recording_dir: Path) -> list[dict]:
    """Parse events.jsonl into a list of event dicts."""
    events = []
    with _sealed.open_text(recording_dir / "events.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _read_png(path: Path) -> Optional[bytes]:
    """Read PNG bytes, or None if the file does not exist."""
    return _sealed.read_bytes(path) if _sealed.is_file(path) else None


def _png_viewport(png: bytes, *, source: str) -> tuple[int, int]:
    """Return a PNG's exact ``(width, height)`` or reject invalid evidence."""

    frame = imgops.decode_bgr(png)
    if frame is None:
        raise ValueError(f"could not decode {source} PNG")
    return (int(frame.shape[1]), int(frame.shape[0]))


def _validated_event_viewport(
    event: dict,
    *,
    key: str,
    png: Optional[bytes],
    event_index: int,
) -> Optional[tuple[int, int]]:
    """Bind an event viewport declaration to its exact retained PNG.

    Older recordings do not carry per-event viewport fields. They remain
    valid and use the PNG dimensions as the source of truth. A new recording
    that does carry the field must match the retained evidence exactly.
    """

    declared = event.get(key)
    actual = (
        _png_viewport(png, source=f"event {event_index} {key}")
        if png is not None
        else None
    )
    if declared is None:
        return actual
    if (
        not isinstance(declared, (list, tuple))
        or len(declared) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) for value in declared
        )
        or int(declared[0]) <= 0
        or int(declared[1]) <= 0
    ):
        raise ValueError(
            f"events.jsonl event {event_index} {key} must be two positive integers"
        )
    if actual is None:
        raise ValueError(
            f"events.jsonl event {event_index} declares {key} without its PNG"
        )
    normalized = (int(declared[0]), int(declared[1]))
    if normalized != actual:
        raise ValueError(
            f"events.jsonl event {event_index} {key} {normalized} does not match "
            f"the retained PNG {actual}"
        )
    return actual


def _validate_point_in_viewport(
    point: Point,
    viewport: tuple[int, int],
    *,
    event_index: int,
    label: str,
) -> None:
    """Reject pointer evidence outside its declared coordinate space."""

    if not (0 <= point[0] < viewport[0] and 0 <= point[1] < viewport[1]):
        raise ValueError(
            f"events.jsonl event {event_index} {label} {point} is outside "
            f"viewport {viewport}"
        )


def _clamped_crop_region(
    click: Point,
    frame_w: int,
    frame_h: int,
    size: tuple[int, int] = (TEMPLATE_W, TEMPLATE_H),
) -> Region:
    """Compute a ``size`` crop centered on ``click``, clamped so it stays
    inside the frame (shrunk only if the frame itself is smaller)."""
    w = min(size[0], frame_w)
    h = min(size[1], frame_h)
    x = min(max(0, click[0] - w // 2), frame_w - w)
    y = min(max(0, click[1] - h // 2), frame_h - h)
    return (x, y, w, h)


#: Smallest template height kept when clipping parameter text out of a crop.
MIN_CLIPPED_TEMPLATE_H = 24


def _clip_away_param_lines(
    region: Region,
    click: Point,
    lines: list[OcrLine],
    exclude_texts: tuple[str, ...],
) -> Region:
    """Shrink a click template so it doesn't picture a typed parameter value.

    A click on a form field right below (or above) a field the demonstration
    just filled captures that value in its 64 px template. The value changes
    every run, so the template never matches and the step falls to weaker
    rungs. Move the crop's top or bottom edge past each OCR line that shows a
    parameter value and doesn't contain the click. Keep the original crop when
    the result would be shorter than ``MIN_CLIPPED_TEMPLATE_H``.
    """
    x, y, w, h = region
    top, bottom = y, y + h
    cy = click[1]
    for line in lines:
        lx, ly, lw, lh = (int(v) for v in line.region)
        if lx >= x + w or lx + lw <= x or ly >= bottom or ly + lh <= top:
            continue
        if ly <= cy < ly + lh or not _contains_excluded(line.text, exclude_texts):
            continue
        if ly + lh <= cy:
            top = max(top, ly + lh + 2)
        else:
            bottom = min(bottom, ly - 2)
    if bottom - top < MIN_CLIPPED_TEMPLATE_H or not (top <= cy < bottom):
        return region
    return (x, top, w, bottom - top)


def _discriminative_crop_region(frame: np.ndarray, click: Point) -> Region:
    """Crop region centered on ``click`` with enough structure to match.

    Starts at TEMPLATE_W x TEMPLATE_H and grows through CROP_GROWTH_LADDER
    until the crop's grayscale standard deviation reaches MIN_TEMPLATE_STD
    (a fully flat crop — e.g. the middle of an empty textarea — matches
    everywhere and nowhere). Falls back to the largest ladder size.
    """
    frame_h, frame_w = frame.shape[:2]
    gray = imgops.bgr_to_gray(frame)
    region = _clamped_crop_region(click, frame_w, frame_h)
    for size in CROP_GROWTH_LADDER:
        region = _clamped_crop_region(click, frame_w, frame_h, size)
        x, y, w, h = region
        if float(gray[y : y + h, x : x + w].std()) >= MIN_TEMPLATE_STD:
            break
    return region


def _crop_png(png: bytes, region: Region) -> bytes:
    """Crop ``region`` out of a PNG and re-encode as PNG bytes."""
    img = imgops.decode_bgr(png)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    x, y, w, h = region
    encoded = imgops.encode_png(img[y : y + h, x : x + w])
    if encoded is None:
        raise ValueError("could not encode crop as PNG")
    return encoded


def _regions_intersect(a: Region, b: Region) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _best_crop_text(
    lines: list[OcrLine], click_y: Optional[int] = None
) -> Optional[str]:
    """Best label text for a target crop.

    OCR often splits one button label into several fragments (e.g. ``Save``
    + ``Encounter``); all confident fragments whose boxes vertically overlap
    the click row are joined left-to-right. Falls back to the single most
    confident line when no fragment overlaps ``click_y`` (or it is not
    given). Returns None if nothing confident was recognized.
    """
    confident = [
        l for l in lines if l.confidence >= MIN_OCR_CONFIDENCE and l.text.strip()
    ]
    if not confident:
        return None
    if click_y is not None:
        row = [
            l for l in confident if l.region[1] <= click_y <= l.region[1] + l.region[3]
        ]
        if row:
            row.sort(key=lambda l: l.region[0])
            return " ".join(l.text.strip() for l in row)
    best = max(confident, key=lambda l: l.confidence)
    return best.text.strip()


def _best_target_text(
    crop_lines: list[OcrLine],
    frame_lines: list[OcrLine],
    click: Point,
) -> Optional[str]:
    """Return the complete OCR label under ``click`` when one is retained.

    A fixed target crop can cut a wide control label even when the full-frame
    OCR engine retained the complete line. That truncation can remove a
    consequential verb such as ``Save`` or ``Submit``. Prefer the most
    confident complete full-frame line whose box contains the exact click
    point. Keep the crop's word boundaries when both OCR passes contain the
    same characters. Fall back to the crop when no complete line exists.
    """
    click_x, click_y = click
    crop_text = _best_crop_text(crop_lines, click_y=click_y)
    clicked_lines = [
        line
        for line in frame_lines
        if line.confidence >= MIN_OCR_CONFIDENCE
        and line.text.strip()
        and line.region[0] <= click_x <= line.region[0] + line.region[2]
        and line.region[1] <= click_y <= line.region[1] + line.region[3]
    ]
    if clicked_lines:
        best = max(clicked_lines, key=lambda line: line.confidence)
        frame_text = best.text.strip()
        if (
            crop_text
            and re.sub(r"\W+", "", crop_text).casefold()
            == re.sub(r"\W+", "", frame_text).casefold()
        ):
            return crop_text
        return frame_text
    return crop_text


def _action_scoped_target_line(
    frame_lines: list[OcrLine],
    marked_region: Region,
    *,
    frame_size: tuple[int, int],
    reference_date: Optional[date],
    click: Optional[Point] = None,
) -> Optional[tuple[Region, str]]:
    """Select one stable visual locator from an operator-marked action row.

    With ``click`` (a marked dialog), the line under the click, the control's
    own label, is the locator when it qualifies. A dialog's longest line is
    usually its body text, which says nothing about which button to press.

    A complete marked row is strong identity evidence, but it is often too
    wide for strict template matching after the row moves. Prefer one unique,
    stable OCR line inside the row as the visual template. Identifier-shaped
    text wins when available. The recorded click keeps its offset from this
    locator, so replay can find the row by its stable key and still actuate the
    demonstrated control anywhere else inside that row.
    """

    rx, ry, rw, rh = marked_region
    normalized_counts: Counter[str] = Counter(
        normalize_text(line.text.strip())
        for line in frame_lines
        if line.confidence >= MIN_OCR_CONFIDENCE and line.text.strip()
    )
    candidates: list[tuple[tuple[int, int, int, float], OcrLine, str]] = []
    for line in frame_lines:
        text = line.text.strip()
        if line.confidence < MIN_OCR_CONFIDENCE or not text:
            continue
        x, y, w, h = line.region
        center_x = x + w // 2
        center_y = y + h // 2
        if not (rx <= center_x < rx + rw and ry <= center_y < ry + rh):
            continue
        if volatility.is_volatile_line(text, reference_date=reference_date):
            continue
        normalized = normalize_text(text)
        if normalized_counts[normalized] != 1:
            continue
        compact = re.sub(r"\W+", "", text)
        if len(compact) < MIN_TEXT_PRESENT_LEN:
            continue
        has_alpha = any(char.isalpha() for char in compact)
        has_digit = any(char.isdigit() for char in compact)
        score = (
            int(has_alpha and has_digit),
            int(has_digit),
            len(compact),
            float(line.confidence),
        )
        candidates.append((score, line, text))
    if not candidates:
        return None

    if click is not None:
        under_click = [
            candidate
            for candidate in candidates
            if candidate[1].region[0]
            <= click[0]
            < candidate[1].region[0] + candidate[1].region[2]
            and candidate[1].region[1]
            <= click[1]
            < candidate[1].region[1] + candidate[1].region[3]
        ]
        if under_click:
            candidates = under_click
    _score, selected, text = max(candidates, key=lambda candidate: candidate[0])
    x, y, w, h = selected.region
    frame_w, frame_h = frame_size
    pad = 4
    x0 = max(0, rx, x - pad)
    y0 = max(0, ry, y - pad)
    x1 = min(frame_w, rx + rw, x + w + pad)
    y1 = min(frame_h, ry + rh, y + h + pad)
    return (x0, y0, x1 - x0, y1 - y0), text


def _landmarks_for(
    frame_lines: list[OcrLine],
    crop_region: Region,
    click: Point,
    *,
    exclude_texts: tuple[str, ...] = (),
    reference_date: Optional[date] = None,
) -> list[Landmark]:
    """Derive up to 2 landmarks from OCR lines outside the template crop.

    ``relation`` encodes where the *landmark* sits relative to the target
    (dominant axis of the landmark-center -> click-point vector): a click
    to the landmark's right yields ``left_of``. ``distance_px`` is the
    Euclidean distance between the two, and ``dx_px``/``dy_px`` carry the
    exact landmark-center -> click-point offsets so the geometry rung can
    reconstruct the target precisely (see ``ir.Landmark``).

    Landmark hygiene: a landmark is *stable geometry evidence*, so lines
    that embed a demo parameter value (``exclude_texts`` — the value varies
    per run, silently degrading healing for every real run) or classify as
    volatile (clock times, near dates, digit-dominated fragments) are never
    used as landmarks.
    """
    candidates = []
    for line in frame_lines:
        if line.confidence < MIN_OCR_CONFIDENCE or not line.text.strip():
            continue
        if _regions_intersect(line.region, crop_region):
            continue
        text = line.text.strip()
        if _contains_excluded(text, exclude_texts):
            continue
        if volatility.classify_text(text, reference_date=reference_date):
            continue
        if _text_carries_phi(text):
            # A landmark is nearby ROW text used by the geometry rung; on a
            # patient list that is often the name itself. When the optional
            # Presidio scrub detects an identifier, drop the landmark so no
            # patient name is mined into the bundle as geometry evidence (audit
            # REM-2). Geometry is a fallback rung and the identity gate still
            # disposes, so dropping a PHI landmark is safe (see docs/phi_at_rest).
            continue
        lx, ly, lw, lh = line.region
        cx, cy = lx + lw // 2, ly + lh // 2
        dx, dy = click[0] - cx, click[1] - cy
        dist = math.hypot(dx, dy)
        relation: Literal["left_of", "right_of", "above", "below"]
        if abs(dx) >= abs(dy):
            # Target is to the landmark's right -> the landmark is left_of it.
            relation = "left_of" if dx >= 0 else "right_of"
        else:
            relation = "above" if dy >= 0 else "below"
        candidates.append((dist, relation, line.text.strip(), dx, dy))
    candidates.sort(key=lambda c: c[0])
    return [
        Landmark(
            relation=rel,
            ocr_text=text,
            distance_px=int(round(dist)),
            dx_px=int(dx),
            dy_px=int(dy),
        )
        for dist, rel, text, dx, dy in candidates[:2]
    ]


def _largest_changed_region(before_png: bytes, after_png: bytes) -> Optional[Region]:
    """Bounding rect of the largest changed area between two frames.

    ``cv2.absdiff`` -> grayscale -> threshold ``DIFF_THRESHOLD`` -> dilate ->
    largest external contour's bounding rect. Returns None if nothing
    changed.
    """
    before = imgops.decode_bgr(before_png)
    after = imgops.decode_bgr(after_png)
    if before is None or after is None:
        return None
    if before.shape != after.shape:
        return (0, 0, after.shape[1], after.shape[0])
    diff = imgops.absdiff(before, after)
    gray = imgops.bgr_to_gray(diff)
    mask = imgops.threshold_binary(gray, DIFF_THRESHOLD, 255)
    mask = imgops.dilate_rect(mask, 9, iterations=2)
    largest = imgops.largest_external_region(mask)
    if largest is None:
        return None
    x, y, w, h = largest
    if w <= 0 or h <= 0:
        return None
    return (int(x), int(y), int(w), int(h))


def _param_free_band(
    region: Region, carriers: Sequence[OcrLine], lines: Sequence[OcrLine]
) -> Optional[Region]:
    """Best horizontal slice of ``region`` that no ``carriers`` row touches.

    Removing a parameter-contaminated region outright also removes the step's
    only screen-state gate. Screens lay out in rows, so the value's own row can
    usually be carved out and the rest of the mined region kept: for a save
    confirmation, carving the echoed note leaves the destination screen's own
    chrome, which is a sound invariant.

    Carving, not re-diffing: the changed area is normally one connected blob,
    so erasing the glyphs from the change mask would just leave a ring whose
    bounding rect still contains them.

    Each carrier's row is blocked with ``CARVE_ROW_PAD`` of margin (antialiasing
    and OCR box slop). A slice shorter than ``CARVE_MIN_HEIGHT`` is discarded --
    a sliver is glyph noise, not structure. Among the survivors the slice
    carrying the most readable text wins, height breaking ties: a slice holding
    the destination screen's heading discriminates far better than a taller
    slice of blank page, which would match any blank screen. Returns None when
    nothing survives, which drops the postcondition (the fail-safe direction).
    """
    x, y, w, h = region
    bottom = y + h
    blocked = sorted(
        (
            max(y, line.region[1] - CARVE_ROW_PAD),
            min(bottom, line.region[1] + line.region[3] + CARVE_ROW_PAD),
        )
        for line in carriers
        if line.region[1] - CARVE_ROW_PAD < bottom
        and line.region[1] + line.region[3] + CARVE_ROW_PAD > y
    )
    slices: list[tuple[int, int]] = []
    cursor = y
    for b0, b1 in blocked:
        if b0 > cursor:
            slices.append((cursor, b0))
        cursor = max(cursor, b1)
    if cursor < bottom:
        slices.append((cursor, bottom))
    tall = [s for s in slices if s[1] - s[0] >= CARVE_MIN_HEIGHT]
    if not tall:
        return None

    def readable(s: tuple[int, int]) -> int:
        return sum(
            1
            for line in lines
            if line.confidence >= MIN_OCR_CONFIDENCE
            and line.text.strip()
            and line.region[1] >= s[0]
            and line.region[1] + line.region[3] <= s[1]
        )

    top, end = max(tall, key=lambda s: (readable(s), s[1] - s[0]))
    return (x, top, w, end - top)


def _pad_region(region: Region, frame_w: int, frame_h: int) -> Region:
    """``region`` grown by ``REGION_STABLE_PAD``, clamped to the frame."""
    x, y, w, h = region
    x0 = max(0, x - REGION_STABLE_PAD)
    y0 = max(0, y - REGION_STABLE_PAD)
    x1 = min(frame_w, x + w + REGION_STABLE_PAD)
    y1 = min(frame_h, y + h + REGION_STABLE_PAD)
    return (x0, y0, x1 - x0, y1 - y0)


def _param_text_in_region(
    region: Region, lines: list[OcrLine], excluded: tuple[str, ...]
) -> tuple[list[OcrLine], Optional[str]]:
    """Confident lines inside ``region`` that render an excluded value.

    Detection is deliberately asymmetric to ``_contains_excluded``. A region is
    contaminated only when it renders (most of) an excluded value, measured
    over the region's lines JOINED -- OCR splits a rendered value across lines,
    and no single fragment need carry it. The reverse containment
    ``_contains_excluded`` also accepts (excluded value CONTAINS the text) must
    NOT disqualify a region: an application's own literal word is frequently a
    substring of a longer demonstrated value (MockMed's ``Triage`` inside the
    note ``E2E triage booking three months``), and treating that as
    contamination would disarm perfectly stable regions.

    Returns ``(carriers, evidence)``: the individual lines carrying any part of
    the value -- the rows ``_param_free_band`` must carve out -- and the joined
    text that proves the contamination, or ``(_, None)`` when the region is
    clean.
    """
    inside = [
        line
        for line in lines
        if line.confidence >= MIN_OCR_CONFIDENCE
        and line.text.strip()
        and _regions_intersect(line.region, region)
    ]
    if not inside:
        return [], None
    joined = " ".join(line.text.strip() for line in inside)
    hay = _squash(joined)
    for raw in excluded:
        ex = _squash(raw)
        if len(ex) < MIN_EXCLUDE_CHARS:
            continue
        if coverage(ex, hay) < EXCLUDE_CONTAINMENT_RATIO:
            continue
        carriers = [
            line for line in inside if _contains_excluded(line.text, (raw,))
        ] or inside
        return carriers, joined
    return [], None


def _squash(text: str) -> str:
    """Lowercase and remove ALL whitespace (OCR-tolerant comparison key)."""
    return "".join(normalize_text(text).split())


def _contains_excluded(text: str, excluded: tuple[str, ...]) -> bool:
    """True if ``text`` fuzzily contains (or is contained by) an excluded value.

    OCR mangles whitespace and the odd character, so exact substring checks
    miss e.g. the typed note inside a "Encounter saved — <note>" banner.
    Both sides are squashed (lowercased, whitespace removed) and an excluded
    value counts as present when at least ``EXCLUDE_CONTAINMENT_RATIO`` of
    its characters match ``text`` via difflib matching blocks.

    Args:
        text: A TEXT_PRESENT candidate (raw OCR line text).
        excluded: Excluded (parameterized) values, raw.

    Returns:
        True when the candidate must not be asserted.
    """
    hay = _squash(text)
    if not hay:
        return False
    for raw in excluded:
        ex = _squash(raw)
        if len(ex) < MIN_EXCLUDE_CHARS:
            continue
        if ex in hay or hay in ex:
            return True
        matcher = difflib.SequenceMatcher(None, ex, hay)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        if matched / len(ex) >= EXCLUDE_CONTAINMENT_RATIO:
            return True
    return False


def _matches_label(text: str, labels: tuple[str, ...]) -> bool:
    """True if ``text`` whole-line fuzzy-matches a click-target label."""
    sq = _squash(text)
    if not sq:
        return False
    for label in labels:
        lsq = _squash(label)
        if not lsq:
            continue
        if difflib.SequenceMatcher(None, sq, lsq).ratio() >= LABEL_MATCH_RATIO:
            return True
    return False


def _text_carries_phi(text: str) -> bool:
    """Whether the optional Presidio pass flags this text as carrying PII/PHI.

    Wires openadapt-privacy as an OPTIONAL dependency (audit REM-2 / GAP-3):
    when text scrubbing is active (the ``privacy`` extra is installed and
    ``OPENADAPT_FLOW_SCRUB`` is ``auto``/``on``) a scrub that CHANGES the text
    means an identifier was found. Graceful fallback: when the extra is absent
    under ``auto`` this returns False (no crash — the governance guard blocks
    committing any residual plaintext identifiers instead); under ``on`` the
    privacy module fails closed (raises) exactly as elsewhere. Import is lazy so
    the compiler core never pulls in Presidio/spaCy.
    """
    if not text or not text.strip():
        return False
    from openadapt_flow import privacy

    if not privacy.text_scrubbing_enabled():
        return False
    scrubbed = privacy.scrub_text(text)
    return bool(scrubbed) and scrubbed != text


def _new_text_postcondition(
    before_lines: list[OcrLine],
    after_lines: list[OcrLine],
    *,
    exclude_texts: tuple[str, ...] = (),
    avoid_labels: tuple[str, ...] = (),
    next_lines: Optional[list[OcrLine]] = None,
    reference_date: Optional[date] = None,
    click_point: Optional[Point] = None,
) -> Optional[Postcondition]:
    """TEXT_PRESENT for the most STABLE new OCR text after the action.

    Selection is for stability, not novelty ("longest new text" latched
    onto clock fragments, counters, and other patients' rows — see
    docs/validation/VALIDATION.md):

    1. Candidates must be new (not in the before frame), confidently read,
       not a parameterized value, and not a click-target label.
    2. Volatile candidates are rejected (clock times, dates near the
       recording date, digit-dominated fragments, low-entropy noise); a
       date FAR from the recording date — a DOB in an identity banner — is
       deliberately eligible.
    3. Candidates must persist into the NEXT step's before frame (two
       captures of the same screen, moments apart) when one exists: text
       that already vanished within the demonstration cannot anchor a
       replay.
    4. Ranking prefers alphabetic content over raw length, with a
       proximity tiebreak toward the click target among competitive
       candidates (text near the action is likelier to describe its
       effect than a distant data row).

    Args:
        before_lines: OCR lines from the before frame.
        after_lines: OCR lines from the after frame.
        exclude_texts: Texts that must NOT be asserted (parameterized typed
            values, which vary per run — including any screen text that
            embeds them, e.g. a save-confirmation banner).
        avoid_labels: Click-target label texts (anchor ``ocr_text`` values)
            that must not be asserted: labels are mutable evidence the
            resolution ladder heals through (rename drift), not invariants.
        next_lines: OCR lines from the NEXT event's before frame (same
            screen state, captured moments later), or None when this is the
            recording's last frame.
        reference_date: The recording date (enables the near/far date
            split; None treats every date as volatile).
        click_point: The step's click point, for the proximity tiebreak.

    Returns:
        A TEXT_PRESENT postcondition, or None if there is no suitable
        stable new text.
    """
    before_norm = {normalize_text(l.text) for l in before_lines}
    before_squashed = [_squash(l.text) for l in before_lines if l.text.strip()]

    def seen_before(text: str, norm: str) -> bool:
        if norm in before_norm:
            return True
        sq = _squash(text)
        for prior in before_squashed:
            if sq == prior:
                return True
            ratio = difflib.SequenceMatcher(None, sq, prior).ratio()
            if ratio >= SEEN_BEFORE_RATIO:
                return True
        return False

    next_hay: Optional[str] = None
    if next_lines is not None:
        next_hay = _squash(" ".join(l.text for l in next_lines))

    candidates: list[tuple[OcrLine, int]] = []
    for line in after_lines:
        if line.confidence < MIN_OCR_CONFIDENCE:
            continue
        text = line.text.strip()
        norm = normalize_text(text)
        if len(norm) < MIN_TEXT_PRESENT_LEN or seen_before(text, norm):
            continue
        if volatility.classify_text(text, reference_date=reference_date):
            continue
        if _contains_excluded(text, exclude_texts):
            continue
        if _matches_label(text, avoid_labels):
            continue
        if _text_carries_phi(text):
            # PHI scrub on the compile path (audit REM-2 / GAP-3): when the
            # optional openadapt-privacy (Presidio) pass detects a patient
            # identifier in a candidate assertion, that candidate is DROPPED so
            # no name / DOB / MRN is mined into ``expect[].text``. A scrubbed
            # placeholder is NOT substituted — the live screen shows the real
            # value, so a scrubbed assertion would only fail at replay; the
            # right move is to not assert on identifier text at all.
            continue
        if next_hay is not None:
            if coverage(_squash(text), next_hay) < PERSISTENCE_RATIO:
                continue  # gone within the demo itself: ephemeral
        alpha = sum(1 for c in _squash(text) if c.isalpha())
        candidates.append((line, alpha))
    if not candidates:
        return None
    best_alpha = max(alpha for _, alpha in candidates)
    pool = [
        (line, alpha)
        for line, alpha in candidates
        if alpha >= PROXIMITY_POOL_RATIO * best_alpha
    ]
    if click_point is not None and len(pool) > 1:

        def dist(line: OcrLine) -> float:
            lx, ly, lw, lh = line.region
            return math.hypot(
                lx + lw / 2 - click_point[0], ly + lh / 2 - click_point[1]
            )

        chosen = min(pool, key=lambda c: dist(c[0]))[0]
    else:
        chosen = max(pool, key=lambda c: c[1])[0]
    return Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=chosen.text.strip())


def _values_to_params(params: Mapping[str, str]) -> dict[str, list[str]]:
    by_value: dict[str, list[str]] = {}
    for name, value in params.items():
        if value:
            by_value.setdefault(value, []).append(name)
    return by_value


def _postconditions(
    before_png: Optional[bytes],
    after_png: Optional[bytes],
    *,
    exclude_texts: tuple[str, ...] = (),
    avoid_labels: tuple[str, ...] = (),
    bundle: Optional[Path] = None,
    step_id: Optional[str] = None,
    include_region_stable: bool = True,
    before_lines: Optional[list[OcrLine]] = None,
    after_lines: Optional[list[OcrLine]] = None,
    next_lines: Optional[list[OcrLine]] = None,
    next_before_png: Optional[bytes] = None,
    reference_date: Optional[date] = None,
    click_point: Optional[Point] = None,
    dropped_param_regions: Optional[list[tuple[Region, str]]] = None,
) -> list[Postcondition]:
    """Derive postconditions from a step's before/after frames.

    When ``bundle`` and ``step_id`` are given, the REGION_STABLE
    postcondition also carries a template crop of the expected region
    content (``templates/<step_id>_expect.png``): real apps re-layout by a
    few pixels between runs (auto-scrolling panes, banner heights), which a
    fixed-position phash cannot tolerate — the replayer first looks for the
    expected content NEAR the recorded region and only then falls back to
    the exact-position hash.

    ``include_region_stable=False`` skips the diff-based REGION_STABLE
    entirely: for parameterized TYPE steps the changed region IS the typed
    value's pixels, and the value varies per run — asserting its rendering
    is the pixel-level equivalent of asserting the excluded text.

    That flag only covers the TYPE step that *enters* a parameter. A LATER
    step's changed region can render the SAME value — a save click whose diff
    is the confirmation banner and the new record row, both echoing the typed
    note — and freezing those pixels is exactly as demo-bound. Such a region
    is screened here against ``exclude_texts`` using the frame OCR already
    computed for ``TEXT_PRESENT`` (see ``_param_text_in_region``). A
    contaminated region is first NARROWED — the value's own row is carved out
    (``_param_free_band``), which usually keeps a sound invariant built from
    the destination screen's own chrome. Only when nothing usable survives is
    the postcondition dropped, and that ``(region, evidence)`` is appended to
    ``dropped_param_regions`` when the caller supplies a sink, so the drop is
    reported rather than silent.

    ``next_before_png`` / ``next_lines`` — the NEXT event's before frame
    (the same screen state captured moments later) and its OCR — arm the
    empirical stability checks: a REGION_STABLE region that already changed
    between the two captures (animations, clocks, fading toasts) is
    self-mutating and is not asserted, and TEXT_PRESENT candidates must
    persist into the later capture.

    ``before_lines`` / ``after_lines`` let the caller supply cached OCR;
    when omitted the frames are OCRed here.
    """
    if before_png is None or after_png is None:
        return []
    if before_lines is None:
        before_lines = ocr(before_png)
    if after_lines is None:
        after_lines = ocr(after_png)
    if next_lines is None and next_before_png is not None:
        next_lines = ocr(next_before_png)
    expect: list[Postcondition] = []
    changed = (
        _largest_changed_region(before_png, after_png)
        if include_region_stable
        else None
    )
    padded: Optional[Region] = None
    if changed is not None:
        # ``after_png`` was already decoded successfully upstream (it produced
        # ``changed``), so the re-decode here is known-valid; cv2's stub types
        # the result as Optional, hence the cast.
        after = cast(
            np.ndarray,
            imgops.decode_bgr(after_png),
        )
        frame_h, frame_w = after.shape[:2]
        padded = _pad_region(changed, frame_w, frame_h)
        if exclude_texts:
            # Parameter hygiene at the PIXEL level. A region that RENDERS a
            # parameter's demonstrated value is run-specific data, not a stable
            # property of the application: freezing its phash/template halts
            # every run whose value differs, decided by the glyphs of that
            # run's value.
            #
            # Removing the postcondition would also remove the step's only
            # screen-state gate, so first CARVE the value's row out of the
            # region: what remains is normally the destination screen's own
            # chrome, which is a sound invariant. Only when nothing usable
            # survives is the postcondition dropped -- the fail-safe
            # direction, since an unverified step costs one missing check
            # while a contaminated one false-halts every differing run.
            carriers, evidence = _param_text_in_region(
                padded, after_lines, exclude_texts
            )
            if evidence is not None:
                contaminated = padded
                band = _param_free_band(padded, carriers, after_lines)
                if (
                    band is not None
                    and _param_text_in_region(band, after_lines, exclude_texts)[1]
                    is None
                ):
                    padded = band
                else:
                    changed, padded = None, None
                    if dropped_param_regions is not None:
                        dropped_param_regions.append((contaminated, evidence))
    if changed is not None and padded is not None:
        if (
            next_before_png is not None
            and phash_distance(
                phash_png(after_png, region=padded),
                phash_png(next_before_png, region=padded),
            )
            > REGION_STABLE_TOLERANCE
        ):
            # The region kept changing with NO action in between — it is
            # self-mutating (animation, clock, fading toast) and would
            # false-halt any replay; never assert it.
            changed = None
    if changed is not None and padded is not None:
        template_rel: Optional[str] = None
        if bundle is not None and step_id is not None:
            template_rel = f"templates/{step_id}_expect.png"
            _sealed.write_bytes(bundle / template_rel, _crop_png(after_png, padded))
        expect.append(
            Postcondition(
                kind=PostconditionKind.REGION_STABLE,
                region=padded,
                phash=phash_png(after_png, region=padded),
                phash_tolerance=REGION_STABLE_TOLERANCE,
                template=template_rel,
            )
        )
    text_pc = _new_text_postcondition(
        before_lines,
        after_lines,
        exclude_texts=exclude_texts,
        avoid_labels=avoid_labels,
        next_lines=next_lines,
        reference_date=reference_date,
        click_point=click_point,
    )
    if text_pc is not None:
        expect.append(text_pc)
    return expect


def _structural_postconditions(event: dict) -> list[Postcondition]:
    """Structural fallback postconditions from a recorded event.

    Used only for steps that mined ZERO visual postconditions (identical
    before/after frames, or every candidate rejected as volatile): when the
    recorder captured structural observations (see ``Recorder`` /
    ``StructuralBackend``) and one of them changed across the action, the
    change itself — never the literal, instance-specific value — is
    asserted, so the step can actually fail at replay instead of passing
    vacuously.
    """
    pcs: list[Postcondition] = []
    pages_before, pages_after = event.get("pages_before"), event.get("pages_after")
    if (
        isinstance(pages_before, int)
        and isinstance(pages_after, int)
        and pages_after > pages_before
    ):
        pcs.append(Postcondition(kind=PostconditionKind.NEW_TAB_OPENED))
    url_before, url_after = event.get("url_before"), event.get("url_after")
    title_before = event.get("title_before")
    title_after = event.get("title_after")
    if url_before and url_after and url_before != url_after:
        pcs.append(Postcondition(kind=PostconditionKind.URL_CHANGED))
    elif title_before and title_after and title_before != title_after:
        pcs.append(Postcondition(kind=PostconditionKind.TITLE_CHANGED))
    return pcs


def lint_param_leakage(workflow: Workflow, param_values: tuple[str, ...]) -> list[str]:
    """Scan a compiled workflow for demo parameter values baked in as
    literals outside the designated parameter slots.

    A parameter's demonstrated value must never become an invariant: baked
    into a postcondition it false-halts every run whose value differs from
    the demo's; baked into a geometry landmark it silently degrades healing
    the same way. Designated slots where the demo value IS allowed:

    - ``workflow.params`` and ``step.text`` of a parameterized TYPE step
      (the recorded example/default, substituted per run);
    - ``anchor.ocr_text`` / ``anchor.context_text`` (resolution and
      identity evidence: the identity check detects an embedded demo value
      and re-anchors on the RUN's value at replay — see runtime.identity
      param mode).

    Values shorter than ``MIN_EXCLUDE_CHARS`` squashed characters are
    skipped (they fuzzily match everything).

    Scope: this lint reads the compiled workflow, so it sees values baked in
    as TEXT. The same class can also be encoded as PIXELS — a REGION_STABLE
    region that renders the value — which no amount of workflow inspection can
    detect once the region is a phash. That case is screened during mining in
    ``_postconditions`` (which still has the frames and their OCR) and reported
    by ``compile_recording`` through ``param_hygiene.json``; see the
    "Parameter-hygiene NOTICES" block there.

    Returns:
        Human-readable violation strings (empty when clean).
    """
    values = tuple(v for v in param_values if len(_squash(v)) >= MIN_EXCLUDE_CHARS)
    if not values:
        return []
    violations: list[str] = []
    for step in workflow.steps:
        for pc in step.expect:
            if pc.text and _contains_excluded(pc.text, values):
                violations.append(
                    f"{step.id}: {pc.kind.value} postcondition "
                    f"{pc.text!r} embeds a demo parameter value"
                )
        if step.anchor is not None:
            for lm in step.anchor.landmarks:
                if _contains_excluded(lm.ocr_text, values):
                    violations.append(
                        f"{step.id}: geometry landmark {lm.ocr_text!r} "
                        "embeds a demo parameter value"
                    )
    return violations


def _text_preview(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Nearby-OCR field-label fallback (used by the pass-1 TYPE branch when the
# event carries a field_rect but no recorded field_label): a form label sits
# LEFT of or ABOVE its field, close by. Bounds keep the fallback conservative
# (a distant heading must not become a parameter name).
LABEL_OCR_MAX_LEFT_GAP_PX = 220
LABEL_OCR_MAX_ABOVE_GAP_PX = 90
LABEL_OCR_MAX_CHARS = 80


def _field_label_from_ocr(
    lines: list[OcrLine], rect: tuple[int, int, int, int]
) -> Optional[str]:
    """Nearest OCR line left of / above a field rect -- the form-label heuristic.

    LAST-RESORT label evidence for substrates with no DOM/a11y label seam:
    given the typed field's on-screen rectangle (recorded as ``field_rect``),
    pick the closest OCR line that sits where a form label sits -- immediately
    LEFT of the field (vertically overlapping) or immediately ABOVE it
    (horizontally overlapping or left-aligned). Lines inside the rect (the
    field's own contents), too far away, or empty are rejected; ties go to
    the smaller gap, with LEFT preferred over ABOVE at equal distance.
    Deterministic, uses the already-cached OCR of the BEFORE frame (no extra
    model or engine calls beyond the frame's one OCR pass).
    """
    rx, ry, rw, rh = (int(v) for v in rect)
    if rw <= 0 or rh <= 0:
        return None
    best: Optional[tuple[float, int, str]] = None  # (gap, tiebreak, text)
    for line in lines:
        text = " ".join(line.text.split())
        if not text or len(text) > LABEL_OCR_MAX_CHARS:
            continue
        lx, ly, lw, lh = (int(v) for v in line.region)
        cx, cy = lx + lw / 2, ly + lh / 2
        if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
            continue  # inside the field: its value, not its label
        v_overlap = min(ly + lh, ry + rh) - max(ly, ry)
        h_overlap = min(lx + lw, rx + rw) - max(lx, rx)
        left_gap = rx - (lx + lw)
        above_gap = ry - (ly + lh)
        if v_overlap > 0 and 0 <= left_gap <= LABEL_OCR_MAX_LEFT_GAP_PX:
            candidate = (float(left_gap), 0, text)
        elif (h_overlap > 0 or abs(lx - rx) <= 40) and (
            0 <= above_gap <= LABEL_OCR_MAX_ABOVE_GAP_PX
        ):
            candidate = (float(above_gap), 1, text)
        else:
            continue
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best is not None else None


def _exact_left_field_label_landmark(
    lines: list[OcrLine],
    target_region: Region,
    click: Point,
    *,
    exclude_texts: tuple[str, ...] = (),
    reference_date: Optional[date] = None,
) -> Optional[tuple[str, Landmark]]:
    """Return one unique, row-aligned label left of an opaque field.

    An open native select can repeat its current option in the closed field,
    popup, and suggestion list. The option text is then not target identity.
    Pixel-only recordings need an independent relation that survives the open
    menu. This function mines only a label whose normalized OCR text occurs
    exactly once in the recorded frame, whose box is left of and vertically
    aligned with the qualified field region, and whose text is stable and
    passes the configured exclusions, volatility classification, and identifier
    heuristic. It records an exact normalized OCR match and the exact
    label-center-to-click offset. There is no fuzzy fallback in this contract.
    """

    rx, ry, rw, rh = (int(value) for value in target_region)
    if rw <= 0 or rh <= 0:
        return None
    recognized = [line for line in lines if normalize_text(line.text)]
    counts = Counter(normalize_text(line.text) for line in recognized)
    candidates: list[tuple[float, float, str, Landmark]] = []
    for line in recognized:
        if line.confidence < MIN_OCR_CONFIDENCE:
            continue
        text = " ".join(line.text.split())
        normalized = normalize_text(text)
        if counts[normalized] != 1 or len(text) > LABEL_OCR_MAX_CHARS:
            continue
        if _contains_excluded(text, exclude_texts):
            continue
        if volatility.classify_text(text, reference_date=reference_date):
            continue
        if _text_carries_phi(text):
            continue
        lx, ly, lw, lh = (int(value) for value in line.region)
        if lw <= 0 or lh <= 0 or lx + lw > rx:
            continue
        if not ly <= click[1] <= ly + lh:
            continue
        gap = float(rx - (lx + lw))
        if gap > LABEL_OCR_MAX_LEFT_GAP_PX:
            continue
        center = (lx + lw // 2, ly + lh // 2)
        dx = int(click[0] - center[0])
        dy = int(click[1] - center[1])
        if dx <= 0:
            continue
        candidates.append(
            (
                gap,
                abs(float(dy)),
                text,
                Landmark(
                    relation="left_of",
                    ocr_text=text,
                    distance_px=int(round(math.hypot(dx, dy))),
                    match_mode="exact",
                    dx_px=dx,
                    dy_px=dy,
                ),
            )
        )
    if not candidates:
        return None
    _gap, _vertical_delta, text, landmark = min(
        candidates,
        key=lambda candidate: (candidate[1], candidate[0], candidate[2].casefold()),
    )
    return text, landmark


def _identity_unarmed_reason(
    frame_lines: list[OcrLine],
    *,
    band,
    exclude_region,
) -> str:
    """Human-readable reason a click step compiled with NO identity band.

    Mirrors the filters of ``context_from_lines`` to name which one left
    the band empty — surfaced in the bundle (``Step.identity_unarmed_
    reason``) and in every run report so unguarded clicks are auditable.
    """
    _, band_y, _, band_h = band
    in_band = []
    for line in frame_lines:
        text = (getattr(line, "text", "") or "").strip()
        if not text or line.confidence < MIN_OCR_CONFIDENCE:
            continue
        _, ly, _, lh = line.region
        if band_y <= ly + lh // 2 < band_y + band_h:
            in_band.append(line)
    if not in_band:
        return (
            "no readable text in the target's row band at compile time "
            "(icon-only or unlabeled row)"
        )
    ex_x, ex_y, ex_w, ex_h = exclude_region
    outside_crop = [
        line
        for line in in_band
        if not (
            line.region[0] < ex_x + ex_w
            and ex_x < line.region[0] + line.region[2]
            and line.region[1] < ex_y + ex_h
            and ex_y < line.region[1] + line.region[3]
        )
    ]
    if not outside_crop:
        return (
            "the only readable row text is the target's own label "
            "(mutable evidence, excluded from identity)"
        )
    return (
        "row text outside the target's label is too generic "
        "(< 12 squashed chars after volatile-line filtering)"
    )


# -- pixel identifier-crop emission ------------------------------------------
#
# Degrade-reason taxonomy for ``Step.identifier_crop_missing_reason``: every
# identity-applicable click that compiles WITHOUT a pixel identifier crop
# records WHY (never a silent gap), mirroring ``identity_unarmed_reason``.
IDCROP_REASON_STRUCTURED = (
    "structured identity (DOM/UIA/AX) owns this step's identity; no identifier "
    "pixels persisted at rest — mark the identifier field/region at record "
    "time (--identifier) to also arm the pixel tier for remote-display replays"
)
IDCROP_REASON_UNARMED = (
    "identity is not armed on this step (see identity_unarmed_reason) — "
    "there is no identity evidence to crop"
)
IDCROP_REASON_NO_BAND_REGION = (
    "the OCR identity band kept no lines to bound a crop from at compile time"
)
IDCROP_REASON_MARKED_INVALID = (
    "the marked identifier region is empty after clamping to the recorded "
    "frame (degenerate or fully outside the frame)"
)

#: Bundle subdirectory for compiler-emitted identifier crops. Deliberately
#: under ``templates/`` so the crops — pixels of the record-identifying region,
#: PHI by construction — ride the SAME at-rest handling as every other image
#: crop: hashed into the integrity manifest (bundle_validation), sealed to
#: ``.enc`` in an encrypted bundle (ir._seal_template_assets, TEMPLATE_AAD),
#: and surfaced by the run gate's cleartext-asset check (run_gate gate 5).
IDENTIFIER_CROP_DIR = "templates/identifiers"


def _clamp_to_frame(region: Region, frame_size: tuple[int, int]) -> Optional[Region]:
    """Intersect ``region`` with the frame; None when nothing remains."""
    fw, fh = frame_size
    x, y, w, h = region
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(fw, int(x) + int(w)), min(fh, int(y) + int(h))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _marked_identifier_region(value: object, *, source: str) -> Optional[Region]:
    """Validate an explicitly marked identifier region from the recording.

    ``value`` comes from a click event's ``identifier_region`` (web recorder
    ``--identifier FIELD`` rect) or ``meta.json``'s ``identifier_region``
    (desktop ``--identifier X,Y,W,H``). Malformed shapes fail LOUDLY — an
    operator who marked the identifying region must not get a silently
    unmarked bundle.
    """
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        )
    ):
        raise ValueError(
            f"malformed identifier_region in {source}: expected [x, y, w, h] "
            f"integers, got {value!r}"
        )
    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))


HOVER_CROP_DIR = "templates/hover"


def _hover_evidence(
    bundle: Path,
    step_id: str,
    before_png: bytes,
    hover_path: Path,
    click: Point,
    *,
    dwell_ms: object = None,
) -> Optional[HoverEvidence]:
    """The recorded hover response for a click with no record identity.

    The recorder saved ``frames/{i}_hover.png`` after resting the pointer on
    the target (``Recorder(capture_hover=True)``). What changed near the
    target between the before frame and that frame is the response
    (:func:`openadapt_flow.runtime.hover_evidence.measure_response`); its
    pixels land under ``templates/hover/`` so they are sealed with the other
    image crops. None when there is no hover frame, nothing near the target
    changed, or the frames cannot be compared.
    """
    from openadapt_flow.runtime import hover_evidence as hover_mod

    if not _sealed.is_file(hover_path):
        return None
    hover_png = _sealed.read_bytes(hover_path)
    try:
        response = hover_mod.measure_response(before_png, hover_png, click)
    except ValueError:
        logger.warning("hover frame for %s differs in size; no hover evidence", step_id)
        return None
    dwell = (
        int(dwell_ms)
        if isinstance(dwell_ms, int) and 0 <= dwell_ms <= 5000
        else hover_mod.HOVER_DWELL_MS
    )
    if response is None:
        return None  # nothing answered the pointer: no evidence to keep
    crop_rel = f"{HOVER_CROP_DIR}/{step_id}.png"
    (bundle / HOVER_CROP_DIR).mkdir(parents=True, exist_ok=True)
    (bundle / crop_rel).write_bytes(response.crop)
    return HoverEvidence(
        response=True,
        region=hover_mod.relative(response.region, click),
        crop=crop_rel,
        dwell_ms=dwell,
    )


def _emit_identifier_crop(
    bundle: Path,
    step_id: str,
    before_png: bytes,
    *,
    frame_size: tuple[int, int],
    marked_region: Optional[Region],
    context_text: Optional[str],
    structured_identity: Optional[str],
    frame_lines: list[OcrLine],
    crop_region: Region,
    click: Point,
    reference_date: Optional[date],
) -> tuple[Optional[str], Optional[Region], Optional[str]]:
    """Emit the pixel identifier crop for one click step, or say why not.

    Returns ``(crop_rel, region, missing_reason)`` — exactly one of
    ``crop_rel`` / ``missing_reason`` is set. Source precedence:

    1. an EXPLICITLY MARKED region (record-time ``--identifier``) wins and is
       honored even when structured identity was captured (the operator's
       stated intent: also arm the pixel tier, e.g. for a bundle recorded on a
       structured substrate but replayed over Citrix/RDP);
    2. otherwise, when structured identity was captured, NO crop is written
       (the structured tier owns identity; no identifier pixels at rest);
    3. otherwise, when the OCR identity band armed identity, the crop is the
       tight bounding box of the surviving band lines
       (:func:`~openadapt_flow.runtime.identity.context_region_from_lines`) —
       the automatic pixel-substrate path that makes
       ``verify_pixel_identity`` reachable on Citrix/RDP recordings;
    4. otherwise identity is unarmed and there is nothing to crop.

    The crop lands in :data:`IDENTIFIER_CROP_DIR` (under ``templates/``) so it
    is sealed/hashed with the other image crops. With the default config
    (``identity.PIXEL_VERIFY_ENABLED`` is False) arming the pixel tier is
    MISMATCH-or-ABSTAIN only, so a crop can only add a safe HALT on a wrong
    identifier — never a pixel false-accept. The jitter-robust positive VERIFY
    path exists but is opt-in per risk class (benchmark/pixel_identity_aligned,
    docs/LIMITS.md).
    """
    region: Optional[Region] = None
    marked_invalid = False
    if marked_region is not None:
        region = _clamp_to_frame(marked_region, frame_size)
        if region is None:
            marked_invalid = True
            logger.warning(
                "identifier-crop %s: marked region %s is outside the recorded "
                "frame %s; falling back to the automatic identity band",
                step_id,
                marked_region,
                frame_size,
            )
    if region is None:
        if structured_identity is not None and not marked_invalid:
            return None, None, IDCROP_REASON_STRUCTURED
        if context_text is None and structured_identity is None:
            reason = IDCROP_REASON_UNARMED
            if marked_invalid:
                reason = f"{IDCROP_REASON_MARKED_INVALID}; {reason}"
            return None, None, reason
        band_box = context_region_from_lines(
            frame_lines,
            exclude_region=crop_region,
            band=band_region(click, crop_region[3], frame_size),
            point=click,
            min_confidence=MIN_OCR_CONFIDENCE,
            reference_date=reference_date,
        )
        region = _clamp_to_frame(band_box, frame_size) if band_box else None
        if region is None:
            if structured_identity is not None:
                # Marked region invalid AND structured present: fall back to
                # the structured tier owning identity (band may be empty).
                return (
                    None,
                    None,
                    (f"{IDCROP_REASON_MARKED_INVALID}; {IDCROP_REASON_STRUCTURED}"),
                )
            reason = IDCROP_REASON_NO_BAND_REGION
            if marked_invalid:
                reason = f"{IDCROP_REASON_MARKED_INVALID}; {reason}"
            return None, None, reason
    crop_rel = f"{IDENTIFIER_CROP_DIR}/{step_id}.png"
    (bundle / IDENTIFIER_CROP_DIR).mkdir(parents=True, exist_ok=True)
    _sealed.write_bytes(bundle / crop_rel, _crop_png(before_png, region))
    return crop_rel, region, None


def _surface_binding_from_meta(
    meta: dict,
    backend_hints: Optional[BackendHints],
    target_surface: Optional[ExecutionTargetKind] = None,
) -> tuple[Optional[ExecutionTargetKind], Optional[ExecutionMode]]:
    """Resolve the recording's surface binding (roadmap Section 5).

    The recorder stamps the exact surface the demonstration was captured on
    (``meta.json`` ``surface``); the compiled bundle carries it, plus the
    execution mode that surface implies, so a workflow qualified on one
    surface refuses to run on another without an explicit, report-recorded
    override. A legacy recording without the stamp compiles to an unbound
    bundle (surface None), except that an rdp/citrix recording's
    ``backend_hints`` already names its pixel surface exactly.

    Raises:
        ValueError: On an unknown surface token, or a ``surface`` that
            contradicts ``backend_hints.backend`` (a recording captures
            exactly one execution surface).
    """
    surface_raw = meta.get("surface")
    if surface_raw is None and backend_hints is not None:
        surface_raw = backend_hints.backend
    if target_surface is not None:
        if surface_raw is not None and surface_raw != target_surface:
            raise ValueError(
                "explicit target_surface contradicts the recording's surface; "
                "a recording captures exactly one execution surface"
            )
        surface_raw = target_surface
    if surface_raw is None:
        return None, None
    if surface_raw not in ("web", "windows", "macos", "linux", "rdp", "citrix"):
        raise ValueError(
            "meta.json surface must be one of web/windows/macos/linux/rdp/citrix"
        )
    surface = cast(ExecutionTargetKind, surface_raw)
    if backend_hints is not None and backend_hints.backend != surface:
        raise ValueError(
            "meta.json surface contradicts backend_hints.backend; a "
            "recording captures exactly one execution surface"
        )
    from openadapt_flow.surface_selection import execution_mode_for_surface

    return surface, execution_mode_for_surface(surface)


# -- multi-surface recordings (docs/MULTI_SURFACE.md) --------------------------

#: Parameter values shorter than this are not generalized out of a URL/title
#: (a two-letter value would match half the alphabet).
_SURFACE_PARAM_MIN_CHARS = 3


def _surface_url_pattern(url: str, exclude_texts: tuple[str, ...]) -> str:
    """Full-match regex for a recorded page URL.

    Scheme, host, and port are exact. Each path segment is exact unless it
    carries a demonstrated parameter value or looks like an identifier (a
    digit, as in ``/patient/12345``, or a long opaque token); such a segment
    becomes one path segment of any value, so the bundle never seals a
    record identifier. The query and fragment are not asserted: they carry
    tokens (OAuth ``state``) and record identifiers that change on every run.
    """

    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    values = [v for v in exclude_texts if len(v.strip()) >= _SURFACE_PARAM_MIN_CHARS]
    segments = []
    for segment in (parts.path or "/").split("/"):
        decoded = unquote(segment)
        if _segment_is_identifier(decoded) or any(
            value in segment or value in decoded for value in values
        ):
            segments.append("[^/?#]+")
        else:
            segments.append(re.escape(segment))
    origin = re.escape(f"{parts.scheme}://{parts.netloc}".lower())
    return origin + "/".join(segments) + r"(?:[?#].*)?"


#: An API version marker (``v2``) is structure, not an identifier.
_VERSION_SEGMENT_RE = re.compile(r"[vV]\d{1,2}")
#: A long opaque token (a session id or hash) even without a digit. A slug
#: with separators (``order-history``) stays exact.
_OPAQUE_SEGMENT_RE = re.compile(r"[A-Za-z0-9]{24,}")


def _segment_is_identifier(segment: str) -> bool:
    if not segment or _VERSION_SEGMENT_RE.fullmatch(segment):
        return False
    return bool(re.search(r"\d", segment) or _OPAQUE_SEGMENT_RE.fullmatch(segment))


def _surface_title_pattern(
    title: object, exclude_texts: tuple[str, ...]
) -> Optional[str]:
    """Full-match regex for a recorded page title, or None to not assert it.

    A demonstrated parameter value becomes "any text". A title the privacy
    pass flags as carrying an identifier is not asserted at all.
    """

    if not isinstance(title, str) or not title.strip():
        return None
    if _text_carries_phi(title):
        return None
    values = sorted(
        (v for v in exclude_texts if len(v.strip()) >= _SURFACE_PARAM_MIN_CHARS),
        key=len,
        reverse=True,
    )
    if not values:
        return re.escape(title)
    splitter = re.compile("|".join(re.escape(v) for v in values))
    pieces = splitter.split(title)
    return ".+".join(re.escape(piece) for piece in pieces)


def _dialog_identity(raw: dict, exclude_texts: tuple[str, ...]) -> DialogResponse:
    """The sealed identity of a recorded browser dialog.

    A message with no demonstrated parameter value and no digits is kept
    exactly. Otherwise it becomes a full-match pattern: each parameter value
    matches any text and each digit run matches any digits, so a record
    number, a date, or a typed value is never sealed into the bundle. A
    message the privacy pass still flags after that is refused: the operator
    must make the value a parameter.
    """

    response = raw.get("response")
    message = str(raw.get("message") or "")
    values = sorted(
        (v for v in exclude_texts if len(v.strip()) >= _SURFACE_PARAM_MIN_CHARS),
        key=len,
        reverse=True,
    )
    pieces = (
        re.compile("|".join(re.escape(v) for v in values)).split(message)
        if values
        else [message]
    )
    literal = " ".join(re.sub(r"\d+", "0", piece) for piece in pieces)
    if _text_carries_phi(literal):
        raise ValueError(
            "a recorded browser dialog message carries an identifier that is "
            "not a demonstrated parameter; make that value a parameter and "
            "compile again"
        )
    if len(pieces) == 1 and not re.search(r"\d", message):
        return DialogResponse.model_validate({"response": response, "message": message})
    pattern = ".+".join(
        r"\d+".join(re.escape(part) for part in re.split(r"\d+", piece))
        for piece in pieces
    )
    return DialogResponse.model_validate(
        {"response": response, "message_pattern": pattern}
    )


def _surfaces_from_meta(
    meta: dict,
    exclude_texts: tuple[str, ...],
    step_id_for_event: dict[int, str],
) -> list[SurfaceSpec]:
    """``meta.json`` ``surfaces`` -> ``Workflow.surfaces`` (patterns derived)."""

    from openadapt_flow.runtime.surfaces import url_origin

    raw = meta.get("surfaces")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("meta.json surfaces must be a non-empty list")
    specs: list[SurfaceSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("meta.json surfaces entries must be objects")
        sid = entry.get("id")
        kind = entry.get("kind")
        if "opened_by_event" not in entry:
            specs.append(SurfaceSpec.model_validate({"id": sid, "kind": kind}))
            continue
        event_index = int(entry["opened_by_event"])
        if event_index not in step_id_for_event:
            raise ValueError(f"surface {sid!r} is opened by an unknown event")
        url = str(entry.get("url") or "")
        origin = url_origin(url)
        if origin is None:
            raise ValueError(
                f"surface {sid!r} was recorded without an http(s) URL; Flow "
                "cannot identify that page at replay"
            )
        specs.append(
            SurfaceSpec.model_validate(
                {
                    "id": sid,
                    "kind": kind,
                    "opened_by_step": step_id_for_event[event_index],
                    "opener": entry.get("opener"),
                    "open_order": int(entry.get("open_order", 0)),
                    "opener_link": entry.get("opener_link"),
                    "origin": origin,
                    "url_pattern": _surface_url_pattern(url, exclude_texts),
                    "title_pattern": _surface_title_pattern(
                        entry.get("title"), exclude_texts
                    ),
                }
            )
        )
    return specs


# Parameter names must be usable as ValueExpr(param=...) / ApiBinding threads
# and environment-variable stems -- the same shape ir.ApiBinding enforces.
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _apply_param_overrides(
    events: list[dict],
    params: dict[str, str],
    secret_params: list[str],
    *,
    param_overrides: dict[str, str],
    secret_param_steps: frozenset[str],
) -> list[str]:
    """Apply operator-CONFIRMED parameter decisions to the raw events, in place.

    The one-shot confirm pass (``compiler.param_confirm``) resolves flagged
    field-label proposals into decisions; this function is their single
    application point. Each ``{step_id: name}`` entry turns that TYPE event's
    demonstrated constant into ``param=name`` exactly as if the demonstrator
    had recorded it that way, so every downstream safeguard (postcondition
    exclusion, leakage lint, secret handling) sees a first-class parameter.
    Fail-loud on ANY inconsistent decision -- an override that cannot be
    applied exactly as confirmed must never be silently dropped.

    Returns:
        Demonstrated values of compile-time SECRET steps (excluded from
        postconditions by the caller; never placed in ``params``).
    """
    from openadapt_flow.compiler.annotate import is_credential_label

    unknown_secret = secret_param_steps - set(param_overrides)
    if unknown_secret:
        raise ValueError(
            "secret_param_steps must be a subset of param_overrides step ids; "
            f"extra: {sorted(unknown_secret)}"
        )
    by_id = {f"step_{int(e['i']):03d}": e for e in events}
    secret_values: list[str] = []
    for step_id, pname in sorted(param_overrides.items()):
        if not _PARAM_NAME_RE.match(pname):
            raise ValueError(
                f"param override for {step_id}: invalid parameter name {pname!r}"
            )
        event = by_id.get(step_id)
        if event is None:
            raise ValueError(f"param override for unknown step id {step_id!r}")
        if event.get("kind") != "type":
            raise ValueError(
                f"param override for {step_id}: not a TYPE step "
                f"(kind={event.get('kind')!r})"
            )
        if event.get("param") or event.get("secret"):
            raise ValueError(
                f"param override for {step_id}: step already carries an "
                "explicit param/secret (the demonstrator's naming wins)"
            )
        text = event.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError(
                f"param override for {step_id}: no demonstrated text to parameterize"
            )
        if pname in params or pname in secret_params:
            raise ValueError(
                f"param override for {step_id}: name {pname!r} already "
                "declared by the recording"
            )
        event["param"] = pname
        if step_id in secret_param_steps or is_credential_label(
            event.get("field_label")
        ):
            # Compile-time secret: the BUNDLE carries no literal. (The
            # recording still does -- see the compile_recording docstring
            # caveat -- so the value is still postcondition-excluded.) A
            # value typed into a credential field (password, PIN, ...) is
            # always a secret once it becomes a parameter: a plain parameter
            # would store it as the default and example in workflow.json.
            event["secret"] = True
            event.pop("text", None)
            secret_params.append(pname)
            secret_values.append(text)
        else:
            params[pname] = text
    return secret_values


def compile_recording(
    recording_dir: Path | str,
    out_bundle_dir: Path | str,
    *,
    name: str,
    **kwargs: Any,
) -> Workflow:
    """Compile a recording directory into a workflow bundle.

    See :func:`_compile_recording` for the compiler itself. This wrapper
    decides where the output's bytes go. An encrypted recording (a sealed
    set, see :mod:`openadapt_flow.sealed_set`) is read in memory and compiles
    into a sealed bundle: every crop, sidecar and ``workflow.json`` is
    encrypted before it reaches the disk, under ``OPENADAPT_BUNDLE_KEY`` or,
    when that is unset, the recording's own key. Without a key the compile
    refuses before reading anything.
    """
    recording = Path(recording_dir)
    bundle = Path(out_bundle_dir)
    sealed_input = _sealed.is_sealed(recording)
    sealed_output = _sealed.is_sealed(bundle)
    if _sealed.active_writer(bundle) is not None or not (sealed_input or sealed_output):
        return _compile_recording(recording, bundle, name=name, **kwargs)
    if sealed_input:
        # Opening the recording verifies its whole file set before compiling.
        _sealed.open_set(recording)
    passphrase = _sealed.passphrase_for("bundle") or _sealed.recording_key()
    if not passphrase:
        raise _sealed.SealedSetKeyError(_sealed.missing_key_message(bundle, "bundle"))
    if not sealed_output and bundle.exists() and any(bundle.iterdir()):
        raise _sealed.SealedSetError(
            f"{bundle} already holds an unencrypted bundle; compile an "
            "encrypted recording into a new directory so no plaintext is mixed "
            "into the sealed output"
        )
    with _sealed.writing(bundle, "bundle", passphrase, resume=sealed_output):
        return _compile_recording(recording, bundle, name=name, **kwargs)


def _compile_recording(
    recording_dir: Path | str,
    out_bundle_dir: Path | str,
    *,
    name: str,
    risk_overrides: Optional[dict[str, str]] = None,
    param_overrides: Optional[dict[str, str]] = None,
    secret_param_steps: Optional[Iterable[str]] = None,
    mine_effects: bool = False,
    admit_api_bindings: bool = False,
    admission_fixture_url: Optional[str] = None,
    skill_library_root: Optional[Path | str] = None,
    annotate: bool = False,
    annotator: Optional["StepAnnotator"] = None,
    target_surface: Optional[ExecutionTargetKind] = None,
    pixel_verify_enabled: bool = False,
    bind_effect_params: bool = False,
    drop_redundant_keys: bool = False,
    phi_free_identity: bool = True,
) -> Workflow:
    """Compile a recording directory into a workflow bundle.

    For each click (or double_click) event: crop a template (160x64, clamped
    to the frame, centered on the click), OCR the crop for ``ocr_text``,
    derive up to two landmarks from nearby OCR lines outside the crop,
    record the target's identity context band (``anchor.context_text`` —
    row text outside the crop, verified before every click at replay time;
    see :mod:`openadapt_flow.runtime.identity`), emit a pixel identifier
    crop (``anchor.identifier_crop`` under ``templates/identifiers/``) for
    identity-armed steps without structured identity or with a record-time
    ``--identifier`` marking — arming the pixel-compare identity tier on
    remote-display/pixel replays, with every crop-less identity-applicable
    step recording WHY in ``Step.identifier_crop_missing_reason`` (see
    :func:`_emit_identifier_crop`) — and derive postconditions
    (REGION_STABLE on the largest changed region plus TEXT_PRESENT for the
    most STABLE new text — volatile candidates such as clock fragments,
    near dates and counters are rejected, and candidates must persist into
    the next frame; see :mod:`openadapt_flow.volatility`). Steps that mined
    nothing fall back to structural postconditions (URL/title change, new
    tab) when the recorder captured them. Type/key events carry
    their text/param/key through. Parameterized typed values and click
    target labels are never asserted in any step's postconditions (the
    former vary per run; the latter are mutable evidence the resolution
    ladder heals through under rename drift), and a final lint fails
    compilation if a demo parameter value leaked into any postcondition or
    geometry landmark. The bundle gets ``workflow.json``,
    ``templates/*.png`` and a generated readable ``workflow.py``.

    Risk is AUTO-CLASSIFIED: each CLICK/DOUBLE_CLICK step whose intent or
    button label is write-shaped (create/update/delete/submit/save/confirm
    ...) compiles as ``risk="irreversible"`` (see :mod:`openadapt_flow.risk`);
    everything else is ``"reversible"``. ``risk_overrides`` still wins, in
    either direction. Irreversible steps refuse to act when they only resolve
    below the OCR rung or when their identity band is unreadable (see
    :class:`~openadapt_flow.runtime.Replayer`), so this arms those safeguards
    by default for consequential writes instead of only when a human marks a
    step. The classifier leans irreversible when unsure on a write-shaped step
    (the safe direction; false-positive posture documented in the module).

    Args:
        recording_dir: Recording directory (meta.json, events.jsonl, frames/).
        out_bundle_dir: Output bundle directory (created if missing).
        name: Workflow name.
        risk_overrides: Optional ``{step_id: risk}`` map (step ids are
            positional: ``step_000`` is the first recorded event). Values
            must be ``"reversible"`` or ``"irreversible"``.
        param_overrides: Optional ``{step_id: param_name}`` map turning a
            demonstrated TYPE constant into a named parameter -- the
            OPERATOR-CONFIRMED application of a field-label proposal (the
            one-shot confirm pass, ``compiler.param_confirm``; see also
            ``compiler.annotate.FieldLabelAnnotator``). Applied at the EVENT
            level before compilation, so the value flows through exactly the
            recorded-``param=`` machinery: example/default in
            ``workflow.params`` + ``param_specs``, postcondition exclusion,
            and the parameter-leakage lint. Fails loud on an unknown step id,
            a non-TYPE step, a step that already carries an explicit
            ``param``/secret, an empty typed value, or a duplicate/invalid
            name. Human confirmation is the CONTRACT here: the param name is
            the thread the effect verifier binds to (``ValueExpr(param=...)``)
            so it is never silently inferred.
        secret_param_steps: Optional step ids (a subset of
            ``param_overrides`` keys) whose confirmed parameter is a SECRET:
            the bundle carries NO literal (``step.text`` is None,
            ``step.secret`` is True, the name joins ``secret_params``) and the
            value is injected at replay from
            ``OPENADAPT_FLOW_SECRET_<PARAM>``. CAVEAT (documented, honest):
            marking secret at COMPILE time protects the BUNDLE only -- the
            recording itself was made without ``--secret``, so its
            events.jsonl and frames still hold the demonstrated value;
            re-record with ``--secret`` for full at-rest secrecy.
        mine_effects: Opt-in system-of-record effect mining
            (``compiler.effect_mining``). When True, each step gets candidate
            typed ``Effect``s auto-derived from what the demonstration observed:
            a real ``record_written`` / ``field_equals`` from a captured
            ``/api/db``-style SoR delta, a flagged placeholder for a
            consequential step whose binding is app-specific (not derivable),
            or nothing (with an honest "no verifiable effect derivable" log).
            Default False keeps the bundle byte-identical to before; even when
            True a bundle is unchanged wherever mining derives nothing.
        bind_effect_params: With ``mine_effects``, a mined ``field_equals``
            whose value is a parameter's demonstrated value reads back that
            parameter at run time instead of the demonstrated literal, so a
            run with a new value can verify. A value shared by two parameters
            stays literal. Default False keeps the mined contract as before.
        admit_api_bindings: Opt-in compile-time REST ``ApiBinding``
            certification (``compiler.binding_admission``). For each
            consequential write whose Effect was mined from an observed
            system-of-record delta, propose a REST binding and copy it onto
            the step only if the same Effect CONFIRMs on the held-out
            fixture at ``admission_fixture_url``. A refused proposal leaves
            the step on the GUI ladder. FHIR / MCP / tool bindings are not
            synthesized. Default False keeps the bundle byte-identical.
        admission_fixture_url: Base URL of the held-out REST fixture
            (MockMed ``fault_server`` in tests). Required when
            ``admit_api_bindings`` is True; without it the compiler logs
            and leaves every step on the GUI ladder.
        skill_library_root: Optional
            :class:`~openadapt_flow.learning.library.SkillLibrary` root.
            When a binding is admitted, the lifted program is stored with
            provenance naming the certified REST admission.
        annotate: Opt-in COMPILE-TIME model annotation
            (``compiler.annotate``). When True, a :class:`StepAnnotator` proposes
            richer step LABELS, RISK refinements, and typed PARAMETER inferences
            over the compiled workflow, and its proposals are applied with the
            confirm-don't-trust asymmetry: a risk UPGRADE (reversible ->
            irreversible) and a pure parameter TYPE enrichment are applied; a
            risk DOWNGRADE or a consequential parameter inference is FLAGGED for
            an operator, never applied. Applied risk upgrades land in
            ``step.risk``; the full proposal/flag audit trail is written to a
            bundle sidecar ``annotations.json``. This is COMPILE-TIME ONLY -- the
            replayer never reads it and makes ZERO model calls. Default False
            keeps the bundle byte-identical to today (heuristic only, no model,
            no key needed).
        annotator: The :class:`StepAnnotator` to use when ``annotate`` is True.
            None defaults to :class:`~openadapt_flow.compiler.annotate.
            AnthropicStepAnnotator` (the real model, resolved lazily -- it needs
            an API key only when it actually runs). Tests pass a network-free
            fake. Ignored when ``annotate`` is False.
        target_surface: Explicit surface binding for a legacy recording whose
            ``meta.json`` predates surface stamps. This may only fill missing
            metadata; contradicting a recorded surface fails compilation.
        pixel_verify_enabled: The target runtime's
            ``runtime.pixel_verify_enabled``. The default fix-up passes attach
            a pixel identifier crop as a step's only identity evidence only
            when that runtime can verify it (see
            :func:`openadapt_flow.runtime.identity.verifiable_identity_evidence`).
        drop_redundant_keys: Let the fix-up passes drop a demonstrated Enter
            after a ``<select>`` type and a Ctrl+A on a field that was empty
            in the demonstration. Off by default: the TYPE read-back at replay
            doesn't catch a wrong drop (see
            :mod:`openadapt_flow.compiler.fixup_passes`).

    Returns:
        The compiled :class:`Workflow` (also saved to the bundle).

    Raises:
        ValueError: On an unknown event kind, an unknown ``risk_overrides``
            step id, an invalid risk value, or when the parameter-leakage
            lint finds a demonstrated parameter value baked into a
            postcondition or geometry landmark.
        FileNotFoundError: If a click event's before frame is missing, or any
            event has only one half of its before/after frame pair.
    """
    from openadapt_flow.compiler.codegen import render_workflow_py

    recording = Path(recording_dir)
    bundle = Path(out_bundle_dir)
    (bundle / "templates").mkdir(parents=True, exist_ok=True)

    meta = json.loads(_sealed.read_text(recording / "meta.json"))
    backend_hints_raw = meta.get("backend_hints")
    backend_hints: Optional[BackendHints] = None
    if backend_hints_raw is not None:
        if not isinstance(backend_hints_raw, dict):
            raise ValueError("meta.json backend_hints must be an object")
        try:
            backend_hints = BackendHints.model_validate(backend_hints_raw)
        except Exception as exc:
            raise ValueError(
                "meta.json backend_hints does not match the closed "
                "rdp/citrix execution-target schema"
            ) from exc
    surface, execution_mode = _surface_binding_from_meta(
        meta, backend_hints, target_surface
    )
    events = _load_events(recording)
    params: dict[str, str] = dict(meta.get("params") or {})
    secret_params: list[str] = list(meta.get("secret_params") or [])
    # Operator-CONFIRMED parameter decisions (the one-shot confirm pass):
    # applied at the EVENT level, before anything else reads the events, so a
    # confirmed parameter flows through the exact machinery a recorded
    # ``param=`` uses -- example/default, postcondition exclusion, leakage
    # lint, secret handling. Fails loud on any inconsistent decision.
    secret_exclude_values = _apply_param_overrides(
        events,
        params,
        secret_params,
        param_overrides=dict(param_overrides or {}),
        secret_param_steps=frozenset(secret_param_steps or ()),
    )
    viewport = meta.get("viewport")
    # Recording-wide marked identifier region (desktop `record --identifier
    # X,Y,W,H` — a pixel capture has no field identity, so the operator marks
    # the record-identifying region once, e.g. the patient banner). A click
    # event's own `identifier_region` (web `--identifier FIELD` rect) takes
    # precedence per step.
    meta_identifier_region = _marked_identifier_region(
        meta.get("identifier_region"), source="meta.json"
    )

    # Parameterized values vary per run, so they must never be asserted in
    # ANY step's postconditions — not just the TYPE step's own: the typed
    # value can surface later (e.g. in a save-confirmation banner after the
    # subsequent click), and baking the demo-time value in would make the
    # bundle replayable only with that exact value.
    param_values: list[str] = [v for v in params.values() if v]
    for event in events:
        if event.get("kind") == "type" and event.get("param"):
            text = event.get("text")
            if text:
                param_values.append(text)
    # A compile-time secret's demonstrated value is excluded from
    # postconditions too, even though it never lands in workflow.params.
    param_values.extend(secret_exclude_values)
    exclude_texts: tuple[str, ...] = tuple(dict.fromkeys(param_values))

    # The recording date arms the near/far date split in the volatility
    # classifier (a DOB is identity data; "last updated" is chronology).
    reference_date: Optional[date] = None
    try:
        reference_date = datetime.fromisoformat(
            str(meta.get("created_at") or "")
        ).date()
    except ValueError:
        reference_date = None

    # Each recording frame is OCRed at most once across both passes.
    ocr_cache: dict[tuple[int, str], list[OcrLine]] = {}
    from openadapt_flow.runtime import layout_drift as _layout

    layout_fingerprints: dict[str, "_layout.LayoutFingerprint"] = {}
    from openadapt_flow.runtime import render_scale as _render_scale

    scale_salt = _render_scale.new_salt_hex()
    scale_landmarks: dict[str, list[list[Any]]] = {}

    def cached_lines(i: int, suffix: str, png: bytes) -> list[OcrLine]:
        key = (i, suffix)
        if key not in ocr_cache:
            ocr_cache[key] = ocr(png)
        return ocr_cache[key]

    # Pass 1 builds the steps (anchors, actions); postconditions are derived
    # in pass 2, once every click target's label is known — target labels
    # are mutable evidence (rename drift) and must not be asserted.
    pending: list[tuple[Step, Optional[bytes], Optional[bytes], dict]] = []
    for event in events:
        i = int(event["i"])
        kind = event["kind"]
        step_id = f"step_{i:03d}"
        before_path = recording / "frames" / f"{i:04d}_before.png"
        after_path = recording / "frames" / f"{i:04d}_after.png"
        if _sealed.is_file(before_path) != _sealed.is_file(after_path):
            missing = before_path if not _sealed.is_file(before_path) else after_path
            raise FileNotFoundError(
                f"incomplete frame pair for {kind} event {i}: missing {missing}"
            )
        before_png = _read_png(before_path)
        after_png = _read_png(after_path)
        before_viewport = _validated_event_viewport(
            event,
            key="viewport_before",
            png=before_png,
            event_index=i,
        )
        after_viewport = _validated_event_viewport(
            event,
            key="viewport_after",
            png=after_png,
            event_index=i,
        )
        if (
            kind != "switch_surface"
            and before_viewport is not None
            and after_viewport is not None
            and before_viewport != after_viewport
        ):
            raise ValueError(
                f"viewport changed during event {i}: before "
                f"{before_viewport[0]}x{before_viewport[1]}, after "
                f"{after_viewport[0]}x{after_viewport[1]}"
            )

        if kind in ("click", "double_click", "right_click", "drag"):
            if before_png is None:
                raise FileNotFoundError(
                    f"missing before frame for {kind} event {i} in {recording}"
                )
            click: Point = (int(event["x"]), int(event["y"]))
            assert before_viewport is not None
            _validate_point_in_viewport(
                click,
                before_viewport,
                event_index=i,
                label="pointer",
            )
            # ``before_png`` is a captured frame we already hold; the decode is
            # known-valid. cv2's stub types imdecode as Optional, hence the cast.
            frame = cast(
                np.ndarray,
                imgops.decode_bgr(before_png),
            )
            crop_region = _discriminative_crop_region(frame, click)
            frame_lines = cached_lines(i, "before", before_png)
            # Font/layout fingerprint of the frame the step acts on: frame
            # size and font metrics, no text (runtime.layout_drift).
            layout_fingerprints[step_id] = _layout.fingerprint(
                frame_lines, (int(frame.shape[1]), int(frame.shape[0]))
            )
            # Unique label lines as salted hashes with their centres, for the
            # display-scale estimate (runtime.render_scale). No text.
            scale_landmarks[step_id] = _render_scale.scale_landmark_set(
                frame_lines, salt_hex=scale_salt, exclude_texts=exclude_texts
            )
            crop_region = _clip_away_param_lines(
                crop_region, click, frame_lines, exclude_texts
            )
            template_bytes = _crop_png(before_png, crop_region)
            template_rel = f"templates/{step_id}.png"
            _sealed.write_bytes(bundle / template_rel, template_bytes)

            ocr_text = _best_target_text(
                ocr(before_png, region=crop_region),
                frame_lines,
                click,
            )
            if ocr_text and _contains_excluded(ocr_text, exclude_texts):
                # The text under the click is (part of) a value typed from a
                # parameter, so it changes every run: never anchor on it.
                ocr_text = None
            landmarks = _landmarks_for(
                frame_lines,
                crop_region,
                click,
                exclude_texts=exclude_texts,
                reference_date=reference_date,
            )
            # Identity evidence: the target's row text OUTSIDE its own crop
            # (a table row's discriminative name column sits outside the
            # 160x64 template — see runtime.identity). Verified against the
            # live band before every click at replay time.
            context_text = context_from_lines(
                frame_lines,
                exclude_region=crop_region,
                band=band_region(
                    click, crop_region[3], (frame.shape[1], frame.shape[0])
                ),
                # Row refinement: record only the click point's OWN text
                # row, matching what replay-time verification reads (the
                # 64px band spans 2-3 rows of a dense table).
                point=click,
                min_confidence=MIN_OCR_CONFIDENCE,
                reference_date=reference_date,
            )
            # Structured identity (DOM / a11y text of the clicked row),
            # captured by the recorder when the recording backend exposed it
            # (openadapt_flow.backend.IdentityBackend). Stored alongside the
            # OCR context band; replay prefers it (no OCR glyph ambiguity) and
            # falls back to the band on pixel-only substrates.
            structured_identity = event.get("structured_identity") or None
            # The row's stable record id (a DOM data-* id, the row link's
            # path), captured with the structured identity. Hashed into the
            # identity template so replay acts only on the row with this id.
            record_key = (
                event.get("record_key") or None if structured_identity else None
            )
            structured_row = event.get("structured_row")
            if not structured_identity or not isinstance(structured_row, bool):
                structured_row = None
            # Structural locator (DOM selector / role+name, or UIA identifiers)
            # of the clicked element, captured by the recorder when the
            # recording backend exposed it (StructuralActionBackend). Drives the
            # structural ACTION rung at replay -- the SAME element is re-found
            # deterministically, surviving the render drift the visual template
            # cannot. The visual anchor above is kept as the fallback floor.
            structural_raw = event.get("structural") or None
            structural = (
                StructuralLocator.model_validate(structural_raw)
                if structural_raw
                else None
            )
            # Pixel identifier crop: persist the record-identifying pixels
            # (MRN / name+DOB region) so the pixel-compare identity tier
            # (runtime.identity.verify_pixel_identity) arms on remote-display
            # /pixel replays — the substrates where no DOM/UIA text exists to
            # verify "right record" with. Emitted for identity-armed steps
            # without structured identity (automatic band box) or when the
            # operator marked the region at record time (--identifier); every
            # crop-less identity-applicable step records WHY
            # (Step.identifier_crop_missing_reason). See _emit_identifier_crop.
            event_marked = _marked_identifier_region(
                event.get("identifier_region"), source=f"events.jsonl event {i}"
            )
            identifier_crop_rel, identifier_region, idcrop_missing = (
                _emit_identifier_crop(
                    bundle,
                    step_id,
                    before_png,
                    frame_size=(frame.shape[1], frame.shape[0]),
                    marked_region=(
                        event_marked
                        if event_marked is not None
                        else meta_identifier_region
                    ),
                    context_text=context_text,
                    structured_identity=structured_identity,
                    frame_lines=frame_lines,
                    crop_region=crop_region,
                    click=click,
                    reference_date=reference_date,
                )
            )
            # The OCR fallback must compare like-for-like evidence. Derive the
            # persisted identity template from the original full-frame OCR
            # lines filtered to the exact identifier region. Re-OCRing a crop
            # can segment or recognize identical pixels differently (notably
            # l/1 and O/0), while replacing the original row context can drop
            # names/DOBs on some OCR builds.
            cropped_context: Optional[str] = None
            if identifier_region is not None:
                cropped_context = identifier_text_from_lines(
                    frame_lines,
                    region=identifier_region,
                    min_confidence=MIN_OCR_CONFIDENCE,
                    reference_date=reference_date,
                )
                if cropped_context is not None:
                    context_text = cropped_context
            anchor_template = template_rel
            anchor_region = crop_region
            anchor_ocr_text = ocr_text
            anchor_landmarks = landmarks
            anchor_click = click
            if (
                event_marked is not None
                and identifier_crop_rel is not None
                and identifier_region is not None
            ):
                rx, ry, rw, rh = identifier_region
                if rx <= click[0] < rx + rw and ry <= click[1] < ry + rh:
                    selected_target = _action_scoped_target_line(
                        frame_lines,
                        identifier_region,
                        frame_size=(frame.shape[1], frame.shape[0]),
                        reference_date=reference_date,
                        click=(
                            click if event.get("identifier_scope") == "dialog" else None
                        ),
                    )
                    if selected_target is not None:
                        anchor_region, anchor_ocr_text = selected_target
                        _sealed.write_bytes(
                            bundle / template_rel, _crop_png(before_png, anchor_region)
                        )
                        if event.get("identifier_scope") == "dialog":
                            # The click landed on the control's own label.
                            # Aim at the label's center, where every rung
                            # (template and OCR) locates it, so a rescaled
                            # or rethemed dialog is pressed in the same place.
                            ax, ay, aw, ah = anchor_region
                            if ax <= click[0] < ax + aw and ay <= click[1] < ay + ah:
                                anchor_click = (ax + aw // 2, ay + ah // 2)
                    else:
                        # A marked image-only row still provides a complete
                        # visual target when no stable unique text is present.
                        anchor_template = identifier_crop_rel
                        anchor_region = identifier_region
                        anchor_ocr_text = cropped_context or ocr_text
                    anchor_landmarks = []
            anchor = Anchor(
                template=anchor_template,
                region=anchor_region,
                click_point=anchor_click,
                ocr_text=anchor_ocr_text,
                context_text=context_text,
                structured_identity=structured_identity,
                record_key=record_key,
                structured_row=structured_row,
                structural=structural,
                landmarks=anchor_landmarks,
                identifier_crop=identifier_crop_rel,
                identifier_region=identifier_region,
            )
            drag_end_anchor: Optional[Anchor] = None
            if kind == "drag":
                drag_end: Point = (int(event["end_x"]), int(event["end_y"]))
                _validate_point_in_viewport(
                    drag_end,
                    before_viewport,
                    event_index=i,
                    label="drag destination",
                )
                end_crop_region = _discriminative_crop_region(frame, drag_end)
                end_template_rel = f"templates/{step_id}_drag_end.png"
                _sealed.write_bytes(
                    bundle / end_template_rel, _crop_png(before_png, end_crop_region)
                )
                end_ocr_text = _best_crop_text(
                    ocr(before_png, region=end_crop_region), click_y=drag_end[1]
                )
                drag_end_structural_raw = event.get("drag_end_structural") or None
                drag_end_structural = (
                    StructuralLocator.model_validate(drag_end_structural_raw)
                    if drag_end_structural_raw
                    else None
                )
                drag_end_anchor = Anchor(
                    template=end_template_rel,
                    region=end_crop_region,
                    click_point=drag_end,
                    ocr_text=end_ocr_text,
                    structural=drag_end_structural,
                    landmarks=_landmarks_for(
                        frame_lines,
                        end_crop_region,
                        drag_end,
                        exclude_texts=exclude_texts,
                        reference_date=reference_date,
                    ),
                )
            # Identity-protection audit trail: an UNARMED click proceeds
            # with NO identity verification at replay (docs/LIMITS.md), so
            # the bundle records armed/unarmed per step — with the reason
            # — for operator review BEFORE the workflow ever runs.
            # An explicit/compiler-emitted identifier crop is itself a live
            # identity tier on pixel-only RDP/Citrix/VDI surfaces. Treat it as
            # armed in the audit/policy flag as well as in the replayer; the
            # former context-only flag made governed execution refuse the very
            # remote-display crop the compiler had emitted.
            identity_armed = (
                context_text is not None
                or structured_identity is not None
                or identifier_crop_rel is not None
            )
            unarmed_reason: Optional[str] = None
            if not identity_armed:
                unarmed_reason = _identity_unarmed_reason(
                    frame_lines,
                    band=band_region(
                        click,
                        crop_region[3],
                        (frame.shape[1], frame.shape[0]),
                    ),
                    exclude_region=crop_region,
                )
            hover = (
                _hover_evidence(
                    bundle,
                    step_id,
                    before_png,
                    recording / "frames" / f"{i:04d}_hover.png",
                    click,
                    dwell_ms=event.get("hover_dwell_ms"),
                )
                if not identity_armed and kind != "drag"
                else None
            )
            if kind == "drag":
                destination = drag_end_anchor.ocr_text if drag_end_anchor else None
                intent = "drag " + (
                    f"'{ocr_text}'" if ocr_text else "recorded visual target"
                )
                intent += " to " + (
                    f"'{destination}'" if destination else "recorded visual destination"
                )
            else:
                verb = {
                    "click": "click",
                    "double_click": "double-click",
                    "right_click": "right-click",
                }[kind]
                # A single OCR glyph read off an icon (a floppy-disk Save reads
                # as "日") is not a label; naming the step after it would tell
                # a reviewer the click targets text that is not on screen.
                intent = (
                    f"{verb} '{ocr_text}'"
                    if is_meaningful_label(ocr_text)
                    else f"{verb} recorded visual target"
                )
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=intent,
                        action={
                            "click": ActionKind.CLICK,
                            "double_click": ActionKind.DOUBLE_CLICK,
                            "right_click": ActionKind.RIGHT_CLICK,
                            "drag": ActionKind.DRAG,
                        }[kind],
                        anchor=anchor,
                        drag_end_anchor=drag_end_anchor,
                        identity_armed=identity_armed,
                        identity_unarmed_reason=unarmed_reason,
                        identifier_crop_missing_reason=idcrop_missing,
                        hover=hover,
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "type":
            param = event.get("param")
            text = event.get("text")
            secret = bool(event.get("secret"))
            # Receiving-field label EVIDENCE (ergonomic parameter naming):
            # best is the recorder's passive DOM/a11y capture on the event
            # (``field_label``); last resort is the nearest OCR line left
            # of / above the recorded field rect on the BEFORE frame. Pure
            # metadata -- never read at replay; it only feeds the flagged,
            # operator-confirmed parameter proposals (FieldLabelAnnotator).
            field_label: Optional[str] = None
            raw_label = event.get("field_label")
            if isinstance(raw_label, str) and raw_label.strip():
                field_label = " ".join(raw_label.split())
            else:
                raw_rect = event.get("field_rect")
                if (
                    before_png is not None
                    and isinstance(raw_rect, (list, tuple))
                    and len(raw_rect) == 4
                ):
                    field_label = _field_label_from_ocr(
                        cached_lines(i, "before", before_png),
                        (
                            int(raw_rect[0]),
                            int(raw_rect[1]),
                            int(raw_rect[2]),
                            int(raw_rect[3]),
                        ),
                    )
            if secret:
                # A secret's literal value is never in the recording, so it
                # is never in the bundle either: the step carries only the
                # param name, and the value is injected from the environment
                # at replay (see ir.Step.secret / runtime.Replayer).
                text = None
                intent = f"type <{param}> (secret)"
            elif param:
                intent = f"type <{param}>"
            else:
                intent = f"type '{_text_preview(text or '')}'"
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=intent,
                        action=ActionKind.TYPE,
                        text=text,
                        param=param,
                        secret=secret,
                        field_label=field_label,
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "key":
            key = event["key"]
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"press {key}",
                        action=ActionKind.KEY,
                        key=key,
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "hotkey":
            key = str(event["key"])
            modifiers = [str(value) for value in event.get("modifiers", [])]
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"press {'+'.join([*modifiers, key])}",
                        action=ActionKind.HOTKEY,
                        key=key,
                        modifiers=modifiers,
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "scroll":
            dx, dy = int(event.get("dx", 0)), int(event.get("dy", 0))
            # SCROLL steps get NO postconditions (pass 2 skips them):
            # scrolling shifts the whole viewport, so a frame diff spans
            # nearly the full screen and would assert mutable page content
            # as an invariant. The scroll's purpose — bringing the next
            # target into view — is verified by the next anchored step's
            # resolution ladder, which fails if the scroll did not land.
            # The frames are still carried: a scroll's BEFORE frame is the
            # previous step's screen moments later, which the empirical
            # stability checks use.
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"scroll by ({dx}, {dy})",
                        action=ActionKind.SCROLL,
                        scroll_dx=dx,
                        scroll_dy=dy,
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "read":
            # A demonstrated read: the person marked a field's value box as an
            # output. It compiles to a READ step; nothing is actuated, so it
            # gets no postconditions (pass 2 skips it). A marked label box
            # becomes the anchor, so replay moves the value box with the label.
            from openadapt_flow.ir import ReadStepSpec
            from openadapt_flow.records.schema import FieldSpec, ReadProof

            value_region = tuple(int(v) for v in event["region"])
            if len(value_region) != 4:
                raise ValueError(f"read event {i} needs region [x, y, w, h]")
            read_anchor: Optional[Anchor] = None
            label_region = event.get("label_region")
            if label_region is not None:
                if before_png is None:
                    raise ValueError(f"read event {i} has a label box but no frame")
                lx, ly, lw, lh = (int(v) for v in label_region)
                label_rel = f"templates/{step_id}.png"
                _sealed.write_bytes(
                    bundle / label_rel, _crop_png(before_png, (lx, ly, lw, lh))
                )
                read_anchor = Anchor(
                    template=label_rel,
                    region=(lx, ly, lw, lh),
                    click_point=(lx + lw // 2, ly + lh // 2),
                    ocr_text=event.get("label_text"),
                )
            field_spec = FieldSpec.model_validate(event["field"])
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"read {field_spec.name}",
                        action=ActionKind.READ,
                        anchor=read_anchor,
                        read=ReadStepSpec(
                            schema_name=str(event["schema_name"]),
                            field=field_spec,
                            region=value_region,
                            proof=ReadProof.model_validate(event.get("proof") or {}),
                            on_review=event.get("on_review", "halt"),
                        ),
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        elif kind == "switch_surface":
            target = str(event.get("surface") or "")
            from openadapt_flow.runtime.surfaces import url_origin

            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"switch to surface '{target}'",
                        action=ActionKind.SWITCH_SURFACE,
                        surface=target,
                        # Identity of a browser target: the origin the
                        # demonstration saw on arriving there.
                        surface_origin=url_origin(event.get("url_after")),
                    ),
                    before_png,
                    after_png,
                    event,
                )
            )
        else:
            raise ValueError(f"unknown event kind {kind!r} (event {i})")
        if kind != "switch_surface":
            stamped = pending[-1][0]
            if event.get("surface") is not None:
                stamped.surface = str(event["surface"])
            if event.get("opens"):
                stamped.opens = [str(sid) for sid in event["opens"]]
            if event.get("closes_surface") is True:
                stamped.closes_surface = True

    # Collapse a demonstrated native/pixel option selection into one explicit
    # SELECT_OPTION contract. This is deliberately evidence-gated and remote-only:
    # the prior click names an exact field region; typing produces a provisional
    # visual change without committing the value there; the immediately
    # following Enter/Tab starts from that exact provisional frame; and only its
    # after-frame makes the demonstrated value readable in the field. Runtime
    # can then deliver text+commit atomically and verify the parameterized value
    # in a qualified readback band mapped from the freshly resolved target,
    # rather than falsely requiring an uncommitted native select to expose the
    # value after TYPE alone.
    absorbed_step_aliases: dict[str, str] = {}
    if surface in ("rdp", "citrix"):
        collapsed: list[tuple[Step, Optional[bytes], Optional[bytes], dict]] = []
        index = 0
        while index < len(pending):
            current = pending[index]
            if index + 1 < len(pending) and collapsed:
                step, type_before, type_after, type_event = current
                key_step, key_before, key_after, key_event = pending[index + 1]
                previous, _previous_before, previous_after, _previous_event = collapsed[
                    -1
                ]
                commit = (
                    "Enter"
                    if (key_step.key or "").casefold() in {"enter", "return"}
                    else "Tab"
                    if (key_step.key or "").casefold() == "tab"
                    else None
                )
                demonstrated = step.text
                previous_anchor = previous.anchor
                target_region = (
                    previous_anchor.region if previous_anchor is not None else None
                )
                selection_region = (
                    _selection_readback_region(key_after, target_region)
                    if key_after is not None and target_region is not None
                    else None
                )
                if (
                    previous.action is ActionKind.CLICK
                    and previous_anchor is not None
                    and step.action is ActionKind.TYPE
                    and not step.secret
                    and key_step.action is ActionKind.KEY
                    and commit is not None
                    and demonstrated
                    and target_region is not None
                    and selection_region is not None
                    and type_before is not None
                    and type_after is not None
                    and key_before is not None
                    and key_after is not None
                    and previous_after is not None
                    and hashlib.sha256(previous_after).digest()
                    == hashlib.sha256(type_before).digest()
                    and hashlib.sha256(type_after).digest()
                    == hashlib.sha256(key_before).digest()
                    and hashlib.sha256(type_before).digest()
                    != hashlib.sha256(type_after).digest()
                    and not _exact_ocr_value_present(
                        type_before, demonstrated, selection_region
                    )
                    and not _exact_ocr_value_present(
                        type_after, demonstrated, selection_region
                    )
                    and _exact_ocr_value_present(
                        key_after, demonstrated, selection_region
                    )
                ):
                    selected_anchor = previous_anchor.model_copy(deep=True)
                    selected_field_label = step.field_label
                    exact_label = _exact_left_field_label_landmark(
                        cached_lines(
                            int(type_event["i"]),
                            "before",
                            type_before,
                        ),
                        selected_anchor.region,
                        selected_anchor.click_point,
                        exclude_texts=exclude_texts,
                        reference_date=reference_date,
                    )
                    if exact_label is not None:
                        selected_field_label, label_landmark = exact_label
                        retained_landmarks = [
                            landmark
                            for landmark in selected_anchor.landmarks
                            if normalize_text(landmark.ocr_text)
                            != normalize_text(label_landmark.ocr_text)
                        ]
                        selected_anchor = selected_anchor.model_copy(
                            update={
                                "landmarks": [
                                    label_landmark,
                                    *retained_landmarks,
                                ]
                            }
                        )
                    merged_event = dict(type_event)
                    for name, value in key_event.items():
                        if name.endswith("_after"):
                            merged_event[name] = value
                    selected = step.model_copy(
                        update={
                            "action": ActionKind.SELECT_OPTION,
                            "intent": (
                                f"select <{step.param}>"
                                if step.param is not None
                                else f"select '{_text_preview(demonstrated)}'"
                            ),
                            "selection_commit_key": commit,
                            "selection_region": selection_region,
                            "anchor": selected_anchor,
                            "field_label": selected_field_label,
                            "identity_armed": previous.identity_armed,
                            "identity_unarmed_reason": (
                                previous.identity_unarmed_reason
                            ),
                            "identifier_crop_missing_reason": (
                                previous.identifier_crop_missing_reason
                            ),
                        }
                    )
                    # The focusing click, provisional TYPE, and commit key are
                    # one demonstrated selection gesture. Keep only the
                    # anchored SELECT_OPTION contract so replay focuses exactly once,
                    # then re-resolves and leases that same field before the
                    # atomic text+commit delivery.
                    collapsed.pop()
                    absorbed_step_aliases[previous.id] = selected.id
                    absorbed_step_aliases[key_step.id] = selected.id
                    collapsed.append((selected, type_before, key_after, merged_event))
                    index += 2
                    continue
            collapsed.append(current)
            index += 1
        pending = collapsed

    # Pass 2: derive postconditions, never asserting parameterized values or
    # any click target's label, selecting for stability (volatility
    # classifier + persistence into the next frame), and falling back to
    # structural postconditions for steps that would otherwise be vacuous.
    anchor_labels: tuple[str, ...] = tuple(
        step.anchor.ocr_text
        for step, _, _, _ in pending
        if step.anchor is not None and step.anchor.ocr_text
    )
    steps: list[Step] = []
    # Param-hygiene notices: REGION_STABLE postconditions rejected because the
    # changed region rendered a demonstrated parameter value. Reported (not
    # raised) below — the drop is the safe outcome, but it must not be silent.
    param_region_drops: list[tuple[str, Region, str]] = []
    # Submit-binding observations recorded on click events (unlabeled
    # controls only), attached once every risk decision is final.
    binding_observations: dict[str, object] = {}
    for j, (step, step_before, step_after, event) in enumerate(pending):
        steps.append(step)
        if isinstance(event.get("dialog"), dict):
            # The browser dialog this action raised, answered by the backend
            # at replay only when it shows the same message (values and
            # digits generalized, never sealed).
            step.dialog = _dialog_identity(event["dialog"], exclude_texts)
        if event.get("submit_binding") is not None:
            binding_observations[step.id] = event["submit_binding"]
        if step.action in (ActionKind.SCROLL, ActionKind.READ):
            continue  # see the scroll and read branches in pass 1
        if step.action is ActionKind.SWITCH_SURFACE:
            # The switch's screen identity: one stable text line from the
            # FIRST frame of the new surface (every line there is "new"), with
            # the same volatility, persistence, parameter, and PHI filters as
            # any other step. Its before frame is another surface, so a diff
            # would mean nothing.
            if step_after is not None:
                switch_next: Optional[list[OcrLine]] = None
                switch_next_before = pending[j + 1][1] if j + 1 < len(pending) else None
                if switch_next_before is not None:
                    switch_next = cached_lines(
                        int(pending[j + 1][3]["i"]), "before", switch_next_before
                    )
                text_pc = _new_text_postcondition(
                    [],
                    cached_lines(int(event["i"]), "after", step_after),
                    exclude_texts=exclude_texts,
                    avoid_labels=anchor_labels,
                    next_lines=switch_next,
                    reference_date=reference_date,
                )
                step.expect = [text_pc] if text_pc is not None else []
            step.identity_armed = bool(step.expect) or step.surface_origin is not None
            step.identity_unarmed_reason = (
                None
                if step.identity_armed
                else "no stable text on the new surface and no browser origin"
            )
            continue
        step_region_drops: list[tuple[Region, str]] = []
        next_before: Optional[bytes] = None
        next_lines: Optional[list[OcrLine]] = None
        if j + 1 < len(pending):
            next_before = pending[j + 1][1]
            if next_before is not None:
                next_lines = cached_lines(
                    int(pending[j + 1][3]["i"]), "before", next_before
                )
        i = int(event["i"])
        step.expect = _postconditions(
            step_before,
            step_after,
            exclude_texts=exclude_texts,
            avoid_labels=anchor_labels,
            bundle=bundle,
            step_id=step.id,
            # A parameterized TYPE/SELECT_OPTION step's changed region is the
            # value's own pixels — never assert it (it varies per run).
            include_region_stable=not (
                step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
                and (step.param is not None or step.secret)
            ),
            before_lines=(
                cached_lines(i, "before", step_before)
                if step_before is not None
                else None
            ),
            after_lines=(
                cached_lines(i, "after", step_after) if step_after is not None else None
            ),
            next_lines=next_lines,
            next_before_png=next_before,
            reference_date=reference_date,
            click_point=(step.anchor.click_point if step.anchor is not None else None),
            dropped_param_regions=step_region_drops,
        )
        for region, matched in step_region_drops:
            param_region_drops.append((step.id, region, matched))
        if not step.expect:
            # Nothing visual survived mining: fall back to structural
            # postconditions (URL/title change, new tab) so the step is not
            # a vacuous pass. Steps with no structural change either stay
            # honestly vacuous (docs/LIMITS.md).
            step.expect = _structural_postconditions(event)

    # Auto risk-classification: infer risk="irreversible" for write-shaped
    # steps (create/update/delete/submit/save/confirm ... — keyword + action
    # heuristics on the intent and button label; see openadapt_flow.risk) so
    # the irreversible-step safeguards (below-OCR-rung refusal, unreadable-
    # identity-band refusal) are armed BY DEFAULT rather than only when a human
    # passes risk_overrides. Conservative: an unsure write-shaped step leans
    # irreversible (the safe direction). Explicit risk_overrides below win.
    for step in steps:
        if step.action is ActionKind.SWITCH_SURFACE:
            step.risk = "reversible"
            step.risk_explanation = "a surface switch delivers no input"
            step.risk_review_required = False
            continue
        inference = infer_step_risk(step)
        step.risk = inference.risk
        step.risk_explanation = inference.explanation
        step.risk_review_required = inference.requires_review

    # A demonstration that entered data and ends without any write-shaped step
    # still committed that data through an unlabelled control (an icon-only
    # Save). Presume those icon-only clicks commit the record, in the safe
    # direction, so the program never describes a data-entry task as one that
    # only reads. Explicit risk_overrides below still win.
    for step_id in presumed_commit_step_ids(steps):
        presumed = next(step for step in steps if step.id == step_id)
        presumed.risk = "irreversible"
        presumed.risk_explanation = PRESUMED_COMMIT_EXPLANATION
        presumed.risk_review_required = False

    # A click that only opens a confirmation dialog is not the save: the
    # dialog's Confirm click is. Explicit risk_overrides below still win.
    by_step_id = {step.id: step for step in steps}
    for opener_id, confirm_id in confirmation_opener_step_ids(steps):
        opener = by_step_id[opener_id]
        opener.risk = "reversible"
        opener.risk_explanation = (
            f"opens a confirmation dialog; the save is the next click ({confirm_id})"
        )
        opener.risk_review_required = False
        confirm = by_step_id[confirm_id]
        confirm.risk_explanation = (
            f"confirms the dialog opened by {opener_id}; this click saves"
        )

    # Local edits the recording proves stayed local (select-all before
    # typing, Enter on a typed dropdown option, an in-form tab): classified
    # without a person, each with an outcome check that halts the run if the
    # action does anything else at run time. Missing or mixed evidence leaves
    # the step for a person. Explicit risk_overrides below still win.
    # docs/design/LOCAL_EDIT_CLASSIFICATION.md
    from openadapt_flow.compiler.local_edit import local_edits
    from openadapt_flow.network_capture import NETWORK_FILE

    network_lines = None
    if (recording / NETWORK_FILE).is_file():
        from openadapt_flow import network_capture as _nc

        try:
            network_lines = _nc.load(recording)
        except Exception:  # noqa: BLE001 - unreadable capture proves nothing
            network_lines = None
    for edit in local_edits(steps, events, network_lines):
        edited = by_step_id[edit.step_id]
        edited.risk = "reversible"
        edited.risk_explanation = edit.explanation
        edited.risk_review_required = False
        edited.expect = list(edited.expect) + list(edit.postconditions)

    if risk_overrides:
        by_id = {step.id: step for step in steps}
        resolved_overrides: dict[str, tuple[str, str]] = {}
        for step_id, risk in risk_overrides.items():
            resolved_step_id = absorbed_step_aliases.get(step_id, step_id)
            if resolved_step_id not in by_id:
                raise ValueError(
                    f"risk_overrides names unknown step {step_id!r} "
                    f"(steps: {', '.join(by_id)})"
                )
            if risk not in ("reversible", "irreversible"):
                raise ValueError(
                    f"invalid risk {risk!r} for {step_id!r} (use "
                    "'reversible' or 'irreversible')"
                )
            prior = resolved_overrides.get(resolved_step_id)
            if prior is not None and prior[1] != risk:
                raise ValueError(
                    "conflicting risk_overrides for absorbed selection gesture "
                    f"{prior[0]!r} and {step_id!r}"
                )
            resolved_overrides[resolved_step_id] = (step_id, risk)
        for resolved_step_id, (source_step_id, risk) in resolved_overrides.items():
            # Validated against the two legal values just above, so this narrows
            # the free-form ``dict[str, str]`` override value to Step.risk's Literal.
            by_id[resolved_step_id].risk = cast(
                Literal["reversible", "irreversible"], risk
            )
            by_id[
                resolved_step_id
            ].risk_explanation = (
                f"operator-qualified override from {source_step_id}: {risk}"
            )
            by_id[resolved_step_id].risk_review_required = False

    # System-of-record effect mining (opt-in). Runs LAST, after risk_overrides,
    # so each step's `risk` (the consequential-write signal) is final. Attaches
    # auto-derived typed effects to `Step.effects`; never fabricates a binding
    # (see compiler.effect_mining). Off by default → bundle byte-identical.
    if mine_effects:
        from openadapt_flow.compiler.effect_mining import (
            derive_different_path_readback,
            mine_reference,
            mine_step_effects,
        )

        references: dict[str, str] = {}
        view_lines: dict[str, list[OcrLine]] = {}
        bindings: dict[str, str] = {}
        if bind_effect_params:
            by_value: dict[str, list[str]] = {}
            for pname, pvalue in params.items():
                if pvalue:
                    by_value.setdefault(pvalue, []).append(pname)
            bindings = {v: names[0] for v, names in by_value.items() if len(names) == 1}
        for step, _sb, _sa, event in pending:
            mined = mine_step_effects(
                event,
                step,
                exclude_texts=exclude_texts,
                param_bindings=bindings or None,
            )
            if mined.effects:
                step.effects = mined.effects
            log = logger.info if mined.disposition != "none" else logger.debug
            log("effect-mining %s: %s", step.id, mined.reason)
            i_event = int(event["i"])
            if _sa is not None:
                view_lines[step.id] = cached_lines(i_event, "after", _sa)
            if mined.disposition == "readback" and _sb is not None and _sa is not None:
                # A reference number the save's screen newly shows: same-screen
                # evidence, and a way to find the record again.
                found = mine_reference(
                    cached_lines(i_event, "before", _sb),
                    view_lines[step.id],
                    exclude_texts,
                )
                if found is not None:
                    capture, demo_value = found
                    for effect in step.effects:
                        if effect.readback is not None:
                            effect.readback.reference = capture
                    references[step.id] = demo_value
                    logger.info("effect-mining %s: learned a reference number", step.id)

        # Cross-step pass: upgrade a same-surface on-screen read-back to the
        # stronger DIFFERENT-PATH oracle when the demonstration re-opened the
        # record by an independent path, or found it again by a search on this
        # run's input or on the reference number the save showed.
        unique_bindings = {
            value: names[0]
            for value, names in _values_to_params(params).items()
            if len(names) == 1
        }
        upgraded = derive_different_path_readback(
            steps,
            references=references,
            view_lines=view_lines,
            param_bindings=unique_bindings,
            secret_params=frozenset(secret_params),
        )
        if upgraded:
            logger.info(
                "effect-mining: upgraded %d on-screen read-back(s) to "
                "different-path (re-open) oracle",
                upgraded,
            )

    if admit_api_bindings:
        from openadapt_flow.compiler.binding_admission import (
            AdmissionFixture,
            certify_steps_on_fixture,
        )
        from openadapt_flow.learning.library import SkillLibrary

        if not admission_fixture_url:
            logger.info(
                "admit_api_bindings requested but admission_fixture_url is "
                "empty -- leaving every step on the GUI ladder"
            )
        else:
            library = (
                SkillLibrary(skill_library_root)
                if skill_library_root is not None
                else None
            )
            decisions = certify_steps_on_fixture(
                steps,
                [event for _step, _sb, _sa, event in pending],
                fixture=AdmissionFixture(
                    base_url=admission_fixture_url,
                    params=dict(params),
                ),
                exclude_texts=exclude_texts,
                params=dict(params),
                library=library,
                skill_id=name,
                workflow_name=name,
            )
            admitted = sum(1 for d in decisions if d.admitted)
            logger.info(
                "binding-admission: admitted %d/%d REST binding(s); "
                "the rest stay on the GUI ladder",
                admitted,
                len(decisions),
            )

    # Default fix-up passes (see compiler.fixup_passes): replace a demonstrated
    # gesture that asserts nothing with an equivalent checked one, using only
    # the recording's own frames. Every change is recorded in
    # ``compile_notes.json`` so the bundle stays auditable.
    from openadapt_flow.compiler.fixup_passes import (
        apply_fixup_passes,
        write_compile_notes,
    )

    steps, compile_notes = apply_fixup_passes(
        steps,
        {step.id: (sb, sa) for step, sb, sa, _event in pending},
        bundle,
        crop_png=_crop_png,
        phash_png=phash_png,
        surface=surface,
        pixel_verify_enabled=pixel_verify_enabled,
        drop_redundant_keys=drop_redundant_keys,
    )
    for note in compile_notes:
        logger.info("compile-pass %s: %s", note["step"], note["change"])

    for step in steps:
        if step.action is ActionKind.SWITCH_SURFACE and step.risk != "reversible":
            raise ValueError(
                f"step {step.id!r} is a surface switch; it delivers no input "
                "and cannot be marked irreversible"
            )
    workflow = Workflow(
        name=name,
        recording_id=meta.get("id"),
        viewport=tuple(viewport) if viewport else None,
        backend_hints=backend_hints,
        surface=surface,
        execution_mode=execution_mode,
        params=params,
        # Workflow-program IR, Phase 1: emit a TYPED spec for each recorded
        # parameter alongside the frozen ``params`` dict -- generalizing the
        # recorder's single "note value at replay" into a first-class,
        # typed+required param. Phase 1 types every recorded value as a
        # string with its demo value as the example/default; richer types
        # (entity_ref/enum/date) come from disambiguation in a later phase.
        param_specs={
            pname: ParamSpec(name=pname, type=ParamKind.STRING, example=value)
            for pname, value in params.items()
        },
        secret_params=secret_params,
        steps=steps,
        surfaces=_surfaces_from_meta(
            meta,
            exclude_texts,
            {
                int(event["i"]): absorbed_step_aliases.get(
                    f"step_{int(event['i']):03d}", f"step_{int(event['i']):03d}"
                )
                for event in events
            },
        ),
    )

    source_recording_sha256: Optional[str] = None
    if _sealed.is_file(recording / ".openadapt-approval.json"):
        from openadapt_flow.sanitized_artifact import load_valid_approval

        source_recording_sha256 = load_valid_approval(recording)[
            "approved_derivative_sha256"
        ]
    compiler_config = {
        "annotate": annotate,
        "annotator": (
            f"{type(annotator).__module__}.{type(annotator).__name__}"
            if annotator is not None
            else None
        ),
        "mine_effects": mine_effects,
        "admit_api_bindings": admit_api_bindings,
        "admission_fixture_url": admission_fixture_url,
        "name": name,
        "param_overrides": dict(sorted((param_overrides or {}).items())),
        "risk_overrides": dict(sorted((risk_overrides or {}).items())),
        "secret_param_steps": sorted(secret_param_steps or ()),
        "target_surface": target_surface,
    }
    if bind_effect_params:
        # Recorded only when set, so every existing compile keeps its digest.
        compiler_config["bind_effect_params"] = True
    compiler_config_sha256 = hashlib.sha256(
        json.dumps(
            compiler_config,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    workflow.manifest = BundleManifest(
        provenance=BundleProvenance(
            source_recording_sha256=source_recording_sha256,
            compiler_config_sha256=compiler_config_sha256,
        )
    )

    # Parameter-hygiene lint: a demo parameter value baked in as a literal
    # outside the designated slots makes the bundle silently demo-bound —
    # fail compilation loudly instead of emitting a self-disarming bundle.
    violations = lint_param_leakage(workflow, exclude_texts)
    if violations:
        raise ValueError(
            "parameter leakage: the compiled bundle embeds demonstrated "
            "parameter values outside designated parameter slots:\n  "
            + "\n  ".join(violations)
        )

    # Parameter-hygiene NOTICES (the pixel counterpart of the lint above). The
    # lint catches a demo value baked in as a literal; this reports the class it
    # cannot see — a REGION_STABLE region whose PIXELS render the value. Those
    # regions are dropped during mining (fail-safe: an unverified step beats a
    # postcondition that false-halts every run with a different value), but a
    # bundle that silently lost a postcondition is undiagnosable, so the drop is
    # logged and recorded in ``param_hygiene.json``. NOT fatal: dropping is the
    # correct outcome, not a compilation error. The sidecar carries MASKED text
    # only (a parameter value can be PHI) and is removed when a recompile finds
    # nothing to report, so it never goes stale.
    from openadapt_flow.compiler.annotate import mask_value as _mask_value

    if param_region_drops:
        for drop_step_id, drop_region, drop_text in param_region_drops:
            logger.warning(
                "param-hygiene %s: REGION_STABLE dropped — changed region %s "
                "renders a demonstrated parameter value (%s); freezing it would "
                "false-halt every run whose value differs. This step has no "
                "pixel-region invariant.",
                drop_step_id,
                drop_region,
                _mask_value(drop_text),
            )
        _sealed.write_text(
            bundle / "param_hygiene.json",
            json.dumps(
                {
                    "note": (
                        "REGION_STABLE postconditions dropped because the "
                        "changed region rendered a demonstrated parameter "
                        "value. These steps carry no pixel-region invariant; "
                        "add an effect assertion if the step needs one. "
                        "Matched text is masked."
                    ),
                    "dropped_region_stable": [
                        {
                            "step_id": drop_step_id,
                            "region": list(drop_region),
                            "matched_text": _mask_value(drop_text),
                        }
                        for drop_step_id, drop_region, drop_text in param_region_drops
                    ],
                },
                indent=2,
            ),
        )
    else:
        _sealed.unlink(bundle / "param_hygiene.json", missing_ok=True)

    # DETERMINISTIC field-label parameter proposals (no model, $0, always on):
    # a typed constant whose step carries field_label evidence yields a
    # slugified name proposal, emitted through the SAME confirm-don't-trust
    # pipeline as model annotations. Every such proposal is CONSEQUENTIAL
    # (constant -> run-varying parameter changes what the workflow does), so
    # apply_annotations FLAGS it and applies NOTHING -- the sidecar
    # ``param_proposals.json`` only feeds the one-shot operator confirm pass
    # (compiler.param_confirm). Unconfirmed proposals stay constants
    # (fail-closed; the bundle replays byte-for-byte as demonstrated). The
    # sidecar carries MASKED values only; steps whose demonstrator already
    # passed ``param=`` (or that this compile parameterized via confirmed
    # ``param_overrides``) emit no proposal.
    from openadapt_flow.compiler.annotate import (
        FieldLabelAnnotator,
        apply_annotations,
        mask_value,
    )

    label_result = apply_annotations(workflow, FieldLabelAnnotator())
    label_proposals: list[dict[str, Optional[str] | bool]] = []
    for sa in label_result.proposals.steps:
        for prop in sa.params:
            label_proposals.append(
                {
                    "step_id": sa.step_id,
                    "name": prop.name,
                    "field_label": prop.source_label,
                    "masked_example": (
                        mask_value(prop.example)
                        if prop.example and not prop.secret
                        else None
                    ),
                    "secret": prop.secret,
                }
            )
    if label_proposals:
        _sealed.write_text(
            bundle / "param_proposals.json",
            json.dumps(
                {
                    "note": (
                        "Flagged field-label parameter proposals -- "
                        "NOT applied. Run the confirm pass "
                        "(openadapt-flow compile, interactive or "
                        "--accept-params/--params-from) to turn a proposal "
                        "into a parameter; unconfirmed proposals remain "
                        "demonstrated constants."
                    ),
                    "proposals": label_proposals,
                },
                indent=2,
            ),
        )
        for flag in label_result.flagged:
            logger.warning(
                "field-label parameter proposal (needs operator confirmation) %s: %s",
                flag.step_id,
                flag.detail,
            )
    else:
        # A recompile after the confirm pass must not leave a stale sidecar
        # behind: the sidecar always reflects the OUTSTANDING proposals.
        _sealed.unlink(bundle / "param_proposals.json", missing_ok=True)

    # Opt-in COMPILE-TIME model annotation (off by default). Runs LAST, over the
    # fully-built workflow. Applies model proposals with the confirm-don't-trust
    # asymmetry (risk upgrade / type-enrichment applied; downgrade /
    # consequential param FLAGGED), and writes the audit trail to a bundle
    # sidecar. COMPILE-TIME ONLY: the replayer never reads this and makes ZERO
    # model calls. Off -> `workflow` and the bundle are byte-identical to today.
    if annotate:
        from openadapt_flow.compiler.annotate import AnthropicStepAnnotator

        ann = annotator if annotator is not None else AnthropicStepAnnotator()
        result = apply_annotations(workflow, ann)
        workflow = result.workflow
        _sealed.write_text(
            bundle / "annotations.json",
            result.model_dump_json(indent=2, exclude={"workflow"}),
        )
        for flag in result.flagged:
            logger.warning(
                "annotation FLAGGED (needs operator confirmation) %s: %s",
                flag.step_id,
                flag.detail,
            )
        for applied in result.applied:
            logger.info("annotation applied %s: %s", applied.step_id, applied.detail)

    # A check the person demonstrated at the end of the recording ("Show how
    # to check the save", check.json): its navigation becomes the save's
    # read-back and leaves the task, and the marked fields are read back. A
    # check that can't be compiled fails the compile rather than being dropped.
    from openadapt_flow.compiler.readback_authoring import CHECK_FILE, apply_check

    if _sealed.is_file(recording / CHECK_FILE):
        workflow, _check = apply_check(workflow, recording)
        logger.info(
            "compiled the demonstrated check: %d field(s) on %s (%s)",
            len(_check.fields),
            _check.save_step,
            _check.tier,
        )

    # A web recording made with --capture-network: propose an API step for
    # the save when its request can be explained field by field. It runs only
    # after a person admits it (openadapt-flow api admit).
    from openadapt_flow.network_capture import NETWORK_FILE

    if _sealed.is_file(recording / NETWORK_FILE):
        from openadapt_flow.compiler.api_proposal import propose_api_step

        workflow, _api_step, _api_reasons = propose_api_step(workflow, recording)
        if (
            _api_step is not None
            and _api_step.api_binding is not None
            and _api_step.api_binding.web_form is not None
        ):
            logger.info(
                "proposed an API step for %s (%d GUI steps); a person admits it "
                "with 'api admit'",
                _api_step.id,
                len(_api_step.api_binding.web_form.replaces_steps),
            )
        else:
            logger.info("no API step proposed: %s", "; ".join(_api_reasons))
        # Say what happened either way, so "no API step" is never silent.
        _sealed.write_text(
            bundle / "api_proposal.json",
            json.dumps(
                {
                    "proposed": _api_step is not None,
                    "step": None if _api_step is None else _api_step.id,
                    "reasons": _api_reasons,
                },
                indent=2,
            )
            + "\n",
        )

    # Bind each consequential click on an unlabeled control to the form whose
    # fields the demonstration filled, or record why it cannot be bound (the
    # replayer refuses that click). Runs after every risk decision, model
    # annotation included, so the binding covers exactly the final writes.
    _attach_submit_bindings(workflow.steps, binding_observations)

    # PHI-at-rest remediation (audit REM-2): replace the plaintext identity
    # band (``anchor.context_text``) and structured identity on every anchor
    # with a salted-hash, shape-preserving TEMPLATE, so no readable patient
    # name / DOB / MRN is persisted in ``workflow.json`` (or reprinted into the
    # human-readable ``workflow.py``). The wrong-patient guard re-runs the SAME
    # token-level identity check against the template at replay
    # (openadapt_flow.runtime.identity_template). Runs LAST so param-hygiene
    # lint and optional model annotation still see the plaintext. Backward
    # compatible: bundles compiled before this carry the plaintext fields and
    # replay unchanged. Multi-trace induction compiles with this off and
    # hashes the bands itself once the loop's row columns are known
    # (compiler.induction.phi_free_program_identity).
    if phi_free_identity:
        _phi_free_identity(workflow)

    # PHI governance manifest (audit REM-1): classify the bundle so an operator
    # inventory and the pre-commit/CI guard can act on it.
    from openadapt_flow import privacy as _privacy

    workflow.phi_scrubbed = _privacy.text_scrubbing_enabled()
    workflow.contains_phi = any(
        s.anchor is not None and (s.anchor.context_text or s.anchor.structured_identity)
        for s in workflow.steps
    )
    workflow.encrypted = False

    kept = {s.id for s in workflow.steps}
    layout_fingerprints = {k: v for k, v in layout_fingerprints.items() if k in kept}
    if layout_fingerprints:
        fp_path = bundle / _layout.FINGERPRINT_ASSET
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        _sealed.write_bytes(
            fp_path,
            _layout.dump_bundle_fingerprints(
                layout_fingerprints,
                scale_landmarks={"salt": scale_salt, "steps": scale_landmarks},
            ),
        )

    write_compile_notes(bundle, compile_notes)
    workflow.save(bundle)
    _sealed.write_text(bundle / "workflow.py", render_workflow_py(workflow))
    # Named sections for program maps (display metadata; see
    # openadapt_flow.sections). Computed from the final program, the recorded
    # UI structure and any narration; outside both bundle digests.
    from openadapt_flow import sections as _sections

    _sections.write(
        bundle,
        _sections.build(
            workflow,
            context=_sections.context_for_workflow(events, workflow),
            narration=_sections.narration_by_step(recording),
        ),
    )
    return workflow


def _attach_submit_bindings(steps: list[Step], observations: dict[str, object]) -> None:
    """Attach :class:`~openadapt_flow.ir.SubmitBinding` evidence in place.

    A binding applies to a consequential CLICK/DOUBLE_CLICK whose target has
    no usable structural locator (an unlabeled control) and whose recorded event
    carried a submit-binding observation. Each such step gets either a
    binding (every condition in :mod:`openadapt_flow.submit_binding` held in
    the demonstration) or a plain-language reason it could not be bound.

    A consequential click on a labelled control that compiled with no
    identity evidence, after fields were typed, and whose observation shows
    it is a button of the one form owning those fields, gets a
    :class:`~openadapt_flow.ir.FormIdentity` instead: a create-new form's
    save. Steps without an observation (pixel recordings, recordings made
    before this capability) are left unchanged.
    """

    from openadapt_flow.policy import is_identity_armed
    from openadapt_flow.submit_binding import (
        decide_form_identity,
        decide_submit_binding,
        observation_from_page,
    )

    typed_since_commit: list[str] = []
    for step in steps:
        if step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
            typed_since_commit.append(step.id)
            continue
        raw = observations.get(step.id)
        if (
            raw is not None
            and step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and step.risk == "irreversible"
            and step.anchor is not None
            and not structural_locator_is_locatable(step.anchor.structural)
        ):
            try:
                observation = observation_from_page(raw)
            except ValueError:
                step.submit_binding = None
                step.submit_binding_unbound_reason = (
                    "the recorded structural evidence for this control is malformed"
                )
            else:
                binding, reason = decide_submit_binding(
                    observation, field_step_ids=typed_since_commit
                )
                step.submit_binding = binding
                step.submit_binding_unbound_reason = reason
        elif (
            raw is not None
            and step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and step.risk == "irreversible"
            and step.anchor is not None
            and typed_since_commit
            and not is_identity_armed(step)
        ):
            # A labelled save with no record on screen to check: a create-new
            # form. The record is the one the run typed into the form.
            try:
                form_identity, _ = decide_form_identity(observation_from_page(raw))
            except ValueError:
                form_identity = None
            if form_identity is not None:
                step.anchor.form_identity = form_identity
                step.identity_armed = True
                step.identity_unarmed_reason = None
        if step.risk == "irreversible":
            # A consequential step closes the entry it committed.
            typed_since_commit = []


def _phi_free_identity(workflow: Workflow) -> None:
    """Convert every anchor's plaintext identity evidence to a PHI-free,
    salted-hash :class:`~openadapt_flow.ir.IdentityTemplate` in place.

    A single per-bundle salt is used so an external
    ``OPENADAPT_FLOW_IDENTITY_SALT`` (kept out of the bundle) applies uniformly.
    Idempotent and safe on anchors that carry no identity evidence.
    """
    from openadapt_flow.runtime.identity_template import (
        build_identity_template,
        new_salt_hex,
    )

    salt = new_salt_hex()
    for step in workflow.steps:
        anchor = step.anchor
        if anchor is None or anchor.identity_template is not None:
            continue
        if not (anchor.context_text or anchor.structured_identity):
            continue
        template = build_identity_template(
            anchor.context_text,
            structured_identity=anchor.structured_identity,
            param_examples=workflow.params,
            salt_hex=salt,
            record_key=anchor.record_key,
            structured_row=anchor.structured_row,
        )
        if template is None:
            continue
        anchor.identity_template = template
        anchor.context_text = None
        anchor.structured_identity = None
        anchor.record_key = None
        anchor.structured_row = None
