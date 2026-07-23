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
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional, cast

if TYPE_CHECKING:
    from openadapt_flow.compiler.annotate import StepAnnotator

import cv2
import numpy as np

from openadapt_flow import volatility
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    BundleManifest,
    BundleProvenance,
    Landmark,
    ParamKind,
    ParamSpec,
    Point,
    Postcondition,
    PostconditionKind,
    Region,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.risk import classify_step_risk
from openadapt_flow.runtime.identity import (
    band_region,
    context_from_lines,
    context_region_from_lines,
    coverage,
)
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
    with (recording_dir / "events.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _read_png(path: Path) -> Optional[bytes]:
    """Read PNG bytes, or None if the file does not exist."""
    return path.read_bytes() if path.exists() else None


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


def _discriminative_crop_region(frame: np.ndarray, click: Point) -> Region:
    """Crop region centered on ``click`` with enough structure to match.

    Starts at TEMPLATE_W x TEMPLATE_H and grows through CROP_GROWTH_LADDER
    until the crop's grayscale standard deviation reaches MIN_TEMPLATE_STD
    (a fully flat crop — e.g. the middle of an empty textarea — matches
    everywhere and nowhere). Falls back to the largest ladder size.
    """
    frame_h, frame_w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    region = _clamped_crop_region(click, frame_w, frame_h)
    for size in CROP_GROWTH_LADDER:
        region = _clamped_crop_region(click, frame_w, frame_h, size)
        x, y, w, h = region
        if float(gray[y : y + h, x : x + w].std()) >= MIN_TEMPLATE_STD:
            break
    return region


def _crop_png(png: bytes, region: Region) -> bytes:
    """Crop ``region`` out of a PNG and re-encode as PNG bytes."""
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    x, y, w, h = region
    crop = img[y : y + h, x : x + w]
    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        raise ValueError("could not encode crop as PNG")
    return buf.tobytes()


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
    before = cv2.imdecode(np.frombuffer(before_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    after = cv2.imdecode(np.frombuffer(after_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if before is None or after is None:
        return None
    if before.shape != after.shape:
        return (0, 0, after.shape[1], after.shape[0])
    diff = cv2.absdiff(before, after)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    kernel = np.ones((9, 9), dtype=np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(largest)
    if w <= 0 or h <= 0:
        return None
    return (int(x), int(y), int(w), int(h))


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
    if changed is not None:
        # ``after_png`` was already decoded successfully upstream (it produced
        # ``changed``), so the re-decode here is known-valid; cv2's stub types
        # the result as Optional, hence the cast.
        after = cast(
            np.ndarray,
            cv2.imdecode(np.frombuffer(after_png, dtype=np.uint8), cv2.IMREAD_COLOR),
        )
        frame_h, frame_w = after.shape[:2]
        x, y, w, h = changed
        x0 = max(0, x - REGION_STABLE_PAD)
        y0 = max(0, y - REGION_STABLE_PAD)
        x1 = min(frame_w, x + w + REGION_STABLE_PAD)
        y1 = min(frame_h, y + h + REGION_STABLE_PAD)
        padded: Region = (x0, y0, x1 - x0, y1 - y0)
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
    if changed is not None:
        template_rel: Optional[str] = None
        if bundle is not None and step_id is not None:
            template_rel = f"templates/{step_id}_expect.png"
            (bundle / template_rel).write_bytes(_crop_png(after_png, padded))
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
    (bundle / crop_rel).write_bytes(_crop_png(before_png, region))
    return crop_rel, region, None


def compile_recording(
    recording_dir: Path | str,
    out_bundle_dir: Path | str,
    *,
    name: str,
    risk_overrides: Optional[dict[str, str]] = None,
    mine_effects: bool = False,
    annotate: bool = False,
    annotator: Optional["StepAnnotator"] = None,
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
        mine_effects: Opt-in system-of-record effect mining
            (``compiler.effect_mining``). When True, each step gets candidate
            typed ``Effect``s auto-derived from what the demonstration observed:
            a real ``record_written`` / ``field_equals`` from a captured
            ``/api/db``-style SoR delta, a flagged placeholder for a
            consequential step whose binding is app-specific (not derivable),
            or nothing (with an honest "no verifiable effect derivable" log).
            Default False keeps the bundle byte-identical to before; even when
            True a bundle is unchanged wherever mining derives nothing.
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

    meta = json.loads((recording / "meta.json").read_text())
    events = _load_events(recording)
    params: dict[str, str] = dict(meta.get("params") or {})
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
        if before_path.is_file() != after_path.is_file():
            missing = before_path if not before_path.is_file() else after_path
            raise FileNotFoundError(
                f"incomplete frame pair for {kind} event {i}: missing {missing}"
            )
        before_png = _read_png(before_path)
        after_png = _read_png(after_path)

        if kind in ("click", "double_click"):
            if before_png is None:
                raise FileNotFoundError(
                    f"missing before frame for {kind} event {i} in {recording}"
                )
            click: Point = (int(event["x"]), int(event["y"]))
            # ``before_png`` is a captured frame we already hold; the decode is
            # known-valid. cv2's stub types imdecode as Optional, hence the cast.
            frame = cast(
                np.ndarray,
                cv2.imdecode(
                    np.frombuffer(before_png, dtype=np.uint8), cv2.IMREAD_COLOR
                ),
            )
            crop_region = _discriminative_crop_region(frame, click)
            template_bytes = _crop_png(before_png, crop_region)
            template_rel = f"templates/{step_id}.png"
            (bundle / template_rel).write_bytes(template_bytes)

            ocr_text = _best_crop_text(
                ocr(before_png, region=crop_region), click_y=click[1]
            )
            frame_lines = cached_lines(i, "before", before_png)
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
            anchor = Anchor(
                template=template_rel,
                region=crop_region,
                click_point=click,
                ocr_text=ocr_text,
                context_text=context_text,
                structured_identity=structured_identity,
                structural=structural,
                landmarks=landmarks,
                identifier_crop=identifier_crop_rel,
                identifier_region=identifier_region,
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
            verb = "double-click" if kind == "double_click" else "click"
            intent = (
                f"{verb} '{ocr_text}'"
                if ocr_text
                else f"{verb} at ({click[0]}, {click[1]})"
            )
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=intent,
                        action=(
                            ActionKind.DOUBLE_CLICK
                            if kind == "double_click"
                            else ActionKind.CLICK
                        ),
                        anchor=anchor,
                        identity_armed=identity_armed,
                        identity_unarmed_reason=unarmed_reason,
                        identifier_crop_missing_reason=idcrop_missing,
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
        else:
            raise ValueError(f"unknown event kind {kind!r} (event {i})")

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
    for j, (step, step_before, step_after, event) in enumerate(pending):
        steps.append(step)
        if step.action is ActionKind.SCROLL:
            continue  # see the scroll branch in pass 1
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
            # A parameterized TYPE step's changed region is the typed
            # value's own pixels — never assert it (it varies per run).
            include_region_stable=not (
                step.action is ActionKind.TYPE
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
        )
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
        step.risk = classify_step_risk(step)

    if risk_overrides:
        by_id = {step.id: step for step in steps}
        for step_id, risk in risk_overrides.items():
            if step_id not in by_id:
                raise ValueError(
                    f"risk_overrides names unknown step {step_id!r} "
                    f"(steps: {', '.join(by_id)})"
                )
            if risk not in ("reversible", "irreversible"):
                raise ValueError(
                    f"invalid risk {risk!r} for {step_id!r} (use "
                    "'reversible' or 'irreversible')"
                )
            # Validated against the two legal values just above, so this narrows
            # the free-form ``dict[str, str]`` override value to Step.risk's Literal.
            by_id[step_id].risk = cast(Literal["reversible", "irreversible"], risk)

    # System-of-record effect mining (opt-in). Runs LAST, after risk_overrides,
    # so each step's `risk` (the consequential-write signal) is final. Attaches
    # auto-derived typed effects to `Step.effects`; never fabricates a binding
    # (see compiler.effect_mining). Off by default → bundle byte-identical.
    if mine_effects:
        from openadapt_flow.compiler.effect_mining import (
            derive_different_path_readback,
            mine_step_effects,
        )

        for step, _sb, _sa, event in pending:
            mined = mine_step_effects(event, step, exclude_texts=exclude_texts)
            if mined.effects:
                step.effects = mined.effects
            log = logger.info if mined.disposition != "none" else logger.debug
            log("effect-mining %s: %s", step.id, mined.reason)

        # Cross-step pass: upgrade a same-surface on-screen read-back to the
        # stronger DIFFERENT-PATH oracle when the demonstration re-opened the
        # record by an independent path (the default-eligible signal).
        upgraded = derive_different_path_readback(steps)
        if upgraded:
            logger.info(
                "effect-mining: upgraded %d on-screen read-back(s) to "
                "different-path (re-open) oracle",
                upgraded,
            )

    workflow = Workflow(
        name=name,
        recording_id=meta.get("id"),
        viewport=tuple(viewport) if viewport else None,
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
        secret_params=list(meta.get("secret_params") or []),
        steps=steps,
    )

    source_recording_sha256: Optional[str] = None
    if (recording / ".openadapt-approval.json").is_file():
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
        "name": name,
        "risk_overrides": dict(sorted((risk_overrides or {}).items())),
    }
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

    # Opt-in COMPILE-TIME model annotation (off by default). Runs LAST, over the
    # fully-built workflow. Applies model proposals with the confirm-don't-trust
    # asymmetry (risk upgrade / type-enrichment applied; downgrade /
    # consequential param FLAGGED), and writes the audit trail to a bundle
    # sidecar. COMPILE-TIME ONLY: the replayer never reads this and makes ZERO
    # model calls. Off -> `workflow` and the bundle are byte-identical to today.
    if annotate:
        from openadapt_flow.compiler.annotate import (
            AnthropicStepAnnotator,
            apply_annotations,
        )

        ann = annotator if annotator is not None else AnthropicStepAnnotator()
        result = apply_annotations(workflow, ann)
        workflow = result.workflow
        (bundle / "annotations.json").write_text(
            result.model_dump_json(indent=2, exclude={"workflow"})
        )
        for flag in result.flagged:
            logger.warning(
                "annotation FLAGGED (needs operator confirmation) %s: %s",
                flag.step_id,
                flag.detail,
            )
        for applied in result.applied:
            logger.info("annotation applied %s: %s", applied.step_id, applied.detail)

    # PHI-at-rest remediation (audit REM-2): replace the plaintext identity
    # band (``anchor.context_text``) and structured identity on every anchor
    # with a salted-hash, shape-preserving TEMPLATE, so no readable patient
    # name / DOB / MRN is persisted in ``workflow.json`` (or reprinted into the
    # human-readable ``workflow.py``). The wrong-patient guard re-runs the SAME
    # token-level identity check against the template at replay
    # (openadapt_flow.runtime.identity_template). Runs LAST so param-hygiene
    # lint and optional model annotation still see the plaintext. Backward
    # compatible: bundles compiled before this carry the plaintext fields and
    # replay unchanged.
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

    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow))
    return workflow


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
        )
        if template is None:
            continue
        anchor.identity_template = template
        anchor.context_text = None
        anchor.structured_identity = None
